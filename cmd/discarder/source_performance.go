package main

import (
	"context"
	"errors"
	"net/http"
	"sort"
	"sync/atomic"
	"time"
)

const (
	performanceWindow        = 60 * time.Second
	sourceEvaluationInterval = 15 * time.Second
	slowSourceRetry          = 5 * time.Minute
	sourceExploreInterval    = 60 * time.Second
	sourceExploreDuration    = 15 * time.Second
)

var errSourceRotated = errors.New("source request rotated")
var errSourceStalled = errors.New("source made no progress for 15 seconds")

// sourceHealthState is persisted by the supervisor between line rotations.
// Window measurements expire after 60 seconds, even across process restarts.
// In-flight probes are deliberately not persisted.
type sourceHealthState struct {
	RetryAfter          string  `json:"retry_after,omitempty"`
	ConsecutiveFailures int     `json:"consecutive_failures"`
	QuarantineLevel     int     `json:"quarantine_level"`
	LastError           string  `json:"last_error,omitempty"`
	SlowRetryAfter      string  `json:"slow_retry_after,omitempty"`
	TotalBytes          int64   `json:"total_bytes"`
	SuccessfulRequests  int64   `json:"successful_requests"`
	FailedRequests      int64   `json:"failed_requests"`
	StalledRequests     int64   `json:"stalled_requests"`
	WindowBytes         int64   `json:"window_bytes"`
	ActiveSeconds       float64 `json:"active_seconds"`
	SampleSeconds       float64 `json:"sample_seconds"`
	ObservedAt          string  `json:"observed_at,omitempty"`
}

type performanceBucket struct {
	second        int64
	bytes         int64
	activeSeconds float64
}

type sourcePerformance struct {
	buckets            [61]performanceBucket
	totalBytes         int64
	successfulRequests int64
	failedRequests     int64
	stalledRequests    int64
	observedAt         time.Time
	lowWindows         int
	slowRetryAfter     time.Time
}

type sourceChoice struct {
	url       string
	epoch     uint64
	probe     bool
	exploring bool
	fallback  bool
}

func combinedSources(primary, fallback []string) []string {
	result := append([]string(nil), primary...)
	seen := make(map[string]bool, len(primary)+len(fallback))
	for _, rawURL := range primary {
		seen[rawURL] = true
	}
	for _, rawURL := range fallback {
		if !seen[rawURL] {
			result = append(result, rawURL)
			seen[rawURL] = true
		}
	}
	return result
}

func validateSourceConfig(config sourceFileConfig) error {
	if err := validateSourceURLs(config.Sources); err != nil {
		return err
	}
	if len(config.FallbackSources) > 0 {
		return validateSourceURLs(config.FallbackSources)
	}
	return nil
}

func (set *sourceSet) pools() ([]string, []string) {
	loaded := set.value.Load()
	if loaded == nil {
		return nil, nil
	}
	snapshot := loaded.(sourceSetSnapshot)
	return append([]string(nil), snapshot.URLs...), append([]string(nil), snapshot.FallbackURLs...)
}

func (health *sourceHealth) performanceForLocked(url string) *sourcePerformance {
	if health.performance == nil {
		health.performance = make(map[string]*sourcePerformance)
	}
	performance := health.performance[url]
	if performance == nil {
		performance = &sourcePerformance{}
		health.performance[url] = performance
	}
	return performance
}

// account divides time across second buckets, so multiple connections contribute
// connection-seconds while observed wall time is counted only once per second.
func (health *sourceHealth) account(url string, epoch uint64, from, now time.Time, bytes int64) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch[url] != epoch || epoch == 0 {
		return
	}
	performance := health.performanceForLocked(url)
	performance.totalBytes += bytes
	performance.observedAt = now
	if !now.After(from) {
		return
	}
	total := now.Sub(from).Seconds()
	remainingBytes := bytes
	for from.Before(now) {
		end := time.Unix(from.Unix()+1, 0)
		if end.After(now) {
			end = now
		}
		seconds := end.Sub(from).Seconds()
		portion := int64(float64(bytes) * seconds / total)
		if end.Equal(now) {
			portion = remainingBytes
		}
		remainingBytes -= portion
		bucket := &performance.buckets[positiveMod(from.Unix(), int64(len(performance.buckets)))]
		if bucket.second != from.Unix() {
			*bucket = performanceBucket{second: from.Unix()}
		}
		bucket.bytes += portion
		bucket.activeSeconds += seconds
		from = end
	}
}

