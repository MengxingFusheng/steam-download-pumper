FROM golang:1.23 AS discarder-builder

WORKDIR /src
COPY go.mod ./
COPY cmd/discarder ./cmd/discarder
RUN go test ./cmd/discarder \
    && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/discarder ./cmd/discarder

FROM cm2network/steamcmd:root

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 trickle ca-certificates iproute2 \
    && mkdir -p /usr/lib/trickle \
    && ln -sf /usr/lib/x86_64-linux-gnu/trickle/trickle-overload.so /usr/lib/trickle/trickle-overload.so \
    && ln -sf /usr/lib/x86_64-linux-gnu/trickle/libtrickle.so /usr/lib/trickle/libtrickle.so \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY steam_pumper /app/steam_pumper
COPY --from=discarder-builder /out/discarder /usr/local/bin/discarder

RUN mkdir -p /data /steam/downloads /tmp /run \
    && chown -R steam:steam /data /steam /app /tmp /run \
    && chmod 1777 /tmp /steam/downloads

USER root

ENV CONFIG_PATH=/data/config.json \
    WEB_PORT=80 \
    STEAMCMD_BIN=/home/steam/steamcmd/steamcmd.sh \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

EXPOSE 80

ENTRYPOINT ["python3", "-m", "steam_pumper.main"]
