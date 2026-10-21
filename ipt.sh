#!/usr/bin/env bash

set -u

APP_NAME="ipt-forward-manager"
VERSION="2.3"
DEFAULT_COMMENT_PREFIX="xjj-forward"
BACKUP_DIR="/root/iptables-backup"
CONF_DIR="/root/iptables-forward-conf"
MANAGED_CONF="$CONF_DIR/managed-forwards.conf"
LOG_FILE="/var/log/ipt-forward-manager.log"
LOCK_FILE="/run/ipt-forward-manager.lock"
SYSCTL_FILE="/etc/sysctl.d/99-ipt-forward-manager.conf"
ESTABLISHED_COMMENT="${APP_NAME}-established"
SELF_PATH="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"

if [ -t 1 ]; then
  GREEN=$'\033[32m'
  YELLOW=$'\033[33m'
  RED=$'\033[31m'
  BLUE=$'\033[34m'
  CYAN=$'\033[36m'
  BOLD=$'\033[1m'
  DIM=$'\033[2m'
  RESET=$'\033[0m'
else
  GREEN="" YELLOW="" RED="" BLUE="" CYAN="" BOLD="" DIM="" RESET=""
fi

HR="────────────────────────────────────────────────────────────"
LOCK_HELD=0
LAST_BACKUP=""
VERR=""
IPT_ERR=""
DW=0

# ---------------------------------------------------------------- 输出

ok()   { printf '  %s✔%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
err()  { printf '  %s✖%s %s\n' "$RED" "$RESET" "$*" >&2; }
info() { printf '  %s›%s %s\n' "$BLUE" "$RESET" "$*"; }
note() { printf '  %s%s%s\n' "$DIM" "$*" "$RESET"; }

title() {
  echo
  printf '  %s%s%s\n' "$BOLD$CYAN" "$1" "$RESET"
  printf '  %s%s%s\n' "$DIM" "$HR" "$RESET"
}

# 计算显示宽度（中文按 2 列计），结果写入 DW
dw() {
  local s="$1" b c
  local LC_ALL=C
  b=${#s}
  LC_ALL=C.UTF-8
  c=${#s}
  DW=$(( c + (b - c) / 2 ))
}

# 按显示宽度输出一个表格单元：cell 文本 宽度 [颜色]
cell() {
  local s="$1" w="$2" color="${3:-}"
  dw "$s"
  printf '%s%s%s' "$color" "$s" "${color:+$RESET}"
  [ "$w" -gt "$DW" ] && printf '%*s' $(( w - DW )) ''
  printf ' '
}

indent() { sed 's/^/    /'; }

log() {
  mkdir -p "$(dirname "$LOG_FILE")"
  echo "[$(date '+%F %T')] $*" >> "$LOG_FILE"
}

pause() {
  echo
  read -rp "  按 Enter 返回菜单..." _ || true
}

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    err "请使用 root 运行：sudo bash $0"
    exit 1
  fi
}

safe_mkdirs() {
  mkdir -p "$BACKUP_DIR" "$CONF_DIR"
}

timestamp() {
  date '+%Y%m%d-%H%M%S'
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# ---------------------------------------------------------------- 锁

lock_acquire() {
  command -v flock >/dev/null 2>&1 || return 0
  [ "$LOCK_HELD" -eq 1 ] && return 0
  exec 9>"$LOCK_FILE" || return 1
  if ! flock -w "${1:-30}" 9; then
    err "另一个实例（可能是 DDNS 定时任务）正在修改规则，请稍后重试。"
    exec 9>&-
    return 1
  fi
  LOCK_HELD=1
}

lock_release() {
  [ "$LOCK_HELD" -eq 1 ] || return 0
  flock -u 9 2>/dev/null
  exec 9>&-
  LOCK_HELD=0
}

# ---------------------------------------------------------------- 校验

valid_port() {
  if [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$1" -le 65535 ]; then
    return 0
  fi
  VERR="端口必须是 1-65535 的整数：$1"
  return 1
}

valid_ipv4() {
  local o='(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])'
  [[ "$1" =~ ^$o\.$o\.$o\.$o$ ]]
}

valid_target() {
  if valid_ipv4 "$1"; then
    return 0
  fi
  if [ "${#1}" -le 253 ] && [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{0,62}$ ]]; then
    return 0
  fi
  VERR="不是有效的 IPv4 地址或域名：$1"
  return 1
}

valid_comment() {
  if [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$ ]]; then
    return 0
  fi
  VERR="备注只能包含字母、数字和 . _ : -，以字母或数字开头，最长 200 个字符：$1"
  return 1
}

default_comment() {
  local c="${DEFAULT_COMMENT_PREFIX}-$1-to-$2-$3"
  printf '%s' "${c:0:200}"
}

# ---------------------------------------------------------------- 交互输入

# ask 变量名 提示 校验函数 [默认值]；输入 q 或 EOF 返回 1
ask() {
  local __var="$1" __prompt="$2" __check="$3" __def="${4:-}" __val
  while true; do
    if [ -n "$__def" ]; then
      read -rp "  $__prompt [$__def]: " __val || return 1
    else
      read -rp "  $__prompt: " __val || return 1
    fi
    __val="$(trim "$__val")"
    [ "$__val" = "q" ] && return 1
    [ -z "$__val" ] && __val="$__def"
    if [ -z "$__val" ]; then
      err "不能为空，请重新输入（输入 q 取消）。"
      continue
    fi
    if "$__check" "$__val"; then
      printf -v "$__var" '%s' "$__val"
      return 0
    fi
    err "$VERR"
  done
}

confirm() {
  local yn
  echo
  read -rp "  $1 [y/N]: " yn || return 1
  [[ "$yn" =~ ^[yY]$ ]]
}

protocols_from_choice() {
  case "$1" in
    1|tcp|TCP) echo "tcp" ;;
    2|udp|UDP) echo "udp" ;;
    3|both|BOTH|all|ALL) echo "tcp udp" ;;
    *) echo "" ;;
  esac
}

choice_from_protocols() {
  case "$1" in
    tcp) echo 1 ;;
    udp) echo 2 ;;
    *) echo 3 ;;
  esac
}

protocol_label() {
  case "$1" in
    "tcp udp") echo "TCP+UDP" ;;
    *) echo "${1^^}" ;;
  esac
}

# ask_protocols 变量名 [默认选项] [提示]
ask_protocols() {
  local __var="$1" __def="${2:-3}" __prompt="${3:-协议}" __c __p
  while true; do
    read -rp "  $__prompt  1) TCP  2) UDP  3) TCP+UDP [$__def]: " __c || return 1
    __c="$(trim "$__c")"
    [ "$__c" = "q" ] && return 1
    __p="$(protocols_from_choice "${__c:-$__def}")"
    if [ -n "$__p" ]; then
      printf -v "$__var" '%s' "$__p"
      return 0
    fi
    err "请输入 1、2 或 3（输入 q 取消）。"
  done
}

