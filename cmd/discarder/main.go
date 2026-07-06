package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

type options struct {
	workerID             string
	minSessionSeconds    int
	restartJitterSeconds float64
	readTimeoutSeconds   int
	bindIP               string
	urls                 []string
}

func main() {
	opts := parseOptions()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := run(ctx, opts); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func parseOptions() options {
	var opts options
	flag.StringVar(&opts.workerID, "worker-id", "0", "worker id for user-agent")
	flag.IntVar(&opts.minSessionSeconds, "min-session-seconds", 300, "minimum intended worker session duration")
	flag.Float64Var(&opts.restartJitterSeconds, "restart-jitter-seconds", 3, "maximum jitter after each short download")
	flag.IntVar(&opts.readTimeoutSeconds, "read-timeout-seconds", 30, "HTTP client timeout per request")
	flag.StringVar(&opts.bindIP, "bind-ip", "", "local IPv4 address to bind outbound connections")
	flag.Parse()
	opts.urls = flag.Args()
	if len(opts.urls) == 0 {
		fmt.Fprintln(os.Stderr, "at least one URL is required")
		os.Exit(2)
	}
	return opts
}

func run(ctx context.Context, opts options) error {
	timeout := time.Duration(opts.readTimeoutSeconds) * time.Second
	client := newHTTPClient(timeout, opts.bindIP)
	urlIndex := 0
	failures := 0
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}
		url := opts.urls[urlIndex%len(opts.urls)]
		urlIndex++
		if _, err := downloadOnce(ctx, client, url, opts.workerID); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			failures++
			fmt.Fprintf(os.Stderr, "worker=%s url=%s error=%v\n", opts.workerID, url, err)
			if err := sleepWithContext(ctx, time.Second); err != nil {
				return nil
			}
			continue
		}
		failures = 0
		if opts.restartJitterSeconds > 0 {
			jitter := time.Duration(rand.Float64() * opts.restartJitterSeconds * float64(time.Second))
			if err := sleepWithContext(ctx, jitter); err != nil {
				return nil
			}
		}
		if failures > 100 && opts.minSessionSeconds <= 0 {
			return fmt.Errorf("too many consecutive failures")
		}
	}
}

func newHTTPClient(timeout time.Duration, bindIP string) *http.Client {
	dialer := &net.Dialer{
		Timeout:   timeout,
		KeepAlive: 30 * time.Second,
	}
	if bindIP != "" {
		parsedIP := net.ParseIP(bindIP)
		if parsedIP == nil || parsedIP.To4() == nil {
			fmt.Fprintf(os.Stderr, "invalid bind-ip %q\n", bindIP)
			os.Exit(2)
		}
		dialer.LocalAddr = &net.TCPAddr{IP: parsedIP}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = func(ctx context.Context, _network, address string) (net.Conn, error) {
		return dialer.DialContext(ctx, "tcp4", address)
	}
	transport.ForceAttemptHTTP2 = false
	return &http.Client{Transport: transport, Timeout: timeout}
}

func downloadOnce(ctx context.Context, client *http.Client, url string, workerID string) (int64, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	request.Header.Set("User-Agent", "steam-download-pumper/"+workerID)
	response, err := client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 400 {
		return 0, fmt.Errorf("unexpected HTTP status %s", response.Status)
	}
	buffer := make([]byte, 1024*1024)
	return io.CopyBuffer(io.Discard, response.Body, buffer)
}

func sleepWithContext(ctx context.Context, duration time.Duration) error {
	if duration <= 0 {
		return nil
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
