#!/usr/bin/env bash
# ictl 节点端：从服务端拉取转发规则，应用到 iptables 并上报结果

set -u

VERSION="0.1.6"
LATEST_URL="https://github.com/moran1024a/iptables-control/releases/latest/download/ictl.sh"
GH_PROXY="https://gh-proxy.com/"   # --mirror 时使用的 GitHub 下载加速
BIN="/usr/local/bin/ictl"
CONF_DIR="/etc/ictl"
CONF="$CONF_DIR/agent.conf"
LIB="/var/lib/ictl"
CONFIG="$LIB/config"
APPLIED="$LIB/applied"
RESOLVED="$LIB/resolved"
STATE="$LIB/state"
UPDATE_STATE="$LIB/update"        # 自动更新：下次检查时间、最近一次检查结果
UPDATE_EVERY=21600                # 自动更新检查间隔（6 小时），另加最多 30 分钟随机偏移
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

# AUTO_UPDATE 是否自动更新（默认开启），MIRROR 下载更新时是否经加速代理
URL="" KEY="" AUTO_UPDATE=1 MIRROR=0
load_conf() {
  local k v
  URL="" KEY="" AUTO_UPDATE=1 MIRROR=0
  [ -f "$CONF" ] || return 1
  while IFS='=' read -r k v; do
    case "$k" in
      URL) URL="$v" ;;
      KEY) KEY="$v" ;;
      AUTO_UPDATE) [ "$v" = "0" ] && AUTO_UPDATE=0 ;;
      MIRROR) [ "$v" = "1" ] && MIRROR=1 ;;
    esac
  done < "$CONF"
  [ -n "$URL" ] && [ -n "$KEY" ]
}

save_conf() {
  mkdir -p "$CONF_DIR"
  (umask 077 && printf 'URL=%s\nKEY=%s\nAUTO_UPDATE=%s\nMIRROR=%s\n' "$URL" "$KEY" "$AUTO_UPDATE" "$MIRROR" > "$CONF.tmp") &&
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

# 发送一次请求并处理响应；参数 init 表示首次连接。NEW=1 表示收到了新配置
NEW=0
sync_once() {
  local state="$R_STATE" d out code body req_tag size now
  NEW=0
  [ "${1:-}" = "init" ] && state="init"
  derive_keys

  d="$(mktemp -d /run/ictl.XXXXXX)" || return 1
  printf 't=%s\nv=%s\nstate=%s\nerr=%s\nagent=%s\n' "$(date +%s)" "$R_V" "$state" "$R_ERR" "$VERSION" |
    aes | tail -c +9 > "$d/msg"
  req_tag="$(hmac_hex "$MK" < "$d/msg")"
  { cat "$d/msg"; hex2bin "$req_tag"; } | openssl base64 -A > "$d/req"
  out="$(curl -sS --connect-timeout 5 --max-time 15 -w '\n%{http_code}' \
    -H 'Content-Type: text/plain' --data-binary @"$d/req" "$URL" 2>/dev/null)"
  code="${out##*$'\n'}"
  body="${out%$'\n'*}"

  if [ "$code" = "200" ]; then
    # 校验响应的 HMAC（绑定本次请求）后解密
    printf '%s' "$body" | openssl base64 -d -A > "$d/resp" 2>/dev/null
    size="$(wc -c < "$d/resp")"
    body=""
    if [ "$size" -ge 40 ]; then
      head -c $((size - 32)) "$d/resp" > "$d/rmsg"
      if [ "$({ hex2bin "$req_tag"; cat "$d/rmsg"; } | hmac_hex "$MK")" = "$(tail -c 32 "$d/resp" | bin2hex)" ]; then
        body="$({ printf 'Salted__'; cat "$d/rmsg"; } | aes -d)"
      fi
    fi
    [ -n "$body" ] || code="bad"
  fi
  rm -rf "$d"

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
  with_lock local_apply_saved
  while true; do
    NEW=0
    with_lock cycle
    [ "$NEW" -eq 1 ] && continue
    auto_update
    sleep $((15 + RANDOM % 6))   # 15-20 秒
  done
}

# 到时间时检查更新；有新版本则替换脚本并原地重启守护进程（规则不变，转发不中断）
auto_update() {
  local k v next="" now
  load_conf
  [ "$AUTO_UPDATE" -eq 1 ] || return 0
  now="$(date +%s)"
  if [ -f "$UPDATE_STATE" ]; then
    while IFS='=' read -r k v; do
      [ "$k" = "next" ] && next="$v"
    done < "$UPDATE_STATE"
  fi
  if [[ ! "$next" =~ ^[0-9]+$ ]]; then
    save_update_state "$((now + UPDATE_EVERY + RANDOM % 1800))" ""
    return 0
  fi
  [ "$now" -ge "$next" ] || return 0

  local mirror=""
  [ "$MIRROR" -eq 1 ] && mirror="--mirror"
  save_update_state "$((now + UPDATE_EVERY + RANDOM % 1800))" "检查中"   # 先推迟下次检查，避免下载卡住被重启后反复检查
  if fetch_update "$mirror"; then
    save_update_state "$((now + UPDATE_EVERY + RANDOM % 1800))" "已更新 $VERSION → $NEW_VERSION"
    echo "ictl: auto update $VERSION -> $NEW_VERSION"
    exec "$BIN" --daemon
  fi
  case "$FETCH_RESULT" in
    latest) v="已是最新版本" ;;
    older) v="下载到的版本 $NEW_VERSION 较旧，未更新" ;;
    download) v="下载失败" ;;
    *) v="下载的文件无效" ;;
  esac
  save_update_state "$((now + UPDATE_EVERY + RANDOM % 1800))" "$v"
}