# 解析一行 "入站端口 目标 目标端口 [协议] [备注]"
# 成功返回 0 并设置 F_*；格式错误返回 1 并设置 VERR；空行/注释行返回 2
parse_forward_line() {
  local line extra
  line="$(trim "${1%%#*}")"
  [ -z "$line" ] && return 2

  F_IN="" F_HOST="" F_TPORT="" F_PROTO_IN="" F_COMMENT="" extra=""
  read -r F_IN F_HOST F_TPORT F_PROTO_IN F_COMMENT extra <<< "$line"

  if [ -z "$F_TPORT" ]; then
    VERR="字段不足，至少需要：入站端口 目标 目标端口"
    return 1
  fi
  if [ -n "$extra" ]; then
    VERR="字段过多（备注不能包含空格）"
    return 1
  fi
  valid_port "$F_IN" || { VERR="入站$VERR"; return 1; }
  valid_target "$F_HOST" || return 1
  valid_port "$F_TPORT" || { VERR="目标$VERR"; return 1; }

  F_PROTO_IN="${F_PROTO_IN:-both}"
  F_PROTOS="$(protocols_from_choice "$F_PROTO_IN")"
  if [ -z "$F_PROTOS" ]; then
    VERR="协议无效：$F_PROTO_IN（可用 tcp / udp / both）"
    return 1
  fi

  F_COMMENT="${F_COMMENT:-$(default_comment "$F_IN" "$F_HOST" "$F_TPORT")}"
  valid_comment "$F_COMMENT" || return 1
  return 0
}

# ---------------------------------------------------------------- 规则解析

# awk 函数：解析一条 iptables -S / iptables-save 规则
# 设置 C(备注) P(协议) DP(目的端口) D(目的地址) TD(DNAT 目标) J(动作)
PARSE_AWK='
function parse(line,   p, s, out, ch, n, t, i) {
  C = ""; P = ""; DP = ""; D = ""; TD = ""; J = ""
  p = index(line, "--comment ")
  if (p > 0) {
    s = substr(line, p + 10)
    if (substr(s, 1, 1) == "\"") {
      s = substr(s, 2); out = ""
      while (length(s) > 0) {
        ch = substr(s, 1, 1)
        if (ch == "\\") { out = out substr(s, 2, 1); s = substr(s, 3); continue }
        if (ch == "\"") { s = substr(s, 2); break }
        out = out ch; s = substr(s, 2)
      }
      C = out
    } else {
      n = index(s, " ")
      if (n > 0) { C = substr(s, 1, n - 1); s = substr(s, n) } else { C = s; s = "" }
    }
    line = substr(line, 1, p - 1) s
  }
  n = split(line, t, " ")
  for (i = 1; i < n; i++) {
    if (t[i] == "-p") P = t[i + 1]
    else if (t[i] == "--dport") DP = t[i + 1]
    else if (t[i] == "-d") { D = t[i + 1]; sub(/\/32$/, "", D) }
    else if (t[i] == "--to-destination") TD = t[i + 1]
    else if (t[i] == "-j") J = t[i + 1]
  }
}
'

# 输出匹配规则的序号：rule_nums 表 链 备注 [协议] [端口] [目的IP] [DNAT目标] [动作]，空参数表示不限
rule_nums() {
  iptables -t "$1" -S "$2" 2>/dev/null | awk -v c="$3" -v p="${4:-}" -v dp="${5:-}" -v d="${6:-}" -v td="${7:-}" -v j="${8:-}" "$PARSE_AWK"'
    NR > 1 {
      parse($0)
      if (C == c && (p == "" || P == p) && (dp == "" || DP == dp) && (d == "" || D == d) && (td == "" || TD == td) && (j == "" || J == j))
        print NR - 1
    }'
}

# 按序号从大到小删除，序号不会因删除而错位；返回删除条数到 DEL_COUNT
delete_rule_nums() {
  local table="$1" chain="$2" n
  shift 2
  for n in $(rule_nums "$table" "$chain" "$@" | sort -rn); do
    if iptables -t "$table" -D "$chain" "$n" 2>/dev/null; then
      DEL_COUNT=$((DEL_COUNT + 1))
    else
      log "delete failed: $table $chain #$n"
    fi
  done
}

# PREROUTING 中的 DNAT 规则，每行：备注(无则为 -)<TAB>协议<TAB>入站端口<TAB>目标IP<TAB>目标端口
dnat_records() {
  iptables -t nat -S PREROUTING 2>/dev/null | awk "$PARSE_AWK"'
    NR > 1 {
      parse($0)
      if (J != "DNAT" || (P != "tcp" && P != "udp") || DP !~ /^[0-9]+$/ || TD !~ /:[0-9]+$/) next
      ip = TD; sub(/:[0-9]+$/, "", ip)
      tp = TD; sub(/^.*:/, "", tp)
      cc = (C == "" ? "-" : C)
      print cc "\t" P "\t" DP "\t" ip "\t" tp
    }'
}

# 带备注的转发配置（tcp/udp 合并），每行：备注<TAB>入站端口<TAB>目标IP<TAB>目标端口<TAB>协议列表
forward_configs() {
  dnat_records | awk -F'\t' '$1 != "-"' | sort -t$'\t' -k3,3n -k1,1 -k4,4 -k5,5n | awk -F'\t' '
    {
      key = $1 "\t" $3 "\t" $4 "\t" $5
      if (!(key in seen)) { seen[key] = 1; keys[++n] = key }
      if ($2 == "tcp") tcp[key] = 1; else udp[key] = 1
    }
    END {
      for (i = 1; i <= n; i++) {
        k = keys[i]
        pr = (k in tcp) ? "tcp" : ""
        if (k in udp) pr = (pr == "" ? "udp" : pr " udp")
        print k "\t" pr
      }
    }'
}

current_target_ip() {
  dnat_records | awk -F'\t' -v c="$1" -v p="$2" -v i="$3" -v t="$4" '$1 == c && $2 == p && $3 == i && $5 == t { print $4; exit }'
}

# 占用某协议入站端口的 DNAT：输出 "备注<TAB>IP:端口"
dnat_owner() {
  dnat_records | awk -F'\t' -v p="$1" -v i="$2" '$2 == p && $3 == i { print $1 "\t" $4 ":" $5; exit }'
}

# ---------------------------------------------------------------- 托管配置（DDNS 用）

managed_tmp() {
  safe_mkdirs
  mktemp "$CONF_DIR/.managed.XXXXXX"
}

register_forward_config() {
  local in_port="$1" host="$2" tport="$3" proto="$4" comment="$5" tmp
  tmp="$(managed_tmp)"
  touch "$MANAGED_CONF"
  awk -v i="$in_port" -v t="$tport" -v p="$proto" -v c="$comment" '
    NF < 5 { next }
    !($1 == i && $3 == t && $4 == p && $5 == c) { print }
  ' "$MANAGED_CONF" > "$tmp"
  echo "$in_port $host $tport $proto $comment" >> "$tmp"
  mv "$tmp" "$MANAGED_CONF"
  log "managed config updated: $in_port $host $tport $proto $comment"
}

remove_managed_forward_config() {
  local in_port="$1" tport="$2" proto="$3" comment="$4" tmp
  [ -f "$MANAGED_CONF" ] || return 0
  tmp="$(managed_tmp)"
  awk -v i="$in_port" -v t="$tport" -v p="$proto" -v c="$comment" '
    NF < 5 { next }
    !($1 == i && $3 == t && $4 == p && $5 == c) { print }
  ' "$MANAGED_CONF" > "$tmp"
  mv "$tmp" "$MANAGED_CONF"
  log "managed config removed: in=$in_port target_port=$tport proto=$proto comment=$comment"
}

remove_managed_by_comment() {
  local comment="$1" tmp
  [ -f "$MANAGED_CONF" ] || return 0
  tmp="$(managed_tmp)"
  awk -v c="$comment" 'NF < 5 { next } $5 != c { print }' "$MANAGED_CONF" > "$tmp"
  mv "$tmp" "$MANAGED_CONF"
  log "managed config removed by comment: $comment"
}

# 托管配置中记录的目标（可能是域名）：managed_host 备注 入站端口 目标端口 协议列表
managed_host() {
  local proto
  [ -f "$MANAGED_CONF" ] || return 0
  for proto in $4; do
    awk -v c="$1" -v i="$2" -v t="$3" -v p="$proto" '$1 == i && $3 == t && $4 == p && $5 == c { print $2; found = 1; exit } END { exit !found }' "$MANAGED_CONF" && return 0
  done
}

# ---------------------------------------------------------------- 基础操作

backup_rules() {
  safe_mkdirs
  local base="$BACKUP_DIR/iptables-$(timestamp)" n=1
  local file="$base.rules"
  while [ -e "$file" ]; do
    n=$((n + 1))
    file="$base-$n.rules"
  done
  if ! iptables-save > "$file"; then
    rm -f "$file"
    err "备份失败：iptables-save 执行出错。"
    return 1
  fi
  LAST_BACKUP="$file"
  note "已备份当前规则 → $file"
  log "backup created: $file"
}

# 修改规则前的准备：加锁并备份
prepare_change() {
  lock_acquire || return 1
  backup_rules || return 1
}

save_rules() {
  if command -v netfilter-persistent >/dev/null 2>&1; then
    if netfilter-persistent save >/dev/null 2>&1; then
      ok "规则已持久化（netfilter-persistent）"
      log "rules saved by netfilter-persistent"
    else
      err "netfilter-persistent save 执行失败，请手动检查。"
      log "netfilter-persistent save failed"
      return 1
    fi
  else
    mkdir -p /etc/iptables
    if iptables-save > /etc/iptables/rules.v4; then
      ok "规则已保存到 /etc/iptables/rules.v4"
      warn "未安装 iptables-persistent，重启后不一定自动加载。"
      log "rules saved to /etc/iptables/rules.v4"
    else
      err "保存到 /etc/iptables/rules.v4 失败。"
      return 1
    fi
  fi
}

check_iptables() {
  title "检查 iptables 环境"

  if command -v iptables >/dev/null 2>&1; then
    ok "iptables：$(iptables --version)"
  else
    err "iptables 未安装。"
    if command -v apt >/dev/null 2>&1 && confirm "是否现在安装 iptables 和 iptables-persistent？"; then
      apt update && apt install -y iptables iptables-persistent netfilter-persistent
    else
      note "已跳过安装。"
    fi
  fi

  if command -v iptables-save >/dev/null 2>&1; then
    ok "iptables-save 可用"
  else
    err "iptables-save 不可用，请检查 iptables 安装。"
  fi

  if command -v netfilter-persistent >/dev/null 2>&1; then
    ok "netfilter-persistent 已安装"
  else
    warn "netfilter-persistent 未安装，保存规则时会写入 /etc/iptables/rules.v4"
  fi
}

ip_forward_state() {
  cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo 0
}

# 确保 IPv4 转发开启并持久化；参数 quiet 时只在有改动时输出
ensure_ip_forward() {
  local quiet="${1:-}"

  if ! grep -qsE '^\s*net\.ipv4\.ip_forward\s*=\s*1\s*$' "$SYSCTL_FILE"; then
    mkdir -p "$(dirname "$SYSCTL_FILE")"
    echo "net.ipv4.ip_forward=1" > "$SYSCTL_FILE"
    ok "已写入持久配置 $SYSCTL_FILE"
    log "sysctl file written: $SYSCTL_FILE"
  elif [ -z "$quiet" ]; then
    ok "持久配置已存在：$SYSCTL_FILE"
  fi

  # /etc/sysctl.conf 最后加载，其中的 =0 会覆盖上面的配置
  if grep -qsE '^\s*net\.ipv4\.ip_forward\s*=\s*0' /etc/sysctl.conf; then
    sed -i -E 's/^(\s*net\.ipv4\.ip_forward\s*=\s*0.*)$/# \1  # disabled by ipt-forward-manager/' /etc/sysctl.conf
    ok "已注释 /etc/sysctl.conf 中的 net.ipv4.ip_forward=0"
    log "commented ip_forward=0 in /etc/sysctl.conf"
  fi

  if [ "$(ip_forward_state)" != "1" ]; then
    if command -v sysctl >/dev/null 2>&1; then
      sysctl -qw net.ipv4.ip_forward=1 2>/dev/null
    fi
    [ "$(ip_forward_state)" = "1" ] || echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null
    if [ "$(ip_forward_state)" = "1" ]; then
      ok "已开启 IPv4 转发"
      log "ip_forward enabled"
    else
      err "IPv4 转发开启失败，请手动检查。"
      log "ip_forward enable failed"
      return 1
    fi
  elif [ -z "$quiet" ]; then
    ok "IPv4 转发当前已开启"
  fi
}

check_sysctl() {
  title "检查/修复 IPv4 转发"
  ensure_ip_forward
}

# 放行转发连接的回程流量，FORWARD 默认策略为 DROP 时必需
ensure_forward_established() {
  local spec=(-m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment "$ESTABLISHED_COMMENT" -j ACCEPT)
  iptables -C FORWARD "${spec[@]}" >/dev/null 2>&1 && return 0
  if iptables -I FORWARD 1 "${spec[@]}" >/dev/null 2>&1; then
    log "forward established rule added"
  else
    warn "无法添加 FORWARD 回程放行规则（conntrack），若 FORWARD 默认策略为 DROP，转发可能不通。"
  fi
}

resolve_target() {
  local target="$1" resolved=""

  if valid_ipv4 "$target"; then
    echo "$target"
    return 0
  fi

  if command -v getent >/dev/null 2>&1; then
    resolved="$(getent ahostsv4 "$target" 2>/dev/null | awk '{print $1; exit}')"
  fi
  if [ -z "$resolved" ] && command -v dig >/dev/null 2>&1; then
    resolved="$(dig +short A "$target" 2>/dev/null | grep -E '^([0-9]{1,3}\.){3}[0-9]{1,3}$' | head -n 1)"
  fi
  if [ -z "$resolved" ] && command -v host >/dev/null 2>&1; then
    resolved="$(host -t A "$target" 2>/dev/null | awk '/has address/ {print $4; exit}')"
  fi

  valid_ipv4 "$resolved" || return 1
  echo "$resolved"
}

# 规则不存在则添加：0 已添加，2 已存在，1 失败（错误信息在 IPT_ERR）
ipt_ensure() {
  local table="$1" op="$2" chain="$3"
  shift 3
  iptables -t "$table" -C "$chain" "$@" >/dev/null 2>&1 && return 2
  local pos=(-A "$chain")
  [ "$op" = "-I" ] && pos=(-I "$chain" 1)
  IPT_ERR="$(iptables -t "$table" "${pos[@]}" "$@" 2>&1)" || return 1
  return 0
}

# add_one_forward 入站端口 目标(IP/域名) 目标端口 协议 备注 [已解析IP]
add_one_forward() {
  local in_port="$1" host="$2" tport="$3" proto="$4" comment="$5" ip="${6:-}"
  local label owner todest rc pre_added=0 post_added=0 fwd_added=0

  label="$(printf '%-4s %5s → %s:%s' "${proto^^}" "$in_port" "$host" "$tport")"

  if [ -z "$ip" ] && ! ip="$(resolve_target "$host")"; then
    err "$label  域名解析失败"
    log "resolve failed target=$host proto=$proto in=$in_port target_port=$tport comment=$comment"
    return 1
  fi
  [ "$host" != "$ip" ] && label="$label ($ip)"
  todest="$ip:$tport"

  owner="$(dnat_owner "$proto" "$in_port")"
  if [ -n "$owner" ] && [ "$owner" != "$comment"$'\t'"$todest" ]; then
    err "$label  入站端口已被占用：${owner#*$'\t'} [${owner%%$'\t'*}]"
    return 1
  fi

  ensure_forward_established

  local pre=(-p "$proto" --dport "$in_port" -m comment --comment "$comment" -j DNAT --to-destination "$todest")
  local post=(-p "$proto" -d "$ip" --dport "$tport" -m comment --comment "$comment" -j MASQUERADE)
  local fwd=(-p "$proto" -d "$ip" --dport "$tport" -m comment --comment "$comment" -j ACCEPT)

  ipt_ensure nat -A PREROUTING "${pre[@]}"; rc=$?
  if [ "$rc" -eq 1 ]; then
    err "$label  添加 DNAT 失败：$IPT_ERR"
    return 1
  fi
  [ "$rc" -eq 0 ] && pre_added=1

  ipt_ensure nat -A POSTROUTING "${post[@]}"; rc=$?
  if [ "$rc" -eq 1 ]; then
    err "$label  添加 MASQUERADE 失败：$IPT_ERR"
    [ "$pre_added" -eq 1 ] && iptables -t nat -D PREROUTING "${pre[@]}" 2>/dev/null
    return 1
  fi
  [ "$rc" -eq 0 ] && post_added=1

  ipt_ensure filter -I FORWARD "${fwd[@]}"; rc=$?
  if [ "$rc" -eq 1 ]; then
    err "$label  添加 FORWARD 放行失败：$IPT_ERR"
    [ "$pre_added" -eq 1 ] && iptables -t nat -D PREROUTING "${pre[@]}" 2>/dev/null
    [ "$post_added" -eq 1 ] && iptables -t nat -D POSTROUTING "${post[@]}" 2>/dev/null
    return 1
  fi
  [ "$rc" -eq 0 ] && fwd_added=1

  register_forward_config "$in_port" "$host" "$tport" "$proto" "$comment"
  log "add forward proto=$proto in=$in_port target=$host($ip):$tport comment=$comment"

  if [ $((pre_added + post_added + fwd_added)) -eq 0 ]; then
    ok "$label  ${DIM}[$comment] 已存在${RESET}"
  else
    ok "$label  ${DIM}[$comment]${RESET}"
  fi
}

# 删除一条转发的三类规则：delete_forward_rules 备注 协议 入站端口 目标IP 目标端口
delete_forward_rules() {
  local comment="$1" proto="$2" in_port="$3" ip="$4" tport="$5"
  DEL_COUNT=0
  delete_rule_nums nat PREROUTING "$comment" "$proto" "$in_port" "" "$ip:$tport" DNAT
  delete_rule_nums nat POSTROUTING "$comment" "$proto" "$tport" "$ip" "" MASQUERADE
  delete_rule_nums filter FORWARD "$comment" "$proto" "$tport" "$ip" "" ACCEPT
  log "delete forward proto=$proto in=$in_port target=$ip:$tport comment=$comment rules=$DEL_COUNT"
}

# 删除 nat 表所有链和 FORWARD 链中带该备注的规则
delete_by_comment_core() {
  local comment="$1" chain
  DEL_COUNT=0
  for chain in $(iptables -t nat -S 2>/dev/null | awk '$1 == "-P" || $1 == "-N" { print $2 }'); do
    delete_rule_nums nat "$chain" "$comment"
  done
  delete_rule_nums filter FORWARD "$comment"
  remove_managed_by_comment "$comment"
  log "delete by comment: $comment rules=$DEL_COUNT"
}

# nat 表与 FORWARD 链中带该备注的规则原文
rules_with_comment() {
  { iptables -t nat -S 2>/dev/null; iptables -S FORWARD 2>/dev/null; } | awk -v c="$1" "$PARSE_AWK"'
    /^-A / { parse($0); if (C == c) print }'
}

# ---------------------------------------------------------------- 选择列表

# 列出转发配置并让用户选择，结果写入 S_COMMENT S_IN S_IP S_TPORT S_PROTOS S_HOST
SEL_MAX=0
valid_index() {
  if [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le "$SEL_MAX" ]; then
    return 0
  fi
  VERR="请输入 1-$SEL_MAX 之间的序号。"
  return 1
}

print_config_header() {
  printf '  %s' "$BOLD"
  cell "序号" 4; cell "入站端口" 8; cell "协议" 7; cell "目标" 34; printf '备注%s\n' "$RESET"
}

print_config_row() {
  local idx="$1" comment="$2" in_port="$3" ip="$4" tport="$5" protos="$6" host
  host="$(managed_host "$comment" "$in_port" "$tport" "$protos")"
  local target="$ip:$tport"
  [ -n "$host" ] && [ "$host" != "$ip" ] && target="$host:$tport ($ip)"
  printf '  '
  cell "$idx" 4 "$CYAN"; cell "$in_port" 8; cell "$(protocol_label "$protos")" 7; cell "$target" 34; printf '%s\n' "$comment"
}

select_forward_config() {
  local configs=() i idx
  mapfile -t configs < <(forward_configs)

  if [ "${#configs[@]}" -eq 0 ]; then
    warn "没有找到带备注的 DNAT 转发规则。"
    return 1
  fi

  print_config_header
  for i in "${!configs[@]}"; do
    IFS=$'\t' read -r S_COMMENT S_IN S_IP S_TPORT S_PROTOS <<< "${configs[$i]}"
    print_config_row $((i + 1)) "$S_COMMENT" "$S_IN" "$S_IP" "$S_TPORT" "$S_PROTOS"
  done
  echo

  SEL_MAX="${#configs[@]}"
  ask idx "选择序号（q 取消）" valid_index || return 1
  IFS=$'\t' read -r S_COMMENT S_IN S_IP S_TPORT S_PROTOS <<< "${configs[$((idx - 1))]}"
  S_HOST="$(managed_host "$S_COMMENT" "$S_IN" "$S_TPORT" "$S_PROTOS")"
  S_HOST="${S_HOST:-$S_IP}"
}

# ---------------------------------------------------------------- 添加

add_forward_interactive() {
  title "添加单条转发"
  note "随时输入 q 取消"

  local in_port host tport protos comment proto
  ask in_port "入站端口" valid_port || return
  ask host "目标 IP/域名" valid_target || return
  ask tport "目标端口" valid_port "$in_port" || return
  ask_protocols protos 3 || return
  ask comment "备注" valid_comment "$(default_comment "$in_port" "$host" "$tport")" || return

  echo
  info "$(protocol_label "$protos")  $in_port → $host:$tport  [$comment]"
  confirm "确认添加？" || { note "已取消。"; return; }

  prepare_change || return
  ensure_ip_forward quiet
  local ip
  if ! ip="$(resolve_target "$host")"; then
    err "域名解析失败：$host"
    return
  fi
  local added=0
  for proto in $protos; do
    add_one_forward "$in_port" "$host" "$tport" "$proto" "$comment" "$ip" && added=1
  done
  [ "$added" -eq 1 ] && save_rules
}

print_line_table_header() {
  printf '  %s' "$BOLD"
  cell "入站端口" 8; cell "目标" 30; cell "目标端口" 8; cell "协议" 7; printf '备注%s\n' "$RESET"
}

print_line_table_row() {
  printf '  '
  cell "$F_IN" 8; cell "$F_HOST" 30; cell "$F_TPORT" 8; cell "$(protocol_label "$F_PROTOS")" 7; printf '%s\n' "$F_COMMENT"
}

# 应用一组已校验的转发行
apply_forward_lines() {
  local line proto ip ok_n=0 fail_n=0
  for line in "$@"; do
    parse_forward_line "$line" || continue
    if ! ip="$(resolve_target "$F_HOST")"; then
      err "$F_IN → $F_HOST:$F_TPORT  域名解析失败"
      fail_n=$((fail_n + 1))
      continue
    fi
    for proto in $F_PROTOS; do
      if add_one_forward "$F_IN" "$F_HOST" "$F_TPORT" "$proto" "$F_COMMENT" "$ip"; then
        ok_n=$((ok_n + 1))
      else
        fail_n=$((fail_n + 1))
      fi
    done
  done
  echo
  info "成功 $ok_n 条，失败 $fail_n 条"
  [ "$ok_n" -gt 0 ] && save_rules
}

batch_add_forward() {
  title "批量添加转发"
  note "格式：入站端口 目标IP或域名 目标端口 [协议 tcp/udp/both，默认 both] [备注]"
  note "例如：443 1.1.1.1 443 both xjj-forward-443"
  note "      8443 home.example.com 443 tcp"
  note "每行一条，输入 END 结束"
  echo

  local lines=() line rc
  while read -rp "  > " line; do
    line="$(trim "$line")"
    case "$line" in
      END|end) break ;;
    esac
    parse_forward_line "$line"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      lines+=("$line")
    elif [ "$rc" -eq 1 ]; then
      err "已忽略：$VERR"
    fi
  done

  if [ "${#lines[@]}" -eq 0 ]; then
    warn "没有输入有效规则。"
    return
  fi

  echo
  print_line_table_header
  for line in "${lines[@]}"; do
    parse_forward_line "$line"
    print_line_table_row
  done

  confirm "确认添加以上 ${#lines[@]} 条？" || { note "已取消。"; return; }
  prepare_change || return
  ensure_ip_forward quiet
  apply_forward_lines "${lines[@]}"
}

