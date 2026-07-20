package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type options struct {
	workerID              string
	lineID                string
	connections           int
	maxConnections        int
	minSessionSeconds     int
	startupJitterSeconds  float64
	restartJitterSeconds  float64
	readTimeoutSeconds    int
	statusIntervalSeconds int
	bindIP                string
	urls                  []string
	statusWriter          io.Writer
}

const maxConnectionLimit = 12

type statusEvent struct {
	Type                string `json:"type"`
	LineID              string `json:"line_id"`
	BindIP              string `json:"bind_ip,omitempty"`
	Bytes               int64  `json:"bytes"`
	Connections         int32  `json:"connections"`
	URL                 string `json:"url,omitempty"`
	State               string `json:"state,omitempty"`
	ConsecutiveFailures int    `json:"consecutive_failures,omitempty"`
	RetryAfter          string `json:"retry_after,omitempty"`
	RetryInSeconds      int64  `json:"retry_in_seconds,omitempty"`
	Error               string `json:"error,omitempty"`
	Recovered           bool   `json:"recovered,omitempty"`
}

type countingWriter struct {
	total *atomic.Int64
}

type statusSink struct {
	mu     sync.Mutex
	writer io.Writer
}

type idleTimeoutConn struct {
	net.Conn
	timeout time.Duration
}

type sourceHealth struct {
	mu     sync.Mutex
	states map[string]*sourceState
}

type sourceState struct {
	consecutiveFailures int
	quarantineLevel     int
	retryAfter          time.Time
	probeInFlight       bool
	lastError           string
}

type sourceSnapshot struct {
	State               string
	ConsecutiveFailures int
	RetryAfter          time.Time
	RetryIn             time.Duration
	LastError           string
}

func newSourceHealth() *sourceHealth {
	return &sourceHealth{states: make(map[string]*sourceState)}
}

func (health *sourceHealth) claim(url string, now time.Time) (bool, time.Duration, bool) {
	health.mu.Lock()
	defer health.mu.Unlock()
	state := health.states[url]
	if state == nil {
		return true, 0, false
	}
	if now.Before(state.retryAfter) {
		return false, state.retryAfter.Sub(now), false
	}
	if state.quarantineLevel == 0 {
		return true, 0, false
	}
	if state.probeInFlight {
		return false, 250 * time.Millisecond, false
	}
	state.probeInFlight = true
	return true, 0, true
}

func (health *sourceHealth) failed(url string, now time.Time, lastError string, wasProbe bool) sourceSnapshot {
	health.mu.Lock()
	defer health.mu.Unlock()
	state := health.states[url]
	if state == nil {
		state = &sourceState{}
		health.states[url] = state
	}
	state.consecutiveFailures++
	state.lastError = lastError
	if wasProbe {
		state.probeInFlight = false
		state.quarantineLevel = min(max(state.quarantineLevel+1, 1), 3)
		state.retryAfter = now.Add(quarantineDelay(state.quarantineLevel))
	} else if state.consecutiveFailures >= 3 {
		if state.quarantineLevel == 0 {
			state.quarantineLevel = 1
			state.retryAfter = now.Add(quarantineDelay(state.quarantineLevel))
		}
	} else {
		state.retryAfter = now.Add(retryDelay(state.consecutiveFailures))
	}
	return snapshotFor(state, now)
}

func (health *sourceHealth) succeeded(url string) (bool, sourceSnapshot) {
	health.mu.Lock()
	defer health.mu.Unlock()
	_, hadFailures := health.states[url]
	delete(health.states, url)
	return hadFailures, sourceSnapshot{State: "healthy"}
}

func (health *sourceHealth) releaseProbe(url string) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if state := health.states[url]; state != nil {
		state.probeInFlight = false
	}
}

func (health *sourceHealth) snapshot(url string, now time.Time) sourceSnapshot {
	health.mu.Lock()
	defer health.mu.Unlock()
	return snapshotFor(health.states[url], now)
}

func snapshotFor(state *sourceState, now time.Time) sourceSnapshot {
	if state == nil {
		return sourceSnapshot{State: "healthy"}
	}
	status := "degraded"
	if state.quarantineLevel > 0 {
		status = "quarantined"
	}
	if state.probeInFlight {
		status = "probing"
	}
	retryIn := state.retryAfter.Sub(now)
	if retryIn < 0 {
		retryIn = 0
	}
	return sourceSnapshot{
		State:               status,
		ConsecutiveFailures: state.consecutiveFailures,
		RetryAfter:          state.retryAfter,
		RetryIn:             retryIn,
		LastError:           state.lastError,
	}
}

