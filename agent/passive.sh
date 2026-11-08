#!/usr/bin/env bash
# ictl 节点端：从服务端拉取转发规则，应用到 iptables 并上报结果

set -u
umask 077

VERSION="0.6.0-rc.1"
BIN="/usr/local/lib/ictl/passive.sh"
CONF_DIR="/etc/ictl"
CONF="$CONF_DIR/agent.conf"
LIB="/var/lib/ictl"
CONFIG="$LIB/config"
APPLIED="$LIB/applied"
RESOLVED="$LIB/resolved"
STATE="$LIB/state"
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
  for c in curl openssl ip iptables iptables-save iptables-restore getent flock od systemctl conntrack; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    err "缺少依赖：${missing[*]}"
    exit 1
  fi
}

# ---------------------------------------------------------------- 配置与状态

# 自动更新开关由管理器维护，此处仅上报。
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
R_V="" R_STATE="ok" R_ERR="" NET="" ST_TIME="" SKEW="" R_GEN=0 CONFIRM="" BLOCK_GEN="" BLOCK_ERR=""
load_state() {
  local k v
  R_V="" R_STATE="ok" R_ERR="" NET="" ST_TIME="" SKEW="" R_GEN=0 CONFIRM="" BLOCK_GEN="" BLOCK_ERR=""
  if [ -f "$STATE" ]; then
    while IFS='=' read -r k v; do
      case "$k" in
        v) R_V="$v" ;;
        state) R_STATE="$v" ;;
        err) R_ERR="$v" ;;
        net) NET="$v" ;;
        time) ST_TIME="$v" ;;
        skew) SKEW="$v" ;;
        generation) R_GEN="$v" ;;
        confirm) CONFIRM="$v" ;;
        blocked_generation) BLOCK_GEN="$v" ;;
        blocked_error) BLOCK_ERR="$v" ;;
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
  printf 'generation=%s\nconfirm=%s\nblocked_generation=%s\nblocked_error=%s\n' "$R_GEN" "$CONFIRM" "$BLOCK_GEN" "$BLOCK_ERR" >> "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
}

