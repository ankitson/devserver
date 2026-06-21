FROM golang:1.23-alpine AS build

WORKDIR /app
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/dns-probe .

FROM alpine:3.21

ENV DNS_PROBE_LOG=/logs/dns-probe.jsonl
ENV DNS_PROBE_INTERVAL=5
ENV GODEBUG=netdns=go+1

COPY --from=build /out/dns-probe /usr/local/bin/dns-probe

ENTRYPOINT ["dns-probe"]