save_update_state() {
  printf 'next=%s\nchecked=%s\nresult=%s\n' "$1" "$(date +%s)" "$2" > "$UPDATE_STATE.tmp" &&
    mv "$UPDATE_STATE.tmp" "$UPDATE_STATE"
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
  [ "${3:-}" = "--mirror" ] && MIRROR=1
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

# 命令行接入：ictl.sh --url <同步地址> --key <密钥> [--mirror]
quick_setup() {
  local url="" key="" mirror=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --url) url="${2:-}"; shift 2 ;;
      --key) key="${2:-}"; shift 2 ;;
      --mirror) mirror="--mirror"; shift ;;
      *) err "未知参数：$1"; exit 1 ;;
    esac
  done
  title "ictl 节点端 $VERSION"
  save_server "$url" "$key" "$mirror"
}

# $1 是否比 $2 新
version_gt() {
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$1" ]
}

# 从 GitHub Release 下载最新版本替换 $BIN；参数 --mirror 时经加速代理下载。
# 替换成功返回 0，NEW_VERSION 为新版本；否则 FETCH_RESULT 为原因：latest / older / download / invalid
NEW_VERSION="" FETCH_RESULT=""
fetch_update() {
  local tmp="$BIN.download" url="$LATEST_URL?t=$(date +%s)"   # 时间戳避免拿到代理缓存的旧文件
  NEW_VERSION="" FETCH_RESULT=""
  [ "${1:-}" = "--mirror" ] && url="$GH_PROXY$url"
  mkdir -p "$(dirname "$BIN")"
  if ! curl -fsSL --connect-timeout 10 --max-time 120 "$url" -o "$tmp"; then
    rm -f "$tmp"
    FETCH_RESULT="download"
    return 1
  fi
  NEW_VERSION="$(grep -m 1 '^VERSION=' "$tmp" | cut -d '"' -f 2)"
  if [[ ! "$NEW_VERSION" =~ ^[0-9][0-9A-Za-z.-]*$ ]] || ! grep -q '^# ictl 节点端' "$tmp" || ! bash -n "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    FETCH_RESULT="invalid"
    return 1
  fi
  if [ -f "$BIN" ] && ! version_gt "$NEW_VERSION" "$VERSION"; then
    rm -f "$tmp"
    FETCH_RESULT="older"
    [ "$NEW_VERSION" = "$VERSION" ] && FETCH_RESULT="latest"
    return 1
  fi
  chmod 755 "$tmp"
  mv "$tmp" "$BIN"
}