with_lock() {
  if [ "${ICTL_LOCK_HELD:-0}" = 1 ]; then "$@"; return $?; fi
  local rc
  exec 9>"$LOCK" || { err "无法打开锁文件 $LOCK"; return 1; }
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

# Explicit IPv4 routes; the profile journal is shared with the Go Agent.
route_fields() {
  local value="$1" tag listen source device gateway
  R_LISTEN="" R_IN="" R_OUT="" R_IDEV="" R_ODEV="" R_IGW="" R_OGW=""
  IFS=, read -r tag listen source device gateway <<< "$value"
  [ "$tag" = v2 ] && [ "$(tr -cd ',' <<< "$value" | wc -c)" = 4 ] && [[ "$listen" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] || return 1
  if [ "$source,$device,$gateway" != '-,-,-' ]; then
    [ "$device" != - ] && valid_ipv4 "$source" && [[ "$device" =~ ^[A-Za-z0-9_.:-]{1,15}$ ]] || return 1
    { [ "$gateway" = - ] || valid_ipv4 "$gateway"; } || return 1
    R_OUT="$source" R_ODEV="$device" R_OGW="$gateway"
  fi
  R_LISTEN="$listen"
}
route_numbers() {
  local hash
  hash="$(printf '%s' "$1" | sha256_hex)" || return 1
  ROUTE_TABLE=$((100000+16#${hash:0:5}))
  ROUTE_MARK=$((0x49000000+ROUTE_TABLE))
  ROUTE_PRIORITY=$((18000+ROUTE_TABLE%10000))
}
# Run in a subshell: checking a neighbour must not overwrite this profile's IDs.
route_policy_owned() (
  local priority mark table protocol key expected_mark
  read -r priority mark table protocol <<< "$(awk '{p=$1;for(i=2;i<NF;i++){if($i=="fwmark")m=$(i+1);if($i=="lookup")t=$(i+1);if($i=="proto")r=$(i+1)}print p,m,t,r}' <<< "$1")"
  [[ "$table" =~ ^[0-9]+$ ]] && [ "$protocol" = 242 ] && [ -f "$LIB/routing/$table" ] || return 1
  key="$(cat "$LIB/routing/$table")" || return 1
  route_numbers "$key" || return 1
  printf -v expected_mark '0x%x' "$ROUTE_MARK"
  [ "$table" = "$ROUTE_TABLE" ] && [ "$priority" = "$ROUTE_PRIORITY:" ] || return 1
  [ "$mark" = "$expected_mark" ] || [ "$mark" = "$expected_mark/0xffffffff" ]
)
route_prepare_profile() {
  local local_ip="$1" device="$2" gateway="$3" key="$1,$2,$3" path existing routes line prefix resolved_gw='-' current own_rule=''
  local prior_routes wanted=$'default 32767\n' metric
  route_numbers "$key" || return 1
  path="$LIB/routing/$ROUTE_TABLE"
  ip -o -4 address show dev "$device" | awk '$3=="inet" {split($4,a,"/");print a[1]}' | grep -Fxq "$local_ip" || { echo "地址 $local_ip 未绑定在网卡 $device" >&2; return 1; }
  routes="$(ip -N -4 route show table "$ROUTE_TABLE" 2>&1)" || {
    [[ "$routes" == *'FIB table does not exist'* ]] || { echo "$routes" >&2; return 1; }; routes='';
  }
  existing="$(ip -N -4 -o rule show)" || return 1
  existing="$(awk -v p="$ROUTE_PRIORITY:" '$1==p' <<< "$existing")"
  if [ -f "$path" ]; then
    [ "$(cat "$path")" = "$key" ] || { echo '路由表标识冲突' >&2; return 1; }
    if [ -n "$routes" ] && grep -v 'proto 242' <<< "$routes" | grep -q .; then echo '路由表含有其他工具的规则' >&2; return 1; fi
  elif [ -n "$routes" ]; then echo '路由表被占用' >&2; return 1
  fi
  prior_routes="$routes"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    route_policy_owned "$line" || { echo '策略路由优先级冲突' >&2; return 1; }
    awk -v t="$ROUTE_TABLE" '{for(i=1;i<NF;i++)if($i=="lookup"&&$(i+1)==t)found=1} END{exit !found}' <<< "$line" && own_rule=1
  done <<< "$existing"
  mkdir -p "$LIB/routing" || return 1
  printf '%s\n' "$key" > "$path.tmp" && mv "$path.tmp" "$path" || return 1
  routes="$(ip -N -4 route show dev "$device" scope link)" || return 1
  while read -r prefix line; do
    [ -n "$prefix" ] && [ "$prefix" != default ] || continue
    ip -4 route replace "$prefix" dev "$device" src "$local_ip" table "$ROUTE_TABLE" proto 242 || return 1
    wanted+="$prefix 0"$'\n'
  done <<< "$routes"
  if [ "$gateway" = - ]; then
    routes="$(ip -N -4 route show default dev "$device")" || return 1
    while read -r line; do
      [ -n "$line" ] || continue
      current="$(awk '{for(i=1;i<NF;i++)if($i=="via")print $(i+1)}' <<< "$line")"
      [ -n "$current" ] || continue
      [ "$resolved_gw" = - ] || [ "$resolved_gw" = "$current" ] || { echo '网卡存在多个网关，请明确指定' >&2; return 1; }
      resolved_gw="$current"
    done <<< "$routes"
    gateway="$resolved_gw"
  fi
  ip -4 route replace unreachable default metric 32767 table "$ROUTE_TABLE" proto 242 || return 1
  if [ "$gateway" != - ]; then
    ip -4 route replace default via "$gateway" dev "$device" src "$local_ip" metric 10 table "$ROUTE_TABLE" proto 242 || return 1
    wanted+=$'default 10\n'
  fi
  # Remove withdrawn routes only after their replacements are in place.
  while read -r prefix line; do
    [ -n "$prefix" ] || continue
    case "$prefix" in unreachable|7) prefix="${line%% *}" ;; esac
    metric="$(awk 'BEGIN{m=0}{for(i=1;i<NF;i++)if($i=="metric")m=$(i+1)}END{print m}' <<< "$line")"
    grep -Fxq "$prefix $metric" <<< "$wanted" && continue
    ip -4 route del "$prefix" metric "$metric" table "$ROUTE_TABLE" proto 242 || return 1
  done <<< "$prior_routes"
  [ -n "$own_rule" ] || ip -4 rule add priority "$ROUTE_PRIORITY" fwmark "$ROUTE_MARK/0xffffffff" table "$ROUTE_TABLE" protocol 242 || return 1
  current="$(cat "/proc/sys/net/ipv4/conf/$device/rp_filter")" || return 1
  if [ "$current" = 1 ] || { [ "$current" = 0 ] && [ "$(cat /proc/sys/net/ipv4/conf/all/rp_filter)" = 1 ]; }; then
    [ -f "$LIB/routing/rp_$device" ] || printf '%s\n' "$current" > "$LIB/routing/rp_$device" || return 1
    printf '2\n' > "/proc/sys/net/ipv4/conf/$device/rp_filter" || return 1
  fi
}
prepare_routes() {
  local id proto port host target route ip result
  [ -f "$CONFIG" ] || return 0
  load_resolved
  while read -r id proto port host target route; do
    [ -n "${route:-}" ] || continue
    route_fields "$route" || { echo '无效的地址路由配置' >&2; return 1; }
    if [ -n "$R_IN" ]; then route_prepare_profile "$R_IN" "$R_IDEV" "$R_IGW" || return 1; fi
    [ -n "$R_OUT" ] || continue
    route_prepare_profile "$R_OUT" "$R_ODEV" "$R_OGW" || return 1
    ip="$host"
    valid_ipv4 "$ip" || ip="${RCACHE[$host]:-}"
    valid_ipv4 "$ip" || { echo "缺少下一跳的地址缓存：$host" >&2; return 1; }
    route_numbers "$R_OUT,$R_ODEV,$R_OGW" || return 1
    result="$(ip -4 route get "$ip" from "$R_OUT" mark "$ROUTE_MARK")" || return 1
    awk -v d="$R_ODEV" '{for(i=1;i<NF;i++)if($i=="dev"&&$(i+1)==d)ok=1} END{exit !ok}' <<< "$result" || { echo '所选出口无法路由到下一跳' >&2; return 1; }
  done < "$CONFIG"
}
prune_routes() {
  local keep="${1:-}" path key local_ip device gateway existing
  [ -d "$LIB/routing" ] || return 0
  for path in "$LIB/routing/"[0-9]*; do
    [ -f "$path" ] || continue
    key="$(cat "$path")" || return 1
    grep -Fxq "$key" <<< "$keep" && continue
    route_numbers "$key" || return 1
    [ "${path##*/}" = "$ROUTE_TABLE" ] || return 1
    existing="$(ip -N -4 -o rule show)" || return 1
    if awk -v p="$ROUTE_PRIORITY:" '$1==p' <<< "$existing" | grep -q "lookup $ROUTE_TABLE .*proto 242"; then
      ip -4 rule del priority "$ROUTE_PRIORITY" fwmark "$ROUTE_MARK/0xffffffff" table "$ROUTE_TABLE" protocol 242 || return 1
    fi
    ip -4 route flush table "$ROUTE_TABLE" proto 242 || return 1
    rm -f "$path" || return 1
  done
  for path in "$LIB/routing/"rp_*; do
    [ -f "$path" ] || continue
    device="${path##*/rp_}"
    awk -F, -v d="$device" '$2==d{found=1} END{exit !found}' <<< "$keep" && continue
    [[ "$device" =~ ^[A-Za-z0-9_.:-]{1,15}$ ]] || return 1
    if [ "$(cat "/proc/sys/net/ipv4/conf/$device/rp_filter" 2>/dev/null)" = 2 ]; then
      cat "$path" > "/proc/sys/net/ipv4/conf/$device/rp_filter" || return 1
    fi
    rm -f "$path" || return 1
  done
}
current_route_profiles() {
  local id proto port host target route
  [ -f "$CONFIG" ] || return 0
  while read -r id proto port host target route; do
    [ -n "${route:-}" ] || continue
    route_fields "$route" || return 1
    if [ -n "$R_IN" ]; then printf '%s,%s,%s\n' "$R_IN" "$R_IDEV" "$R_IGW"; fi
    if [ -n "$R_OUT" ]; then printf '%s,%s,%s\n' "$R_OUT" "$R_ODEV" "$R_OGW"; fi
  done < "$CONFIG"
}

# 服务端下发的配置：首行 v=版本，其余每行 规则id 协议 入站端口 目标地址 目标端口
valid_config() {
  local line first=1 route
  local re='^[0-9]+ (both|tcp|udp) ([1-9][0-9]{0,4}) [A-Za-z0-9.-]{1,253} ([1-9][0-9]{0,4})( [A-Za-z0-9_.,:-]+)?$'
  while IFS= read -r line; do
    if [ "$first" -eq 1 ]; then
      [[ "$line" =~ ^v=[0-9a-f]{1,64}$ ]] || return 1
      first=0
      continue
    fi
    [ -z "$line" ] && continue
    [[ "$line" =~ $re ]] || return 1
    [ "${BASH_REMATCH[2]}" -le 65535 ] && [ "${BASH_REMATCH[3]}" -le 65535 ] || return 1
    route="${BASH_REMATCH[4]:-}"
    [ -z "$route" ] || route_fields "${route# }" || return 1
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

# Only inspect the peer and the kernel; never execute or source another manager's files.
IPT_CONFIG="/root/iptables-forward-conf/managed-forwards.conf"
LEGACY_PERSIST="/etc/iptables/rules.v4"
is_port_error() { [[ "$1" == '端口冲突：'* || "$1" == '端口检查失败：'* ]]; }

check_peer_ports() {
  local line modern=0 first=1 incoming proto target ip key content
  local -a fields
  [ -e "$IPT_CONFIG" ] || [ -L "$IPT_CONFIG" ] || return 0
  [ -f "$IPT_CONFIG" ] && [ -r "$IPT_CONFIG" ] || { echo '端口检查失败：无法读取单机脚本配置'; return 1; }
  content="$(cat "$IPT_CONFIG")" || { echo '端口检查失败：无法读取单机脚本配置'; return 1; }
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$first" = 1 ] && [ "$line" = '# ipt-forward-manager v3' ]; then modern=1; first=0; continue; fi
    first=0; line="${line%%#*}"; read -r -a fields <<< "$line"
    [ "${#fields[@]}" -gt 0 ] || continue
    if [ "$modern" = 1 ]; then
      [ "${#fields[@]}" = 7 ] && [[ "${fields[0]}" =~ ^[0-9a-f]{32}$ ]] && valid_ipv4 "${fields[6]}" || {
        echo '端口检查失败：单机脚本配置格式无效'; return 1;
      }
      incoming="${fields[1]}" target="${fields[3]}" proto="${fields[4]}"
    else
      [ "${#fields[@]}" = 5 ] || { echo '端口检查失败：单机脚本配置格式无效'; return 1; }
      incoming="${fields[0]}" target="${fields[2]}" proto="${fields[3]}"
    fi
    [[ "$incoming" =~ ^[1-9][0-9]{0,4}$ && "$target" =~ ^[1-9][0-9]{0,4}$ ]] &&
      [ "$incoming" -le 65535 ] && [ "$target" -le 65535 ] && [[ "$proto" =~ ^(tcp|udp|both)$ ]] || {
      echo '端口检查失败：单机脚本配置端口或协议无效'; return 1;
    }
    for ip in $(protos "$proto"); do
      key="$ip:$incoming"
      if [ -n "${requested[$key]:-}" ]; then echo "端口冲突：${ip^^} $incoming 已被单机脚本保存的配置占用"; return 1; fi
    done
  done <<< "$content"
}

# Inspect ingress translation actions, not conntrack status matches such as
# "ct status dnat" in ipt.sh FORWARD accept rules. No Python dependency here.
check_native_ports() {
  command -v nft >/dev/null 2>&1 || return 0
  local listing key proto port result
  listing="$(nft -n list ruleset 2>/dev/null)" || { echo '端口检查失败：无法读取原生 nftables'; return 1; }
  for key in "${!requested[@]}"; do
    proto="${key%:*}" port="${key#*:}"
    result="$(awk -v p="$proto" -v port="$port" '
      function possible(line,    n,a,i,m,r,hit) {
        if (line ~ /tcp dport/ && p!="tcp") return 0
        if (line ~ /udp dport/ && p!="udp") return 0
        n=split(line,a,/[ \t]+/)
        for(i=1;i<n;i++) {
          if ((a[i]=="l4proto" || (a[i]=="protocol" && a[i-1]=="ip")) && a[i+1] ~ /^(tcp|udp|6|17)$/) {
            if (a[i+1]!=p && a[i+1]!=(p=="tcp"?"6":"17")) return 0
          }
          if(a[i]=="dport" && a[i+1] ~ /^[0-9]+(-[0-9]+)?$/) {
            m=split(a[i+1],r,"-"); hit=(port>=r[1] && port<=(m==1?r[1]:r[2]))
            if (!hit) return 0
          }
        }
        return 1
      }
      function visit(k,    i,line,n,a,j,target) {
        if (visited[k] || own[k]) return 0
        visited[k]=1
        for(i=1;i<=count[k];i++) {
          line=rules[k,i]
          if (!possible(line)) continue
          if (line ~ /(^|[ \t])(dnat|tproxy)([ \t]+ip6?)?[ \t]+to([ \t]|$)/ ||
              line ~ /(^|[ \t])(redirect|vmap)([ \t;]|$)/) {
            print "端口冲突：" p " " port " 与原生 nft " label[k] " 规则可能重叠"; return 1
          }
          n=split(line,a,/[ \t]+/)
          for(j=1;j<=n;j++) if(a[j]=="jump" || a[j]=="goto") {
            target=a[j+1]; gsub(/[;]$/, "", target)
            if (!((scope[k] SUBSEP target) in label)) {
              print "端口检查失败：无法解析 nft " label[k] " 的跳转目标"; return 1
            }
            if (visit(scope[k] SUBSEP target)) return 1
          }
        }
        return 0
      }
      /^[ \t]*table / { family=$2; table=$3; chain=""; next }
      family!="ip" && family!="inet" { next }
      /^[ \t]*chain / {
        chain=$2; k=family SUBSEP table SUBSEP chain
        scope[k]=family SUBSEP table; label[k]=table "/" chain
        own[k]=(chain ~ /^ICTL-(PRE|POST|FWD|ROUTE)$/); next
      }
      /^[ \t]*}[ \t]*$/ { chain=""; next }
      chain!="" {
        line=$0; gsub(/"([^"\\]|\\.)*"/,"",line)
        if (line ~ /hook prerouting([ \t;]|$)/) { starts[k]=1; next }
        rules[k,++count[k]]=line
      }
      END {
        for (k in starts) if (visit(k)) exit
      }
    ' <<< "$listing")" || { echo '端口检查失败：无法解析原生 nftables'; return 1; }
    [ -z "$result" ] || { echo "$result"; return 1; }
  done
}

# Receives a validated wire configuration; caller holds /run/ictl.lock throughout the apply.
check_port_conflicts() {
  local config="$1" id proto port host target p key line address ip d error rc
  local -a columns
  local -A requested=()
  while read -r id proto port host target; do
    [[ "$id" != v=* ]] && [ -n "$id" ] || continue
    for p in $(protos "$proto"); do requested["$p:$port"]=1; done
  done <<< "$config"
  [ "${#requested[@]}" -gt 0 ] || return 0
  if [ "${ICTL_WAKE_CHECK:-0}" != 1 ] && [ -f "$CONF_DIR/wake.conf" ] && grep -qx 'ENABLED=1' "$CONF_DIR/wake.conf"; then
    local wake_port
    wake_port="$(sed -n 's/^PORT=//p' "$CONF_DIR/wake.conf")"
    if [[ ! "$wake_port" =~ ^[0-9]+$ ]]; then ERRS+=('端口检查失败：远程唤醒配置损坏'); return 1; fi
    if [ "${requested[udp:$wake_port]:-0}" = 1 ]; then ERRS+=("端口冲突：UDP $wake_port 已预留为远程唤醒端口"); return 1; fi
  fi
  if ! error="$(check_peer_ports)"; then ERRS+=("$error"); return 1; fi
  if ! error="$(check_native_ports)"; then ERRS+=("$error"); return 1; fi
  d="$(mktemp -d "$LIB/ports.XXXXXX")" || { ERRS+=('端口检查失败：无法创建临时文件'); return 1; }
  for key in "${!requested[@]}"; do printf '%s\n' "${key/:/ }"; done > "$d/wanted"
  if ! iptables-save > "$d/rules" 2> "$d/error"; then
    rm -rf "$d"; ERRS+=('端口检查失败：无法读取防火墙规则'); return 1
  fi
  error="$(awk '
    function hasport(list,port,    n,a,b,i,m,lo,hi) {
      n=split(list,a,",")
      for(i=1;i<=n;i++) {
        m=split(a[i],b,":")
        if(m>2 || a[i] ~ /[^0-9:]/) return -1
        lo=(b[1]==""?0:b[1]+0); hi=(m==1?lo:(b[2]==""?65535:b[2]+0))
        if(port>=lo && port<=hi) return 1
      }
      return 0
    }
    function matches(r,proto,port,    yes,m) {
      if(protocol[r]!="") {
        yes=(protocol[r]==proto || protocol[r]==(proto=="tcp"?"6":"17") || protocol[r]=="all" || protocol[r]=="0")
        if(yes==negproto[r]) return 0
      }
      if(ports[r]!="") {
        m=hasport(ports[r],port)
        if(m>=0 && m==negport[r]) return 0
      }
      return 1
    }
    function visit(table,chain,proto,port,    k,i,r,j) {
      k=table SUBSEP chain
      if(visited[k] || chain=="ICTL-PRE" || chain=="ICTL-POST" || chain=="ICTL-FWD") return 0
      visited[k]=1
      for(i=1;i<=count[k];i++) {
        r=rules[k,i]; if(!matches(r,proto,port)) continue
        j=jump[r]
        if(j=="DNAT" || j=="REDIRECT" || j=="TPROXY") {
          print "端口冲突：" toupper(proto) " " port " 已被 " table "/" chain " 转发规则占用"; return 1
        }
        if(j!="" && visit(table,j,proto,port)) return 1
      }
      return 0
    }
    FILENAME==ARGV[1] { want[++w]=$1; number[w]=$2; next }
    /^\*/ { table=substr($0,2); next }
    /^-A / {
      if(table!="nat" && table!="mangle") next
      line=$0; gsub(/"([^"\\]|\\.)*"/,"COMMENT",line); n=split(line,a,/ +/)
      r=++total; k=table SUBSEP a[2]; rules[k,++count[k]]=r
      for(i=3;i<n;i++) {
        value=a[i+1]; neg=(a[i-1]=="!")
        if(value=="!") { value=a[i+2]; neg=1 }
        if(a[i]=="-p" || a[i]=="--protocol") { protocol[r]=value; negproto[r]=neg }
        if(a[i]=="--dport" || a[i]=="--destination-port" || a[i]=="--dports" || a[i]=="--destination-ports") { ports[r]=value; negport[r]=neg }
        if(a[i]=="-j" || a[i]=="--jump" || a[i]=="-g" || a[i]=="--goto") jump[r]=value
      }
    }
    END {
      for(q=1;q<=w;q++) {
        for(k in visited) delete visited[k]
        if(visit("nat","PREROUTING",want[q],number[q]) || visit("mangle","PREROUTING",want[q],number[q])) exit 1
      }
    }
  ' "$d/wanted" "$d/rules")"; rc=$?
  if [ "$rc" -ne 0 ]; then rm -rf "$d"; ERRS+=("${error:-端口检查失败：无法解析防火墙规则}"); return 1; fi
  for proto in tcp udp; do
    if [ "$proto" = tcp ]; then p=-lnt; else p=-aun; fi
    if ! ss -H "$p" > "$d/sockets" 2> "$d/error"; then
      rm -rf "$d"; ERRS+=('端口检查失败：无法读取本机端口，请检查 ss（iproute2）'); return 1
    fi
    while read -r -a columns; do
      [ "${#columns[@]}" -gt 0 ] || continue
      p=3; [[ "${columns[0]}" != tcp && "${columns[0]}" != udp ]] || p=4
      if [ "${#columns[@]}" -le "$p" ]; then error='端口检查失败：本机端口输出格式无效'; break; fi
      address="${columns[$p]}"; port="${address##*:}"; ip="${address%:*}"
      ip="${ip#\[}"; ip="${ip%\]}"; ip="${ip%%%*}"
      [[ "${ICTL_WAKE_IPV6:-0}" = 1 || "$ip" != *:* || "$ip" = :: || "$ip" = ::ffff:* ]] || continue
      if [[ ! "$port" =~ ^[0-9]+$ ]]; then error='端口检查失败：本机端口输出格式无效'; break; fi
      if [ -n "${requested[$proto:$port]:-}" ]; then error="端口冲突：${proto^^} $port 已被本机服务占用"; break; fi
    done < "$d/sockets"
    [ -z "$error" ] || break
  done
  rm -rf "$d"
  if [ -n "$error" ]; then ERRS+=("$error"); return 1; fi
}

# Earlier standalone releases saved the entire firewall. Remove only our own entries
# from that legacy boot snapshot; the live firewall and unrelated entries stay intact.
clean_legacy_persistence() {
  local tmp rc=0
  [ -e "$LEGACY_PERSIST" ] || return 0
  [ -f "$LEGACY_PERSIST" ] || return 1
  grep -Eq '(^:|^-A )(ICTL-PRE|ICTL-POST|ICTL-FWD|ICTL-ROUTE) |-[jg] ICTL-(PRE|POST|FWD|ROUTE)( |$)' "$LEGACY_PERSIST" || rc=$?
  [ "$rc" -ne 1 ] || return 0
  [ "$rc" -eq 0 ] || return 1
  tmp="$(mktemp "${LEGACY_PERSIST}.XXXXXX")" || return 1
  if ! cp -p "$LEGACY_PERSIST" "$tmp" || ! awk '
    { line=$0; gsub(/"([^"\\]|\\.)*"/,"COMMENT",line)
      if(line !~ /(^:|^-A )(ICTL-PRE|ICTL-POST|ICTL-FWD|ICTL-ROUTE) / && line !~ /-[jg] ICTL-(PRE|POST|FWD|ROUTE)( |$)/) print
    }' "$LEGACY_PERSIST" > "$tmp"; then rm -f "$tmp"; return 1; fi
  if cmp -s "$tmp" "$LEGACY_PERSIST"; then rm -f "$tmp"; return 0; fi
  if [ -L "$LEGACY_PERSIST" ] || ! mkdir -p "$LIB"; then rm -f "$tmp"; return 1; fi
  if [ ! -f "$LIB/pre-coexist-rules.v4.bak" ] && ! cp -p "$LEGACY_PERSIST" "$LIB/pre-coexist-rules.v4.bak"; then
    rm -f "$tmp"; return 1
  fi
  mv "$tmp" "$LEGACY_PERSIST" || { rm -f "$tmp"; return 1; }
}

