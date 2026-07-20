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
	Generation          string `json:"generation,omitempty"`
	SourceEpoch         uint64 `json:"source_epoch,omitempty"`
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
	mu          sync.Mutex
	states      map[string]*sourceState
	activeEpoch map[string]uint64
	nextEpoch   uint64
}

type sourceSet struct {
	value atomic.Value
}

type sourceSetSnapshot struct {
	Generation string
	URLs       []string
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
	if err := validateSourceURLs(config.Sources); err != nil {
		return err
	}
	set.value.Store(sourceSetSnapshot{
		Generation: config.Generation,
		URLs:       append([]string(nil), config.Sources...),
	})
	return nil
}

func (set *sourceSet) snapshot() []string {
	loaded := set.value.Load()
	if loaded == nil {
		return nil
	}
	return append([]string(nil), loaded.(sourceSetSnapshot).URLs...)
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
	Generation string   `json:"generation"`
	Sources    []string `json:"sources"`
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
	if err := validateSourceURLs(config.Sources); err != nil {
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
	health.retain(config.Sources)
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
}

func newSourceHealth() *sourceHealth {
	return &sourceHealth{states: make(map[string]*sourceState)}
}

func (health *sourceHealth) claim(url string, now time.Time) (bool, time.Duration, bool) {
	allowed, retry, probe, _ := health.claimCurrent(url, now)
	return allowed, retry, probe
}

func (health *sourceHealth) claimCurrent(url string, now time.Time) (bool, time.Duration, bool, uint64) {
	health.mu.Lock()
	defer health.mu.Unlock()
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
		return sourceSnapshot{State: "healthy"}, false
	}
	return health.failedLocked(url, now, lastError, wasProbe), true
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
	return hadFailures, sourceSnapshot{State: "healthy"}
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
	return snapshotFor(health.states[url], now)
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
	health.retain(initialSources)
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
		var sourceEpoch uint64
		sourceGeneration := ""
		var nextRetry time.Duration
		sources := opts.urls
		if opts.sourceSet != nil {
			sources = opts.sourceSet.snapshot()
			sourceGeneration = opts.sourceSet.generation()
		}
		for checked := 0; checked < len(sources); checked++ {
			candidate := sources[urlIndex%len(sources)]
			urlIndex++
			allowed, retryIn, claimedProbe, epoch := health.claimCurrent(candidate, time.Now())
			if allowed {
				url = candidate
				probe = claimedProbe
				sourceEpoch = epoch
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
			sink.emit(sourceStatusEvent(
				opts, url, health.snapshot(url, time.Now()), false, sourceGeneration, sourceEpoch,
			))
		}
		if _, err := downloadOnceTracked(ctx, client, url, workerID, totalBytes); err != nil {
			if ctx.Err() != nil {
				if probe {
					health.releaseProbeCurrent(url, sourceEpoch)
				}
				return nil
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
			continue
		}
		if recovered, snapshot := health.succeededCurrent(url, sourceEpoch); recovered {
			sink.emit(sourceStatusEvent(opts, url, snapshot, true, sourceGeneration, sourceEpoch))
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
	transport.ForceAttemptHTTP2 = false
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