# ---------------------------------------------------------------- 删除

delete_by_comment() {
  title "选择删除转发"
  select_forward_config || return

  local protos="$S_PROTOS"
  if [ "$S_PROTOS" = "tcp udp" ]; then
    ask_protocols protos 3 "删除哪些协议（未选的保留）" || return
  fi

  echo
  info "删除 $(protocol_label "$protos")  $S_IN → $S_HOST:$S_TPORT  [$S_COMMENT]"
  confirm "确认删除？" || { note "已取消。"; return; }

  prepare_change || return
  local proto
  for proto in $protos; do
    delete_forward_rules "$S_COMMENT" "$proto" "$S_IN" "$S_IP" "$S_TPORT"
    remove_managed_forward_config "$S_IN" "$S_TPORT" "$proto" "$S_COMMENT"
    ok "已删除 ${proto^^} $S_IN → $S_IP:$S_TPORT（$DEL_COUNT 条规则）"
  done
  save_rules
}

batch_delete_by_comment() {
  title "按备注批量删除"

  local existing
  existing="$(forward_configs | cut -f1 | sort -u)"
  if [ -n "$existing" ]; then
    note "现有备注："
    printf '%s\n' "$existing" | indent
    echo
  fi
  note "每行输入一个备注，输入 END 结束"

  local comments=() c
  while read -rp "  > " c; do
    c="$(trim "$c")"
    case "$c" in
      END|end) break ;;
      "") continue ;;
    esac
    comments+=("$c")
  done

  if [ "${#comments[@]}" -eq 0 ]; then
    warn "没有输入任何备注。"
    return
  fi

  local found=0 rules
  for c in "${comments[@]}"; do
    echo
    printf '  %s%s%s\n' "$BOLD" "$c" "$RESET"
    rules="$(rules_with_comment "$c")"
    if [ -n "$rules" ]; then
      printf '%s\n' "$rules" | indent
      found=1
    else
      note "    未找到对应规则"
    fi
  done

  [ "$found" -eq 1 ] || { warn "没有可删除的规则。"; return; }
  confirm "确认删除以上规则？" || { note "已取消。"; return; }

  prepare_change || return
  for c in "${comments[@]}"; do
    delete_by_comment_core "$c"
    [ "$DEL_COUNT" -gt 0 ] && ok "已删除 [$c]（$DEL_COUNT 条规则）"
  done
  save_rules
}

