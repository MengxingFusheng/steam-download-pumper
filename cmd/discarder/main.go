package main

import (
	"bufio"
	"context"
	cryptorand "crypto/rand"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type options struct {
	workerID                  string
	lineID                    string
	connections               int
	maxConnections            int
	minSessionSeconds         int
	startupJitterSeconds      float64
	restartJitterSeconds      float64
	readTimeoutSeconds        int
	statusIntervalSeconds     int
	targetMbps                float64
	bindIP                    string
	urls                      []string
	sourcesFile               string
	rejectPrivateDestinations bool
	sourceSet                 *sourceSet
	statusWriter              io.Writer
	controlSignals            <-chan os.Signal
	workerRunner              func(context.Context, int, uint64) error
}

const maxConnectionLimit = 12

type statusEvent struct {
	Type                string             `json:"type"`
	LineID              string             `json:"line_id"`
	BindIP              string             `json:"bind_ip,omitempty"`
	Bytes               int64              `json:"bytes"`
	Connections         int32              `json:"connections"`
	URL                 string             `json:"url,omitempty"`
	State               string             `json:"state,omitempty"`
	ConsecutiveFailures int                `json:"consecutive_failures,omitempty"`
	RetryAfter          string             `json:"retry_after,omitempty"`
	RetryInSeconds      int64              `json:"retry_in_seconds,omitempty"`
	Error               string             `json:"error,omitempty"`
	Recovered           bool               `json:"recovered,omitempty"`
	Generation          string             `json:"generation,omitempty"`
	SourceEpoch         uint64             `json:"source_epoch,omitempty"`
	WindowBytes         int64              `json:"window_bytes,omitempty"`
	ActiveSeconds       float64            `json:"active_seconds,omitempty"`
	SampleSeconds       float64            `json:"sample_seconds,omitempty"`
	UnitConnectionMbps  float64            `json:"unit_connection_mbps,omitempty"`
	Health              *sourceHealthState `json:"health,omitempty"`
}

type countingWriter struct {
	total   *atomic.Int64
	request *atomic.Int64
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
	mu                     sync.Mutex
	states                 map[string]*sourceState
	activeEpoch            map[string]uint64
	nextEpoch              uint64
	performance            map[string]*sourcePerformance
	lastEvaluation         time.Time
	lastMetrics            time.Time
	lastExplore            time.Time
	explorationURL         string
	explorationUntil       time.Time
	explorationReservation int
	explorationIndex       int
	explorationConnection  int
	allocation             sourceAllocation
}

type sourceSet struct {
	value atomic.Value
}

type sourceSetSnapshot struct {
	Generation   string
	URLs         []string
	FallbackURLs []string
}

type lookupIPFunc func(context.Context, string, string) ([]net.IP, error)

func newSourceSet(urls []string) (*sourceSet, error) {
	set := &sourceSet{}
	if err := set.replace(urls); err != nil {
		return nil, err
	}
	return set, nil
}

func (set *sourceSet) replace(urls []string) error {
	if err := validateSourceURLs(urls); err != nil {
		return err
	}
	generation := ""
	if loaded := set.value.Load(); loaded != nil {
		generation = loaded.(sourceSetSnapshot).Generation
	}
	set.value.Store(sourceSetSnapshot{Generation: generation, URLs: append([]string(nil), urls...)})
	return nil
}

func (set *sourceSet) replaceConfig(config sourceFileConfig) error {
	if err := validateSourceConfig(config); err != nil {
		return err
	}
	set.value.Store(sourceSetSnapshot{
		Generation:   config.Generation,
		URLs:         append([]string(nil), config.Sources...),
		FallbackURLs: append([]string(nil), config.FallbackSources...),
	})
	return nil
}

func (set *sourceSet) snapshot() []string {
	loaded := set.value.Load()
	if loaded == nil {
		return nil
	}
	snapshot := loaded.(sourceSetSnapshot)
	return combinedSources(snapshot.URLs, snapshot.FallbackURLs)
}

func (set *sourceSet) generation() string {
	loaded := set.value.Load()
	if loaded == nil {
		return ""
	}
	return loaded.(sourceSetSnapshot).Generation
}

func validateSourceURLs(urls []string) error {
	if len(urls) == 0 {
		return fmt.Errorf("source list cannot be empty")
	}
	if len(urls) > 100 {
		return fmt.Errorf("source list cannot contain more than 100 URLs")
	}
	seen := make(map[string]struct{}, len(urls))
	for _, rawURL := range urls {
		parsed, err := url.Parse(rawURL)
		if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
			return fmt.Errorf("invalid HTTP/HTTPS source URL %q", rawURL)
		}
		if parsed.User != nil || parsed.Fragment != "" {
			return fmt.Errorf("source URL cannot contain credentials or a fragment: %q", rawURL)
		}
		if _, exists := seen[rawURL]; exists {
			return fmt.Errorf("duplicate source URL %q", rawURL)
		}
		seen[rawURL] = struct{}{}
	}
	return nil
}

