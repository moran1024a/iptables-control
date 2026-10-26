#!/usr/bin/env bash
# ictl 节点端：从服务端拉取转发规则，应用到 iptables 并上报结果

set -u

VERSION="0.1.7"
LATEST_URL="https://github.com/moran1024a/iptables-control/releases/latest/download/ictl.sh"
GH_PROXY="https://gh-proxy.com/"   # 手动更新加 --mirror 时使用的 GitHub 下载加速
BIN="/usr/local/bin/ictl"
CONF_DIR="/etc/ictl"
CONF="$CONF_DIR/agent.conf"
LIB="/var/lib/ictl"
CONFIG="$LIB/config"
APPLIED="$LIB/applied"
RESOLVED="$LIB/resolved"
STATE="$LIB/state"
UPDATE_RETRY=600                  # 自动更新失败后的重试间隔（秒）
LOCK="/run/ictl.lock"
SERVICE_NAME="ictl-agent"
SERVICE="/etc/systemd/system/$SERVICE_NAME.service"
SYSCTL="/etc/sysctl.d/99-ictl.conf"
SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"

if [ -t 1 ]; then
  GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m' CYAN=$'\033[36m' BOLD=$'\033[1m' DIM=$'\033[2m' RESET=$'\033[0m'
else
  GREEN="" YELLOW="" RED="" CYAN="" BOLD="" DIM="" RESET=""
fi

ok()   { printf '  %s✔%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
err()  { printf '  %s✖%s %s\n' "$RED" "$RESET" "$*" >&2; }
note() { printf '  %s%s%s\n' "$DIM" "$*" "$RESET"; }

title() {
  echo
  printf '  %s%s%s\n' "$BOLD$CYAN" "$1" "$RESET"
}

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    err "请使用 root 运行：sudo bash $0"
    exit 1
  fi
  local c missing=()
  for c in curl openssl iptables iptables-restore getent flock od systemctl; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    err "缺少依赖：${missing[*]}"
    exit 1
  fi
}

# ---------------------------------------------------------------- 配置与状态

# AUTO_UPDATE 是否按服务端的更新信号自动更新（默认开启）
URL="" KEY="" AUTO_UPDATE=1
load_conf() {
  local k v
  URL="" KEY="" AUTO_UPDATE=1
  [ -f "$CONF" ] || return 1
  while IFS='=' read -r k v; do
    case "$k" in
      URL) URL="$v" ;;
      KEY) KEY="$v" ;;
      AUTO_UPDATE) [ "$v" = "0" ] && AUTO_UPDATE=0 ;;
    esac
  done < "$CONF"
  [ -n "$URL" ] && [ -n "$KEY" ]
}

save_conf() {
  mkdir -p "$CONF_DIR"
  (umask 077 && printf 'URL=%s\nKEY=%s\nAUTO_UPDATE=%s\n' "$URL" "$KEY" "$AUTO_UPDATE" > "$CONF.tmp") &&
    mv "$CONF.tmp" "$CONF"
}

# 上报给服务端的内容：R_V 已处理的版本，R_STATE ok/error，R_ERR 错误说明
# NET 最近一次请求结果：ok / auth / conn / bad / skew / noconf；ST_TIME 最近一次同步时间
# SKEW 本机比服务端快多少秒（仅 NET=skew 时有值）
R_V="" R_STATE="ok" R_ERR="" NET="" ST_TIME="" SKEW=""
load_state() {
  local k v
  R_V="" R_STATE="ok" R_ERR="" NET="" ST_TIME="" SKEW=""
  if [ -f "$STATE" ]; then
    while IFS='=' read -r k v; do
      case "$k" in
        v) R_V="$v" ;;
        state) R_STATE="$v" ;;
        err) R_ERR="$v" ;;
        net) NET="$v" ;;
        time) ST_TIME="$v" ;;
        skew) SKEW="$v" ;;
      esac
    done < "$STATE"
  elif [ -f "$CONFIG" ]; then
    R_V="$(head -n 1 "$CONFIG")"
    R_V="${R_V#v=}"
  fi
}

save_state() {
  mkdir -p "$LIB"
  printf 'time=%s\nnet=%s\nskew=%s\nv=%s\nstate=%s\nerr=%s\n' "$(date +%s)" "$NET" "$SKEW" "$R_V" "$R_STATE" "$R_ERR" > "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"
}