func (conn *idleTimeoutConn) Read(buffer []byte) (int, error) {
	if conn.timeout > 0 {
		_ = conn.SetReadDeadline(time.Now().Add(conn.timeout))
	}
	return conn.Conn.Read(buffer)
}

func (writer countingWriter) Write(buffer []byte) (int, error) {
	writer.total.Add(int64(len(buffer)))
	return len(buffer), nil
}

func writeStatus(output io.Writer, event statusEvent) error {
	return json.NewEncoder(output).Encode(event)
}

func (sink *statusSink) emit(event statusEvent) {
	if sink == nil || sink.writer == nil {
		return
	}
	sink.mu.Lock()
	defer sink.mu.Unlock()
	_ = writeStatus(sink.writer, event)
}

func main() {
	opts := parseOptions()
	if err := validateOptions(&opts); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
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
	flag.StringVar(&opts.lineID, "line-id", "line-1", "logical line id for status output")
	flag.IntVar(&opts.connections, "connections", 1, "initial concurrent downloads")
	flag.IntVar(&opts.maxConnections, "max-connections", 12, "maximum concurrent downloads")
	flag.IntVar(&opts.minSessionSeconds, "min-session-seconds", 300, "minimum intended worker session duration")
	flag.Float64Var(&opts.startupJitterSeconds, "startup-jitter-seconds", 0, "maximum jitter before the first request")
	flag.Float64Var(&opts.restartJitterSeconds, "restart-jitter-seconds", 3, "maximum jitter after each short download")
	flag.IntVar(&opts.readTimeoutSeconds, "read-timeout-seconds", 30, "HTTP client timeout per request")
	flag.IntVar(&opts.statusIntervalSeconds, "status-interval-seconds", 1, "status output interval, or 0 to disable")
	flag.StringVar(&opts.bindIP, "bind-ip", "", "local IPv4 address to bind outbound connections")
	flag.Parse()
	opts.urls = flag.Args()
	return opts
}

func validateOptions(opts *options) error {
	if opts.connections == 0 {
		opts.connections = 1
	}
	if opts.maxConnections == 0 {
		opts.maxConnections = maxConnectionLimit
	}
	if opts.connections < 1 || opts.connections > maxConnectionLimit {
		return fmt.Errorf("connections must be between 1 and %d", maxConnectionLimit)
	}
	if opts.maxConnections < 1 || opts.maxConnections > maxConnectionLimit {
		return fmt.Errorf("max-connections must be between 1 and %d", maxConnectionLimit)
	}
	if opts.maxConnections < opts.connections {
		return fmt.Errorf("max-connections must be greater than or equal to connections")
	}
	if opts.statusIntervalSeconds < 0 {
		return fmt.Errorf("status-interval-seconds must be 0 or greater")
	}
	if opts.bindIP != "" {
		parsedIP := net.ParseIP(opts.bindIP)
		if parsedIP == nil || parsedIP.To4() == nil {
			return fmt.Errorf("bind-ip must be a valid IPv4 address")
		}
	}
	if len(opts.urls) == 0 {
		return fmt.Errorf("at least one URL is required")
	}
	if opts.lineID == "" {
		opts.lineID = "line-1"
	}
	return nil
}

func run(ctx context.Context, opts options) error {
	if err := validateOptions(&opts); err != nil {
		return err
	}
	writer := opts.statusWriter
	if writer == nil {
		writer = os.Stdout
	}
	sink := &statusSink{writer: writer}
	var totalBytes atomic.Int64
	var activeConnections atomic.Int32
	var currentSource atomic.Value
	currentSource.Store("")
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
		activeConnections.Add(1)
		go func() {
			results <- workerResult{
				id:  id,
				err: runWorker(workerCtx, opts, id, health, &totalBytes, &currentSource, sink),
			}
		}()
	}
	for id := 1; id <= opts.connections; id++ {
		startWorker(id)
	}
	scaleSignals := make(chan os.Signal, 16)
	signal.Notify(scaleSignals, syscall.SIGUSR1, syscall.SIGUSR2)
	defer signal.Stop(scaleSignals)
	var statusTicker *time.Ticker
	var statusUpdates <-chan time.Time
	if opts.statusIntervalSeconds > 0 {
		statusTicker = time.NewTicker(time.Duration(opts.statusIntervalSeconds) * time.Second)
		statusUpdates = statusTicker.C
		defer statusTicker.Stop()
	}
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
						activeConnections.Add(-1)
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
			activeConnections.Add(-1)
			if ctx.Err() != nil {
				return nil
			}
			if result.err != nil {
				fmt.Fprintf(os.Stderr, "connection=%d stopped: %v\n", result.id, result.err)
			}
			startWorker(result.id)
		case <-statusUpdates:
			source, _ := currentSource.Load().(string)
			sink.emit(statusEvent{
				Type:        "status",
				LineID:      opts.lineID,
				BindIP:      opts.bindIP,
				Bytes:       totalBytes.Load(),
				Connections: activeConnections.Load(),
				URL:         source,
			})
		}
	}
}