type sourceFileConfig struct {
	Generation      string                       `json:"generation"`
	Sources         []string                     `json:"sources"`
	FallbackSources []string                     `json:"fallback_sources,omitempty"`
	Health          map[string]sourceHealthState `json:"health,omitempty"`
}

func loadSourceConfig(path string) (sourceFileConfig, error) {
	file, err := os.Open(path)
	if err != nil {
		return sourceFileConfig{}, err
	}
	defer file.Close()
	limited := io.LimitReader(file, 1_048_577)
	data, err := io.ReadAll(limited)
	if err != nil {
		return sourceFileConfig{}, err
	}
	if len(data) > 1_048_576 {
		return sourceFileConfig{}, fmt.Errorf("sources file is too large")
	}
	var raw json.RawMessage
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	if err := decoder.Decode(&raw); err != nil {
		return sourceFileConfig{}, fmt.Errorf("invalid sources file: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return sourceFileConfig{}, fmt.Errorf("sources file contains trailing JSON")
	}
	config := sourceFileConfig{}
	trimmed := strings.TrimSpace(string(raw))
	if strings.HasPrefix(trimmed, "[") {
		if err := json.Unmarshal(raw, &config.Sources); err != nil {
			return sourceFileConfig{}, fmt.Errorf("invalid sources file: %w", err)
		}
	} else {
		objectDecoder := json.NewDecoder(strings.NewReader(trimmed))
		objectDecoder.DisallowUnknownFields()
		if err := objectDecoder.Decode(&config); err != nil {
			return sourceFileConfig{}, fmt.Errorf("invalid sources file: %w", err)
		}
		if config.Generation == "" || len(config.Generation) > 128 {
			return sourceFileConfig{}, fmt.Errorf("source generation must contain 1-128 characters")
		}
	}
	if err := validateSourceConfig(config); err != nil {
		return sourceFileConfig{}, err
	}
	return config, nil
}

func loadSourcesFile(path string) ([]string, error) {
	config, err := loadSourceConfig(path)
	return config.Sources, err
}

func replaceSources(set *sourceSet, health *sourceHealth, urls []string) error {
	if err := set.replace(urls); err != nil {
		return err
	}
	health.retain(urls)
	return nil
}

func replaceSourceConfig(set *sourceSet, health *sourceHealth, config sourceFileConfig) error {
	if err := set.replaceConfig(config); err != nil {
		return err
	}
	health.retain(combinedSources(config.Sources, config.FallbackSources))
	return nil
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
	WindowBytes         int64
	ActiveSeconds       float64
	SampleSeconds       float64
	UnitConnectionMbps  float64
	Health              *sourceHealthState
}

func newSourceHealth() *sourceHealth {
	return &sourceHealth{states: make(map[string]*sourceState), performance: make(map[string]*sourcePerformance)}
}

func (health *sourceHealth) claim(url string, now time.Time) (bool, time.Duration, bool) {
	allowed, retry, probe, _ := health.claimCurrent(url, now)
	return allowed, retry, probe
}

func (health *sourceHealth) claimCurrent(url string, now time.Time) (bool, time.Duration, bool, uint64) {
	health.mu.Lock()
	defer health.mu.Unlock()
	return health.claimLocked(url, now)
}

func (health *sourceHealth) claimLocked(url string, now time.Time) (bool, time.Duration, bool, uint64) {
	epoch := uint64(0)
	if health.activeEpoch != nil {
		var active bool
		epoch, active = health.activeEpoch[url]
		if !active {
			return false, 0, false, 0
		}
	}
	state := health.states[url]
	if state == nil {
		return true, 0, false, epoch
	}
	if now.Before(state.retryAfter) {
		return false, state.retryAfter.Sub(now), false, epoch
	}
	if state.quarantineLevel == 0 {
		return true, 0, false, epoch
	}
	if state.probeInFlight {
		return false, 250 * time.Millisecond, false, epoch
	}
	state.probeInFlight = true
	return true, 0, true, epoch
}

func (health *sourceHealth) failed(url string, now time.Time, lastError string, wasProbe bool) sourceSnapshot {
	health.mu.Lock()
	defer health.mu.Unlock()
	return health.failedLocked(url, now, lastError, wasProbe)
}

func (health *sourceHealth) failedCurrent(
	url string,
	epoch uint64,
	now time.Time,
	lastError string,
	wasProbe bool,
) (sourceSnapshot, bool) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch == nil || epoch == 0 || health.activeEpoch[url] != epoch {
		return sourceSnapshot{State: "unknown"}, false
	}
	health.performanceForLocked(url).failedRequests++
	if recentBytes, _, _ := health.performance[url].window(now, 3*time.Second); !wasProbe && recentBytes > 0 {
		// Other requests are still delivering. Retry this worker without
		// blocking every connection to an otherwise productive endpoint.
		return health.snapshotLocked(url, now), true
	}
	health.failedLocked(url, now, lastError, wasProbe)
	return health.snapshotLocked(url, now), true
}