with_lock() {
  local rc
  exec 9>"$LOCK"
  if ! flock -w 60 9; then
    exec 9>&-
    err "另一个 ictl 进程正在运行，请稍后重试。"
    return 1
  fi
  "$@"
  rc=$?
  flock -u 9
  exec 9>&-
  return "$rc"
}

# ---------------------------------------------------------------- 校验与解析

valid_ipv4() {
  local o='(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])'
  [[ "$1" =~ ^$o\.$o\.$o\.$o$ ]]
}

# 服务端下发的配置：首行 v=版本，其余每行 规则id 协议 入站端口 目标地址 目标端口
valid_config() {
  local line first=1
  local re='^[0-9]+ (both|tcp|udp) ([1-9][0-9]{0,4}) [A-Za-z0-9.-]{1,253} ([1-9][0-9]{0,4})$'
  while IFS= read -r line; do
    if [ "$first" -eq 1 ]; then
      [[ "$line" =~ ^v=[0-9a-f]{1,64}$ ]] || return 1
      first=0
      continue
    fi
    [ -z "$line" ] && continue
    [[ "$line" =~ $re ]] || return 1
    [ "${BASH_REMATCH[2]}" -le 65535 ] && [ "${BASH_REMATCH[3]}" -le 65535 ] || return 1
  done <<< "$1"
  [ "$first" -eq 0 ]
}

protos() {
  case "$1" in
    both) echo "tcp udp" ;;
    *) echo "$1" ;;
  esac
}

# 域名 → IP 缓存，解析失败时沿用上次成功的结果
declare -A RCACHE=()
RCACHE_DIRTY=0
load_resolved() {
  local h ip
  RCACHE=()
  RCACHE_DIRTY=0
  [ -f "$RESOLVED" ] || return 0
  while read -r h ip; do
    [ -n "$ip" ] && RCACHE["$h"]="$ip"
  done < "$RESOLVED"
}

save_resolved() {
  local h
  [ "$RCACHE_DIRTY" -eq 1 ] || return 0
  for h in "${!RCACHE[@]}"; do
    printf '%s %s\n' "$h" "${RCACHE[$h]}"
  done > "$RESOLVED.tmp"
  mv "$RESOLVED.tmp" "$RESOLVED"
}

# 结果写入 RIP；RSTALE=1 表示本次解析失败，使用的是缓存
resolve() {
  local h="$1" ip
  RIP="" RSTALE=0
  if [[ "$h" =~ ^[0-9.]+$ ]]; then
    valid_ipv4 "$h" || return 1
    RIP="$h"
    return 0
  fi
  ip="$(getent ahostsv4 "$h" 2>/dev/null | awk '{ print $1; exit }')"
  if valid_ipv4 "$ip"; then
    RIP="$ip"
    if [ "${RCACHE[$h]:-}" != "$ip" ]; then
      RCACHE["$h"]="$ip"
      RCACHE_DIRTY=1
    fi
    return 0
  fi
  [ -n "${RCACHE[$h]:-}" ] || return 1
  RIP="${RCACHE[$h]}"
  RSTALE=1
}

# ---------------------------------------------------------------- 载荷加密
# 与服务端一致：AES-256-CTR（openssl 随机 salt + PBKDF2 派生）+ HMAC-SHA256。
# 密钥只通过环境变量和标准输入传给 openssl，不出现在命令行参数里。

hex2bin() { printf "$(sed 's/../\\x&/g' <<< "$1")"; }
bin2hex() { od -An -v -tx1 | tr -d ' \n'; }
sha256_hex() { openssl dgst -sha256 | awk '{ print $NF }'; }