# ---------------------------------------------------------------- iptables

# 根据本地缓存的配置生成 iptables-restore 输入，写入 RENDERED；问题追加到 ERRS
render() {
  local id proto lport host tport route p pre="" post="" fwd="" mangle="" in_match snat out_mark in_mark next_ip listen_ip local_addresses
  local -a incoming
  load_resolved
  if [ -f "$CONFIG" ]; then
    while read -r id proto lport host tport route; do
      [ -z "$tport" ] && continue
      if ! resolve "$host"; then
        ERRS+=("rule $id resolve failed: $host")
        continue
      fi
      [ "$RSTALE" -eq 1 ] && ERRS+=("rule $id resolve failed: $host, using cached $RIP")
      incoming=('-m addrtype --dst-type LOCAL'); snat="MASQUERADE"; in_mark=""; out_mark=""
      if [ -n "$route" ]; then
        route_fields "$route" || { ERRS+=('无效的地址路由配置'); continue; }
        if [ -n "$R_LISTEN" ]; then
          next_ip="$RIP"
          if ! resolve "$R_LISTEN"; then ERRS+=("rule $id receive address resolve failed: $R_LISTEN"); continue; fi
          [ "$RSTALE" -eq 1 ] && ERRS+=("rule $id receive address using cache: $R_LISTEN")
          listen_ip="$RIP"; RIP="$next_ip"
          if ! local_addresses="$(ip -o -4 address show)"; then ERRS+=('无法读取本机地址'); continue; fi
          incoming=("-d $listen_ip")
          if ! awk -v ip="$listen_ip" '{split($4,a,"/");if(a[1]==ip)found=1} END{exit !found}' <<< "$local_addresses"; then
            incoming+=('-m addrtype --dst-type LOCAL')
          fi
        else
          incoming=("-m addrtype --dst-type LOCAL -d $R_IN")
          route_numbers "$R_IN,$R_IDEV,$R_IGW"; in_mark="$ROUTE_MARK"
        fi
        if [ -n "$R_OUT" ]; then
          snat="SNAT --to-source $R_OUT"
          route_numbers "$R_OUT,$R_ODEV,$R_OGW"; out_mark="$ROUTE_MARK"
        fi
      fi
      for p in $(protos "$proto"); do
        for in_match in "${incoming[@]}"; do
          if [ -n "$out_mark" ]; then
            mangle+="-A ICTL-ROUTE -p $p $in_match --dport $lport -j MARK --set-xmark $out_mark/0xffffffff"$'\n'
          fi
          pre+="-A ICTL-PRE -p $p $in_match --dport $lport -j DNAT --to-destination $RIP:$tport"$'\n'
        done
        if [ -n "$in_mark" ]; then
          mangle+="-A ICTL-ROUTE -m conntrack --ctstate DNAT --ctdir REPLY --ctproto $p --ctorigdst $R_IN --ctorigdstport $lport --ctreplsrc $RIP --ctreplsrcport $tport -j MARK --set-xmark $in_mark/0xffffffff"$'\n'
        fi
        post+="-A ICTL-POST -p $p -d $RIP --dport $tport -m conntrack --ctstate DNAT --ctorigdstport $lport -j $snat"$'\n'
        # 原始/回复元组在两个方向保持不变；不用 -p，RELATED ICMP 也按原连接匹配。
        fwd+="-A ICTL-FWD -m conntrack --ctstate DNAT --ctproto $p --ctorigdstport $lport --ctreplsrc $RIP --ctreplsrcport $tport -j ACCEPT"$'\n'
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
${fwd}COMMIT
*mangle
:ICTL-ROUTE - [0:0]
${mangle}COMMIT
"
}

