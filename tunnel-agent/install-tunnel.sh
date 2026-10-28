#!/usr/bin/env bash
# Independent tunnel Agent installer. Does not change the forwarding Agent or host firewall.
set -euo pipefail
umask 077
VERSION="0.1.0"
URL="" TOKEN="" UNINSTALL=0 YES=0
while (($#)); do
  case "$1" in
    --url|--token) (($# >= 2)) || { echo "$1 缺少参数" >&2; exit 1; }; [[ $1 == --url ]] && URL=$2 || TOKEN=$2; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -y|--yes) YES=1; shift ;;
    *) echo "用法：$0 --url https://域名/隧道路径 --token 密钥 | --uninstall [-y]" >&2; exit 1 ;;
  esac
done
[[ $EUID == 0 ]] || { echo "请使用 sudo 或 root 运行" >&2; exit 1; }
for c in curl python3 sha256sum systemctl flock; do command -v "$c" >/dev/null || { echo "缺少依赖：$c" >&2; exit 1; }; done
install -d -m 700 /var/lib/ictl-tunnel
exec 9>/var/lib/ictl-tunnel/update.lock
flock -n 9 || { echo "已有安装任务正在运行" >&2; exit 1; }
if ((UNINSTALL)); then
  if ((!YES)); then read -r -p '删除隧道 Agent、专属 sing-box 和配置？[y/N] ' answer; [[ $answer == y || $answer == Y ]] || exit 0; fi
  for svc in ictl-tunnel-agent ictl-tunnel-core; do
    if systemctl cat "$svc" >/dev/null 2>&1; then systemctl disable --now "$svc"; fi
  done
  rm -f /etc/systemd/system/ictl-tunnel-{agent,core}.service /usr/local/bin/ictl-tunnel-agent
  rm -rf /etc/ictl-tunnel /var/lib/ictl-tunnel /usr/local/lib/ictl-tunnel
  systemctl daemon-reload
  echo "隧道 Agent 已卸载"; exit 0
fi
[[ $URL == https://* && $TOKEN =~ ^[0-9a-fA-F]{64}$ ]] || { echo "需要 HTTPS 地址和 64 位十六进制密钥" >&2; exit 1; }
URL=${URL%/}
case $(uname -m) in x86_64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; *) echo "仅支持 Linux amd64/arm64" >&2; exit 1;; esac
[[ $(uname -s) == Linux ]] || { echo "仅支持 Linux" >&2; exit 1; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# curl 的凭据来自权限 600 的配置文件，避免出现在子进程参数中。
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$TMP/curl.conf"
curl --proto '=https' -fsS --max-time 30 --config "$TMP/curl.conf" "$URL/components" -o "$TMP/components.json"
python3 - "$TMP/components.json" "$ARCH" > "$TMP/meta" <<'PY'
import json,re,sys
m=json.load(open(sys.argv[1])); a=m.get('agent_assets',{}).get(sys.argv[2],{})
if m.get('api') != 1 or not re.fullmatch(r'\d+\.\d+\.\d+',m.get('tunnel_agent','')) or not re.fullmatch(r'[0-9a-f]{64}',a.get('sha256','')):
    sys.exit('服务端缺少可用的隧道 Agent 安装包，请先部署完整的 0.2.0 镜像')
print(m['tunnel_agent']);print(a['sha256'])
PY
mapfile -t META < "$TMP/meta"
curl --proto '=https' -fsS --max-time 120 --config "$TMP/curl.conf" "$URL/download/$ARCH" -o "$TMP/agent"
printf '%s  %s\n' "${META[1]}" "$TMP/agent" | sha256sum -c -
chmod 755 "$TMP/agent"
[[ $("$TMP/agent" version) == "${META[0]}" ]] || { echo "Agent 版本校验失败" >&2; exit 1; }
printf '%s\n%s\n' "$URL" "$TOKEN" | python3 -c 'import json,sys; url,token=sys.stdin.read().splitlines();json.dump({"url":url,"token":token},sys.stdout)' > "$TMP/agent.json"
# 先完成下载校验，再停止旧 Agent，保护配置应用和组件更新不并发。
FILES=(/etc/ictl-tunnel/agent.json /usr/local/bin/ictl-tunnel-agent /usr/local/lib/ictl-tunnel/sing-box
       /etc/systemd/system/ictl-tunnel-agent.service /etc/systemd/system/ictl-tunnel-core.service
       /var/lib/ictl-tunnel/config.json)
for i in "${!FILES[@]}"; do [[ ! -f ${FILES[i]} ]] || cp -p "${FILES[i]}" "$TMP/backup-$i"; done
AGENT_WAS_ACTIVE=0 CORE_WAS_ACTIVE=0
systemctl is-active --quiet ictl-tunnel-agent && AGENT_WAS_ACTIVE=1
systemctl is-active --quiet ictl-tunnel-core && CORE_WAS_ACTIVE=1
rollback() {
  code=$?; trap - EXIT
  if ((code != 0)); then
    set +e
    systemctl stop ictl-tunnel-agent ictl-tunnel-core >/dev/null 2>&1
    for i in "${!FILES[@]}"; do
      if [[ -f $TMP/backup-$i ]]; then
        cp -p "$TMP/backup-$i" "${FILES[i]}.restore" && mv -f "${FILES[i]}.restore" "${FILES[i]}"
      else rm -f "${FILES[i]}"; fi
    done
    systemctl daemon-reload
    ((CORE_WAS_ACTIVE == 0)) || systemctl start ictl-tunnel-core
    ((AGENT_WAS_ACTIVE == 0)) || systemctl start ictl-tunnel-agent
    echo "安装失败，已尝试恢复原有文件和服务，请检查以上错误。" >&2
  fi
  rm -rf "$TMP"
  exit "$code"
}
trap rollback EXIT
if systemctl cat ictl-tunnel-agent >/dev/null 2>&1; then systemctl stop ictl-tunnel-agent; fi
"$TMP/agent" install-core --config "$TMP/agent.json"
install -d -m 700 /etc/ictl-tunnel /var/lib/ictl-tunnel
if [[ ! -f /var/lib/ictl-tunnel/config.json ]]; then
  printf '%s\n' '{"inbounds":[],"outbounds":[{"type":"direct","tag":"direct"}]}' > /var/lib/ictl-tunnel/config.json
fi
install -m 600 "$TMP/agent.json" /etc/ictl-tunnel/agent.json
install -m 755 "$TMP/agent" /usr/local/bin/ictl-tunnel-agent.new
mv -f /usr/local/bin/ictl-tunnel-agent.new /usr/local/bin/ictl-tunnel-agent
cat > /etc/systemd/system/ictl-tunnel-core.service <<'UNIT'
[Unit]
Description=ictl managed sing-box
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/local/lib/ictl-tunnel/sing-box run -c /var/lib/ictl-tunnel/config.json
Restart=on-failure
RestartSec=5
UMask=0077
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/ictl-tunnel-agent.service <<'UNIT'
[Unit]
Description=ictl independent tunnel Agent
After=network-online.target ictl-tunnel-core.service
Wants=network-online.target ictl-tunnel-core.service
[Service]
ExecStart=/usr/local/bin/ictl-tunnel-agent run --config /etc/ictl-tunnel/agent.json
Restart=on-failure
RestartSec=5
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now ictl-tunnel-core ictl-tunnel-agent
systemctl restart ictl-tunnel-agent
systemctl is-active --quiet ictl-tunnel-agent
echo "隧道 Agent ${META[0]} 已接入；请在面板中创建隧道。"