func (health *sourceHealth) failedLocked(url string, now time.Time, lastError string, wasProbe bool) sourceSnapshot {
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

func (health *sourceHealth) succeededCurrent(url string, epoch uint64) (bool, sourceSnapshot) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch == nil || epoch == 0 || health.activeEpoch[url] != epoch {
		return false, sourceSnapshot{State: "healthy"}
	}
	_, hadFailures := health.states[url]
	delete(health.states, url)
	health.performanceForLocked(url).successfulRequests++
	return hadFailures, health.snapshotLocked(url, time.Now())
}

func (health *sourceHealth) releaseProbe(url string) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if state := health.states[url]; state != nil {
		state.probeInFlight = false
	}
}

func (health *sourceHealth) releaseProbeCurrent(url string, epoch uint64) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch != nil && health.activeEpoch[url] != epoch {
		return
	}
	if state := health.states[url]; state != nil {
		state.probeInFlight = false
	}
}

func (health *sourceHealth) epoch(url string) uint64 {
	health.mu.Lock()
	defer health.mu.Unlock()
	return health.activeEpoch[url]
}

func (health *sourceHealth) snapshot(url string, now time.Time) sourceSnapshot {
	health.mu.Lock()
	defer health.mu.Unlock()
	return health.snapshotLocked(url, now)
}

func (health *sourceHealth) retain(urls []string) {
	keep := make(map[string]struct{}, len(urls))
	for _, sourceURL := range urls {
		keep[sourceURL] = struct{}{}
	}
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch == nil {
		health.activeEpoch = make(map[string]uint64, len(urls))
	}
	for sourceURL := range health.activeEpoch {
		if _, exists := keep[sourceURL]; !exists {
			delete(health.activeEpoch, sourceURL)
			delete(health.states, sourceURL)
			delete(health.performance, sourceURL)
		}
	}
	for sourceURL := range keep {
		if _, exists := health.activeEpoch[sourceURL]; !exists {
			health.nextEpoch++
			health.activeEpoch[sourceURL] = health.nextEpoch
		}
	}
	for sourceURL := range health.states {
		if _, exists := keep[sourceURL]; !exists {
			delete(health.states, sourceURL)
		}
	}
}