jumps_ok() {
  iptables -t nat -C PREROUTING -j ICTL-PRE 2>/dev/null &&
    iptables -t nat -C POSTROUTING -j ICTL-POST 2>/dev/null &&
    iptables -C FORWARD -j ICTL-FWD 2>/dev/null &&
    iptables -t mangle -C PREROUTING -j ICTL-ROUTE 2>/dev/null
}

ensure_jumps() {
  iptables -w -t nat -C PREROUTING -j ICTL-PRE 2>/dev/null || iptables -w -t nat -A PREROUTING -j ICTL-PRE || return 1
  iptables -w -t nat -C POSTROUTING -j ICTL-POST 2>/dev/null || iptables -w -t nat -A POSTROUTING -j ICTL-POST || return 1
  iptables -w -C FORWARD -j ICTL-FWD 2>/dev/null || iptables -w -I FORWARD 1 -j ICTL-FWD || return 1
  iptables -w -t mangle -C PREROUTING -j ICTL-ROUTE 2>/dev/null || iptables -w -t mangle -A PREROUTING -j ICTL-ROUTE || return 1
}

ensure_ip_forward() {
  [ -f "$SYSCTL" ] || echo "net.ipv4.ip_forward=1" > "$SYSCTL"
  [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" = "1" ] && return 0
  echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || ERRS+=("enable ip_forward failed")
}

# Capture only our chains; counters and generated comments do not affect comparison.
owned_snapshot() {
  local table rules
  for table in nat filter mangle; do
    rules="$(iptables-save -t "$table")" || return 1
    printf '*%s\n' "$table"
    printf '%s\n' "$rules" | awk '/^:ICTL-(PRE|POST|FWD|ROUTE) / {sub(/\[[0-9]+:[0-9]+\]/,"[0:0]"); print}
      /^-A ICTL-(PRE|POST|FWD|ROUTE) / {print}'
    echo COMMIT
  done
}

jump_flags() {
  local table parent chain item
  for item in 'nat PREROUTING ICTL-PRE' 'nat POSTROUTING ICTL-POST' 'filter FORWARD ICTL-FWD' 'mangle PREROUTING ICTL-ROUTE'; do
    read -r table parent chain <<< "$item"
    if iptables -t "$table" -C "$parent" -j "$chain" 2>/dev/null; then echo 1; else echo 0; fi
  done
}

restore_owned() {
  local snapshot="$1" flags="$2" table parent chain flag i=0 item
  # Complete a partial deletion before restored rules can create new connections.
  if [ -f "$LIB/clear-owned.rules" ]; then
    clear_changed_connections "$(cat "$LIB/clear-owned.rules")" '' || return 1
    rm -f "$LIB/clear-owned.rules" || return 1
  fi
  # Empty owned chains absent from the original snapshot before removing them.
  local input="$snapshot"
  [[ "$input" == *':ICTL-PRE '* ]] || input="${input/\*nat/*nat$'\n':ICTL-PRE - [0:0]}"
  [[ "$input" == *':ICTL-POST '* ]] || input="${input/\*nat/*nat$'\n':ICTL-POST - [0:0]}"
  [[ "$input" == *':ICTL-FWD '* ]] || input="${input/\*filter/*filter$'\n':ICTL-FWD - [0:0]}"
  [[ "$input" == *'*mangle'* ]] || input+=$'\n*mangle\n:ICTL-ROUTE - [0:0]\nCOMMIT\n'
  [[ "$input" == *':ICTL-ROUTE '* ]] || input="${input/\*mangle/*mangle$'\n':ICTL-ROUTE - [0:0]}"
  printf '%s\n' "$input" | iptables-restore -w --noflush || return 1
  for item in 'nat PREROUTING ICTL-PRE' 'nat POSTROUTING ICTL-POST' 'filter FORWARD ICTL-FWD' 'mangle PREROUTING ICTL-ROUTE'; do
    read -r table parent chain <<< "$item"
    i=$((i+1)); flag="$(sed -n "${i}p" <<< "$flags")"
    if [ "$flag" = 0 ]; then
      while iptables -t "$table" -C "$parent" -j "$chain" 2>/dev/null; do
        iptables -w -t "$table" -D "$parent" -j "$chain" || return 1
      done
    elif [ "$flag" = 1 ]; then
      iptables -w -t "$table" -C "$parent" -j "$chain" 2>/dev/null ||
        iptables -w -t "$table" -I "$parent" 1 -j "$chain" || return 1
    fi
    if [[ "$snapshot" != *":$chain "* ]]; then
      iptables -w -t "$table" -F "$chain" && iptables -w -t "$table" -X "$chain" || return 1
    fi
  done
}

restore_cached_owned() {
  local snapshot="$1"
  if [[ "$snapshot" != *'*mangle'* ]]; then
    snapshot+=$'\n*mangle\n:ICTL-ROUTE - [0:0]\nCOMMIT\n'
  elif [[ "$snapshot" != *':ICTL-ROUTE '* ]]; then
    snapshot="${snapshot/\*mangle/*mangle$'\n':ICTL-ROUTE - [0:0]}"
  fi
  prepare_routes && restore_owned "$snapshot" $'1\n1\n1\n1' && jumps_ok && prune_routes "$(current_route_profiles)"
}

rules_match() {
  local input="$1" actual="$2" line table="" expected=0 count=0
  local -a args
  while IFS= read -r line; do
    case "$line" in
      '*nat') table=nat ;;
      '*filter') table=filter ;;
      '*mangle') table=mangle ;;
      '-A ICTL-'*)
        read -r -a args <<< "$line"
        args[0]=-C
        iptables -w -t "$table" "${args[@]}" 2>/dev/null || return 1
        expected=$((expected+1)) ;;
    esac
  done <<< "$input"
  count="$(grep -c '^-A ICTL-' <<< "$actual")"
  [ "$expected" -eq "$count" ]
}

