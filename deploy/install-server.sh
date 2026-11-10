#!/usr/bin/env bash
# ictl 服务端一键部署 / 更新 / 卸载（Docker）
#
#   bash install-server.sh [--port 8730] [--force] [--mirror]
#   bash install-server.sh --uninstall [--purge]
#
#   首次运行：创建容器，默认对外端口 8730。
#   再次运行：检查版本，有新版本时更新；沿用现有容器的端口，数据卷保留。
#   --port       指定对外端口（更新时也可用来修改端口）
#   --force      版本相同（或低于当前版本）也重新创建容器
#   --mirror     从国内镜像站 ghcr.nju.edu.cn 拉取镜像；已用镜像站部署的容器更新时自动沿用
#   --uninstall  删除容器和镜像，保留数据卷
#   --purge      与 --uninstall 一起使用，同时删除数据卷（所有数据）

set -euo pipefail
umask 077

VERSION="0.6.0-rc.7"
REGISTRY="ghcr.io"
MIRROR_REGISTRY="ghcr.nju.edu.cn"
REPO="moran1024a/ictl-server"
NAME="ictl-server"
VOLUME="ictl-data"
PORT=""
FORCE=0
UNINSTALL=0
PURGE=0

die() {
  echo "错误：$*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) [ $# -ge 2 ] || die "--port 缺少参数"; PORT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    --mirror) REGISTRY="$MIRROR_REGISTRY"; shift ;;
    *) die "未知参数：$1" ;;
  esac