func (health *sourceHealth) ensureTracked(urls []string) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch != nil {
		return
	}
	health.activeEpoch = make(map[string]uint64, len(urls))
	for _, sourceURL := range urls {
		health.nextEpoch++
		health.activeEpoch[sourceURL] = health.nextEpoch
	}
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
	if writer.total != nil {
		writer.total.Add(int64(len(buffer)))
	}
	if writer.request != nil {
		writer.request.Add(int64(len(buffer)))
	}
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
	flag.Float64Var(&opts.targetMbps, "target-mbps", 0, "assigned line budget; zero disables target-based fallback exploration")
	flag.StringVar(&opts.bindIP, "bind-ip", "", "local IPv4 address to bind outbound connections")
	flag.StringVar(&opts.sourcesFile, "sources-file", "", "JSON file containing reloadable source URLs")
	flag.BoolVar(&opts.rejectPrivateDestinations, "reject-private-destinations", false, "reject non-public IPv4 destinations")
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
	if opts.readTimeoutSeconds < 1 {
		return fmt.Errorf("read-timeout-seconds must be greater than 0")
	}
	if opts.targetMbps < 0 || math.IsNaN(opts.targetMbps) || math.IsInf(opts.targetMbps, 0) {
		return fmt.Errorf("target-mbps must be finite and non-negative")
	}
	if opts.bindIP != "" {
		parsedIP := net.ParseIP(opts.bindIP)
		if parsedIP == nil || parsedIP.To4() == nil {
			return fmt.Errorf("bind-ip must be a valid IPv4 address")
		}
	}
	if len(opts.urls) == 0 && opts.sourcesFile == "" {
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
	initialSources := opts.urls
	initialConfig := sourceFileConfig{Sources: initialSources}
	if opts.sourcesFile != "" {
		config, err := loadSourceConfig(opts.sourcesFile)
		if err != nil {
			return fmt.Errorf("load sources file: %w", err)
		}
		initialSources = config.Sources
		initialConfig = config
	}
	set, err := newSourceSet(initialSources)
	if err != nil {
		return err
	}
	opts.sourceSet = set
	if err := set.replaceConfig(initialConfig); err != nil {
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
		id    int
		token uint64
		err   error
	}
	type workerInstance struct {
		token    uint64
		cancel   context.CancelFunc
		stopping bool
	}
	results := make(chan workerResult, opts.maxConnections+1)
	workers := make(map[int]workerInstance, opts.maxConnections)
	targetConnections := opts.connections
	var nextWorkerToken uint64
	health := newSourceHealth()
	health.allocation.targetMbps = opts.targetMbps
	health.allocation.connections = opts.connections
	health.retain(combinedSources(initialSources, initialConfig.FallbackSources))
	health.restore(initialConfig.Health, time.Now())
	sink.emit(statusEvent{Type: "source-list", LineID: opts.lineID, State: "loaded", Generation: initialConfig.Generation})
	startWorker := func(id int) {
		workerCtx, cancel := context.WithCancel(ctx)
		nextWorkerToken++
		token := nextWorkerToken
		workers[id] = workerInstance{token: token, cancel: cancel}
		activeConnections.Add(1)
		go func() {
			runner := opts.workerRunner
			if runner == nil {
				runner = func(runCtx context.Context, connectionID int, _ uint64) error {
					return runWorker(runCtx, opts, connectionID, health, &totalBytes, &currentSource, sink)
				}
			}
			results <- workerResult{
				id:    id,
				token: token,
				err:   runner(workerCtx, id, token),
			}
		}()
	}
	reconcileWorkers := func() {
		for len(workers) < targetConnections {
			for id := 1; id <= opts.maxConnections; id++ {
				if _, exists := workers[id]; !exists {
					startWorker(id)
					break
				}
			}
		}
	}
	reconcileWorkers()
	controlSignals := opts.controlSignals
	var ownedSignals chan os.Signal
	if controlSignals == nil {
		ownedSignals = make(chan os.Signal, 16)
		controlSignals = ownedSignals
		signal.Notify(ownedSignals, syscall.SIGUSR1, syscall.SIGUSR2, syscall.SIGHUP)
		defer signal.Stop(ownedSignals)
	}
	var statusTicker *time.Ticker
	var statusUpdates <-chan time.Time
	if opts.statusIntervalSeconds > 0 {
		statusTicker = time.NewTicker(time.Duration(opts.statusIntervalSeconds) * time.Second)
		statusUpdates = statusTicker.C
		defer statusTicker.Stop()
	}
	// Wait for cancelled readers before the final counter snapshot. The last
	// reporting interval otherwise disappears when Fleet rotates a line.
	defer func() {
		for _, worker := range workers {
			worker.cancel()
		}
		timer := time.NewTimer(2 * time.Second)
		defer timer.Stop()
	waitForReaders:
		for len(workers) > 0 {
			select {
			case result := <-results:
				if worker, exists := workers[result.id]; exists && worker.token == result.token {
					delete(workers, result.id)
					activeConnections.Add(-1)
				}
			case <-timer.C:
				break waitForReaders
			}
		}
		source, _ := currentSource.Load().(string)
		sink.emit(statusEvent{Type: "status", LineID: opts.lineID, BindIP: opts.bindIP,
			Bytes: totalBytes.Load(), Connections: activeConnections.Load(), URL: source})
	}()
	for {
		select {
		case <-ctx.Done():
			for _, worker := range workers {
				worker.cancel()
			}
			return nil
		case controlSignal := <-controlSignals:
			if controlSignal == syscall.SIGHUP {
				var config sourceFileConfig
				var err error
				if opts.sourcesFile == "" {
					err = fmt.Errorf("sources-file is not configured")
				} else {
					config, err = loadSourceConfig(opts.sourcesFile)
				}
				if err == nil {
					err = replaceSourceConfig(set, health, config)
				}
				if err != nil {
					fmt.Fprintf(os.Stderr, "source-list reload error=%v\n", err)
					sink.emit(statusEvent{Type: "source-list", LineID: opts.lineID, Error: err.Error()})
				} else {
					for id, worker := range workers {
						worker.stopping = true
						workers[id] = worker
						worker.cancel()
					}
					fmt.Fprintf(os.Stderr, "source-list reloaded count=%d\n", len(config.Sources))
					sink.emit(statusEvent{
						Type: "source-list", LineID: opts.lineID, State: "reloaded", Generation: config.Generation,
					})
				}
			} else if controlSignal == syscall.SIGUSR1 && targetConnections < opts.maxConnections {
				targetConnections++
				reconcileWorkers()
			} else if controlSignal == syscall.SIGUSR2 && targetConnections > 1 {
				targetConnections--
				for id := opts.maxConnections; id >= 1; id-- {
					if worker, exists := workers[id]; exists && !worker.stopping {
						worker.stopping = true
						workers[id] = worker
						worker.cancel()
						break
					}
				}
			}
			health.mu.Lock()
			health.allocation.connections = targetConnections
			health.mu.Unlock()
		case result := <-results:
			worker, active := workers[result.id]
			if !active || worker.token != result.token {
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
			reconcileWorkers()
		case <-statusUpdates:
			now := time.Now()
			health.evaluate(now)
			health.emitMetrics(sink, opts, set, now)
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
	client := newHTTPClientWithPolicy(timeout, opts.bindIP, opts.rejectPrivateDestinations, nil)
	initialSources := opts.urls
	if opts.sourceSet != nil {
		initialSources = opts.sourceSet.snapshot()
	}
	health.ensureTracked(initialSources)
	defer health.releaseConnection(connectionID)
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
		sourceGeneration := ""
		primary, fallback := opts.urls, []string(nil)
		if opts.sourceSet != nil {
			primary, fallback = opts.sourceSet.pools()
			sourceGeneration = opts.sourceSet.generation()
		}
		choice, nextRetry := health.choose(primary, fallback, connectionID, urlIndex, time.Now())
		urlIndex++
		url, probe, sourceEpoch := choice.url, choice.probe, choice.epoch
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
			sink.emit(sourceStatusEvent(
				opts, url, health.snapshot(url, time.Now()), false, sourceGeneration, sourceEpoch,
			))
		}
		if received, err := downloadSelectedSource(ctx, client, url, workerID, totalBytes, health, choice, connectionID, primary); err != nil {
			if errors.Is(err, errSourceRotated) {
				if choice.exploring && received > 0 && ctx.Err() == nil {
					if recovered, snapshot := health.finishExploration(choice, time.Now()); recovered {
						sink.emit(sourceStatusEvent(opts, url, snapshot, true, sourceGeneration, sourceEpoch))
					}
				}
				if probe {
					health.releaseProbeCurrent(url, sourceEpoch)
				}
				continue
			}
			if ctx.Err() != nil {
				if probe {
					health.releaseProbeCurrent(url, sourceEpoch)
				}
				return nil
			}
			if choice.exploring {
				health.failedExploration(choice, time.Now())
			}
			snapshot, recorded := health.failedCurrent(
				url, sourceEpoch, time.Now(), err.Error(), probe,
			)
			if !recorded {
				continue
			}
			sink.emit(sourceStatusEvent(opts, url, snapshot, false, sourceGeneration, sourceEpoch))
			fmt.Fprintf(os.Stderr, "worker=%s url=%s error=%v state=%s retry_in=%s\n",
				workerID, url, err, snapshot.State, snapshot.RetryIn)
			if snapshot.RetryIn <= 0 {
				if err := sleepWithContext(ctx, retryDelay(1)); err != nil {
					return nil
				}
			}
			continue
		}
		success := health.succeededCurrent
		if choice.exploring {
			success = func(_ string, _ uint64) (bool, sourceSnapshot) { return health.finishExploration(choice, time.Now()) }
		}
		if recovered, snapshot := success(url, sourceEpoch); recovered {
			sink.emit(sourceStatusEvent(opts, url, snapshot, true, sourceGeneration, sourceEpoch))
			fmt.Fprintf(os.Stderr, "worker=%s url=%s recovered=true\n", workerID, url)
		}
		// Modern Ookla downloads are sized requests in a continuous stream.
		// Sleeping after every completed chunk wastes available line capacity.
		if opts.restartJitterSeconds > 0 && !isSpeedtestDownload(url) {
			jitter := time.Duration(rand.Float64() * opts.restartJitterSeconds * float64(time.Second))
			if err := sleepWithContext(ctx, jitter); err != nil {
				return nil
			}
		}
	}
}

func sourceStatusEvent(
	opts options,
	url string,
	snapshot sourceSnapshot,
	recovered bool,
	generation string,
	sourceEpoch uint64,
) statusEvent {
	event := statusEvent{
		Type:                "source",
		LineID:              opts.lineID,
		BindIP:              opts.bindIP,
		URL:                 url,
		State:               snapshot.State,
		ConsecutiveFailures: snapshot.ConsecutiveFailures,
		Error:               snapshot.LastError,
		Recovered:           recovered,
		Generation:          generation,
		SourceEpoch:         sourceEpoch,
		WindowBytes:         snapshot.WindowBytes,
		ActiveSeconds:       snapshot.ActiveSeconds,
		SampleSeconds:       snapshot.SampleSeconds,
		UnitConnectionMbps:  snapshot.UnitConnectionMbps,
		Health:              snapshot.Health,
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
	return newHTTPClientWithPolicy(timeout, bindIP, false, nil)
}

var blockedIPv4Networks = func() []*net.IPNet {
	ranges := []string{
		"0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
		"169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
		"192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
		"203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
	}
	networks := make([]*net.IPNet, 0, len(ranges))
	for _, rawRange := range ranges {
		_, network, err := net.ParseCIDR(rawRange)
		if err != nil {
			panic(err)
		}
		networks = append(networks, network)
	}
	return networks
}()

func isPublicIPv4(ip net.IP) bool {
	ipv4 := ip.To4()
	if ipv4 == nil || !ipv4.IsGlobalUnicast() {
		return false
	}
	for _, network := range blockedIPv4Networks {
		if network.Contains(ipv4) {
			return false
		}
	}
	return true
}

func resolvePublicIPv4(ctx context.Context, host string, lookup lookupIPFunc) ([]net.IP, error) {
	if parsed := net.ParseIP(host); parsed != nil {
		if !isPublicIPv4(parsed) {
			return nil, fmt.Errorf("destination %s is not a public IPv4 address", host)
		}
		return []net.IP{parsed.To4()}, nil
	}
	addresses, err := lookup(ctx, "ip4", host)
	if err != nil {
		return nil, fmt.Errorf("resolve destination %s: %w", host, err)
	}
	if len(addresses) == 0 {
		return nil, fmt.Errorf("destination %s has no public IPv4 address", host)
	}
	public := make([]net.IP, 0, len(addresses))
	for _, address := range addresses {
		if !isPublicIPv4(address) {
			return nil, fmt.Errorf("destination %s did not resolve only to public IPv4 addresses", host)
		}
		public = append(public, address.To4())
	}
	return public, nil
}

func resolvePublicIPv4Bounded(
	ctx context.Context,
	host string,
	lookup lookupIPFunc,
	timeout time.Duration,
) ([]net.IP, error) {
	lookupContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return resolvePublicIPv4(lookupContext, host, lookup)
}

// DNS must follow the same policy-routed source address as the HTTP connection.
// Docker's embedded resolver forwards through the container's primary address,
// which may belong to a disconnected PPPoE account even when this line is healthy.
func newBoundDNSResolver(bindIP string, servers []string, timeout time.Duration) *net.Resolver {
	var next atomic.Uint64
	return &net.Resolver{
		PreferGo: true,
		Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
			server := servers[(next.Add(1)-1)%uint64(len(servers))]
			dialer := &net.Dialer{Timeout: timeout}
			ip := net.ParseIP(bindIP)
			if strings.HasPrefix(network, "tcp") {
				dialer.LocalAddr = &net.TCPAddr{IP: ip}
				return dialer.DialContext(ctx, "tcp4", server)
			}
			dialer.LocalAddr = &net.UDPAddr{IP: ip}
			return dialer.DialContext(ctx, "udp4", server)
		},
	}
}

func newHTTPClientWithPolicy(
	timeout time.Duration,
	bindIP string,
	rejectPrivateDestinations bool,
	lookup lookupIPFunc,
) *http.Client {
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
	if lookup == nil {
		lookup = net.DefaultResolver.LookupIP
		if bindIP != "" {
			var servers []string
			for _, server := range strings.FieldsFunc(os.Getenv("DISCARDER_DNS_SERVERS"), func(r rune) bool { return r == ',' || r == ' ' }) {
				servers = append(servers, net.JoinHostPort(server, "53"))
			}
			if len(servers) > 0 {
				resolver := newBoundDNSResolver(bindIP, servers, timeout)
				lookup = resolver.LookupIP
				dialer.Resolver = resolver
			}
		}
	}
	transport.DialContext = func(ctx context.Context, _network, address string) (net.Conn, error) {
		dialAddress := address
		if rejectPrivateDestinations {
			host, port, err := net.SplitHostPort(address)
			if err != nil {
				return nil, err
			}
			addresses, err := resolvePublicIPv4Bounded(ctx, host, lookup, timeout)
			if err != nil {
				return nil, err
			}
			var lastError error
			for _, destination := range addresses {
				dialAddress = net.JoinHostPort(destination.String(), port)
				conn, dialErr := dialer.DialContext(ctx, "tcp4", dialAddress)
				if dialErr == nil {
					return &idleTimeoutConn{Conn: conn, timeout: timeout}, nil
				}
				lastError = dialErr
			}
			return nil, lastError
		}
		conn, err := dialer.DialContext(ctx, "tcp4", dialAddress)
		if err != nil {
			return nil, err
		}
		return &idleTimeoutConn{Conn: conn, timeout: timeout}, nil
	}
	transport.ForceAttemptHTTP2 = true
	transport.MaxIdleConnsPerHost = maxConnectionLimit
	transport.DisableCompression = true
	transport.ResponseHeaderTimeout = timeout
	transport.TLSHandshakeTimeout = timeout
	client := &http.Client{Transport: transport}
	if rejectPrivateDestinations {
		transport.Proxy = nil
		client.CheckRedirect = func(request *http.Request, via []*http.Request) error {
			if len(via) > 3 {
				return fmt.Errorf("more than three redirects are not allowed")
			}
			if request.URL.Scheme != "http" && request.URL.Scheme != "https" {
				return fmt.Errorf("redirect target must use HTTP or HTTPS")
			}
			if request.URL.User != nil || request.URL.Fragment != "" {
				return fmt.Errorf("redirect target cannot contain credentials or a fragment")
			}
			_, err := resolvePublicIPv4Bounded(
				request.Context(), request.URL.Hostname(), lookup, timeout,
			)
			return err
		}
	}
	return client
}

func downloadOnce(ctx context.Context, client *http.Client, url string, workerID string) (int64, error) {
	return downloadOnceTracked(ctx, client, url, workerID, nil)
}

// expandRequestURL makes a unique request for Speedtest XHR download sources.
// Modern servers require a UUID guid and use nocache to prevent intermediary
// caches from serving an old response. Other source URLs are left untouched.
func expandRequestURL(rawURL string) string {
	if !strings.Contains(rawURL, "{{guid}}") && !strings.Contains(rawURL, "{{nonce}}") {
		return rawURL
	}
	guid := newRequestGUID()
	return strings.NewReplacer("{{guid}}", guid, "{{nonce}}", guid).Replace(rawURL)
}

func newRequestGUID() string {
	var value [16]byte
	if _, err := cryptorand.Read(value[:]); err != nil {
		return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", time.Now().UnixNano(), rand.Uint32(), rand.Uint32(), rand.Uint32(), rand.Uint64())
	}
	value[6] = (value[6] & 0x0f) | 0x40
	value[8] = (value[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		value[0:4], value[4:6], value[6:8], value[8:10], value[10:16])
}

func downloadOnceTracked(
	ctx context.Context,
	client *http.Client,
	url string,
	workerID string,
	totalBytes *atomic.Int64,
) (int64, error) {
	return downloadOnceMeasured(ctx, client, url, workerID, totalBytes, nil)
}

func downloadOnceMeasured(ctx context.Context, client *http.Client, url string, workerID string, totalBytes, requestBytes *atomic.Int64) (int64, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	request.Header.Set("User-Agent", requestUserAgent(url, workerID))
	response, err := client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return 0, fmt.Errorf("unexpected HTTP status %s", response.Status)
	}
	buffer := make([]byte, 64*1024)
	contentType := strings.ToLower(response.Header.Get("Content-Type"))
	if strings.Contains(contentType, "text/html") || strings.Contains(contentType, "application/json") || strings.Contains(contentType, "xml") {
		return 0, fmt.Errorf("download endpoint returned an error document (%s)", contentType)
	}
	body := bufio.NewReader(response.Body)
	prefix, _ := body.Peek(512)
	trimmed := strings.ToLower(strings.TrimSpace(string(prefix)))
	if strings.HasPrefix(trimmed, "<!doctype html") || strings.HasPrefix(trimmed, "<html") || strings.HasPrefix(trimmed, "{\"error\"") {
		return 0, fmt.Errorf("download endpoint returned an error document")
	}
	destination := io.Writer(io.Discard)
	if totalBytes != nil || requestBytes != nil {
		destination = countingWriter{total: totalBytes, request: requestBytes}
	}
	received, copyErr := io.CopyBuffer(destination, body, buffer)
	if received == 0 && copyErr == nil {
		return 0, fmt.Errorf("download endpoint returned an empty body")
	}
	// Traffic generation needs useful bytes, not a complete copy of the file.
	// Some speedtest servers close a large response early; substantial progress
	// must not quarantine a working source and strand every connection.
	if received >= 1<<20 && errors.Is(copyErr, io.ErrUnexpectedEOF) {
		return received, nil
	}
	return received, copyErr
}

func requestUserAgent(rawURL string, workerID string) string {
	if isSpeedtestDownload(rawURL) {
		return "fleet-speedtest-authorized/1.0"
	}
	return "broadband-pumper/" + workerID
}

func isSpeedtestDownload(rawURL string) bool {
	parsed, err := url.Parse(rawURL)
	if err == nil && parsed.Path == "/download" {
		query := parsed.Query()
		if query.Get("guid") != "" && query.Get("nocache") != "" {
			return true
		}
	}
	return false
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