clear_changed_connections() {
  local previous="$1" current="$2" line proto port ip target out old_post new_post
  while IFS= read -r line; do
    [[ "$line" = '-A ICTL-PRE '* ]] || continue
    if [[ "$line" =~ -p[[:space:]](tcp|udp).*--dport[[:space:]]([0-9]+).*--to-destination[[:space:]]([0-9.]+):([0-9]+) ]]; then
      proto="${BASH_REMATCH[1]}" port="${BASH_REMATCH[2]}" ip="${BASH_REMATCH[3]}" target="${BASH_REMATCH[4]}"
      old_post="$(grep -E "^-A ICTL-POST -p $proto .*--ctorigdstport $port( |$)" <<< "$previous")"
      new_post="$(grep -E "^-A ICTL-POST -p $proto .*--ctorigdstport $port( |$)" <<< "$current")"
      if grep -Fxq -- "$line" <<< "$current" && [ "$old_post" = "$new_post" ]; then continue; fi
      command -v conntrack >/dev/null || { ERRS+=('切换规则需要 conntrack，请安装 conntrack 后重试'); return 1; }
      if ! out="$(conntrack -D -f ipv4 -p "$proto" --orig-port-dst "$port" --reply-src "$ip" --reply-port-src "$target" --dst-nat 2>&1)"; then
        [[ "$out" = *'0 flow entries'* ]] || { ERRS+=("清理旧连接失败：$out"); return 1; }
      fi
    fi
  done <<< "$previous"
}

