FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/moran1024a/iptables-control"
LABEL org.opencontainers.image.description="ictl server: iptables 转发中心化管理面板"

RUN apk add --no-cache openssl

WORKDIR /app
COPY server/ictl-server.py ./
COPY server/web ./web
COPY agent/ictl.sh ./agent/ictl.sh

ENV ICTL_DB=/data/ictl.db \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8730

CMD ["python3", "/app/ictl-server.py", "serve"]
