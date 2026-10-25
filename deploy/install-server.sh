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

VERSION="0.1.4"
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
    --port) PORT="${2:-}"; shift 2 ;;
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

# 已有容器：读取版本和端口绑定
BIND_IP=""
if current_image="$(docker container inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null)"; then
  current="${current_image##*:}"
  if [ "$FORCE" -eq 0 ] && [ "$current" != "$VERSION" ] &&
    [ "$(printf '%s\n%s\n' "$current" "$VERSION" | sort -V | tail -n 1)" = "$current" ]; then
    die "当前版本 $current 比本脚本的版本 $VERSION 新，未做改动。若通过加速代理下载，可能拿到了缓存的旧脚本，请稍后重试；确需降级请加 --force。"
  fi
  [ "${current_image%%/*}" = "$MIRROR_REGISTRY" ] && REGISTRY="$MIRROR_REGISTRY"
  binding="$(docker container inspect -f '{{with index .HostConfig.PortBindings "8730/tcp"}}{{(index . 0).HostIp}}|{{(index . 0).HostPort}}{{end}}' "$NAME")"
  cur_ip="${binding%%|*}"
  cur_port="${binding##*|}"
  [ "$cur_ip" = "0.0.0.0" ] || [ "$cur_ip" = "::" ] && cur_ip=""
  if [ -z "$PORT" ] || [ "$PORT" = "$cur_port" ]; then
    PORT="$cur_port"
    BIND_IP="$cur_ip"
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

if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "替换已有容器 $NAME（数据卷 $VOLUME 保留）"
  docker rm -f "$NAME" >/dev/null
fi

docker run -d --name "$NAME" --restart unless-stopped \
  -p "${BIND_IP:+$BIND_IP:}$PORT:8730" -v "$VOLUME:/data" "$IMAGE" >/dev/null

# 等待初始化完成
for _ in $(seq 1 20); do
  docker exec "$NAME" python3 /app/ictl-server.py info >/dev/null 2>&1 && break
  sleep 0.5
done

echo
echo "ictl 服务端 $VERSION 已启动"
docker exec "$NAME" python3 /app/ictl-server.py info | sed "s#<服务器地址>:8730#<服务器IP>:$PORT#"
echo
echo "  检查更新：重新运行本脚本"
echo "  查看日志：docker logs $NAME"
echo "  重置账号：docker exec $NAME python3 /app/ictl-server.py reset-admin"
echo "  卸载：    重新运行本脚本并加 --uninstall（加 --purge 同时删除数据）"