apply_rules() {
  local out previous flags actual
  if [ -f "$LIB/clear-owned.rules" ]; then
    remove_rules || { ERRS+=('上次连接清理尚未完成'); return 1; }
  fi
  render
  [ "${#ERRS[@]}" -eq 0 ] || return 1
  check_port_conflicts "$(cat "$CONFIG" 2>/dev/null)" || return 1
  ensure_ip_forward
  if ! out="$(prepare_routes 2>&1)"; then ERRS+=("$out"); return 1; fi
  [ "${#ERRS[@]}" -eq 0 ] || return 1
  previous="$(owned_snapshot)" || { ERRS+=("read iptables failed"); return 1; }
  flags="$(jump_flags)"
  if [ -f "$APPLIED" ] && printf '%s' "$RENDERED" | cmp -s - "$APPLIED" &&
      [ -f "$KERNEL" ] && [ "$(cat "$KERNEL")" = "$previous" ] && jumps_ok; then
    prune_routes "$(current_route_profiles)" || { ERRS+=("清理旧路由失败"); return 1; }
    return 0
  fi
  if ! out="$(printf '%s' "$RENDERED" | iptables-restore -w --noflush 2>&1)" || ! out="$(ensure_jumps 2>&1)"; then
    ERRS+=("apply failed: ${out%%$'\n'*}")
    restore_owned "$previous" "$flags" || ERRS+=("rollback failed")
    return 1
  fi
  if ! actual="$(owned_snapshot)" || ! rules_match "$RENDERED" "$actual"; then
    ERRS+=("verify rules failed")
    restore_owned "$previous" "$flags" || ERRS+=("rollback failed")
    return 1
  fi
  if ! clear_changed_connections "$previous" "$actual"; then
    restore_owned "$previous" "$flags" || ERRS+=("rollback failed")
    return 1
  fi
  if ! printf '%s' "$RENDERED" > "$APPLIED.tmp" || ! printf '%s\n' "$actual" > "$KERNEL.tmp" ||
      ! mv "$APPLIED.tmp" "$APPLIED" || ! mv "$KERNEL.tmp" "$KERNEL"; then
    ERRS+=("save applied rules failed")
    restore_owned "$previous" "$flags" || ERRS+=("rollback failed")
    return 1
  fi
  prune_routes "$(current_route_profiles)" || { ERRS+=("清理旧路由失败"); return 1; }
}

remove_chain() {
  local table="$1" parent="$2" chain="$3" rules out
  while true; do
    # 成功列出整张表才能判断规则/链不存在，不能把 -D/-F 的所有失败当作不存在。
    if ! rules="$(iptables -w -t "$table" -S 2>&1)"; then
      err "无法读取 $table 规则：$rules"
      return 1
    fi
    grep -Fxq -- "-A $parent -j $chain" <<< "$rules" || break
    if ! out="$(iptables -w -t "$table" -D "$parent" -j "$chain" 2>&1)"; then
      err "删除 $parent 到 $chain 的跳转失败：$out"
      return 1
    fi
  done
  grep -Fxq -- "-N $chain" <<< "$rules" || return 0
  if ! out="$(iptables -w -t "$table" -F "$chain" 2>&1)" ||
    ! out="$(iptables -w -t "$table" -X "$chain" 2>&1)"; then
    err "删除链 $chain 失败：$out"
    return 1
  fi
}

remove_rules() {
  local current saved="$LIB/clear-owned.rules" pending
  current="$(owned_snapshot)" || return 1
  pending="$(cat "$saved" 2>/dev/null || true)"
  # Persist exact destinations so retries also work after the chains are gone.
  mkdir -p "$LIB" || return 1
  printf '%s\n%s\n' "$pending" "$current" | awk '!seen[$0]++' > "$saved.tmp" || return 1
  chmod 600 "$saved.tmp" && sync -f "$saved.tmp" && mv "$saved.tmp" "$saved" && sync -f "$LIB" || return 1
  remove_chain nat PREROUTING ICTL-PRE || return 1
  remove_chain nat POSTROUTING ICTL-POST || return 1
  remove_chain filter FORWARD ICTL-FWD || return 1
  remove_chain mangle PREROUTING ICTL-ROUTE || return 1
  clear_changed_connections "$(cat "$saved")" '' || { printf '%s\n' "${ERRS[@]}" >&2; return 1; }
  prune_routes || return 1
  rm -f "$saved"
}

# ---------------------------------------------------------------- 同步