# HMAC-SHA256（RFC 2104）：hmac_hex 十六进制密钥 < 数据
hmac_hex() {
  local k="$1" i b ipad="" opad="" inner
  while [ "${#k}" -lt 128 ]; do k+="00"; done
  for ((i = 0; i < 128; i += 2)); do
    b=$((16#${k:i:2}))
    printf -v ipad '%s%02x' "$ipad" $((b ^ 0x36))
    printf -v opad '%s%02x' "$opad" $((b ^ 0x5c))
  done
  inner="$({ hex2bin "$ipad"; cat; } | sha256_hex)"
  { hex2bin "$opad"; hex2bin "$inner"; } | sha256_hex
}

EK="" MK=""
derive_keys() {
  EK="$(printf 'ictl-enc' | hmac_hex "$KEY")"
  MK="$(printf 'ictl-mac' | hmac_hex "$KEY")"
}

aes() {
  ICTL_EK="$EK" openssl enc -aes-256-ctr -pbkdf2 -iter 1 -md sha256 -pass env:ICTL_EK "$@" 2>/dev/null
}

# ---------------------------------------------------------------- iptables

# 根据本地缓存的配置生成 iptables-restore 输入，写入 RENDERED；问题追加到 ERRS
render() {
  local id proto lport host tport p pre="" post="" fwd=""
  load_resolved
  if [ -f "$CONFIG" ]; then
    while read -r id proto lport host tport; do
      [ -z "$tport" ] && continue
      if ! resolve "$host"; then
        ERRS+=("rule $id resolve failed: $host")
        continue
      fi
      [ "$RSTALE" -eq 1 ] && ERRS+=("rule $id resolve failed: $host, using cached $RIP")
      for p in $(protos "$proto"); do
        pre+="-A ICTL-PRE -p $p -m addrtype --dst-type LOCAL --dport $lport -j DNAT --to-destination $RIP:$tport"$'\n'
        post+="-A ICTL-POST -p $p -d $RIP --dport $tport -m conntrack --ctstate DNAT --ctorigdstport $lport -j MASQUERADE"$'\n'
        fwd+="-A ICTL-FWD -p $p -d $RIP --dport $tport -m conntrack --ctstate DNAT --ctorigdstport $lport -j ACCEPT"$'\n'
      done
    done < "$CONFIG"
  fi
  save_resolved

  RENDERED="*nat
:ICTL-PRE - [0:0]
:ICTL-POST - [0:0]
${pre}${post}COMMIT
*filter
:ICTL-FWD - [0:0]
-A ICTL-FWD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
${fwd}COMMIT
"
}

jumps_ok() {
  iptables -t nat -C PREROUTING -j ICTL-PRE 2>/dev/null &&
    iptables -t nat -C POSTROUTING -j ICTL-POST 2>/dev/null &&
    iptables -C FORWARD -j ICTL-FWD 2>/dev/null
}

ensure_jumps() {
  iptables -t nat -C PREROUTING -j ICTL-PRE 2>/dev/null || iptables -w -t nat -A PREROUTING -j ICTL-PRE
  iptables -t nat -C POSTROUTING -j ICTL-POST 2>/dev/null || iptables -w -t nat -A POSTROUTING -j ICTL-POST
  iptables -C FORWARD -j ICTL-FWD 2>/dev/null || iptables -w -I FORWARD 1 -j ICTL-FWD
}

ensure_ip_forward() {
  [ -f "$SYSCTL" ] || echo "net.ipv4.ip_forward=1" > "$SYSCTL"
  [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" = "1" ] && return 0
  echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || ERRS+=("enable ip_forward failed")
}

# 生成规则；与上次提交的内容不同，或跳转规则丢失时才重新提交
apply_rules() {
  local out
  render
  if [ -f "$APPLIED" ] && printf '%s' "$RENDERED" | cmp -s - "$APPLIED" && jumps_ok; then
    return 0
  fi
  if ! out="$(printf '%s' "$RENDERED" | iptables-restore -w --noflush 2>&1)"; then
    ERRS+=("iptables-restore failed: ${out%%$'\n'*}")
    return 1
  fi
  if ! out="$(ensure_jumps 2>&1)"; then
    ERRS+=("add jump failed: ${out%%$'\n'*}")
    return 1
  fi
  printf '%s' "$RENDERED" > "$APPLIED"
}

remove_rules() {
  while iptables -t nat -D PREROUTING -j ICTL-PRE 2>/dev/null; do :; done
  while iptables -t nat -D POSTROUTING -j ICTL-POST 2>/dev/null; do :; done
  while iptables -D FORWARD -j ICTL-FWD 2>/dev/null; do :; done
  iptables -t nat -F ICTL-PRE 2>/dev/null; iptables -t nat -X ICTL-PRE 2>/dev/null
  iptables -t nat -F ICTL-POST 2>/dev/null; iptables -t nat -X ICTL-POST 2>/dev/null
  iptables -F ICTL-FWD 2>/dev/null; iptables -X ICTL-FWD 2>/dev/null
}

# ---------------------------------------------------------------- 同步

# 本地应用并更新上报状态；CFG_ERR 为配置被拒绝的原因
CFG_ERR=""
local_apply() {
  local e
  ERRS=()
  [ -n "$CFG_ERR" ] && ERRS+=("$CFG_ERR")
  mkdir -p "$LIB"
  ensure_ip_forward
  apply_rules
  if [ "${#ERRS[@]}" -eq 0 ]; then
    R_STATE="ok" R_ERR=""
  else
    R_STATE="error"
    printf -v e '%s; ' "${ERRS[@]}"
    e="${e%; }"
    R_ERR="${e//[$'\r\n']/ }"
    R_ERR="${R_ERR:0:500}"
  fi
}

# 发送一次加密请求：明文从标准输入读取，校验并解密后的响应写入文件 $1，$2 为超时秒数（默认 15）。
# 输出 HTTP 状态码；响应校验失败时为 bad，连接失败时为 000
send_request() {
  local out="$1" d code req_tag size
  d="$(mktemp -d /run/ictl.XXXXXX)" || { echo 000; return; }
  aes | tail -c +9 > "$d/msg"
  req_tag="$(hmac_hex "$MK" < "$d/msg")"
  { cat "$d/msg"; hex2bin "$req_tag"; } | openssl base64 -A > "$d/req"
  code="$(curl -sS --connect-timeout 5 --max-time "${2:-15}" -o "$d/body" -w '%{http_code}' \
    -H 'Content-Type: text/plain' --data-binary @"$d/req" "$URL" 2>/dev/null)"
  if [ "$code" = "200" ]; then
    # 校验响应的 HMAC（绑定本次请求）后解密
    code="bad"
    openssl base64 -d -A < "$d/body" > "$d/resp" 2>/dev/null
    size="$(wc -c < "$d/resp")"
    if [ "$size" -ge 40 ]; then
      head -c $((size - 32)) "$d/resp" > "$d/rmsg"
      if [ "$({ hex2bin "$req_tag"; cat "$d/rmsg"; } | hmac_hex "$MK")" = "$(tail -c 32 "$d/resp" | bin2hex)" ]; then
        { printf 'Salted__'; cat "$d/rmsg"; } | aes -d > "$out" && [ -s "$out" ] && code="200"
      fi
    fi
  fi
  rm -rf "$d"
  echo "${code:-000}"
}

request_text() {
  printf 't=%s\nv=%s\nstate=%s\nerr=%s\nagent=%s\nau=%s\n' "$(date +%s)" "$R_V" "$1" "$R_ERR" "$VERSION" "$AUTO_UPDATE"
}

# 发送一次同步请求并处理响应；参数 init 表示首次连接。NEW=1 表示收到了新配置；
# 服务端发来更新信号时 UPD_TARGET、UPD_HASH 为目标版本和脚本哈希
NEW=0 UPD_TARGET="" UPD_HASH=""
sync_once() {
  local state="$R_STATE" f code body now
  NEW=0 UPD_TARGET="" UPD_HASH=""
  [ "${1:-}" = "init" ] && state="init"
  derive_keys

  f="$(mktemp /run/ictl.XXXXXX)" || return 1
  code="$(request_text "$state" | send_request "$f")"
  body="$(cat "$f")"
  rm -f "$f"
  if [ "$code" = "200" ] && [[ "$body" =~ (^|$'\n')update=([0-9][0-9A-Za-z.-]*)\ ([0-9a-f]{64})($|$'\n') ]]; then
    UPD_TARGET="${BASH_REMATCH[2]}" UPD_HASH="${BASH_REMATCH[3]}"
    body="$(grep -v '^update=' <<< "$body")"
  fi

  case "$code" in
    200)
      NET="ok"
      SKEW=""
      [ "$body" = "same" ] && return 0
      if [[ "$body" =~ ^time=([0-9]+)$ ]]; then
        now="$(date +%s)"
        NET="skew"
        SKEW=$((now - BASH_REMATCH[1]))
        return 0
      fi
      if valid_config "$body"; then
        printf '%s\n' "$body" > "$CONFIG.tmp"
        mv "$CONFIG.tmp" "$CONFIG"
        CFG_ERR=""
      else
        CFG_ERR="invalid config rejected"
        [[ "${body%%$'\n'*}" =~ ^v=([0-9a-f]{1,64})$ ]] || return 0
      fi
      R_V="$(head -n 1 <<< "$body")"
      R_V="${R_V#v=}"
      NEW=1
      ;;
    404) NET="auth" ;;
    bad) NET="bad" ;;
    *) NET="conn" ;;
  esac
}

