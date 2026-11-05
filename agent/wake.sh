#!/usr/bin/env bash
# Local-only configuration and firewall lifecycle for the passive wake-up receiver.
set -euo pipefail
umask 077
PKG=/usr/local/lib/ictl
CONF=/etc/ictl/wake.conf
LIB=/var/lib/ictl
UNIT=/etc/systemd/system/ictl-wake.service
PORT='' ALLOW=''

read_config() {
  "$PKG/wake" --check-config "$CONF"
  PORT="$(sed -n 's/^PORT=//p' "$CONF")"
  ALLOW="$(sed -n 's/^ALLOW=//p' "$CONF")"
}

policy_family() {
  local tool="$1" restore="$2" family="$3" icmp="$4" reject="$5" ip
  local -a ips=()
  IFS=, read -r -a ips <<< "$ALLOW"
  {
    printf '*filter\n:ICTL-WAKE-IN - [0:0]\n:ICTL-WAKE-OUT - [0:0]\n-F ICTL-WAKE-IN\n-F ICTL-WAKE-OUT\n'
    for ip in "${ips[@]}"; do
      if { [ "$family" = 4 ] && [[ "$ip" != *:* ]]; } || { [ "$family" = 6 ] && [[ "$ip" == *:* ]]; }; then
        # Match the original UDP tuple of related ICMP errors, including when
        # no socket exists during a receiver restart. Other ports are unaffected.
        printf '%s\n' "-A ICTL-WAKE-OUT -p $icmp -m conntrack --ctproto udp --ctorigsrc $ip --ctorigdstport $PORT -j DROP"
        printf '%s\n' "-A ICTL-WAKE-IN -p udp --dport $PORT -s $ip -j ACCEPT"
      fi
    done
    printf '%s\n' "-A ICTL-WAKE-IN -p udp --dport $PORT -j REJECT --reject-with $reject"
    printf 'COMMIT\n'
  } | "$restore" -w --noflush || return 1
  "$tool" -w -C OUTPUT -j ICTL-WAKE-OUT 2>/dev/null || "$tool" -w -I OUTPUT 1 -j ICTL-WAKE-OUT || return 1
  "$tool" -w -C INPUT -j ICTL-WAKE-IN 2>/dev/null || "$tool" -w -I INPUT 1 -j ICTL-WAKE-IN || return 1
}

policy_apply() {
  read_config || return 1
  policy_family iptables iptables-restore 4 icmp icmp-port-unreachable || return 1
  if [[ "$ALLOW" == *:* ]]; then
    policy_family ip6tables ip6tables-restore 6 ipv6-icmp icmp6-port-unreachable || return 1
  elif command -v ip6tables >/dev/null; then
    policy_remove_family ip6tables || return 1
  fi
}

policy_remove_family() {
  local tool="$1" parent chain
  for parent in INPUT OUTPUT; do
    chain=ICTL-WAKE-IN; [ "$parent" != OUTPUT ] || chain=ICTL-WAKE-OUT
    while "$tool" -w -C "$parent" -j "$chain" 2>/dev/null; do "$tool" -w -D "$parent" -j "$chain"; done
    if "$tool" -w -S "$chain" >/dev/null 2>&1; then
      "$tool" -w -F "$chain"
      "$tool" -w -X "$chain"
    fi
  done
}

policy_remove() {
  policy_remove_family iptables
  if command -v ip6tables >/dev/null; then policy_remove_family ip6tables; fi
}

install_unit() {
  cat > "$UNIT" <<EOF
[Unit]
Description=ictl passive authenticated wake-up receiver
DefaultDependencies=no
After=local-fs.target
Wants=network-pre.target
Before=network-pre.target shutdown.target
Conflicts=shutdown.target
ConditionPathExists=$CONF

[Service]
Type=simple
ExecStartPre=$PKG/wake.sh --prepare
ExecStart=$PKG/wake --daemon
Restart=on-failure
RestartSec=3
KillMode=mixed
TimeoutStopSec=180
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 "$UNIT"
  systemctl daemon-reload
}