# 本地应用并更新上报状态；CFG_ERR 为配置被拒绝的原因
CFG_ERR=""
local_apply() {
  local e
  ERRS=()
  [ -n "$CFG_ERR" ] && ERRS+=("$CFG_ERR")
  mkdir -p "$LIB"
  [ "${#ERRS[@]}" -ne 0 ] || apply_rules
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

# The selected mode survives Agent replacements. A pause never acknowledges a change.
MODE="passive" R_GEN=0 CONFIRM="" SOURCE="auto" OP="" PHASE="poll"
MODE_FILE="$LIB/mode"
ACK_FILE="$LIB/ack"
KERNEL="$LIB/kernel"
load_mode() {
  MODE="passive"
  [ ! -f "$MODE_FILE" ] || read -r MODE < "$MODE_FILE"
  [[ "$MODE" = active || "$MODE" = passive ]] || { err "本地模式文件无效"; return 1; }
}
save_mode() {
  printf '%s\n' "$MODE" > "$MODE_FILE.tmp" && mv "$MODE_FILE.tmp" "$MODE_FILE"
}

network_inventory() {
  local addresses
  addresses="$(ip -o -4 address show 2>/dev/null)" || return 0
  awk '$3 == "inet" {dev=$2; sub(/@.*/, "", dev); sub(/:$/, "", dev);
    if (dev == "lo" || dev !~ /^[A-Za-z0-9_.:-]+$/ || length(dev)>15) next;
    count++; if(count<=96) {if(rows!="") rows=rows ";"; rows=rows dev "," $4}}
    END {printf "network_addresses=%s\nnetwork_complete=%d\n", rows, count<=96}' <<< "$addresses"
}

request_text() {
  printf 'wake_id=%s\n' "${WAKE_ID:-}"
  printf 'agent_kind=passive\nt=%s\nv=%s\nstate=%s\nerr=%s\nagent=%s\nau=%s\nproto=2\nsource=%s\nphase=%s\nop=%s\ngeneration=%s\nmode=%s\n' \
    "$(date +%s)" "$R_V" "$1" "$R_ERR" "$VERSION" "$AUTO_UPDATE" "$SOURCE" "$PHASE" "$OP" "$R_GEN" "$MODE"
  network_inventory
}

response_field() { sed -n "s/^$1=//p" <<< "$RESPONSE"; }
RESPONSE="" NEW=0
exchange() {
  local file code
  file="$(mktemp /run/ictl.XXXXXX)" || return 1
  code="$(send_request "$file")"
  RESPONSE="$(cat "$file")"; rm -f "$file"
  case "$code" in
    200) NET="ok"; SKEW="" ;;
    404) NET="auth"; return 1 ;;
    bad) NET="bad"; return 1 ;;
    *) NET="conn"; return 1 ;;
  esac
  if [[ "$RESPONSE" =~ ^time=([0-9]+)$ ]]; then
    NET="skew" SKEW=$(($(date +%s)-BASH_REMATCH[1])); return 1
  fi
  [ "$(response_field proto)" = 2 ] || { NET="bad"; return 1; }
}

# Store the full immutable acknowledgment for bounded, idempotent retries.
acknowledge() {
  local attempt result request
  [ -f "$ACK_FILE" ] || return 0
  CONFIRM="unknown"
  request="$(mktemp /run/ictl.XXXXXX)" || return 1
  for attempt in 1 2; do
    { printf 't=%s\n' "$(date +%s)"; grep -v '^t=' "$ACK_FILE"; } > "$request"
    if exchange < "$request"; then
      if [ "$(response_field action)" = pause ]; then
        MODE=passive CONFIRM=waiting
        save_mode || { rm -f "$request"; return 1; }
        rm -f "$ACK_FILE" "$request"
        return 1
      fi
      result="$(response_field result)"
      case "$result" in
        confirmed|deleted|changed|failed|expired)
          CONFIRM="$result"; rm -f "$ACK_FILE" "$request"
          if [ "$result" = confirmed ] || [ "$result" = deleted ]; then BLOCK_GEN="" BLOCK_ERR=""; fi
          if [ "$result" = deleted ]; then MODE=passive; save_mode || return 1; fi
          [ "$result" = confirmed ] || [ "$result" = deleted ]; return $? ;;
      esac
    fi
  done
  rm -f "$request"
  return 1
}

# Apply a snapshot transactionally, preserving the last confirmed local configuration.
apply_snapshot() {
  local config="$1" generation="$2" mode="$3" dir old_mode="$MODE" old_gen="$R_GEN" old_v="$R_V" name snapshot flags
  ERRS=()
  if ! check_port_conflicts "$config"; then
    R_STATE=error R_ERR="${ERRS[0]}"
    return 1
  fi
  dir="$(mktemp -d "$LIB/apply.XXXXXX")" || return 1
  for name in config applied kernel resolved state mode; do
    [ ! -f "$LIB/$name" ] || cp "$LIB/$name" "$dir/$name" || { rm -rf "$dir"; return 1; }
  done
  snapshot="$(owned_snapshot)" && flags="$(jump_flags)" || { rm -rf "$dir"; return 1; }
  if printf '%s\n' "$config" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"; then
    CFG_ERR=""; local_apply
  else
    R_STATE=error R_ERR="save config failed"
  fi
  if [ "$R_STATE" = ok ]; then
    MODE="$mode" R_GEN="$generation" R_V="${config%%$'\n'*}"; R_V="${R_V#v=}"
    if ! save_mode; then
      R_STATE=error R_ERR="save mode/service policy failed"

    fi
  fi
  if [ "$R_STATE" != ok ]; then
    for name in config applied kernel resolved state mode; do
      if [ -f "$dir/$name" ]; then
        cp "$dir/$name" "$LIB/$name.tmp" && mv "$LIB/$name.tmp" "$LIB/$name" || R_ERR+="; restore $name failed"
      else
        rm -f "$LIB/$name" || R_ERR+="; restore $name failed"
      fi
    done
    prepare_routes && restore_owned "$snapshot" "$flags" && prune_routes "$(current_route_profiles)" || R_ERR+="; rollback failed"
    MODE="$old_mode" R_GEN="$old_gen" R_V="$old_v"
  fi
  rm -rf "$dir"
  [ "$R_STATE" = ok ]
}