done
[ "$PURGE" -eq 0 ] || [ "$UNINSTALL" -eq 1 ] || die "--purge 需要与 --uninstall 一起使用"
if [ -n "$PORT" ]; then
  [[ "$PORT" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$PORT" -le 65535 ] || die "端口无效：$PORT"
fi

command -v docker >/dev/null 2>&1 ||
  die "未找到 docker 命令。请先安装 Docker：https://docs.docker.com/engine/install/"
if ! out="$(docker info 2>&1)"; then
  echo "$out" | tail -n 3 >&2
  die "无法连接 Docker 服务：请确认 Docker 已启动（systemctl start docker），并使用 root 或 docker 组用户运行。"
fi

if [ "$UNINSTALL" -eq 1 ]; then
  if image="$(docker container inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null)"; then
    docker rm -f "$NAME" >/dev/null
    echo "已删除容器 $NAME"
    docker rmi "$image" >/dev/null 2>&1 && echo "已删除镜像 $image"
  else
    echo "未找到容器 $NAME"
  fi
  if [ "$PURGE" -eq 1 ]; then
    if docker volume rm "$VOLUME" >/dev/null 2>&1; then
      echo "已删除数据卷 $VOLUME，所有数据已清除"
    else
      echo "未找到数据卷 $VOLUME"
    fi
  elif docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    echo "数据卷 $VOLUME 已保留，重新部署后可继续使用；彻底删除请加 --purge"
  fi
  exit 0
fi

# Stable releases sort after RCs with the same numeric base.
version_gt() {
  local left="${1%%-*}" right="${2%%-*}"
  if [ "$left" != "$right" ]; then
    [ "$(printf '%s\n%s\n' "$left" "$right" | sort -V | tail -n 1)" = "$left" ]
  elif [ "$1" = "$2" ]; then return 1
  elif [[ "$1" != *-* ]]; then return 0
  elif [[ "$2" != *-* ]]; then return 1
  else [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$1" ]; fi
}

# 已有容器：读取版本和端口绑定
BIND_IP=""
if current_image="$(docker container inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null)"; then
  current="${current_image##*:}"
  if [ "$FORCE" -eq 0 ] && [ "$current" != "$VERSION" ] &&
    version_gt "$current" "$VERSION"; then
    die "当前版本 $current 比本脚本的版本 $VERSION 新，未做改动。若通过加速代理下载，可能拿到了缓存的旧脚本，请稍后重试；确需降级请加 --force。"
  fi
  [ "${current_image%%/*}" = "$MIRROR_REGISTRY" ] && REGISTRY="$MIRROR_REGISTRY"
  binding="$(docker container inspect -f '{{with index .HostConfig.PortBindings "8730/tcp"}}{{(index . 0).HostIp}}|{{(index . 0).HostPort}}{{end}}' "$NAME")"
  cur_ip="${binding%%|*}"
  cur_port="${binding##*|}"
  [ "$cur_ip" = "0.0.0.0" ] || [ "$cur_ip" = "::" ] && cur_ip=""
  BIND_IP="$cur_ip"
  if [ -z "$PORT" ] || [ "$PORT" = "$cur_port" ]; then
    PORT="$cur_port"
  else
    FORCE=1   # 修改端口需要重新创建容器
  fi
  if [ "$current" = "$VERSION" ] && [ "$FORCE" -eq 0 ]; then
    echo "ictl 服务端已是最新版本 $VERSION。如需重新创建容器，请加 --force。"
    exit 0
  fi
  if [ "$current" = "$VERSION" ]; then
    echo "ictl 服务端 $VERSION：重新创建容器"
  else
    echo "ictl 服务端：$current → $VERSION"
  fi
fi
PORT="${PORT:-8730}"
IMAGE="$REGISTRY/$REPO:$VERSION"

echo "拉取镜像 $IMAGE"
if ! docker pull "$IMAGE"; then
  [ "$REGISTRY" = "$MIRROR_REGISTRY" ] && die "镜像拉取失败，请检查网络能否访问 $MIRROR_REGISTRY。"
  die "镜像拉取失败，请检查网络能否访问 ghcr.io；国内服务器可加 --mirror 使用镜像站。"
fi

# Preflight the image and the actual existing /data mount before stopping the writer.
docker run --rm --network none --entrypoint python3 "$IMAGE" /app/ictl-server.py --help >/dev/null
DATA_MOUNT="$VOLUME:/data"
OLD_NAME="" BACKUP="" RESTORE_NEEDED=0
if docker container inspect "$NAME" >/dev/null 2>&1; then
  # This installer owns a standard container. Refuse custom deployments before
  # stopping anything rather than silently losing Compose/network/runtime options.
  {
    docker container inspect "$NAME"
    docker image inspect "$(docker container inspect -f '{{.Image}}' "$NAME")"
  } | docker run --rm -i --network none --entrypoint python3 "$IMAGE" -c '
import json, sys
raw = sys.stdin.read()
old, end = json.JSONDecoder().raw_decode(raw)
base = json.loads(raw[end:].strip())[0]
old = old[0]
config, host = old["Config"], old["HostConfig"]
reasons = []
for key in ("Env", "Entrypoint", "Cmd", "User", "WorkingDir", "Labels", "Healthcheck", "StopSignal"):
    actual, expected = config.get(key), base["Config"].get(key)
    if key == "Env":
        actual, expected = sorted(actual or []), sorted(expected or [])
    if (actual or None) != (expected or None):
        reasons.append(key)
networks = old.get("NetworkSettings", {}).get("Networks", {})
if host.get("NetworkMode", "default") not in ("default", "bridge") or set(networks) != {"bridge"}:
    reasons.append("网络")
mounts = old.get("Mounts", [])
if len(mounts) != 1 or mounts[0].get("Destination") != "/data":
    reasons.append("额外挂载")
ports = host.get("PortBindings") or {}
if set(ports) != {"8730/tcp"} or len(ports["8730/tcp"] or []) != 1:
    reasons.append("端口映射")
if host.get("RestartPolicy", {}).get("Name") != "unless-stopped":
    reasons.append("重启策略")
for key in ("Memory", "MemoryReservation", "MemorySwap", "NanoCpus", "CpuShares", "CpuQuota", "CpuPeriod",
            "CpusetCpus", "CpusetMems", "PidsLimit", "Privileged", "ReadonlyRootfs", "AutoRemove",
            "CapAdd", "CapDrop", "Devices", "DeviceRequests", "DeviceCgroupRules", "SecurityOpt", "Sysctls", "Ulimits",
            "Dns", "DnsOptions", "DnsSearch", "ExtraHosts", "Links", "VolumesFrom", "Tmpfs",
            "PidMode", "UTSMode", "UsernsMode", "CgroupParent", "GroupAdd", "Init", "OomKillDisable"):
    if host.get(key):
        reasons.append(key)
for key, defaults in (("Runtime", (None, "", "runc")), ("IpcMode", (None, "", "private")),
                      ("ShmSize", (None, 67108864)), ("MemorySwappiness", (None, -1))):
    if host.get(key) not in defaults:
        reasons.append(key)
if host.get("LogConfig", {}).get("Config"):
    reasons.append("日志选项")
if reasons:
    sys.exit("检测到自定义容器设置（" + "、".join(reasons) + "）；请沿用原 Docker Compose 或部署命令升级。原服务未停止。")
'
  mount="$(docker container inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Type}}|{{.Name}}|{{.Source}}|{{.RW}}{{end}}{{end}}' "$NAME")"
  IFS='|' read -r mount_type mount_name mount_source mount_rw <<< "$mount"
  [ "$mount_rw" = true ] || die "原容器 /data 不是可写挂载，未停止服务"
  case "$mount_type" in
    volume) [ -n "$mount_name" ] || die "无法确定原数据卷"; DATA_MOUNT="$mount_name:/data" ;;
    bind) [ -n "$mount_source" ] || die "无法确定原数据目录"; DATA_MOUNT="$mount_source:/data" ;;
    *) die "不支持的 /data 挂载类型：$mount_type，未停止服务" ;;
  esac
  db_env="$(docker container inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$NAME")"
  if printf '%s\n' "$db_env" | grep '^ICTL_DB=' | grep -qv '^ICTL_DB=/data/ictl.db$'; then
    die "原容器使用自定义数据库路径，请使用源码部署流程备份升级，未停止服务"
  fi
  OLD_NAME="$NAME-pre-update-$(date +%s)-$$"
  BACKUP="/data/ictl.db.pre-deploy-$(date +%s)-$$.bak"
