FROM --platform=$BUILDPLATFORM golang:1.27.3-alpine AS tunnel-builder
WORKDIR /src
COPY tunnel-agent ./
RUN mkdir /assets && for arch in amd64 arm64; do \
    CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -trimpath -buildvcs=false \
    -ldflags='-s -w -buildid=' -o /assets/tunnel-agent-linux-$arch .; done

FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/moran1024a/iptables-control"
LABEL org.opencontainers.image.description="ictl server: iptables 转发中心化管理面板"

RUN apk add --no-cache openssl

WORKDIR /app
COPY server/ictl-server.py ./
COPY server/tunnel_config.py server/components.json ./
COPY server/web ./web
COPY agent/ictl.sh ./agent/ictl.sh
COPY --from=tunnel-builder /assets ./assets

ENV ICTL_DB=/data/ictl.db \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8730

CMD ["python3", "/app/ictl-server.py", "serve"]