cycle() {
  local request action config generation mode hash original_mode original_gen original_v
  mkdir -p "$LIB" || return 1
  load_state; load_mode || return 1
  NEW=0 CFG_ERR=""
  load_conf || { NET=noconf; return 1; }
  derive_keys
  # Retry a lost final receipt before requesting another snapshot.
  if [ -f "$ACK_FILE" ]; then
    acknowledge
    if [ -f "$ACK_FILE" ]; then save_state; return 1; fi
    if [ "$CONFIRM" = deleted ]; then save_state; return 0; fi
    if [ "${1:-auto}" = auto ] && [ "$MODE" = passive ]; then save_state; return 0; fi
  fi
  SOURCE="${1:-auto}" PHASE=poll; [[ "$SOURCE" != manual && "$SOURCE" != wake ]] || PHASE=begin
  OP="$(openssl rand -hex 16)" || return 1
  request="$(mktemp /run/ictl.XXXXXX)" || return 1
  request_text "$R_STATE" > "$request"
  if ! exchange < "$request"; then rm -f "$request"; save_state; return 1; fi
  rm -f "$request"
  action="$(response_field action)"
  if [ "$action" = switch ]; then err "请使用 sudo ictl --sync 安装主动 Agent"; return 22; fi
  if [ "$action" = pause ]; then
    MODE=passive CONFIRM=waiting
    save_mode && save_state || return 1
    return 0
  fi
  [ "$action" != busy ] || return 0
  if [ "$action" = blocked ]; then
    R_STATE=error R_ERR="$(response_field err)"
    BLOCK_GEN="$(response_field generation)" BLOCK_ERR="$R_ERR"
    save_state; return 1
  fi
  if [ "$action" = same ]; then
    local_apply
    if is_port_error "$R_ERR"; then BLOCK_GEN="$R_GEN" BLOCK_ERR="$R_ERR"; fi
    if [ -n "$BLOCK_GEN" ]; then R_STATE=error R_ERR="$BLOCK_ERR"; fi
    save_state || return 1
    # Report a newly detected runtime conflict in this sync, without waiting for the next poll.
    if is_port_error "$R_ERR" && [ "${2:-}" != report ]; then cycle "$SOURCE" report; return $?; fi
    [ "$R_STATE" = ok ]; return $?
  fi
  if [ "$action" != apply ] || [ "$(response_field op)" != "$OP" ]; then
    NET=bad; save_state; return 1
  fi
  generation="$(response_field generation)" mode="$(response_field mode)" hash="$(response_field v)"
  if [ "$SOURCE" = wake ] && [ "$mode" != passive ]; then NET=bad; save_state; return 1; fi
  config="$(sed -n '/^v=/,$p' <<< "$RESPONSE" | sed '/^update=/d;/^$/d')"
  if [[ ! "$generation" =~ ^[1-9][0-9]{0,14}$ ]] || [[ "$mode" != active && "$mode" != passive ]] || ! valid_config "$config" ||
      [ "$(printf '%s' "$(tail -n +2 <<< "$config")" | sha256_hex | cut -c1-12)" != "$hash" ]; then
    NET=bad; save_state; return 1
  fi
  original_mode="$MODE" original_gen="$R_GEN" original_v="$R_V"
  if [ "$SOURCE" = auto ] && [ "$BLOCK_GEN" = "$generation" ]; then
    R_STATE=error R_ERR="$BLOCK_ERR"
  else
    apply_snapshot "$config" "$generation" "$mode" || { R_STATE=error; R_ERR="${R_ERR:-local apply failed}"; }
  fi
  if is_port_error "$R_ERR"; then BLOCK_GEN="$generation" BLOCK_ERR="$R_ERR"; fi
  # An error ACK identifies the attempted snapshot, while persistent state remains the previous one.
  PHASE=ack MODE="$mode" R_GEN="$generation" R_V="$hash"
  request_text "$R_STATE" > "$ACK_FILE.tmp" && mv "$ACK_FILE.tmp" "$ACK_FILE" || {
    MODE="$original_mode" R_GEN="$original_gen" R_V="$original_v" CONFIRM=unknown; save_state; return 1
  }
  if [ "$R_STATE" != ok ]; then MODE="$original_mode" R_GEN="$original_gen" R_V="$original_v"; fi
  CONFIRM=unknown
  save_state || return 1
  acknowledge; local rc=$?
  save_state || return 1
  return "$rc"
}

daemon() {
  MODE=passive
  with_lock local_apply_saved
}

# 开机时先按本地缓存恢复规则
local_apply_saved() {
  local previous flags actual
  load_state
  load_mode || return 1
  if [ "$MODE" = passive ]; then
    # No DNS lookup or server traffic during passive boot.
    ERRS=()
    if [ -f "$APPLIED" ]; then
      if [ ! -f "$CONFIG" ] || ! valid_config "$(cat "$CONFIG")"; then
        ERRS+=('端口检查失败：缺少有效的 Agent 缓存配置')
      else
        check_port_conflicts "$(cat "$CONFIG")"
      fi
    fi
    if [ "${#ERRS[@]}" -ne 0 ]; then
      R_STATE=error R_ERR="${ERRS[0]}" BLOCK_GEN="$R_GEN" BLOCK_ERR="${ERRS[0]}"
      save_state; return 1
    fi
    ensure_ip_forward
    [ "${#ERRS[@]}" -eq 0 ] || return 1
    prepare_routes || { R_STATE=error R_ERR="恢复路由失败"; save_state; return 1; }
    if [ -f "$APPLIED" ]; then
      previous="$(owned_snapshot)" && flags="$(jump_flags)" || return 1
      if ! iptables-restore -w --noflush < "$APPLIED" || ! ensure_jumps ||
          ! actual="$(owned_snapshot)" || ! rules_match "$(cat "$APPLIED")" "$actual" ||
          ! printf '%s\n' "$actual" > "$KERNEL.tmp" || ! mv "$KERNEL.tmp" "$KERNEL"; then
        R_STATE=error R_ERR='restore cached rules failed'
        restore_owned "$previous" "$flags" || R_ERR+='; rollback failed'
        save_state; return 1
      fi
    fi
    if [ -n "$BLOCK_GEN" ]; then R_STATE=error R_ERR="$BLOCK_ERR"; fi
    save_state; return 0
  fi
  local_apply
  local rc=0
  [ "$R_STATE" = ok ] || rc=1
  if is_port_error "$R_ERR"; then BLOCK_GEN="$R_GEN" BLOCK_ERR="$R_ERR"; fi
  if [ -n "$BLOCK_GEN" ]; then R_STATE=error R_ERR="$BLOCK_ERR"; fi
  save_state
  return "$rc"
}

main() {
  case "${1:-}" in
    --wake-sync)
      need_root || return 1
      WAKE_ID="${2:-}"
      [[ "$WAKE_ID" =~ ^[0-9a-f]{32}$ ]] || return 1
      [ "$(cat "$LIB/agent-kind" 2>/dev/null)" = passive ] || return 1
      [ ! -f "$LIB/switch-work/transaction" ] || return 1
      [ -f "$CONF_DIR/wake.conf" ] && grep -qx 'ENABLED=1' "$CONF_DIR/wake.conf" || return 1
      ACK_FILE="$LIB/wake-ack"
      with_lock cycle wake
      return $? ;;
    --apply-file)
      need_root || return 1
      MODE=passive
      with_lock apply_snapshot "$(cat "$2")" "$3" passive || { printf '%s\n' "${ERRS[@]}" >&2; return 1; }
      save_state
      return $? ;;
    --clear-owned) need_root && with_lock remove_rules && with_lock clean_legacy_persistence; return $? ;;
    --snapshot-owned) need_root && owned_snapshot; return $? ;;
    --restore-owned)
      need_root || return 1
      with_lock restore_cached_owned "$(cat "$2")"; return $? ;;
  esac
  if [ "${1:-}" = --daemon ]; then
    need_root || return 1
    daemon
  else
    exec /usr/local/bin/ictl "$@"
  fi
}

if [ "${ICTL_LIBRARY:-0}" != 1 ]; then main "$@"; fi