# 一轮完整同步：请求 → 应用 → 保存状态
cycle() {
  mkdir -p "$LIB"
  load_state
  CFG_ERR=""
  [[ "$R_ERR" == "invalid config rejected"* ]] && CFG_ERR="invalid config rejected"
  if load_conf; then
    sync_once "${1:-}"
  else
    NET="noconf"
    NEW=0
  fi
  local_apply
  save_state
}

daemon() {
  rm -f "$LIB/update"   # 0.1.6 定时检查更新的记录，已不再使用
  with_lock local_apply_saved
  while true; do
    NEW=0
    with_lock cycle
    [ -n "$UPD_TARGET" ] && auto_update
    [ "$NEW" -eq 1 ] || sleep $((20 + RANDOM % 21))   # 20-40 秒
  done
}

# 服务端发来更新信号时，从服务端下载同版本的节点端脚本并校验哈希，替换后原地重启守护进程
# （规则不变，转发不中断）。失败后 UPDATE_RETRY 秒内不再重试
UPD_RETRY_AT=0
auto_update() {
  local f code new
  [ "$AUTO_UPDATE" -eq 1 ] && version_gt "$UPD_TARGET" "$VERSION" || return 0
  [ "$(date +%s)" -ge "$UPD_RETRY_AT" ] || return 0
  UPD_RETRY_AT=$(($(date +%s) + UPDATE_RETRY))
  f="$BIN.download"
  mkdir -p "$(dirname "$BIN")"
  load_state
  code="$(request_text "$R_STATE" | { printf 'get=agent\n'; cat; } | send_request "$f" 120)"
  new="$(grep -m 1 '^VERSION=' "$f" 2>/dev/null | cut -d '"' -f 2)"
  if [ "$code" != "200" ]; then
    echo "ictl: 自动更新到 $UPD_TARGET 失败：下载失败（$code），$((UPDATE_RETRY / 60)) 分钟后重试"
  elif [ "$(sha256_hex < "$f")" != "$UPD_HASH" ] || [ "$new" != "$UPD_TARGET" ] ||
    ! grep -q '^# ictl 节点端' "$f" || ! bash -n "$f" 2>/dev/null; then
    echo "ictl: 自动更新到 $UPD_TARGET 失败：文件校验失败，$((UPDATE_RETRY / 60)) 分钟后重试"
  else
    chmod 755 "$f"
    mv "$f" "$BIN"
    echo "ictl: 自动更新 $VERSION → $new"
    exec "$BIN" --daemon
  fi
  rm -f "$f"
}