# ---------------------------------------------------------------- 修改

modify_rule() {
  title "修改已有转发"
  select_forward_config || return

  local old_comment="$S_COMMENT" old_in="$S_IN" old_ip="$S_IP" old_tport="$S_TPORT" old_protos="$S_PROTOS"
  local in_port host tport protos comment proto

  echo
  note "直接回车保留原值，输入 q 取消"
  ask in_port "入站端口" valid_port "$old_in" || return
  ask host "目标 IP/域名" valid_target "$S_HOST" || return
  ask tport "目标端口" valid_port "$old_tport" || return
  ask_protocols protos "$(choice_from_protocols "$old_protos")" || return
  ask comment "备注" valid_comment "$old_comment" || return

  echo
  info "原：$(protocol_label "$old_protos")  $old_in → $S_HOST:$old_tport  [$old_comment]"
  info "新：$(protocol_label "$protos")  $in_port → $host:$tport  [$comment]"

  # 先检查新配置可用，再动旧规则
  local ip owner
  if ! ip="$(resolve_target "$host")"; then
    err "域名解析失败：$host，未做任何修改。"
    return
  fi
  for proto in $protos; do
    owner="$(dnat_owner "$proto" "$in_port")"
    if [ -n "$owner" ] && [ "${owner%%$'\t'*}" != "$old_comment" ]; then
      err "${proto^^} 入站端口 $in_port 已被占用：${owner#*$'\t'} [${owner%%$'\t'*}]，未做任何修改。"
      return
    fi
  done

  confirm "确认修改？" || { note "已取消。"; return; }

  prepare_change || return
  local managed_copy
  managed_copy="$(managed_tmp)"
  cp "$MANAGED_CONF" "$managed_copy" 2>/dev/null || : > "$managed_copy"

  for proto in $old_protos; do
    delete_forward_rules "$old_comment" "$proto" "$old_in" "$old_ip" "$old_tport"
    remove_managed_forward_config "$old_in" "$old_tport" "$proto" "$old_comment"
  done

  ensure_ip_forward quiet
  local failed=0
  for proto in $protos; do
    add_one_forward "$in_port" "$host" "$tport" "$proto" "$comment" "$ip" || failed=1
  done

  if [ "$failed" -eq 1 ]; then
    if iptables-restore < "$LAST_BACKUP"; then
      mv "$managed_copy" "$MANAGED_CONF"
      err "修改失败，已恢复为修改前的规则。"
      log "modify failed, restored from $LAST_BACKUP"
    else
      rm -f "$managed_copy"
      err "修改失败，且自动恢复失败！请用菜单 17 从备份回滚：$LAST_BACKUP"
      log "modify failed, restore failed: $LAST_BACKUP"
    fi
    return
  fi

  rm -f "$managed_copy"
  save_rules
}

