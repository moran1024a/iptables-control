#!/usr/bin/env bash
# ictl 服务端一键部署（Docker）。重复运行即更新到本脚本对应的版本，数据保留。
#
#   bash install-server.sh [--port 8730] [--trust-proxy]
#
#   --port         对外端口，默认 8730
#   --trust-proxy  服务端位于反向代理之后时使用，信任 X-Forwarded-For 获取真实来源 IP

set -euo pipefail

VERSION="0.1.0"
IMAGE="ghcr.io/moran1024a/ictl-server:$VERSION"
NAME="ictl-server"
VOLUME="ictl-data"
PORT=8730
TRUST_PROXY=0

die() {
  echo "错误：$*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:-}"; shift 2 ;;
    --trust-proxy) TRUST_PROXY=1; shift ;;
    *) die "未知参数：$1" ;;
  esac
done
[[ "$PORT" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$PORT" -le 65535 ] || die "端口无效：$PORT"

command -v docker >/dev/null 2>&1 ||
  die "未找到 docker 命令。请先安装 Docker：https://docs.docker.com/engine/install/"
if ! out="$(docker info 2>&1)"; then
  echo "$out" | tail -n 3 >&2
  die "无法连接 Docker 服务：请确认 Docker 已启动（systemctl start docker），并使用 root 或 docker 组用户运行。"
fi

echo "拉取镜像 $IMAGE"
docker pull "$IMAGE" || die "镜像拉取失败，请检查网络能否访问 ghcr.io。"

if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "替换已有容器 $NAME（数据卷 $VOLUME 保留）"
  docker rm -f "$NAME" >/dev/null
fi

env_args=()
[ "$TRUST_PROXY" -eq 1 ] && env_args=(-e ICTL_TRUST_PROXY=1)
docker run -d --name "$NAME" --restart unless-stopped \
  -p "$PORT:8730" -v "$VOLUME:/data" ${env_args[@]+"${env_args[@]}"} "$IMAGE" >/dev/null

# 等待首次初始化完成
for _ in $(seq 1 20); do
  docker exec "$NAME" python3 /app/ictl-server.py info >/dev/null 2>&1 && break
  sleep 0.5
done

echo
echo "ictl 服务端 $VERSION 已启动"
docker exec "$NAME" python3 /app/ictl-server.py info | sed "s#<服务器地址>:8730#<服务器IP>:$PORT#"
echo
echo "  查看日志：docker logs $NAME"
echo "  重置账号：docker exec $NAME python3 /app/ictl-server.py reset-admin"
echo "  停止删除：docker rm -f $NAME（数据保存在卷 $VOLUME 中）"