# 开机时先按本地缓存恢复规则
local_apply_saved() {
  load_state
  local_apply
  save_state
}

# ---------------------------------------------------------------- 安装与卸载

install_service() {
  mkdir -p "$(dirname "$BIN")"
  if [ "$SELF" != "$BIN" ]; then
    # 先写临时文件再替换，避免改动正在运行的脚本
    cp "$SELF" "$BIN.tmp" && mv "$BIN.tmp" "$BIN" || { err "无法复制脚本到 $BIN"; return 1; }
  fi
  chmod 755 "$BIN"
  cat > "$SERVICE" <<EOF
[Unit]
Description=ictl agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$BIN --daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
  systemctl restart "$SERVICE_NAME"
}

# ---------------------------------------------------------------- 菜单

print_net_result() {
  case "$NET" in
    ok) ok "已连接服务端" ;;
    auth) err "认证失败：同步地址或密钥错误，或节点已在服务端删除" ;;
    conn) err "无法连接服务端" ;;
    bad) err "服务端响应校验失败：密钥不匹配，或响应被篡改" ;;
    skew) err "本机时间与服务端相差 ${SKEW} 秒，服务端拒绝同步，请校准时间（如启用 NTP）" ;;
    noconf) warn "尚未设置服务端地址和密钥" ;;
  esac
  if [ "$NET" = "ok" ]; then
    if [ "$R_STATE" = "ok" ]; then
      ok "规则已应用（版本 ${R_V:-无}）"
    else
      err "规则应用有问题：$R_ERR"
    fi
  fi
}