func positiveMod(value, modulo int64) int64 {
	result := value % modulo
	if result < 0 {
		result += modulo
	}
	return result
}

func (performance *sourcePerformance) window(now time.Time, duration time.Duration) (bytes int64, active, sampled float64) {
	cutoff := now.Add(-duration).Unix()
	for _, bucket := range performance.buckets {
		if bucket.second < cutoff || bucket.second > now.Unix() {
			continue
		}
		bytes += bucket.bytes
		active += bucket.activeSeconds
		sampled += min(bucket.activeSeconds, 1)
	}
	return
}

func performanceRate(performance *sourcePerformance, now time.Time, duration time.Duration) float64 {
	if performance == nil {
		return 0
	}
	bytes, active, _ := performance.window(now, duration)
	if active <= 0 {
		return 0
	}
	return float64(bytes) * 8 / active / 1e6
}

func (health *sourceHealth) availableLocked(url string, now time.Time) bool {
	if health.activeEpoch[url] == 0 {
		return false
	}
	if health.explorationURL == url && now.Before(health.explorationUntil) {
		return false
	}
	if state := health.states[url]; state != nil && (now.Before(state.retryAfter) || state.probeInFlight) {
		return false
	}
	performance := health.performance[url]
	return performance == nil || !now.Before(performance.slowRetryAfter)
}

func (health *sourceHealth) rankedLocked(urls []string, now time.Time) []string {
	ready := make([]string, 0, len(urls))
	for _, rawURL := range urls {
		if health.availableLocked(rawURL, now) {
			ready = append(ready, rawURL)
		}
	}
	sort.SliceStable(ready, func(i, j int) bool {
		return performanceRate(health.performance[ready[i]], now, performanceWindow) > performanceRate(health.performance[ready[j]], now, performanceWindow)
	})
	return ready
}

func (health *sourceHealth) explorationCandidateLocked(urls []string, now time.Time) string {
	ranked := health.rankedLocked(urls, now)
	if len(ranked) == 0 {
		return ""
	}
	// Failed and slow sources receive one half-open probe once their timer expires.
	for _, rawURL := range ranked {
		if state := health.states[rawURL]; state != nil && state.quarantineLevel > 0 {
			return rawURL
		}
		if performance := health.performance[rawURL]; performance != nil && !performance.slowRetryAfter.IsZero() {
			return rawURL
		}
	}
	if len(ranked) <= 3 {
		return ""
	}
	candidates := ranked[3:]
	return candidates[health.explorationIndex%len(candidates)]
}

func (health *sourceHealth) choose(primary, fallback []string, connectionID, offset int, now time.Time) (sourceChoice, time.Duration) {
	health.mu.Lock()
	defer health.mu.Unlock()
	urls := primary
	usingFallback := false
	if len(health.rankedLocked(urls, now)) == 0 {
		urls = fallback
		usingFallback = true
	}
	ranked := health.rankedLocked(urls, now)
	if len(ranked) == 0 {
		nextRetry := 250 * time.Millisecond
		for _, rawURL := range combinedSources(primary, fallback) {
			if state := health.states[rawURL]; state != nil && now.Before(state.retryAfter) {
				if delay := state.retryAfter.Sub(now); nextRetry == 250*time.Millisecond || delay < nextRetry {
					nextRetry = delay
				}
			}
			if performance := health.performance[rawURL]; performance != nil && now.Before(performance.slowRetryAfter) {
				if delay := performance.slowRetryAfter.Sub(now); nextRetry == 250*time.Millisecond || delay < nextRetry {
					nextRetry = delay
				}
			}
		}
		return sourceChoice{}, min(nextRetry, time.Second)
	}
	exploring := false
	candidate := ""
	if !now.Before(health.explorationUntil) && (health.lastExplore.IsZero() || now.Sub(health.lastExplore) >= sourceExploreInterval) && (health.explorationReservation == 0 || health.explorationReservation == connectionID) {
		candidate = health.explorationCandidateLocked(urls, now)
		if candidate != "" {
			exploring = true
			health.explorationIndex++
			health.lastExplore = now
			health.explorationURL = candidate
			health.explorationUntil = now.Add(sourceExploreDuration)
			health.explorationReservation = 0
		}
	}
	if candidate == "" {
		if health.explorationReservation == connectionID {
			health.explorationReservation = 0
		}
		ordinary := make([]string, 0, len(ranked))
		for _, rawURL := range ranked {
			if rawURL == health.explorationURL && now.Before(health.explorationUntil) {
				continue
			}
			if state := health.states[rawURL]; state != nil && state.quarantineLevel > 0 {
				continue
			}
			if performance := health.performance[rawURL]; performance != nil && !performance.slowRetryAfter.IsZero() {
				continue
			}
			ordinary = append(ordinary, rawURL)
		}
		if len(ordinary) == 0 {
			return sourceChoice{}, 250 * time.Millisecond
		}
		candidate = ordinary[offset%min(3, len(ordinary))]
	}
	allowed, retry, probe, epoch := health.claimLocked(candidate, now)
	if !allowed {
		return sourceChoice{}, retry
	}
	return sourceChoice{url: candidate, epoch: epoch, probe: probe, exploring: exploring, fallback: usingFallback}, 0
}