func runWorker(
	ctx context.Context,
	opts options,
	connectionID int,
	health *sourceHealth,
	totalBytes *atomic.Int64,
	currentSource *atomic.Value,
	sink *statusSink,
) error {
	timeout := time.Duration(opts.readTimeoutSeconds) * time.Second
	client := newHTTPClient(timeout, opts.bindIP)
	urlIndex := connectionID - 1
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
		probe := false
		var nextRetry time.Duration
		for checked := 0; checked < len(opts.urls); checked++ {
			candidate := opts.urls[urlIndex%len(opts.urls)]
			urlIndex++
			allowed, retryIn, claimedProbe := health.claim(candidate, time.Now())
			if allowed {
				url = candidate
				probe = claimedProbe
				break
			}
			if retryIn > 0 && (nextRetry == 0 || retryIn < nextRetry) {
				nextRetry = retryIn
			}
		}
		if url == "" {
			if nextRetry <= 0 {
				nextRetry = 250 * time.Millisecond
			}
			jitter := time.Duration(rand.Float64() * float64(250*time.Millisecond))
			if err := sleepWithContext(ctx, nextRetry+jitter); err != nil {
				return nil
			}
			continue
		}
		workerID := fmt.Sprintf("%s-%d", opts.workerID, connectionID)
		currentSource.Store(url)
		if probe {
			sink.emit(sourceStatusEvent(opts, url, health.snapshot(url, time.Now()), false))
		}
		if _, err := downloadOnceTracked(ctx, client, url, workerID, totalBytes); err != nil {
			if ctx.Err() != nil {
				if probe {
					health.releaseProbe(url)
				}
				return nil
			}
			snapshot := health.failed(url, time.Now(), err.Error(), probe)
			sink.emit(sourceStatusEvent(opts, url, snapshot, false))
			fmt.Fprintf(os.Stderr, "worker=%s url=%s error=%v state=%s retry_in=%s\n",
				workerID, url, err, snapshot.State, snapshot.RetryIn)
			continue
		}
		if recovered, snapshot := health.succeeded(url); recovered {
			sink.emit(sourceStatusEvent(opts, url, snapshot, true))
			fmt.Fprintf(os.Stderr, "worker=%s url=%s recovered=true\n", workerID, url)
		}
		if opts.restartJitterSeconds > 0 {
			jitter := time.Duration(rand.Float64() * opts.restartJitterSeconds * float64(time.Second))
			if err := sleepWithContext(ctx, jitter); err != nil {
				return nil
			}
		}
	}
}

func sourceStatusEvent(opts options, url string, snapshot sourceSnapshot, recovered bool) statusEvent {
	event := statusEvent{
		Type:                "source",
		LineID:              opts.lineID,
		BindIP:              opts.bindIP,
		URL:                 url,
		State:               snapshot.State,
		ConsecutiveFailures: snapshot.ConsecutiveFailures,
		Error:               snapshot.LastError,
		Recovered:           recovered,
	}
	if !snapshot.RetryAfter.IsZero() {
		event.RetryAfter = snapshot.RetryAfter.UTC().Format(time.RFC3339)
	}
	if snapshot.RetryIn > 0 {
		event.RetryInSeconds = int64((snapshot.RetryIn + time.Second - 1) / time.Second)
	}
	return event
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

func quarantineDelay(level int) time.Duration {
	switch level {
	case 1:
		return 10 * time.Minute
	case 2:
		return 30 * time.Minute
	default:
		return 60 * time.Minute
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
	return downloadOnceTracked(ctx, client, url, workerID, nil)
}

func downloadOnceTracked(
	ctx context.Context,
	client *http.Client,
	url string,
	workerID string,
	totalBytes *atomic.Int64,
) (int64, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	request.Header.Set("User-Agent", "broadband-pumper/"+workerID)
	response, err := client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 400 {
		return 0, fmt.Errorf("unexpected HTTP status %s", response.Status)
	}
	buffer := make([]byte, 64*1024)
	destination := io.Writer(io.Discard)
	if totalBytes != nil {
		destination = countingWriter{total: totalBytes}
	}
	return io.CopyBuffer(destination, response.Body, buffer)
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