# 保存服务端地址和密钥，立即同步一次并安装服务；连接成功返回 0
save_server() {
  local url="$1" key="$2"
  if [[ ! "$url" =~ ^https?://[^[:space:]]+$ ]]; then
    err "地址格式应为 https://域名/路径"
    return 1
  fi
  if [[ ! "$key" =~ ^[0-9a-f]{64}$ ]]; then
    err "密钥格式不正确（应为 64 位十六进制）"
    return 1
  fi

  load_conf
  URL="$url" KEY="$key"
  save_conf || { err "无法写入 $CONF"; return 1; }

  with_lock cycle init || return 1
  [ "$NEW" -eq 1 ] && with_lock cycle
  print_net_result

  if install_service; then
    ok "已安装并启动服务 $SERVICE_NAME（开机自启）"
  else
    err "服务安装失败，请检查 systemctl status $SERVICE_NAME"
    return 1
  fi
  [ "$NET" = "ok" ]
}

set_server() {
  title "设置服务端地址和密钥"
  local url key
  load_conf
  read -rp "  同步地址${URL:+ [$URL]}: " url || return
  read -rsp "  密钥: " key || return
  echo
  save_server "${url:-$URL}" "$key"
}

# 命令行接入：ictl.sh --url <同步地址> --key <密钥>
quick_setup() {
  local url="" key=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --url) url="${2:-}"; shift 2 ;;
      --key) key="${2:-}"; shift 2 ;;
      --mirror) shift ;;   # 0.1.6 用于记住下载加速，现已不需要，保留以兼容旧命令
      *) err "未知参数：$1"; exit 1 ;;
    esac
  done
  title "ictl 节点端 $VERSION"
  save_server "$url" "$key"
}

# $1 是否比 $2 新
version_gt() {
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$1" ]
}

# 手动更新：从 GitHub Release 下载最新版本替换自身；参数 --mirror 时经加速代理下载。更新成功返回 0
update_agent() {
  title "更新节点端"
  local tmp="$BIN.download" new url="$LATEST_URL?t=$(date +%s)"   # 时间戳避免拿到代理缓存的旧文件
  [ "${1:-}" = "--mirror" ] && url="$GH_PROXY$url"
  mkdir -p "$(dirname "$BIN")"
  if ! curl -fsSL --connect-timeout 10 --max-time 120 "$url" -o "$tmp"; then
    rm -f "$tmp"
    err "下载失败：$url"
    [ "${1:-}" = "--mirror" ] || note "国内服务器可使用加速下载：sudo ictl --update --mirror"
    return 1
  fi
  new="$(grep -m 1 '^VERSION=' "$tmp" | cut -d '"' -f 2)"
  if [ -z "$new" ] || ! grep -q '^# ictl 节点端' "$tmp" || ! bash -n "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    err "下载的文件无效"
    return 1
  fi
  if [ -f "$BIN" ] && ! version_gt "$new" "$VERSION"; then
    rm -f "$tmp"
    if [ "$new" = "$VERSION" ]; then
      ok "已是最新版本 $VERSION"
    else
      warn "下载到的版本 $new 低于当前版本 $VERSION（可能是下载缓存），未做改动"
    fi
    return 1
  fi
  chmod 755 "$tmp"
  mv "$tmp" "$BIN"
  [ -f "$SERVICE" ] && systemctl restart "$SERVICE_NAME"
  ok "已更新：$VERSION → $new"
}

# 菜单中切换自动更新
toggle_auto_update() {
  if ! load_conf; then
    warn "请先设置服务端地址和密钥"
    return
  fi
  AUTO_UPDATE=$((1 - AUTO_UPDATE))
  save_conf || return
  if [ "$AUTO_UPDATE" -eq 1 ]; then
    ok "已开启自动更新（节点端版本低于服务端时，自动从服务端更新到服务端的版本）"
  else
    ok "已关闭自动更新"
  fi
}