func (health *sourceHealth) shouldRotate(choice sourceChoice, connectionID int, primary []string, started, now time.Time) bool {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch[choice.url] != choice.epoch {
		return true
	}
	if performance := health.performance[choice.url]; performance != nil && now.Before(performance.slowRetryAfter) {
		return true
	}
	if choice.exploring {
		return now.Sub(started) >= sourceExploreDuration
	}
	if choice.fallback && len(health.rankedLocked(primary, now)) > 0 {
		return true
	}
	// Long mirror downloads may never reach EOF. Reassign an established
	// connection when new measurements move its source outside the best three.
	if !choice.fallback && now.Sub(started) >= 30*time.Second {
		ranked := health.rankedLocked(primary, now)
		if len(ranked) > 3 {
			preferred := false
			for _, rawURL := range ranked[:3] {
				if rawURL == choice.url {
					preferred = true
					break
				}
			}
			if !preferred {
				return true
			}
		}
	}
	if health.explorationReservation != 0 || now.Before(health.explorationUntil) || (!health.lastExplore.IsZero() && now.Sub(health.lastExplore) < sourceExploreInterval) {
		return false
	}
	if health.explorationCandidateLocked(primary, now) == "" {
		return false
	}
	health.explorationReservation = connectionID
	return true
}

func (health *sourceHealth) finishChoice(choice sourceChoice) {
	if !choice.exploring {
		return
	}
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.explorationURL == choice.url {
		health.explorationURL = ""
		health.explorationUntil = time.Time{}
	}
}

func (health *sourceHealth) evaluate(now time.Time) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if !health.lastEvaluation.IsZero() && now.Sub(health.lastEvaluation) < sourceEvaluationInterval {
		return
	}
	health.lastEvaluation = now
	best := 0.0
	for rawURL, performance := range health.performance {
		if !health.availableLocked(rawURL, now) {
			continue
		}
		_, _, sampled := performance.window(now, performanceWindow)
		if sampled >= 30 {
			best = max(best, performanceRate(performance, now, sourceEvaluationInterval))
		}
	}
	for rawURL, performance := range health.performance {
		if now.Before(performance.slowRetryAfter) {
			continue
		}
		_, _, sampled := performance.window(now, performanceWindow)
		_, active, _ := performance.window(now, sourceEvaluationInterval)
		if sampled < 30 || active < 1 || best <= 0 {
			performance.lowWindows = 0
			continue
		}
		if performanceRate(performance, now, sourceEvaluationInterval) < best*0.25 {
			performance.lowWindows++
		} else {
			performance.lowWindows = 0
			performance.slowRetryAfter = time.Time{}
		}
		if performance.lowWindows >= 3 {
			performance.slowRetryAfter = now.Add(slowSourceRetry)
			performance.lowWindows = 0
			if state := health.states[rawURL]; state != nil {
				state.probeInFlight = false
			}
		}
	}
}