# ---------------------------------------------------------------- 查看

show_summary() {
  title "转发规则摘要"

  local records=() r c p i ip tp host target
  mapfile -t records < <(dnat_records | sort -t$'\t' -k3,3n -k2,2)
  if [ "${#records[@]}" -eq 0 ]; then
    note "没有找到 DNAT 转发规则。"
    return
  fi

  printf '  %s' "$BOLD"
  cell "入站端口" 8; cell "协议" 5; cell "目标" 36; printf '备注%s\n' "$RESET"
  for r in "${records[@]}"; do
    IFS=$'\t' read -r c p i ip tp <<< "$r"
    target="$ip:$tp"
    host=""
    [ "$c" != "-" ] && host="$(managed_host "$c" "$i" "$tp" "$p")"
    [ -n "$host" ] && [ "$host" != "$ip" ] && target="$host:$tp ($ip)"
    printf '  '
    cell "$i" 8 "$CYAN"; cell "${p^^}" 5; cell "$target" 36; printf '%s\n' "$c"
  done
  echo
  note "共 ${#records[@]} 条 DNAT 规则"
}

show_all_rules() {
  title "NAT 表"
  iptables -t nat -L -n -v --line-numbers | indent
  title "FORWARD 链"
  iptables -L FORWARD -n -v --line-numbers | indent
}

