FROM --platform=$BUILDPLATFORM golang:1.27.3-alpine AS agent-build
WORKDIR /src
COPY agent/active/go.mod agent/active/go.sum ./
RUN go mod download
COPY agent/active/*.go ./
RUN mkdir /out && for arch in amd64 arm64; do \
    CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -trimpath -ldflags='-s -w' -o /out/ictl-active-linux-$arch .; \
    done

FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/moran1024a/iptables-control"
LABEL org.opencontainers.image.description="ictl server: nftables / iptables 转发管理面板"

RUN apk add --no-cache openssl

WORKDIR /app
COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server/*.py server/components.json ./
COPY server/web ./web
COPY agent/ictl.sh ./agent/ictl.sh
COPY agent/passive.sh agent/manage.sh agent/control.sh ./agent/
COPY --from=agent-build /out/ ./assets/

ENV ICTL_DB=/data/ictl.db \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8730

CMD ["python3", "/app/ictl-server.py", "serve"]
