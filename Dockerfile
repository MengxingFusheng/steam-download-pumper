FROM golang:1.23 AS discarder-builder

WORKDIR /src
COPY go.mod ./
COPY cmd/discarder ./cmd/discarder
RUN go test ./cmd/discarder \
    && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/discarder ./cmd/discarder

FROM python:3.13-slim

ARG DEFAULT_EGRESS_MODE=single_ip
ARG DEFAULT_LINE_COUNT=2
ARG DEFAULT_LAN_IP=192.168.1.233
ARG DEFAULT_LAN_IPS=192.168.1.233

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY steam_pumper /app/steam_pumper
COPY --from=discarder-builder /out/discarder /usr/local/bin/discarder

RUN mkdir -p /data /tmp /run \
    && chmod 1777 /tmp

ENV CONFIG_PATH=/data/config.json \
    WEB_PORT=80 \
    EGRESS_MODE=${DEFAULT_EGRESS_MODE} \
    LINE_COUNT=${DEFAULT_LINE_COUNT} \
    LAN_IP=${DEFAULT_LAN_IP} \
    LAN_IPS=${DEFAULT_LAN_IPS} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_COLORS=0 \
    NO_COLOR=1 \
    TERM=dumb \
    GOMAXPROCS=2 \
    TZ=Asia/Shanghai

EXPOSE 80

CMD ["python3", "-m", "steam_pumper.main"]