show_comment_rules() {
  title "带备注的规则"
  local rules
  rules="$(iptables-save | grep -F -- '--comment')"
  if [ -n "$rules" ]; then
    printf '%s\n' "$rules" | indent
  else
    note "没有带备注的规则。"
  fi
}

search_rules() {
  title "搜索规则"
  local keyword result
  read -rp "  关键词（端口/IP/备注）: " keyword || return
  keyword="$(trim "$keyword")"
  if [ -z "$keyword" ]; then
    err "关键词不能为空。"
    return
  fi

  result="$(iptables-save | grep -F -e "$keyword")"
  echo
  if [ -z "$result" ]; then
    note "没有找到相关规则。"
    return
  fi
  if [ -t 1 ]; then
    printf '%s\n' "$result" | grep -F --color=always -e "$keyword" | indent
  else
    printf '%s\n' "$result" | indent
  fi
}

test_forward_hit() {
  title "端口命中统计"
  local port
  ask port "入站端口" valid_port || return

  echo
  printf '  %sNAT PREROUTING%s\n' "$BOLD" "$RESET"
  iptables -t nat -L PREROUTING -n -v --line-numbers | awk -v p="dpt:$port" '
    NR <= 2 { print; next }
    { for (i = 1; i <= NF; i++) if ($i == p) { print; found = 1; next } }
    END { if (!found) print "（没有匹配该端口的规则）" }' | indent

  local comments
  comments="$(dnat_records | awk -F'\t' -v p="$port" '$3 == p && $1 != "-" { print $1 }' | sort -u)"
  echo
  printf '  %sFORWARD%s\n' "$BOLD" "$RESET"
  iptables -L FORWARD -n -v --line-numbers | awk -v list="$comments" '
    BEGIN { n = split(list, cs, "\n") }
    NR <= 2 { print; next }
    { for (i = 1; i <= n; i++) if (cs[i] != "" && index($0, "/* " cs[i] " */")) { print; found = 1; next } }
    END { if (!found) print "（没有对应的 FORWARD 规则）" }' | indent

  echo
  note "packets/bytes 增加说明流量命中了规则。"
}

test_target() {
  title "测试目标 TCP 连通性"
  local host port
  ask host "目标 IP/域名" valid_target || return
  ask port "目标端口" valid_port || return

  echo
  if command -v nc >/dev/null 2>&1; then
    if nc -z -w 3 "$host" "$port" >/dev/null 2>&1; then
      ok "TCP $host:$port 可连接"
    else
      err "TCP $host:$port 不可连接或超时"
    fi
  elif command -v timeout >/dev/null 2>&1; then
    if timeout 3 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$host" "$port" >/dev/null 2>&1; then
      ok "TCP $host:$port 可连接"
    else
      err "TCP $host:$port 不可连接或超时"
    fi
  else
    warn "没有 nc/timeout，无法测试。可安装：apt install -y netcat-openbsd"
  fi
  note "UDP 无法可靠探测，这里只测试 TCP。"
}

# ---------------------------------------------------------------- 导入导出