func (health *sourceHealth) snapshotLocked(url string, now time.Time) sourceSnapshot {
	snapshot := snapshotFor(health.states[url], now)
	performance := health.performance[url]
	if performance == nil {
		if health.states[url] == nil {
			snapshot.State = "unknown"
		}
	} else {
		snapshot.WindowBytes, snapshot.ActiveSeconds, snapshot.SampleSeconds = performance.window(now, performanceWindow)
		snapshot.UnitConnectionMbps = performanceRate(performance, now, performanceWindow)
		if health.states[url] == nil && performance.totalBytes == 0 && performance.successfulRequests == 0 {
			snapshot.State = "unknown"
		}
		if now.Before(performance.slowRetryAfter) {
			snapshot.State = "quarantined"
			snapshot.RetryAfter = performance.slowRetryAfter
			snapshot.RetryIn = performance.slowRetryAfter.Sub(now)
			snapshot.LastError = "source throughput below 25% of the best source for three windows"
		} else if !performance.slowRetryAfter.IsZero() && health.explorationURL == url {
			snapshot.State = "probing"
		}
	}
	state := health.exportLocked(url, now)
	snapshot.Health = &state
	return snapshot
}

func formatStateTime(value time.Time) string {
	if value.IsZero() {
		return ""
	}
	return value.UTC().Format(time.RFC3339Nano)
}

func (health *sourceHealth) exportLocked(url string, now time.Time) sourceHealthState {
	result := sourceHealthState{}
	if state := health.states[url]; state != nil {
		result.ConsecutiveFailures = state.consecutiveFailures
		result.QuarantineLevel = state.quarantineLevel
		result.RetryAfter = formatStateTime(state.retryAfter)
		result.LastError = state.lastError
	}
	if performance := health.performance[url]; performance != nil {
		result.SlowRetryAfter = formatStateTime(performance.slowRetryAfter)
		result.TotalBytes = performance.totalBytes
		result.SuccessfulRequests = performance.successfulRequests
		result.FailedRequests = performance.failedRequests
		result.StalledRequests = performance.stalledRequests
		result.WindowBytes, result.ActiveSeconds, result.SampleSeconds = performance.window(now, performanceWindow)
		result.ObservedAt = formatStateTime(performance.observedAt)
	}
	return result
}

func (health *sourceHealth) restore(states map[string]sourceHealthState, now time.Time) {
	health.mu.Lock()
	defer health.mu.Unlock()
	for rawURL, saved := range states {
		if health.activeEpoch[rawURL] == 0 {
			continue
		}
		retryAfter, _ := time.Parse(time.RFC3339Nano, saved.RetryAfter)
		if saved.ConsecutiveFailures > 0 || saved.QuarantineLevel > 0 {
			health.states[rawURL] = &sourceState{consecutiveFailures: max(0, saved.ConsecutiveFailures), quarantineLevel: min(3, max(0, saved.QuarantineLevel)), retryAfter: retryAfter, lastError: saved.LastError}
		}
		performance := health.performanceForLocked(rawURL)
		performance.slowRetryAfter, _ = time.Parse(time.RFC3339Nano, saved.SlowRetryAfter)
		performance.totalBytes = max(0, saved.TotalBytes)
		performance.successfulRequests = max(0, saved.SuccessfulRequests)
		performance.failedRequests = max(0, saved.FailedRequests)
		performance.stalledRequests = max(0, saved.StalledRequests)
		performance.observedAt, _ = time.Parse(time.RFC3339Nano, saved.ObservedAt)
		if age := now.Sub(performance.observedAt); age >= 0 && age < performanceWindow && saved.ActiveSeconds > 0 && saved.SampleSeconds > 0 && saved.WindowBytes >= 0 {
			// Spread the aggregate over its measured wall time to keep minimum-sample
			// checks valid without treating connection-seconds as wall seconds.
			seconds := min(60, max(1, int(saved.SampleSeconds)))
			for i := 0; i < seconds; i++ {
				second := performance.observedAt.Unix() - int64(i)
				performance.buckets[positiveMod(second, 61)] = performanceBucket{second: second, bytes: saved.WindowBytes / int64(seconds), activeSeconds: saved.ActiveSeconds / float64(seconds)}
			}
		}
	}
}