configure() {
  local new old
  [ "$(cat "$LIB/agent-kind" 2>/dev/null)" = passive ] || { echo '只有被动 Agent 可以启用远程唤醒' >&2; return 1; }
  [ ! -f "$LIB/switch-work/transaction" ] || { echo '请先完成本机同步或模式切换' >&2; return 1; }
  new="$(mktemp /etc/ictl/wake-new.XXXXXX)"
  if ! "$PKG/wake" --make-config "$1" "$2" > "$new"; then rm -f "$new"; return 1; fi
  old="$(mktemp /etc/ictl/wake-old.XXXXXX)"
  [ ! -f "$CONF" ] || cp "$CONF" "$old"
  if [ -f "$UNIT" ]; then systemctl stop ictl-wake; fi
  exec 9>/run/ictl.lock
  flock -w 60 9
  # The old reservation is intentionally excluded only for this local check.
  if ! ICTL_LIBRARY=1 ICTL_WAKE_CHECK=1 bash -c 'source "$1/passive.sh"; ERRS=()
if [ -f "$CONFIG" ] && awk -v port="$2" '\''$1 !~ /^v=/ && ($2=="udp" || $2=="both") && $3==port {found=1} END {exit !found}'\'' "$CONFIG"; then
  echo "端口冲突：UDP $2 已被 Agent 保存的转发配置占用" >&2; exit 1
fi
check_port_conflicts "v=check
1 udp $2 127.0.0.1 1" || { printf "%s\n" "${ERRS[@]}" >&2; exit 1; }' _ "$PKG" "$1"; then
    rm -f "$new" "$old"
    flock -u 9; exec 9>&-
    if [ -s "$CONF" ] && grep -qx 'ENABLED=1' "$CONF"; then systemctl start ictl-wake; fi
    return 1
  fi
  mv "$new" "$CONF"
  if ! policy_apply; then
    if [ -s "$old" ]; then mv "$old" "$CONF"; policy_apply || true
    else rm -f "$CONF" "$old"; policy_remove || true; fi
    flock -u 9; exec 9>&-
    if [ -f "$CONF" ]; then systemctl start ictl-wake || true; fi
    return 1
  fi
  rm -f "$old"
  install_unit
  flock -u 9; exec 9>&-
  systemctl enable --now ictl-wake
  printf '本机远程唤醒已启用：UDP %s；请在面板填写同一端口并开启远程唤醒。\n' "$PORT"
}

main() {
  [ "$(id -u)" = 0 ] || { echo '请使用 sudo ictl' >&2; return 1; }
  case "${1:-}" in
    --configure) configure "${2:?需要端口}" "${3:?需要白名单 IP}" ;;
    --prepare|--daemon)
      [ "$(cat "$LIB/agent-kind" 2>/dev/null)" = passive ] || return 1
      exec 9>/run/ictl.lock; flock -w 60 9
      policy_apply
      flock -u 9; exec 9>&-
      if [ "$1" = --daemon ]; then exec "$PKG/wake" --daemon; fi ;;
    --restore)
      [ -f "$CONF" ] && grep -qx 'ENABLED=1' "$CONF" || return 0
      install_unit
      systemctl enable --now ictl-wake ;;
    --disable|--remove)
      if [ -f "$UNIT" ]; then systemctl disable --now ictl-wake; fi
      exec 9>/run/ictl.lock; flock -w 60 9
      policy_remove
      rm -f "$CONF" "$UNIT" "$LIB/wake-replay.json" "$LIB/wake-ack"
      systemctl daemon-reload
      printf '本机远程唤醒已关闭。\n' ;;
    *) echo '无效的远程唤醒管理操作' >&2; return 1 ;;
  esac
}

if [ "${ICTL_LIBRARY:-0}" != 1 ]; then main "$@"; fi