export_conf() {
  title "导出转发为 .conf"
  safe_mkdirs

  local out_file
  read -rp "  导出路径 [$CONF_DIR/forwards-$(timestamp).conf]: " out_file || return
  out_file="$(trim "$out_file")"
  out_file="${out_file:-$CONF_DIR/forwards-$(timestamp).conf}"

  if [ ! -d "$(dirname "$out_file")" ]; then
    err "目录不存在：$(dirname "$out_file")"
    return
  fi

  local configs=() r c i ip tp pr host count=0 skipped tmp
  mapfile -t configs < <(forward_configs)
  skipped="$(dnat_records | grep -c '^-'$'\t')"

  if [ "${#configs[@]}" -eq 0 ]; then
    warn "没有可导出的带备注 DNAT 转发规则。"
    return
  fi

  tmp="$(mktemp)"
  {
    echo "# iptables forward config exported by $APP_NAME $VERSION"
    echo "# format: in_port target_ip_or_domain target_port protocol comment"
    echo "# protocol: tcp / udp / both"
    echo
    for r in "${configs[@]}"; do
      IFS=$'\t' read -r c i ip tp pr <<< "$r"
      host="$(managed_host "$c" "$i" "$tp" "$pr")"
      [ "$pr" = "tcp udp" ] && pr="both"
      printf '%-6s %-30s %-6s %-5s %s\n' "$i" "${host:-$ip}" "$tp" "$pr" "$c"
      count=$((count + 1))
    done
  } > "$tmp"

  if ! mv "$tmp" "$out_file"; then
    rm -f "$tmp"
    err "写入失败：$out_file"
    return
  fi

  ok "已导出 $count 条配置 → $out_file"
  [ "$skipped" -gt 0 ] && warn "跳过了 $skipped 条没有备注的 DNAT 规则（非本脚本创建）。"
  log "config exported: $out_file"
}

import_conf() {
  title "从 .conf 导入转发"

  local file
  ask file ".conf 文件路径" valid_file || return

  local raw=() lines=() line n=0 rc bad=0
  mapfile -t raw < "$file"

  echo
  for line in "${raw[@]}"; do
    n=$((n + 1))
    parse_forward_line "$line"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      lines+=("$(trim "$line")")
    elif [ "$rc" -eq 1 ]; then
      err "第 $n 行：$VERR"
      bad=$((bad + 1))
    fi
  done

  if [ "${#lines[@]}" -eq 0 ]; then
    warn "没有可导入的有效规则。"
    return
  fi

  [ "$bad" -gt 0 ] && echo
  print_line_table_header
  for line in "${lines[@]}"; do
    parse_forward_line "$line"
    print_line_table_row
  done
  echo
  info "有效 ${#lines[@]} 条，错误 $bad 条（错误行会被跳过）"

  confirm "确认导入？" || { note "已取消。"; return; }
  prepare_change || return
  ensure_ip_forward quiet
  apply_forward_lines "${lines[@]}"
}

valid_file() {
  if [ -f "$1" ] && [ -r "$1" ]; then
    return 0
  fi
  VERR="文件不存在或不可读：$1"
  return 1
}

# ---------------------------------------------------------------- 备份/回滚