# 手动更新；--mirror 经加速代理下载，并记住该选择供自动更新使用。更新成功返回 0
update_agent() {
  title "更新节点端"
  load_conf
  if [ "${1:-}" = "--mirror" ] && [ "$MIRROR" -eq 0 ] && [ -f "$CONF" ]; then
    MIRROR=1
    save_conf && note "已开启下载加速，之后的自动更新也经加速代理下载"
  fi
  local mirror=""
  [ "$MIRROR" -eq 1 ] && mirror="--mirror"
  if fetch_update "$mirror"; then
    [ -f "$SERVICE" ] && systemctl restart "$SERVICE_NAME"
    ok "已更新：$VERSION → $NEW_VERSION"
    return 0
  fi
  case "$FETCH_RESULT" in
    download)
      err "下载失败"
      [ -n "$mirror" ] || note "国内服务器可使用加速下载：sudo ictl --update --mirror"
      ;;
    invalid) err "下载的文件无效" ;;
    latest) ok "已是最新版本 $VERSION" ;;
    older) warn "下载到的版本 $NEW_VERSION 低于当前版本 $VERSION（可能是下载缓存），未做改动" ;;
  esac
  return 1
}

# 菜单中切换自动更新和下载加速
toggle_setting() {
  if ! load_conf; then
    warn "请先设置服务端地址和密钥"
    return
  fi
  case "$1" in
    auto)
      AUTO_UPDATE=$((1 - AUTO_UPDATE))
      save_conf && { [ "$AUTO_UPDATE" -eq 1 ] && ok "已开启自动更新（每 6 小时检查一次）" || ok "已关闭自动更新"; }
      ;;
    mirror)
      MIRROR=$((1 - MIRROR))
      save_conf && { [ "$MIRROR" -eq 1 ] && ok "已开启下载加速（经 ${GH_PROXY%/} 下载更新）" || ok "已关闭下载加速"; }
      ;;
  esac
}

on_off() { [ "$1" -eq 1 ] && echo "已开启" || echo "已关闭"; }

show_status() {
  title "状态"
  load_state
  if load_conf; then
    note "同步地址：$URL"
    note "密钥：    ${KEY:0:6}…"
    note "自动更新：$(on_off "$AUTO_UPDATE")；下载加速：$(on_off "$MIRROR")"
    local k v next="" checked="" result=""
    if [ -f "$UPDATE_STATE" ]; then
      while IFS='=' read -r k v; do
        case "$k" in
          next) next="$v" ;;
          checked) checked="$v" ;;
          result) result="$v" ;;
        esac
      done < "$UPDATE_STATE"
    fi
    [ -n "$result" ] && [ -n "$checked" ] && note "上次检查更新：$(date -d "@$checked" '+%F %T')（$result）"
    [ "$AUTO_UPDATE" -eq 1 ] && [[ "$next" =~ ^[0-9]+$ ]] && note "下次检查更新：$(date -d "@$next" '+%F %T')"
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
    echo "  6) 下载加速：$(on_off "$MIRROR")（切换）"
    echo "  7) 卸载"
    echo "  0) 退出"
    local choice
    read -rp "  请选择: " choice || { echo; exit 0; }
    case "$choice" in
      1) set_server ;;
      2) show_status ;;
      3) sync_now ;;
      4) update_agent && { note "请重新运行 sudo ictl"; exit 0; } ;;
      5) toggle_setting auto ;;
      6) toggle_setting mirror ;;
      7) uninstall ;;
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
  *) echo "用法：ictl [--url <同步地址> --key <密钥> [--mirror] | --update [--mirror] | --uninstall [-y] | --daemon]" >&2; exit 1 ;;
esac
