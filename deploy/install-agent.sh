#!/usr/bin/env bash
# Install the explicitly selected release. RC releases never follow /latest.
set -euo pipefail
umask 077
VERSION="0.6.0-rc.5"
BASE="https://github.com/moran1024a/iptables-control/releases/download/v$VERSION"
TRUSTED_BASE="$BASE"
URL='' KEY='' KIND=active
while (($#)); do
  case "$1" in
    --url) URL="${2:?--url 需要参数}"; shift 2 ;;
    --key) KEY="${2:?--key 需要参数}"; shift 2 ;;
    --kind) KIND="${2:?--kind 需要参数}"; shift 2 ;;
    --mirror) BASE="https://gh-proxy.com/$BASE"; shift ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
done
[[ "$KIND" = active || "$KIND" = passive ]] || { echo '类型应为 active 或 passive' >&2; exit 1; }
[[ "$URL" =~ ^https?://[^[:space:]\"\\]+$ && "$KEY" =~ ^[0-9a-f]{64}$ ]] || { echo '需要有效 --url 与 --key' >&2; exit 1; }
[[ "$KIND" != active || "$URL" = https://* ]] || { echo '主动 Agent 需要 HTTPS 接入' >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo '请使用 sudo bash 运行安装脚本' >&2; exit 1; }
tools=(curl openssl ip flock systemctl tar)
packages=(curl openssl util-linux)
if [ "$KIND" = active ]; then tools+=(nft ss conntrack); packages+=(nftables iproute2 conntrack); else tools+=(iptables iptables-save iptables-restore ss conntrack); packages+=(iptables iproute2 conntrack); fi
missing=0
for tool in "${tools[@]}"; do command -v "$tool" >/dev/null || missing=1; done
if [ "$missing" = 1 ]; then
  if command -v apt-get >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
  else
    echo "请先安装缺少的依赖：${tools[*]}（主动需要 nftables/iproute/conntrack-tools）" >&2; exit 1
  fi
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fSL --connect-timeout 10 --max-time 60 "$TRUSTED_BASE/SHA256SUMS" -o "$work/SHA256SUMS"
for file in manage.sh control.sh; do
  curl -fSL --connect-timeout 10 --max-time 60 "$BASE/$file" -o "$work/$file"
  expected="$(awk -v f="$file" '$2==f {print $1}' "$work/SHA256SUMS")"
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] && [ "$(sha256sum "$work/$file" | cut -d' ' -f1)" = "$expected" ] || { echo "校验失败：$file" >&2; exit 1; }
  bash -n "$work/$file"
done
mkdir -p /etc/ictl /usr/local/lib/ictl /var/lib/ictl
exec 8>/run/ictl-manager.lock
flock -n 8 || { echo '另一个安装或同步操作正在运行' >&2; exit 1; }
auto_update=1
if [ -f /etc/ictl/agent.conf ]; then
  old_key="$(sed -n 's/^KEY=//p' /etc/ictl/agent.conf)"
  [ "$old_key" = "$KEY" ] || { echo '已安装不同身份的 Agent；重置本节点密钥请运行 sudo ictl --credentials，迁移到另一节点请先卸载旧身份' >&2; exit 1; }
  auto_update="$(sed -n 's/^AUTO_UPDATE=//p' /etc/ictl/agent.conf)"
  [ "$auto_update" = 0 ] || auto_update=1
fi
backup="$(mktemp -d /var/lib/ictl/install-backup.XXXXXX)"
was_active=0
systemctl is-active --quiet ictl-agent && was_active=1
[ ! -f /var/lib/ictl/switch-work/transaction ] || cp /var/lib/ictl/switch-work/transaction "$backup/transaction"
files=(/etc/ictl/agent.conf /usr/local/lib/ictl/manage.sh /usr/local/lib/ictl/control.sh)
for i in "${!files[@]}"; do
  [ ! -e "${files[$i]}" ] || cp -p "${files[$i]}" "$backup/$i"
done
install_cleanup() {
  local result=$? phase=''
  if [ "$result" != 0 ]; then
    phase="$(sed -n 's/^PHASE=//p' /var/lib/ictl/switch-work/transaction 2>/dev/null || true)"
    if [ -f "$backup/transaction" ] && cmp -s "$backup/transaction" /var/lib/ictl/switch-work/transaction; then phase=''; fi
    case "$phase" in
      handover|installed|confirmed|failed)
        echo "交接尚待恢复或确认；安装前文件保留在 $backup，请使用 sudo ictl --sync 或 --recover 处理。" >&2 ;;
      *)
        for i in "${!files[@]}"; do
          if [ -f "$backup/$i" ]; then cp -p "$backup/$i" "${files[$i]}"
          else rm -f "${files[$i]}"; fi
        done
        # An early-failure cleanup may have restarted the service while the new
        # URL was still on disk. Reload the restored identity as well.
        if [ "$was_active" = 1 ]; then systemctl try-restart ictl-agent; fi
        rm -rf "$backup"
        echo '安装未进入交接，已恢复安装前的接入信息和管理脚本。' >&2 ;;
    esac
  else rm -rf "$backup"; fi
  rm -rf "$work"
  return "$result"
}
trap install_cleanup EXIT
printf 'URL=%s\nKEY=%s\nAUTO_UPDATE=%s\n' "$URL" "$KEY" "$auto_update" > /etc/ictl/agent.conf.new
mv /etc/ictl/agent.conf.new /etc/ictl/agent.conf
for file in manage.sh control.sh; do
  install -m 755 "$work/$file" "/usr/local/lib/ictl/$file.new"
  mv "/usr/local/lib/ictl/$file.new" "/usr/local/lib/ictl/$file"
done
ICTL_MANAGER_LOCK_HELD=1 bash /usr/local/lib/ictl/manage.sh --switch "$KIND"
