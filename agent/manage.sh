#!/usr/bin/env bash
# ictl installation manager: identity survives component replacement.
set -uo pipefail
umask 077
VERSION="0.6.0-rc.5"
PKG=/usr/local/lib/ictl
LIB=/var/lib/ictl
CONF=/etc/ictl/agent.conf
UNIT=/etc/systemd/system/ictl-agent.service
WORK="$LIB/switch-work"
URL='' KEY='' AUTO_UPDATE=1
REPLY='' OP='' TARGET='' GEN='' HASH='' NEW_VERSION='' PHASE='' FROM=''

die() { printf '错误：%s\n' "$*" >&2; return 1; }
read_identity() {
  [ -f "$CONF" ] || { die '尚未接入服务端'; return 1; }
  local k v
  while IFS='=' read -r k v; do case "$k" in URL) URL="$v" ;; KEY) KEY="$v" ;; AUTO_UPDATE) AUTO_UPDATE="$v" ;; esac; done < "$CONF"
  [[ "$URL" =~ ^https?://[^[:space:]\"\\]+$ && "$KEY" =~ ^[0-9a-f]{64}$ ]] || { die '接入配置无效'; return 1; }
  derive_keys
}
field() { sed -n "s/^$1=//p" <<< "$REPLY"; }
network_inventory() {
  local addresses
  addresses="$(ip -o -4 address show 2>/dev/null)" || return 0
  awk '$3 == "inet" {dev=$2; sub(/@.*/, "", dev); sub(/:$/, "", dev);
    if (dev == "lo" || dev !~ /^[A-Za-z0-9_.:-]+$/ || length(dev)>15) next;
    count++; if(count<=96) {if(rows!="") rows=rows ";"; rows=rows dev "," $4}}
    END {printf "network_addresses=%s\nnetwork_complete=%d\n", rows, count<=96}' <<< "$addresses"
}
rpc() {
  local action="$1" code
  code="$({ printf 't=%s\nmanage=%s\nop=%s\ntarget=%s\narch=%s\nagent=%s\n%s\n' \
    "$(date +%s)" "$action" "$OP" "$TARGET" "$ARCH" "$NEW_VERSION" "${2:-}"
    case "$action" in prepare|commit|failed) network_inventory ;; esac
  } | send_request "$WORK/response" 120)"
  [ "$code" = 200 ] || { die "服务端操作失败（$code），请检查接入、时间和服务端日志"; return 1; }
  REPLY="$(cat "$WORK/response")"
  [[ "$REPLY" == proto=2* ]] || { die '服务端协议不兼容'; return 1; }
}
fetch_component() {
  local name="$1" sha="$2" output="$3" code
  [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || { die '组件校验和缺失'; return 1; }
  if [ -f "$output" ] && [ "$(sha256_hex < "$output")" = "$sha" ]; then return 0; fi
  code="$(printf 't=%s\nmanage=asset\nname=%s\n' "$(date +%s)" "$name" | send_request "$output.download" 120)"
  [ "$code" = 200 ] && [ "$(sha256_hex < "$output.download")" = "$sha" ] || { rm -f "$output.download"; die "组件下载或校验失败：$name"; return 1; }
  chmod 755 "$output.download" && mv "$output.download" "$output"
}
journal() {
  printf 'OP=%s\nTARGET=%s\nGEN=%s\nHASH=%s\nNEW_VERSION=%s\nPHASE=%s\nFROM=%s\n' \
    "$OP" "$TARGET" "$GEN" "$HASH" "$NEW_VERSION" "$PHASE" "$FROM" > "$WORK/transaction.tmp" &&
    sync -f "$WORK/transaction.tmp" && mv "$WORK/transaction.tmp" "$WORK/transaction"
}
load_journal() {
  local k v
  while IFS='=' read -r k v; do
    case "$k" in OP|TARGET|GEN|HASH|NEW_VERSION|PHASE|FROM) printf -v "$k" '%s' "$v" ;; esac
  done < "$WORK/transaction"
  [[ "$OP" =~ ^[0-9a-f]{32}$ && "$GEN" =~ ^[0-9]+$ && "$TARGET" =~ ^(active|passive)$ ]]
}
install_unit() {
  local type=simple start="$PKG/active --daemon" restart=on-failure
  if [ "$TARGET" = passive ]; then type=oneshot; start="$PKG/passive.sh --daemon"; restart=no; fi
  cat > "$UNIT.tmp" <<EOF
[Unit]
Description=ictl $TARGET agent
After=network-online.target
Wants=network-online.target

[Service]
Type=$type
ExecStart=$start
Restart=$restart
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 "$UNIT.tmp" && mv "$UNIT.tmp" "$UNIT" && systemctl daemon-reload && systemctl enable ictl-agent >/dev/null
}
finish() {
  # Enable the active daemon only after the matching server receipt was obtained.
  if [ "$TARGET" = active ]; then
    mkdir -p "$LIB/active"
    printf 'confirmed\n' > "$LIB/active/enabled"
    rm -f "$LIB/paused"
  else
    rm -f "$LIB/active/enabled"
    printf 'passive\n' > "$LIB/mode"
  fi
  install_unit || return 1
  install -m 755 "$WORK/manage.sh" /usr/local/bin/ictl.new && mv /usr/local/bin/ictl.new /usr/local/bin/ictl || return 1
  install -m 755 "$WORK/manage.sh" "$PKG/manage.sh"
  if [ "$TARGET" = active ]; then rm -f "$PKG/passive.sh" "$PKG/wake" "$PKG/wake.sh"; else rm -f "$PKG/active"; fi
  local old
  for old in /root/ictl.sh /ictl.sh; do
    if [ -L "$old" ] && [ "$(readlink "$old")" = /usr/local/bin/ictl ]; then continue; fi
    if [ -f "$old" ] && [ ! -L "$old" ] &&
        grep -Fxq '# ictl 节点端：从服务端拉取转发规则，应用到 iptables 并上报结果' "$old" &&
        grep -Fxq 'BIN="/usr/local/bin/ictl"' "$old" && grep -Fxq 'CONF_DIR="/etc/ictl"' "$old" && bash -n "$old"; then
      ln -s /usr/local/bin/ictl "$old.link" && mv -Tf "$old.link" "$old"
    fi
  done
  PHASE=confirmed; journal || return 1
  systemctl start ictl-agent || { die '已完成切换，但服务启动失败，请检查 journalctl -u ictl-agent'; return 1; }
  if [ "$TARGET" = passive ] && [ -f /etc/ictl/wake.conf ] && grep -qx 'ENABLED=1' /etc/ictl/wake.conf; then
    bash "$PKG/wake.sh" --restore || return 1
  fi
  mkdir -p "$LIB/rollback"
  [ ! -f "$WORK/backup.tar" ] || mv -f "$WORK/backup.tar" "$LIB/rollback/previous.tar"
  rm -f "$WORK/transaction"
  printf '已确认：%s Agent %s；节点身份与配置已保留。\n' "$TARGET" "$NEW_VERSION"
}
rollback() {
  local result=0
  printf '交接失败，正在恢复本工具的旧配置。\n' >&2
  if [ "$FROM" != "$TARGET" ]; then
    if [ "$TARGET" = active ] && [ -x "$PKG/active" ]; then ICTL_LOCK_HELD=1 "$PKG/active" --clear || result=1; fi
    if [ "$TARGET" = passive ]; then ICTL_LOCK_HELD=1 bash "$WORK/passive-helper.sh" --clear-owned || result=1; fi
  fi
  # Never restore forwarding while a passive cleanup journal is unfinished.
  if [ -f "$LIB/clear-owned.rules" ]; then
    ICTL_LOCK_HELD=1 bash "$WORK/passive-helper.sh" --clear-owned || result=1
  fi
  if [ "$result" != 0 ]; then
    PHASE=failed; journal
    flock -u 9; exec 9>&-
    rpc failed 'error=连接清理未完成，已保留事务和备份' || true
    die '连接清理未完成；事务和备份已保留，请排除错误后执行 --recover'
    return 1
  fi
  tar -xf "$WORK/backup.tar" -C / || result=1
  if [ "$FROM" = active ] && [ -x "$PKG/active" ]; then
    # Keep the old manager offline after an active -> passive request.
    if [ "$TARGET" = passive ]; then printf 'manual confirmation required\n' > "$LIB/paused"; fi
    ICTL_LOCK_HELD=1 "$PKG/active" --restore || result=1
  elif [ -s "$WORK/old-rules" ]; then
    ICTL_LOCK_HELD=1 bash "$WORK/passive-helper.sh" --restore-owned "$WORK/old-rules" || result=1
  fi
  PHASE=failed; journal
  systemctl daemon-reload || result=1
  flock -u 9; exec 9>&-
  local reason='本机交接失败，已尝试恢复原配置'
  if [ -s "$WORK/apply.log" ]; then
    reason="$(tr '\n' ' ' < "$WORK/apply.log" | head -c 500)"
    case "$reason" in *端口冲突：*) reason="端口冲突：${reason#*端口冲突：}" ;; *端口检查失败：*) reason="端口检查失败：${reason#*端口检查失败：}" ;; esac
  fi
  rpc failed "error=$reason" || true
  if [ "$result" = 0 ] && [ "$FROM" = active ] && [ "$TARGET" = active ]; then
    rm -f "$LIB/paused"
    systemctl start ictl-agent || result=1
  fi
  if [ "$FROM" = passive ] && [ -f "$PKG/wake.sh" ] && [ -f /etc/ictl/wake.conf ]; then
    bash "$PKG/wake.sh" --restore || result=1
  fi
  [ "$result" = 0 ] || die '自动恢复未完成；备份和事务已保留，请勿删除 switch-work'
  return 1
}
sync_cleanup() {
  local result="$1"
  if [ "$result" != 0 ] && [ "$SYNC_PREPARED" = 1 ] && [ "$SYNC_HANDOVER" = 0 ]; then
    # No rules have been handed over yet. Release the server reservation and
    # restore the old service even when upload, checkpoint or backup failed.
    flock -u 9 2>/dev/null || true
    exec 9>&-
    rpc abort || true
    if [ "$SYNC_JOURNALED" = 1 ]; then rm -f "$WORK/transaction"; fi
    if [ "$SYNC_STOPPED" = 1 ] && [ "$FROM" != none ]; then
      if [ "$FROM" = passive ] || [ "$TARGET" = active ]; then
        systemctl start ictl-agent || die '旧服务恢复启动失败，请检查 journalctl -u ictl-agent'
      fi
    fi
    if [ "$SYNC_WAKE_STOPPED" = 1 ]; then systemctl start ictl-wake || true; fi
  fi
  return "$result"
}
perform_sync() (
  SYNC_PREPARED=0 SYNC_HANDOVER=0 SYNC_STOPPED=0 SYNC_WAKE_STOPPED=0 SYNC_JOURNALED=0
  trap 'sync_cleanup "$?"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  sync_transaction "$@"
)
component_matches() {
  [[ "$2" =~ ^[0-9a-f]{64}$ ]] && [ -f "$1" ] && [ "$(sha256_hex < "$1")" = "$2" ]
}
sync_transaction() {
  local wanted="${1:-}" artifact digest manager_digest helper_digest old_kind item
  local failed_active=0
  mkdir -p "$WORK" "$PKG"
  if [ -f "$WORK/transaction" ]; then
    [ "${UPDATE_ONLY:-0}" != 1 ] || { die '存在未完成的同步，请使用 --sync 确认后再更新'; return 1; }
    load_journal || { die '切换日志损坏，未继续写入'; return 1; }
    case "$PHASE" in
      installed|confirmed)
        # A restart may have removed kernel rules since the local apply. Restore
        # the saved snapshot offline before sending (or retrying) its receipt.
        systemctl stop ictl-agent 2>/dev/null || { [ ! -f "$UNIT" ] || return 1; }
        if [ "$TARGET" = active ]; then "$PKG/active" --restore || return 1
        else bash "$PKG/passive.sh" --daemon || return 1; fi
        commit || return 1
        if [ "$(field result)" = deleted ]; then rm -f "$WORK/transaction"; printf '节点已删除，规则已清理。\n'; return 0; fi
        if [ "$(field result)" = confirmed ]; then finish; return $?; fi
        [ "$(field result)" = changed ] || { die '旧切换结果待确认'; return 1; }
        # A newer server snapshot needs another explicit local application below.
        rm -f "$WORK/transaction" ;;
      handover) die '检测到交接中断；执行 sudo ictl --recover 恢复旧规则后重试'; return 1 ;;
      failed) [ "$TARGET" != active ] || failed_active=1 ;;
    esac
  fi
  OP='' NEW_VERSION=''
  rpc info || return 1
  TARGET="$(field mode)"
  [ -z "$wanted" ] || [ "$wanted" = "$TARGET" ] || { die '请先在面板选择相同的目标模式'; return 1; }
  if [ "$TARGET" = active ] && [[ "$URL" != https://* ]]; then die '主动 Agent 需要 HTTPS 同步地址'; return 1; fi
  FROM=none
  if [ -f "$LIB/agent-kind" ]; then read -r FROM < "$LIB/agent-kind"
  elif [ -f "$UNIT" ] || [ -f "$LIB/config" ]; then die '缺少 Agent 类型标识；请先升级到 0.5.3 后再迁移'; return 1; fi
  [ "${UPDATE_ONLY:-0}" != 1 ] || [ "$FROM" = "$TARGET" ] || { die '更新不能转换 Agent 类型；请使用 --sync 确认切换'; return 1; }
  # Ordinary passive sync keeps the installed components and service intact.
  # Missing metadata or an unfinished handover always takes the full path.
  if [ "$FROM" = passive ] && [ "$TARGET" = passive ] && [ "$(field installed)" = passive ] &&
      [ "${UPDATE_ONLY:-0}" != 1 ] && [ ! -f "$WORK/transaction" ] &&
      [ -f "$UNIT" ] && grep -Fxq "ExecStart=$PKG/passive.sh --daemon" "$UNIT" &&
      [ "$(cat "$LIB/mode" 2>/dev/null)" = passive ] &&
      [ "$(field installation_id)" = "$(cat "$LIB/installation-id" 2>/dev/null)" ] &&
      component_matches "$PKG/passive.sh" "$(field passive_sha256)" &&
      component_matches /usr/local/bin/ictl "$(field manager_sha256)"; then
    bash "$PKG/passive.sh" --sync
    return $?
  fi
  rpc prepare || return 1
  OP="$(field op)" GEN="$(field generation)" HASH="$(field v)" NEW_VERSION="$(field version)"
  SYNC_PREPARED=1
  artifact="$(field artifact)" digest="$(field sha256)" manager_digest="$(field manager_sha256)" helper_digest="$(field passive_sha256)"
  [[ "$OP" =~ ^[0-9a-f]{32}$ && "$GEN" =~ ^[0-9]+$ && "$HASH" =~ ^[0-9a-f]{12}$ ]] || { die '服务端切换快照无效'; return 1; }
  field config_b64 | base64 -d > "$WORK/config" || return 1
  fetch_component "$artifact" "$digest" "$WORK/target" || return 1
  fetch_component manage.sh "$manager_digest" "$WORK/manage.sh" || return 1
  fetch_component passive.sh "$helper_digest" "$WORK/passive-helper.sh" || return 1
  if [ "$TARGET" = passive ] && [ -f /etc/ictl/wake.conf ] && grep -qx 'ENABLED=1' /etc/ictl/wake.conf; then
    prepare_wake || return 1
  fi
  bash -n "$WORK/manage.sh" && bash -n "$WORK/passive-helper.sh" || return 1
  if [ "$TARGET" = active ]; then
    [ "$("$WORK/target" --version)" = "$NEW_VERSION" ] || { die '主动组件版本不匹配'; return 1; }
    for item in ip nft ss conntrack; do command -v "$item" >/dev/null || { die "缺少依赖：$item"; return 1; }; done
    # Inspect the old failure journal before replacing it or its backup. Only
    # the new binary knows how to recover pre-1.0.8 empty, unmarked tables.
    if [ "$failed_active" = 1 ]; then "$WORK/target" --recover-empty-table || return 1; fi
    if [ "$FROM" != active ] && command -v iptables >/dev/null && iptables -S FORWARD | grep -qx -- '-P FORWARD DROP'; then
      printf '提示：当前 FORWARD 默认拒绝，请确认已单独放行本节点的转发范围；安装器不会修改该策略。\n' >&2
    fi
  else
    for item in ip iptables iptables-save iptables-restore getent conntrack; do command -v "$item" >/dev/null || { die "缺少依赖：$item"; return 1; }; done
  fi
  PHASE=prepared; journal || return 1
  SYNC_JOURNALED=1
  SYNC_STOPPED=1
  systemctl stop ictl-agent 2>/dev/null || { [ ! -f "$UNIT" ] || return 1; }
  if [ -f /etc/systemd/system/ictl-wake.service ]; then
    SYNC_WAKE_STOPPED=1
    systemctl stop ictl-wake || return 1
  fi
  if [ "$FROM" = active ]; then "$PKG/active" --flush-traffic || return 1; fi
  exec 9>/run/ictl.lock
  flock -w 60 9 || { die '等待规则锁超时'; return 1; }
  local files=()
  for item in /etc/ictl /usr/local/bin/ictl "$PKG" "$UNIT" /etc/systemd/system/ictl-wake.service "$LIB/config" "$LIB/applied" "$LIB/resolved" "$LIB/state" "$LIB/mode" "$LIB/agent-kind" "$LIB/installation-id" "$LIB/active" "$LIB/routing"; do
    [ ! -e "$item" ] || files+=("${item#/}")
  done
  if [ "$FROM" = active ]; then
    ICTL_LOCK_HELD=1 "$PKG/active" --checkpoint || return 1
    cp "$LIB/config" "$WORK/old-config" || return 1
    "$PKG/active" --generation-only > "$WORK/old-generation" || return 1
  elif [ "$FROM" != none ]; then
    ICTL_LOCK_HELD=1 bash "$WORK/passive-helper.sh" --snapshot-owned > "$WORK/old-rules" || return 1
  fi
  tar -cf "$WORK/backup.tar" -C / "${files[@]}" || return 1
  PHASE=handover; journal || return 1
  SYNC_HANDOVER=1
  rm -f "$LIB/active/enabled"
  if [ "$FROM" != "$TARGET" ] && [ "$FROM" != none ]; then
    if [ "$FROM" = active ]; then
      ICTL_LOCK_HELD=1 "$PKG/active" --clear || { rollback; return 1; }
      # The saved port projection remains reserved while the final upload runs.
      flock -u 9
      if ! "$PKG/active" --flush-traffic; then
        flock -w 60 9 || return 1
        rollback; return 1
      fi
      flock -w 60 9 || return 1
    else ICTL_LOCK_HELD=1 bash "$WORK/passive-helper.sh" --clear-owned || { rollback; return 1; }; fi
  fi
  if [ "$TARGET" = active ]; then
    install -m 755 "$WORK/target" "$PKG/active.new" && mv "$PKG/active.new" "$PKG/active" || { rollback; return 1; }
    ICTL_LOCK_HELD=1 "$PKG/active" --apply-file "$WORK/config" --generation "$GEN" > "$WORK/apply.log" 2>&1 || { cat "$WORK/apply.log" >&2; rollback; return 1; }
  else
    install -m 755 "$WORK/target" "$PKG/passive.sh.new" && mv "$PKG/passive.sh.new" "$PKG/passive.sh" || { rollback; return 1; }
    ICTL_LOCK_HELD=1 bash "$PKG/passive.sh" --apply-file "$WORK/config" "$GEN" > "$WORK/apply.log" 2>&1 || { cat "$WORK/apply.log" >&2; rollback; return 1; }
  fi
  printf '%s\n' "$TARGET" > "$LIB/agent-kind"
  if [ "$TARGET" = passive ] && [ -f /etc/ictl/wake.conf ] && grep -qx 'ENABLED=1' /etc/ictl/wake.conf; then
    install_wake || { rollback; return 1; }
  fi
  if [ "$TARGET" = active ] && [ -f "$PKG/wake.sh" ]; then
    # The helper takes the same rule lock; release it for the owned-rule cleanup.
    flock -u 9
    bash "$PKG/wake.sh" --remove || return 1
    flock -w 60 9 || return 1
  fi
  PHASE=installed; journal || return 1
  flock -u 9; exec 9>&-
  commit || { die '已应用，等待服务端确认；再次执行 --sync 重试确认'; return 1; }
  if [ "$(field result)" = deleted ]; then
    rm -f "$WORK/transaction"
    printf '面板节点已删除，转发已清理；可执行 sudo ictl --uninstall。\n'
    return 0
  fi
  [ "$(field result)" = confirmed ] || { die '服务端仍有变更，切换结果尚未确认'; return 1; }
  finish
}
commit() {
  local held='[]'
  if [ "$TARGET" = active ]; then
    held="$("$PKG/active" --held-ports)" || return 1
    # Upload the final counters before a deletion ACK removes the node identity.
    # Persist the upload receipt so a lost commit response can still be retried.
    if [ "$(cat "$WORK/traffic-receipt" 2>/dev/null)" != "$OP" ]; then
      "$PKG/active" --flush-traffic || return 1
      printf '%s\n' "$OP" > "$WORK/traffic-receipt.tmp" && sync -f "$WORK/traffic-receipt.tmp" &&
        mv "$WORK/traffic-receipt.tmp" "$WORK/traffic-receipt" || return 1
    fi
  fi
  rpc commit "held_ports=$held"
}
prepare_wake() {
  local saved="$REPLY" name digest script_digest
  rpc wake-assets || return 1
  name="$(field wake_artifact)" digest="$(field wake_sha256)" script_digest="$(field wake_script_sha256)"
  [[ "$name" = "ictl-wake-linux-$ARCH" ]] || return 1
  fetch_component "$name" "$digest" "$WORK/wake" || return 1
  fetch_component wake.sh "$script_digest" "$WORK/wake.sh" || return 1
  bash -n "$WORK/wake.sh" || return 1
  REPLY="$saved"
}
install_wake() {
  local file
  for file in wake wake.sh; do
    install -m 755 "$WORK/$file" "$PKG/$file.new" && mv "$PKG/$file.new" "$PKG/$file" || return 1
  done
}
wake_settings() {
  local choice port allow
  [ "$(cat "$LIB/agent-kind" 2>/dev/null)" = passive ] || { die '只有被动 Agent 可以设置远程唤醒'; return 1; }
  printf '远程唤醒：默认关闭，白名单只接受 Server 实际出口 IP。\n'
  if [ -f /etc/ictl/wake.conf ]; then cat /etc/ictl/wake.conf; else printf '当前：关闭\n'; fi
  printf '1) 设置并启用\n2) 关闭并清除本地设置\n0) 返回\n'
  read -r -p '请选择：' choice
  case "$choice" in
    1)
      read -r -p 'UDP 监听端口：' port
      read -r -p 'Server 出口 IP（多个用英文逗号分隔）：' allow
      prepare_wake && install_wake || return 1
      bash "$PKG/wake.sh" --configure "$port" "$allow" ;;
    2) [ ! -f "$PKG/wake.sh" ] || bash "$PKG/wake.sh" --disable ;;
  esac
}
credentials() {
  local previous_url="$URL" previous_key="$KEY" new_url new_key expected
  read -r -p "同步地址（留空保留当前地址）：" new_url
  read -r -s -p '新节点密钥：' new_key; printf '\n'
  URL="${new_url:-$previous_url}" KEY="$new_key"
  if ! [[ "$URL" =~ ^https?://[^[:space:]\"\\]+$ && "$KEY" =~ ^[0-9a-f]{64}$ ]]; then
    URL="$previous_url" KEY="$previous_key"; die '地址或密钥格式无效'; return 1
  fi
  derive_keys
  if ! rpc info; then URL="$previous_url" KEY="$previous_key"; derive_keys; return 1; fi
  if [ "$(field mode)" = active ] && [[ "$URL" != https://* ]]; then die '主动 Agent 需要 HTTPS'; return 1; fi
  expected="$(field installation_id)"
  if [ -n "$expected" ] && [ "$expected" != "$(cat "$LIB/installation-id" 2>/dev/null)" ]; then
    die '新密钥属于另一安装身份，未替换当前配置'; return 1
  fi
  printf 'URL=%s\nKEY=%s\nAUTO_UPDATE=%s\n' "$URL" "$KEY" "$AUTO_UPDATE" > "$CONF.new" &&
    chmod 600 "$CONF.new" && mv "$CONF.new" "$CONF" || return 1
  printf '接入凭据已验证并保存，正在同步确认。\n'
  perform_sync
}
main() {
  [ "$(id -u)" = 0 ] || { die '请使用 sudo ictl'; return 1; }
  [ -f "$PKG/control.sh" ] || { die '管理组件不完整，请重新运行本版本安装脚本'; return 1; }
  source "$PKG/control.sh"
  ARCH="$(uname -m)"; case "$ARCH" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; esac
  read_identity || return 1
  mkdir -p "$WORK"
  local action="${1:-}" choice
  if [ -z "$action" ]; then
    printf 'ictl %s\n1) 同步更新／确认切换\n2) 本地状态\n3) 更新当前 Agent\n4) 卸载\n5) 被动节点远程唤醒设置\n6) 修改接入地址／密钥\n0) 退出\n' "$VERSION"
    read -r -p '请选择：' choice
    case "$choice" in 1) action=--sync ;; 2) action=--status ;; 3) action=--update ;; 4) action=--uninstall ;; 5) action=--wake-settings ;; 6) action=--credentials ;; *) return 0 ;; esac
  fi
  if [ "$action" != --status ] && [ "${ICTL_MANAGER_LOCK_HELD:-0}" != 1 ]; then
    exec 8>/run/ictl-manager.lock
    flock -n 8 || { die '另一项安装或同步正在执行'; return 1; }
    export ICTL_MANAGER_LOCK_HELD=1
    # The menu may have remained open while another operation changed identity.
    read_identity || return 1
  fi
  case "$action" in
    --credentials) credentials ;;
    --wake-settings) wake_settings ;;
    --wake-sync) bash "$PKG/passive.sh" --wake-sync "${2:-}" ;;
    --sync|--install) perform_sync ;;
    --update) UPDATE_ONLY=1 perform_sync ;;
    --auto-update)
      [ "$AUTO_UPDATE" = 1 ] || return 0
      [ ! -e "$WORK/transaction" ] || return 0
      [ "$(cat "$LIB/agent-kind" 2>/dev/null)" = active ] || return 0
      UPDATE_ONLY=1 perform_sync active ;;
    --switch) perform_sync "${2:-}" ;;
    --status)
      printf '已安装：'; cat "$LIB/agent-kind" 2>/dev/null || printf '旧版\n'
      if [ -f "$LIB/paused" ]; then printf '后台已暂停，等待手动确认\n'; fi
      systemctl status ictl-agent --no-pager || true
      if [ -f /etc/ictl/wake.conf ]; then
        printf '\n远程唤醒本地设置：\n'; cat /etc/ictl/wake.conf
        systemctl status ictl-wake --no-pager || true
      else printf '\n远程唤醒：关闭\n'; fi ;;
    --recover)
      load_journal || return 1
      case "$PHASE" in handover|failed) ;; *) die '请执行 --sync 确认当前事务，不能回退已提交或尚未交接的事务'; return 1 ;; esac
      if [ -f /etc/systemd/system/ictl-wake.service ]; then systemctl stop ictl-wake || return 1; fi
      systemctl stop ictl-agent || true
      exec 9>/run/ictl.lock; flock -w 60 9 || return 1; rollback ;;
    --uninstall)
      if [ "${2:-}" != -y ]; then read -r -p '删除本节点 Agent、身份和转发？[y/N] ' choice; [ "$choice" = y ] || return 0; fi
      if [ -f "$PKG/wake.sh" ]; then bash "$PKG/wake.sh" --remove || return 1; fi
      systemctl stop ictl-agent || return 1
      local kind; read -r kind < "$LIB/agent-kind"
      if [ "$kind" = active ]; then "$PKG/active" --clear || return 1; else bash "$PKG/passive.sh" --clear-owned || return 1; fi
      systemctl disable ictl-agent || return 1
      rm -f "$UNIT" /etc/sysctl.d/99-ictl.conf
      rm -rf "$PKG" /etc/ictl "$LIB"
      rm -f /usr/local/bin/ictl
      local old
      for old in /root/ictl.sh /ictl.sh; do
        if [ -L "$old" ] && [ "$(readlink "$old")" = /usr/local/bin/ictl ]; then rm -f "$old"; fi
      done
      systemctl daemon-reload ;;
    *) die '用法：ictl [--sync|--switch active/passive|--update|--status|--credentials|--wake-settings|--recover|--uninstall [-y]]'; return 1 ;;
  esac
}
if [ "${ICTL_LIBRARY:-0}" != 1 ]; then main "$@"; fi