func (health *sourceHealth) emitMetrics(sink *statusSink, opts options, set *sourceSet, now time.Time) {
	// One complete snapshot per evaluation interval; request error events remain immediate.
	health.mu.Lock()
	if health.lastMetrics.IsZero() || now.Sub(health.lastMetrics) >= sourceEvaluationInterval {
		health.lastMetrics = now
	} else {
		health.mu.Unlock()
		return
	}
	health.mu.Unlock()
	generation := set.generation()
	for _, rawURL := range set.snapshot() {
		sink.emit(sourceStatusEvent(opts, rawURL, health.snapshot(rawURL, now), false, generation, health.epoch(rawURL)))
	}
}

func downloadSelectedSource(ctx context.Context, client *http.Client, rawURL, workerID string, total *atomic.Int64, health *sourceHealth, choice sourceChoice, connectionID int, primary []string) (int64, error) {
	requestCtx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	defer health.finishChoice(choice)
	var bytes atomic.Int64
	started := time.Now()
	done := make(chan struct{})
	monitorDone := make(chan struct{})
	go func() {
		defer close(monitorDone)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		lastAt, lastProgress := started, started
		lastBytes := int64(0)
		sample := func(now time.Time) {
			current := bytes.Load()
			health.account(rawURL, choice.epoch, lastAt, now, current-lastBytes)
			if current > lastBytes {
				lastProgress = now
			}
			lastAt, lastBytes = now, current
		}
		for {
			select {
			case <-done:
				sample(time.Now())
				return
			case <-requestCtx.Done():
				sample(time.Now())
				return
			case now := <-ticker.C:
				sample(now)
				health.evaluate(now)
				if now.Sub(lastProgress) >= 15*time.Second {
					health.mu.Lock()
					if health.activeEpoch[rawURL] == choice.epoch {
						health.performanceForLocked(rawURL).stalledRequests++
					}
					health.mu.Unlock()
					cancel(errSourceStalled)
					return
				}
				if health.shouldRotate(choice, connectionID, primary, started, now) {
					cancel(errSourceRotated)
					return
				}
			}
		}
	}()
	received, err := downloadOnceMeasured(requestCtx, client, expandRequestURL(rawURL), workerID, total, &bytes)
	close(done)
	<-monitorDone
	if cause := context.Cause(requestCtx); errors.Is(cause, errSourceRotated) || errors.Is(cause, errSourceStalled) {
		return received, cause
	}
	return received, err
}

func (health *sourceHealth) finishExploration(choice sourceChoice, now time.Time) (bool, sourceSnapshot) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch[choice.url] != choice.epoch {
		return false, sourceSnapshot{State: "unknown"}
	}
	performance := health.performanceForLocked(choice.url)
	performance.successfulRequests++
	best := 0.0
	for rawURL, candidate := range health.performance {
		if rawURL != choice.url && health.availableLocked(rawURL, now) {
			best = max(best, performanceRate(candidate, now, sourceEvaluationInterval))
		}
	}
	recovered := health.states[choice.url] != nil || !performance.slowRetryAfter.IsZero()
	if best > 0 && performanceRate(performance, now, sourceEvaluationInterval) < best*0.25 && !performance.slowRetryAfter.IsZero() {
		performance.slowRetryAfter = now.Add(slowSourceRetry)
		return false, health.snapshotLocked(choice.url, now)
	}
	performance.slowRetryAfter = time.Time{}
	performance.lowWindows = 0
	delete(health.states, choice.url)
	return recovered, health.snapshotLocked(choice.url, now)
}

func (health *sourceHealth) releaseConnection(connectionID int) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.explorationReservation == connectionID {
		health.explorationReservation = 0
	}
}

func (health *sourceHealth) failedExploration(choice sourceChoice, now time.Time) {
	health.mu.Lock()
	defer health.mu.Unlock()
	if health.activeEpoch[choice.url] != choice.epoch {
		return
	}
	if performance := health.performance[choice.url]; performance != nil && !performance.slowRetryAfter.IsZero() {
		performance.slowRetryAfter = now.Add(slowSourceRetry)
	}
}