BACKUPS=()
load_backups() {
  BACKUPS=()
  [ -d "$BACKUP_DIR" ] || return 0
  mapfile -t BACKUPS < <(ls -1t "$BACKUP_DIR"/*.rules 2>/dev/null)
}

print_backups() {
  local i f
  if [ "${#BACKUPS[@]}" -eq 0 ]; then
    note "没有找到备份文件。"
    return 1
  fi
  printf '  %s' "$BOLD"
  cell "序号" 4; cell "时间" 20; cell "大小" 8; printf '文件%s\n' "$RESET"
  for i in "${!BACKUPS[@]}"; do
    f="${BACKUPS[$i]}"
    printf '  '
    cell $((i + 1)) 4 "$CYAN"
    cell "$(date -r "$f" '+%F %T')" 20
    cell "$(du -h "$f" | cut -f1)" 8
    printf '%s\n' "$f"
  done
}

list_backups() {
  title "备份列表"
  load_backups
  print_backups
}

restore_backup() {
  title "从备份回滚"
  load_backups
  print_backups || return
  echo

  local idx file
  SEL_MAX="${#BACKUPS[@]}"
  ask idx "选择序号（q 取消）" valid_index || return
  file="${BACKUPS[$((idx - 1))]}"

  warn "这会用 $file 覆盖当前 iptables 规则。"
  confirm "确认回滚？" || { note "已取消。"; return; }

  prepare_change || return
  if ! iptables-restore < "$file"; then
    err "回滚失败：iptables-restore 执行出错，当前规则未改变。"
    log "restore failed: $file"
    return
  fi
  ok "已回滚到 $file"
  log "restore backup: $file"
  save_rules
}

# ---------------------------------------------------------------- 自检/DDNS

refresh_managed_domain_forwards() {
  [ -t 1 ] && title "刷新域名/DDNS 转发"
  safe_mkdirs

  if [ ! -s "$MANAGED_CONF" ]; then
    note "没有托管的转发配置。"
    log "ddns refresh skipped: no managed config"
    return 0
  fi

  lock_acquire 120 || { log "ddns refresh skipped: lock busy"; return 1; }

  local entries=() e in_port host tport proto comment rest ip cur domains=0 changed=0 failed=0
  mapfile -t entries < "$MANAGED_CONF"

  for e in "${entries[@]}"; do
    in_port="" host="" tport="" proto="" comment="" rest=""
    read -r in_port host tport proto comment rest <<< "$e"
    [ -z "$in_port" ] && continue
    [ "${in_port:0:1}" = "#" ] && continue
    if [ -z "$comment" ]; then
      log "ddns refresh skipped malformed line: $e"
      continue
    fi
    valid_ipv4 "$host" && continue
    domains=$((domains + 1))

    if ! ip="$(resolve_target "$host")"; then
      warn "域名解析失败，跳过：$host"
      log "ddns refresh resolve failed target=$host proto=$proto in=$in_port comment=$comment"
      failed=$((failed + 1))
      continue
    fi

    cur="$(current_target_ip "$comment" "$proto" "$in_port" "$tport")"
    if [ "$cur" = "$ip" ]; then
      log "ddns refresh unchanged target=$host ip=$ip proto=$proto in=$in_port comment=$comment"
      continue
    fi

    info "$host：${cur:-无} → $ip（${proto^^} $in_port → $tport）"
    [ -n "$cur" ] && delete_forward_rules "$comment" "$proto" "$in_port" "$cur" "$tport"
    if add_one_forward "$in_port" "$host" "$tport" "$proto" "$comment" "$ip"; then
      changed=$((changed + 1))
    else
      failed=$((failed + 1))
    fi
  done

  if [ "$changed" -gt 0 ]; then
    save_rules
    log "ddns refresh changed=$changed failed=$failed"
  else
    log "ddns refresh no changes (domains=$domains failed=$failed)"
  fi

  if [ "$domains" -eq 0 ]; then
    note "没有使用域名的转发。"
  elif [ "$changed" -eq 0 ] && [ "$failed" -eq 0 ]; then
    ok "$domains 条域名转发均无需更新。"
  else
    info "已更新 $changed 条，失败 $failed 条"
  fi
}

install_self_check_service() {
  title "安装自检/DDNS 定时服务"

  if ! command -v systemctl >/dev/null 2>&1; then
    err "系统没有 systemctl，无法安装。"
    return
  fi
  if [ ! -f "$SELF_PATH" ]; then
    err "无法定位当前脚本路径：$SELF_PATH"
    return
  fi

  cat > /etc/systemd/system/ipt-forward-selfcheck.service <<EOF
[Unit]
Description=iptables forward manager self check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash "$SELF_PATH" --self-check
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/ipt-forward-selfcheck.timer <<EOF
[Unit]
Description=run iptables forward manager self check periodically

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Unit=ipt-forward-selfcheck.service

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable ipt-forward-selfcheck.service >/dev/null 2>&1
  if systemctl enable --now ipt-forward-selfcheck.timer >/dev/null 2>&1; then
    ok "已安装并启用 ipt-forward-selfcheck.service / .timer"
    note "每 5 分钟刷新一次域名/DDNS 转发，脚本路径：$SELF_PATH"
    note "移动或删除脚本后需要重新安装。日志：$LOG_FILE"
    log "self-check service and timer installed"
  else
    err "启用定时器失败，请执行 systemctl status ipt-forward-selfcheck.timer 查看。"
  fi
}

uninstall_self_check_service() {
  title "卸载自检/DDNS 定时服务"
  systemctl disable --now ipt-forward-selfcheck.timer >/dev/null 2>&1 || true
  systemctl disable ipt-forward-selfcheck.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/ipt-forward-selfcheck.timer /etc/systemd/system/ipt-forward-selfcheck.service
  systemctl daemon-reload 2>/dev/null || true
  ok "已卸载。"
  log "self-check service and timer uninstalled"
}

self_check() {
  log "self-check start"

  if command -v iptables >/dev/null 2>&1; then
    log "iptables ok: $(iptables --version)"
  else
    log "iptables missing"
  fi

  local ipf count
  ipf="$(ip_forward_state)"
  log "runtime ip_forward=$ipf"
  if [ "$ipf" != "1" ]; then
    echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true
    log "attempted to enable runtime ip_forward"
  fi

  count="$(iptables-save -t nat 2>/dev/null | grep -c -- '-j DNAT')"
  log "dnat rule count=${count:-0}"

  refresh_managed_domain_forwards
  lock_release
  log "self-check end"
}

view_log() {
  title "脚本日志（最近 100 行）"
  if [ -f "$LOG_FILE" ]; then
    tail -n 100 "$LOG_FILE" | indent
  else
    note "暂无日志。"
  fi
}

# ---------------------------------------------------------------- 菜单

menu_item() {
  printf '  %s%3s)%s ' "$CYAN" "$1" "$RESET"
  cell "$2" 26
}

menu_group() {
  echo
  printf '  %s%s%s\n' "$BOLD" "$1" "$RESET"
}

menu() {
  [ -t 1 ] && clear

  local fwd rules timer
  if [ "$(ip_forward_state)" = "1" ]; then fwd="${GREEN}已开启${RESET}"; else fwd="${RED}未开启${RESET}"; fi
  rules="$(forward_configs | wc -l)"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet ipt-forward-selfcheck.timer 2>/dev/null; then
    timer="${GREEN}运行中${RESET}"
  else
    timer="${DIM}未启用${RESET}"
  fi

  printf '  %s%s%s\n' "$DIM" "$HR" "$RESET"
  printf '  %s%s%s %sv%s%s\n' "$BOLD$CYAN" "$APP_NAME" "$RESET" "$DIM" "$VERSION" "$RESET"
  printf '  IPv4 转发 %s   转发配置 %s%s%s 条   DDNS 定时 %s\n' "$fwd" "$BOLD" "$rules" "$RESET" "$timer"
  printf '  %s%s%s\n' "$DIM" "$HR" "$RESET"

  menu_group "基础检查"
  menu_item 1 "检查 iptables 环境";   menu_item 2 "检查/修复 IPv4 转发"; echo
  menu_group "规则查看"
  menu_item 3 "转发规则摘要";         menu_item 4 "全部 NAT/FORWARD 规则"; echo
  menu_item 5 "搜索规则";             menu_item 6 "带备注的规则"; echo
  menu_item 7 "端口命中统计"; echo
  menu_group "添加 / 删除 / 修改"
  menu_item 8 "添加单条转发";         menu_item 9 "批量添加转发"; echo
  menu_item 10 "选择删除转发";        menu_item 11 "按备注批量删除"; echo
  menu_item 12 "修改已有转发"; echo
  menu_group "导入导出"
  menu_item 13 "导出为 .conf";        menu_item 14 "从 .conf 导入"; echo
  menu_group "备份 / 回滚 / 测试"
  menu_item 15 "备份当前规则";        menu_item 16 "查看备份列表"; echo
  menu_item 17 "从备份回滚";          menu_item 18 "测试目标 TCP 连通"; echo
  menu_item 19 "保存当前规则"; echo
  menu_group "自检 / DDNS / 日志"
  menu_item 20 "安装 DDNS 定时服务";  menu_item 21 "卸载 DDNS 定时服务"; echo
  menu_item 22 "查看日志";            menu_item 23 "立即刷新 DDNS 转发"; echo
  echo
  menu_item 0 "退出"; echo
  printf '  %s%s%s\n' "$DIM" "$HR" "$RESET"
}

main() {
  if [ "${1:-}" = "--self-check" ]; then
    self_check
    exit 0
  fi

  need_root
  safe_mkdirs

  local choice
  while true; do
    menu
    if ! command -v iptables >/dev/null 2>&1; then
      warn "未检测到 iptables，请先执行 1 安装。"
    fi
    read -rp "  请选择: " choice || { echo; exit 0; }
    choice="$(trim "$choice")"

    case "$choice" in
      1) check_iptables ;;
      2) check_sysctl ;;
      3) show_summary ;;
      4) show_all_rules ;;
      5) search_rules ;;
      6) show_comment_rules ;;
      7) test_forward_hit ;;
      8) add_forward_interactive ;;
      9) batch_add_forward ;;
      10) delete_by_comment ;;
      11) batch_delete_by_comment ;;
      12) modify_rule ;;
      13) export_conf ;;
      14) import_conf ;;
      15) title "备份当前规则"; backup_rules && ok "备份完成" ;;
      16) list_backups ;;
      17) restore_backup ;;
      18) test_target ;;
      19) title "保存当前规则"; save_rules ;;
      20) install_self_check_service ;;
      21) uninstall_self_check_service ;;
      22) view_log ;;
      23) refresh_managed_domain_forwards ;;
      0|q) exit 0 ;;
      "") continue ;;
      *) err "无效选择：$choice" ;;
    esac
    lock_release
    pause
  done
}

main "$@"
