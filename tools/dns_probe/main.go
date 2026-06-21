package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

type jsonLogger struct {
	path string
	mu   sync.Mutex
}

func newJSONLogger(path string) (*jsonLogger, error) {
	if i := strings.LastIndex(path, "/"); i > 0 {
		if err := os.MkdirAll(path[:i], 0o755); err != nil {
			return nil, err
		}
	}
	return &jsonLogger{path: path}, nil
}

func (l *jsonLogger) write(event map[string]any) {
	event["ts"] = time.Now().UTC().Format(time.RFC3339Nano)
	line, err := json.Marshal(event)
	if err != nil {
		line, _ = json.Marshal(map[string]any{
			"ts":    time.Now().UTC().Format(time.RFC3339Nano),
			"kind":  "json_marshal_error",
			"error": err.Error(),
		})
	}

	l.mu.Lock()
	defer l.mu.Unlock()

	if f, err := os.OpenFile(l.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644); err == nil {
		_, _ = f.Write(append(line, '\n'))
		_ = f.Close()
	}
	_, _ = os.Stdout.Write(append(line, '\n'))
}

type config struct {
	interval      time.Duration
	timeout       time.Duration
	names         []string
	servers       []string
	httpURLs      []string
	logPath       string
	snapshotEvery int
}

func main() {
	cfg := loadConfig()
	log, err := newJSONLogger(cfg.logPath)
	if err != nil {
		panic(err)
	}

	log.write(map[string]any{
		"kind":           "probe_start",
		"go_version":     runtime.Version(),
		"goos":           runtime.GOOS,
		"goarch":         runtime.GOARCH,
		"pid":            os.Getpid(),
		"hostname":       hostname(),
		"interval":       cfg.interval.String(),
		"timeout":        cfg.timeout.String(),
		"names":          cfg.names,
		"servers":        cfg.servers,
		"http_urls":      cfg.httpURLs,
		"snapshot_every": cfg.snapshotEvery,
		"godebug":        os.Getenv("GODEBUG"),
	})

	for seq := 1; ; seq++ {
		if seq == 1 || seq%cfg.snapshotEvery == 0 {
			log.write(map[string]any{
				"kind":        "snapshot",
				"seq":         seq,
				"resolv_conf": readTextFile("/etc/resolv.conf"),
				"nsswitch":    readTextFile("/etc/nsswitch.conf"),
				"servers":     expandServers(cfg.servers),
			})
		}

		for _, name := range cfg.names {
			ctx, cancel := context.WithTimeout(context.Background(), cfg.timeout)
			event := lookupHost(ctx, net.DefaultResolver, "go_default_lookup_host", name)
			event["seq"] = seq
			log.write(event)
			cancel()

			for _, server := range expandServers(cfg.servers) {
				ctx, cancel := context.WithTimeout(context.Background(), cfg.timeout)
				event := lookupHost(ctx, resolverForServer(server, cfg.timeout), "go_direct_lookup_host", name)
				event["seq"] = seq
				event["server"] = server
				log.write(event)
				cancel()
			}
		}

		for _, url := range cfg.httpURLs {
			ctx, cancel := context.WithTimeout(context.Background(), cfg.timeout+5*time.Second)
			event := httpGet(ctx, url, cfg.timeout)
			event["seq"] = seq
			log.write(event)
			cancel()
		}

		time.Sleep(cfg.interval)
	}
}

func loadConfig() config {
	return config{
		interval: secondsEnv("DNS_PROBE_INTERVAL", "5"),
		timeout:  secondsEnv("DNS_PROBE_TIMEOUT", "2"),
		names: splitCSV("DNS_PROBE_NAMES",
			"api.fastmail.com,api.fastmail.com.,desktop-linux.myth-dab.ts.net,desktop-linux.myth-dab.ts.net."),
		servers: splitCSV("DNS_PROBE_SERVERS", "resolv-conf,127.0.0.53,100.100.100.100,10.2.2.3"),
		httpURLs: splitCSV("DNS_PROBE_HTTP_URLS",
			"https://api.fastmail.com/.well-known/oauth-authorization-server"),
		logPath:       env("DNS_PROBE_LOG", "/logs/dns-probe.jsonl"),
		snapshotEvery: intEnv("DNS_PROBE_SNAPSHOT_EVERY", 12),
	}
}