on_off() { [ "$1" -eq 1 ] && echo "已开启" || echo "已关闭"; }

show_status() {
  title "状态"
  load_state
  if load_conf; then
    note "同步地址：$URL"
    note "密钥：    ${KEY:0:6}…"
    note "自动更新：$(on_off "$AUTO_UPDATE")（跟随服务端版本）"
  else
    warn "尚未设置服务端地址和密钥"
  fi
  if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    ok "服务运行中"
  else
    warn "服务未运行"
  fi
  if [ -n "$ST_TIME" ]; then
    note "最后同步：$(date -d "@$ST_TIME" '+%F %T')"
    print_net_result
  fi

  title "规则"
  local id proto lport host tport shown=0
  load_resolved
  if [ -f "$CONFIG" ]; then
    while read -r id proto lport host tport; do
      [ -z "$tport" ] && continue
      shown=1
      if valid_ipv4 "$host"; then
        printf '  %-6s %-5s %5s → %s:%s\n' "#$id" "$proto" "$lport" "$host" "$tport"
      else
        printf '  %-6s %-5s %5s → %s:%s (%s)\n' "#$id" "$proto" "$lport" "$host" "$tport" "${RCACHE[$host]:-未解析}"
      fi
    done < "$CONFIG"
  fi
  [ "$shown" -eq 1 ] || note "没有规则"

  if [ "$shown" -eq 1 ]; then
    title "命中计数（ICTL-PRE）"
    iptables -t nat -L ICTL-PRE -n -v -x 2>/dev/null | sed 's/^/  /'
  fi
}

sync_now() {
  title "立即同步"
  with_lock cycle || return
  [ "$NEW" -eq 1 ] && with_lock cycle
  print_net_result
}

# 卸载；参数 -y 跳过确认
uninstall() {
  title "卸载节点端"
  local yn
  if [ "${1:-}" != "-y" ]; then
    if ! read -rp "  将停止服务、删除所有 ictl 规则和文件，确认？[y/N]: " yn; then
      echo
      err "无法读取确认输入，非交互运行请使用：ictl --uninstall -y"
      return 1
    fi
    [[ "$yn" =~ ^[yY]$ ]] || { note "已取消。"; return; }
  fi

  systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1
  rm -f "$SERVICE"
  systemctl daemon-reload 2>/dev/null
  remove_rules
  rm -rf "$CONF_DIR" "$LIB" "$SYSCTL" "$LOCK"
  [ -f "$BIN" ] && rm -f "$BIN"
  ok "已卸载。IPv4 转发的运行时设置保持不变，重启后恢复系统默认。"
  note "该节点在服务端面板中仍会显示为离线，可在面板中删除（节点已失联时选择强制删除）。"
  exit 0
}

menu() {
  while true; do
    load_conf
    echo
    printf '  %sictl 节点端%s %sv%s%s\n' "$BOLD$CYAN" "$RESET" "$DIM" "$VERSION" "$RESET"
    echo "  1) 设置服务端地址和密钥"
    echo "  2) 查看状态"
    echo "  3) 立即同步"
    echo "  4) 更新节点端"
    echo "  5) 自动更新：$(on_off "$AUTO_UPDATE")（切换）"
    echo "  6) 卸载"
    echo "  0) 退出"
    local choice
    read -rp "  请选择: " choice || { echo; exit 0; }
    case "$choice" in
      1) set_server ;;
      2) show_status ;;
      3) sync_now ;;
      4) update_agent && { note "请重新运行 sudo ictl"; exit 0; } ;;
      5) toggle_auto_update ;;
      6) uninstall ;;
      0|q) exit 0 ;;
      "") ;;
      *) err "无效选择：$choice" ;;
    esac
  done
}

case "${1:-}" in
  --daemon) need_root; daemon ;;
  --update) need_root; update_agent "${2:-}"; exit 0 ;;
  --uninstall) need_root; uninstall "${2:-}" ;;
  --url|--key) need_root; quick_setup "$@" ;;
  "") need_root; menu ;;
  *) echo "用法：ictl [--url <同步地址> --key <密钥> | --update [--mirror] | --uninstall [-y] | --daemon]" >&2; exit 1 ;;
esac