fi

rollback() {
  trap - ERR INT TERM
  echo "升级失败，正在恢复旧容器及升级前数据库。" >&2
  # Never restore a database while the new process could still be writing it.
  if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "新容器最近日志：" >&2
    docker logs --tail 50 "$NAME" >&2 || true
    docker rm -f "$NAME" >/dev/null || die "停止新容器失败；未恢复数据库，备份：$BACKUP"
  fi
  if [ -n "$OLD_NAME" ]; then
    if [ "$RESTORE_NEEDED" -eq 1 ]; then
      docker run --rm --network none -v "$DATA_MOUNT" --entrypoint python3 "$IMAGE" -c '
import os, shutil, sys
backup=sys.argv[1]; db="/data/ictl.db"
if not os.path.isfile(backup): raise RuntimeError("missing rollback backup")
shutil.copyfile(backup, db+".restore")
os.chmod(db+".restore", 0o600)
os.replace(db+".restore", db)
for suffix in ("-wal", "-shm"):
    try: os.unlink(db+suffix)
    except FileNotFoundError: pass
' "$BACKUP" || die "数据库恢复失败，旧容器保持停止；请检查备份 $BACKUP"
    fi
    docker rename "$OLD_NAME" "$NAME" && docker start "$NAME" >/dev/null || die "恢复旧容器失败，数据库备份：$BACKUP"
    echo "已恢复旧容器；备份保留在 $BACKUP" >&2
  fi
  exit 1
}

if [ -n "$OLD_NAME" ]; then
  case "$current" in
    0.5.3) echo "升级仅保留节点及其分组、账号和访问地址；清空目标、拓扑、配置、订阅和历史。旧规则在节点同步后清理。升级前自动备份数据库。" ;;
    0.6.*) echo "升级前自动备份数据库。" ;;
    *) die "请先升级到 0.5.3，再安装此版本。" ;;
  esac
  docker stop "$NAME" >/dev/null
  if ! docker rename "$NAME" "$OLD_NAME"; then
    docker start "$NAME" >/dev/null
    die "无法保留原容器，升级取消"
  fi
  trap rollback ERR INT TERM
  docker run --rm --network none -v "$DATA_MOUNT" --entrypoint python3 "$IMAGE" -c '
import os, sqlite3, sys
os.umask(0o077)
path=sys.argv[1]
if os.path.exists(path): raise RuntimeError("backup already exists")
source=sqlite3.connect("file:/data/ictl.db?mode=ro",uri=True)
target=sqlite3.connect(path)
try: source.backup(target)
finally: target.close(); source.close()
os.chmod(path,0o600)
' "$BACKUP"
  RESTORE_NEEDED=1
else
  trap rollback ERR INT TERM
fi

docker run -d --name "$NAME" --restart unless-stopped \
  -p "${BIND_IP:+$BIND_IP:}$PORT:8730" -v "$DATA_MOUNT" "$IMAGE" >/dev/null

healthy=0 health_error=""
for _ in $(seq 1 40); do
  if health_error="$(docker exec "$NAME" python3 /app/ictl-server.py health 2>&1)"; then healthy=1; break; fi
  sleep 0.5
done
if [ "$healthy" -ne 1 ]; then
  printf '新容器健康检查失败：\n%s\n' "${health_error:-未返回错误详情}" >&2
  rollback
fi
trap - ERR INT TERM
[ -z "$OLD_NAME" ] || docker rm "$OLD_NAME" >/dev/null

echo
echo "ictl 服务端 $VERSION 已启动"
docker exec "$NAME" python3 /app/ictl-server.py info | sed "s#<服务器地址>:8730#<服务器IP>:$PORT#"
[ -z "$BACKUP" ] || echo "  升级前数据库备份：$BACKUP（人工回退须同时恢复旧版本镜像和此备份）"
echo "  检查更新：重新运行本脚本"
echo "  查看日志：docker logs $NAME"
echo "  重置账号：docker exec $NAME python3 /app/ictl-server.py reset-admin"
echo "  卸载：重新运行本脚本并加 --uninstall（加 --purge 同时删除数据）"