func env(name, def string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return def
}

func splitCSV(name, def string) []string {
	var out []string
	for _, part := range strings.Split(env(name, def), ",") {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

func secondsEnv(name, def string) time.Duration {
	value, err := strconv.ParseFloat(env(name, def), 64)
	if err != nil || value <= 0 {
		value, _ = strconv.ParseFloat(def, 64)
	}
	return time.Duration(value * float64(time.Second))
}

func intEnv(name string, def int) int {
	value, err := strconv.Atoi(env(name, strconv.Itoa(def)))
	if err != nil || value <= 0 {
		return def
	}
	return value
}

func readTextFile(path string) map[string]any {
	data, err := os.ReadFile(path)
	if err != nil {
		return map[string]any{"path": path, "ok": false, "error": err.Error()}
	}
	text := string(data)
	truncated := false
	if len(text) > 20000 {
		text = text[:20000]
		truncated = true
	}
	return map[string]any{"path": path, "ok": true, "text": text, "truncated": truncated}
}

func resolvConfServers() []string {
	data, err := os.ReadFile("/etc/resolv.conf")
	if err != nil {
		return nil
	}
	var servers []string
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[0] == "nameserver" {
			servers = append(servers, fields[1])
		}
	}
	return servers
}

func expandServers(specs []string) []string {
	var out []string
	seen := map[string]bool{}
	add := func(server string) {
		server = strings.TrimSpace(server)
		if server != "" && !seen[server] {
			seen[server] = true
			out = append(out, server)
		}
	}
	for _, spec := range specs {
		if spec == "resolv-conf" {
			for _, server := range resolvConfServers() {
				add(server)
			}
		} else {
			add(spec)
		}
	}
	return out
}

func lookupHost(ctx context.Context, resolver *net.Resolver, kind string, name string) map[string]any {
	start := time.Now()
	event := map[string]any{"kind": kind, "name": name}
	addrs, err := resolver.LookupHost(ctx, name)
	event["elapsed_ms"] = float64(time.Since(start).Microseconds()) / 1000
	if err != nil {
		event["ok"] = false
		addErrorFields(event, err)
		return event
	}
	event["ok"] = true
	event["addresses"] = addrs
	return event
}

func resolverForServer(server string, timeout time.Duration) *net.Resolver {
	return &net.Resolver{
		PreferGo: true,
		Dial: func(ctx context.Context, network string, address string) (net.Conn, error) {
			dialer := net.Dialer{Timeout: timeout}
			return dialer.DialContext(ctx, "udp", net.JoinHostPort(server, "53"))
		},
	}
}

func httpGet(ctx context.Context, url string, timeout time.Duration) map[string]any {
	start := time.Now()
	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   timeout,
			KeepAlive: 30 * time.Second,
			Resolver:  net.DefaultResolver,
		}).DialContext,
		TLSHandshakeTimeout: timeout,
		TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12},
	}
	client := &http.Client{Transport: transport, Timeout: timeout + 5*time.Second}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return map[string]any{"kind": "http_get", "url": url, "ok": false, "error": err.Error()}
	}
	resp, err := client.Do(req)
	event := map[string]any{"kind": "http_get", "url": url, "elapsed_ms": float64(time.Since(start).Microseconds()) / 1000}
	if err != nil {
		event["ok"] = false
		addErrorFields(event, err)
		return event
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
	event["ok"] = true
	event["status"] = resp.Status
	event["status_code"] = resp.StatusCode
	event["body_prefix"] = string(body)
	return event
}

func addErrorFields(event map[string]any, err error) {
	event["error"] = err.Error()
	event["error_type"] = reflect.TypeOf(err).String()

	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		event["dns_error_name"] = dnsErr.Name
		event["dns_error_server"] = dnsErr.Server
		event["dns_error_is_timeout"] = dnsErr.IsTimeout
		event["dns_error_is_temporary"] = dnsErr.IsTemporary
		event["dns_error_is_not_found"] = dnsErr.IsNotFound
	}

	var opErr *net.OpError
	if errors.As(err, &opErr) {
		event["op"] = opErr.Op
		event["network"] = opErr.Net
		if opErr.Addr != nil {
			event["address"] = opErr.Addr.String()
		}
	}
}

func hostname() string {
	name, err := os.Hostname()
	if err != nil {
		return ""
	}
	return name
}
