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
	"sync"
	"syscall"
	"time"
)

type options struct {
	workerID             string
	connections          int
	maxConnections       int
	minSessionSeconds    int
	startupJitterSeconds float64
	restartJitterSeconds float64
	readTimeoutSeconds   int
	bindIP               string
	urls                 []string
}

type idleTimeoutConn struct {
	net.Conn
	timeout time.Duration
}

type sourceHealth struct {
	mu         sync.Mutex
	failures   map[string]int
	retryAfter map[string]time.Time
}

func newSourceHealth() *sourceHealth {
	return &sourceHealth{failures: make(map[string]int), retryAfter: make(map[string]time.Time)}
}

func (health *sourceHealth) ready(url string, now time.Time) bool {
	health.mu.Lock()
	defer health.mu.Unlock()
	return !now.Before(health.retryAfter[url])
}

func (health *sourceHealth) failed(url string, now time.Time) time.Duration {
	health.mu.Lock()
	defer health.mu.Unlock()
	health.failures[url]++
	delay := retryDelay(health.failures[url])
	health.retryAfter[url] = now.Add(delay)
	return delay
}

func (health *sourceHealth) recovered(url string) bool {
	health.mu.Lock()
	defer health.mu.Unlock()
	hadFailures := health.failures[url] > 0
	delete(health.failures, url)
	delete(health.retryAfter, url)
	return hadFailures
}

func (conn *idleTimeoutConn) Read(buffer []byte) (int, error) {
	if conn.timeout > 0 {
		_ = conn.SetReadDeadline(time.Now().Add(conn.timeout))
	}
	return conn.Conn.Read(buffer)
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
	flag.IntVar(&opts.connections, "connections", 1, "initial concurrent downloads")
	flag.IntVar(&opts.maxConnections, "max-connections", 12, "maximum concurrent downloads")
	flag.IntVar(&opts.minSessionSeconds, "min-session-seconds", 300, "minimum intended worker session duration")
	flag.Float64Var(&opts.startupJitterSeconds, "startup-jitter-seconds", 0, "maximum jitter before the first request")
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
	if opts.connections < 1 {
		opts.connections = 1
	}
	if opts.maxConnections < opts.connections {
		opts.maxConnections = opts.connections
	}
	type workerResult struct {
		id  int
		err error
	}
	results := make(chan workerResult, opts.maxConnections+1)
	workers := make(map[int]context.CancelFunc, opts.maxConnections)
	health := newSourceHealth()
	startWorker := func(id int) {
		workerCtx, cancel := context.WithCancel(ctx)
		workers[id] = cancel
		go func() {
			results <- workerResult{id: id, err: runWorker(workerCtx, opts, id, health)}
		}()
	}
	for id := 1; id <= opts.connections; id++ {
		startWorker(id)
	}
	scaleSignals := make(chan os.Signal, 16)
	signal.Notify(scaleSignals, syscall.SIGUSR1, syscall.SIGUSR2)
	defer signal.Stop(scaleSignals)
	for {
		select {
		case <-ctx.Done():
			for _, cancel := range workers {
				cancel()
			}
			return nil
		case scaleSignal := <-scaleSignals:
			if scaleSignal == syscall.SIGUSR1 && len(workers) < opts.maxConnections {
				for id := 1; id <= opts.maxConnections; id++ {
					if _, exists := workers[id]; !exists {
						startWorker(id)
						break
					}
				}
			} else if scaleSignal == syscall.SIGUSR2 && len(workers) > 1 {
				for id := opts.maxConnections; id >= 1; id-- {
					if cancel, exists := workers[id]; exists {
						delete(workers, id)
						cancel()
						break
					}
				}
			}
		case result := <-results:
			if _, active := workers[result.id]; !active {
				continue
			}
			delete(workers, result.id)
			if ctx.Err() != nil {
				return nil
			}
			if result.err != nil {
				fmt.Fprintf(os.Stderr, "connection=%d stopped: %v\n", result.id, result.err)
			}
			startWorker(result.id)
		}
	}
}

func runWorker(ctx context.Context, opts options, connectionID int, health *sourceHealth) error {
	timeout := time.Duration(opts.readTimeoutSeconds) * time.Second
	client := newHTTPClient(timeout, opts.bindIP)
	urlIndex := connectionID - 1
	failures := 0
	startedAt := time.Now()
	if opts.startupJitterSeconds > 0 {
		jitter := time.Duration(rand.Float64() * opts.startupJitterSeconds * float64(time.Second))
		if err := sleepWithContext(ctx, jitter); err != nil {
			return nil
		}
	}
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}
		url := ""
		for checked := 0; checked < len(opts.urls); checked++ {
			candidate := opts.urls[urlIndex%len(opts.urls)]
			urlIndex++
			if health.ready(candidate, time.Now()) {
				url = candidate
				break
			}
		}
		if url == "" {
			if err := sleepWithContext(ctx, 250*time.Millisecond); err != nil {
				return nil
			}
			continue
		}
		workerID := fmt.Sprintf("%s-%d", opts.workerID, connectionID)
		if _, err := downloadOnce(ctx, client, url, workerID); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			failures++
			delay := health.failed(url, time.Now())
			fmt.Fprintf(os.Stderr, "worker=%s url=%s error=%v retry_in=%s\n", workerID, url, err, delay)
			if failures >= 12 && time.Since(startedAt) >= time.Duration(opts.minSessionSeconds)*time.Second {
				return fmt.Errorf("all sources remained unavailable after %d attempts", failures)
			}
			continue
		}
		if health.recovered(url) {
			fmt.Fprintf(os.Stderr, "worker=%s url=%s recovered=true\n", workerID, url)
		}
		failures = 0
		if opts.restartJitterSeconds > 0 {
			jitter := time.Duration(rand.Float64() * opts.restartJitterSeconds * float64(time.Second))
			if err := sleepWithContext(ctx, jitter); err != nil {
				return nil
			}
		}
	}
}

func retryDelay(failures int) time.Duration {
	if failures < 1 {
		return 0
	}
	shift := min(failures-1, 5)
	delay := time.Second * time.Duration(1<<shift)
	if delay > 30*time.Second {
		return 30 * time.Second
	}
	return delay
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
		conn, err := dialer.DialContext(ctx, "tcp4", address)
		if err != nil {
			return nil, err
		}
		return &idleTimeoutConn{Conn: conn, timeout: timeout}, nil
	}
	transport.ForceAttemptHTTP2 = false
	transport.DisableCompression = true
	transport.ResponseHeaderTimeout = timeout
	transport.TLSHandshakeTimeout = timeout
	return &http.Client{Transport: transport}
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
	buffer := make([]byte, 64*1024)
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
