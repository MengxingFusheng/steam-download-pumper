package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func addPerformanceSeconds(health *sourceHealth, url string, start time.Time, seconds, connections int, bytesPerSecond int64) {
	for second := 0; second < seconds; second++ {
		for connection := 0; connection < connections; connection++ {
			health.account(url, health.epoch(url), start.Add(time.Duration(second)*time.Second), start.Add(time.Duration(second+1)*time.Second), bytesPerSecond)
		}
	}
}

func TestPerformanceUsesConnectionSecondsAndExpires(t *testing.T) {
	health := newSourceHealth()
	health.retain([]string{"one", "many", "unknown"})
	now := time.Unix(1000, 0)
	addPerformanceSeconds(health, "one", now, 45, 1, 12_500_000)
	addPerformanceSeconds(health, "many", now, 45, 10, 12_500_000)
	one := health.snapshot("one", now.Add(45*time.Second))
	many := health.snapshot("many", now.Add(45*time.Second))
	if one.UnitConnectionMbps != 100 || many.UnitConnectionMbps != 100 {
		t.Fatalf("rates one=%v many=%v", one.UnitConnectionMbps, many.UnitConnectionMbps)
	}
	if many.ActiveSeconds != 450 || many.SampleSeconds != 45 {
		t.Fatalf("connection vs wall seconds: %+v", many)
	}
	if snapshot := health.snapshot("unknown", now); snapshot.State != "unknown" {
		t.Fatalf("unsampled source=%+v", snapshot)
	}
	if snapshot := health.snapshot("one", now.Add(106*time.Second)); snapshot.WindowBytes != 0 || snapshot.ActiveSeconds != 0 {
		t.Fatalf("expired samples=%+v", snapshot)
	}
}

func TestSlowSourceNeedsThreeWindowsAndProbesRecover(t *testing.T) {
	health := newSourceHealth()
	health.retain([]string{"fast", "slow"})
	now := time.Unix(1000, 0)
	for second := 0; second < 60; second++ {
		addPerformanceSeconds(health, "fast", now.Add(time.Duration(second)*time.Second), 1, 1, 12_500_000)
		addPerformanceSeconds(health, "slow", now.Add(time.Duration(second)*time.Second), 1, 1, 1_000_000)
		if second == 29 || second == 44 || second == 59 {
			health.evaluate(now.Add(time.Duration(second+1) * time.Second))
		}
		if second == 44 && health.snapshot("slow", now.Add(45*time.Second)).State == "quarantined" {
			t.Fatal("quarantined before three windows")
		}
	}
	snapshot := health.snapshot("slow", now.Add(60*time.Second))
	if snapshot.State != "quarantined" || snapshot.ConsecutiveFailures != 0 || snapshot.RetryIn != 5*time.Minute {
		t.Fatalf("slow snapshot=%+v", snapshot)
	}
	if !health.shouldRotate(sourceChoice{url: "slow", epoch: health.epoch("slow")}, 1, []string{"fast", "slow"}, now, now.Add(60*time.Second)) {
		t.Fatal("active slow stream did not rotate")
	}
	retryAt := now.Add(6 * time.Minute)
	choice, _ := health.choose([]string{"fast", "slow"}, nil, 1, 0, retryAt)
	if choice.url != "slow" || !choice.exploring {
		t.Fatalf("slow recovery probe=%+v", choice)
	}
	health.finishChoice(choice)
	addPerformanceSeconds(health, "slow", retryAt, 15, 1, 12_500_000)
	recovered, snapshot := health.finishExploration(choice, retryAt.Add(15*time.Second))
	if !recovered || snapshot.State != "healthy" {
		t.Fatalf("probe recovery=%v snapshot=%+v", recovered, snapshot)
	}
}

func TestSelectionBestThreeAndOnlyOneExploration(t *testing.T) {
	health := newSourceHealth()
	sources := []string{"fast", "second", "third", "other", "last"}
	health.retain(sources)
	now := time.Unix(1000, 0)
	for i, url := range sources {
		addPerformanceSeconds(health, url, now, 40, 1, int64(5-i)*1_000_000)
	}
	now = now.Add(40 * time.Second)
	choice, _ := health.choose(sources, nil, 1, 0, now)
	if !choice.exploring || choice.url != "other" {
		t.Fatalf("first explore=%+v", choice)
	}
	for i := 0; i < 10; i++ {
		other, _ := health.choose(sources, nil, 2, i, now)
		if other.exploring || other.url == "other" || other.url == "last" {
			t.Fatalf("ordinary choice=%+v", other)
		}
	}
	health.finishChoice(choice)
	if next, _ := health.choose(sources, nil, 1, 0, now.Add(59*time.Second)); next.exploring {
		t.Fatal("explored sooner than sixty seconds")
	}
	if next, _ := health.choose(sources, nil, 1, 0, now.Add(time.Minute)); !next.exploring {
		t.Fatal("did not explore after interval")
	}
}

func TestFallbackOnlyWhenPrimaryUnavailableAndReturnsToPrimary(t *testing.T) {
	health := newSourceHealth()
	health.retain([]string{"primary", "fallback"})
	now := time.Unix(1000, 0)
	choice, _ := health.choose([]string{"primary"}, []string{"fallback"}, 1, 0, now)
	if choice.url != "primary" || choice.fallback {
		t.Fatalf("initial=%+v", choice)
	}
	health.failedCurrent("primary", health.epoch("primary"), now, "HTTP 503", false)
	choice, _ = health.choose([]string{"primary"}, []string{"fallback"}, 1, 0, now)
	if choice.url != "fallback" || !choice.fallback {
		t.Fatalf("fallback=%+v", choice)
	}
	if !health.shouldRotate(choice, 1, []string{"primary"}, now, now.Add(time.Second)) {
		t.Fatal("fallback stream prevented primary recovery")
	}
}

func TestHealthStateRestoresAndReloadKeepsSameURL(t *testing.T) {
	health := newSourceHealth()
	health.retain([]string{"keep", "remove"})
	now := time.Now().Truncate(time.Second)
	addPerformanceSeconds(health, "keep", now.Add(-40*time.Second), 40, 2, 1_000_000)
	for i := 0; i < 3; i++ {
		health.failedCurrent("keep", health.epoch("keep"), now, "failed", false)
	}
	saved := health.snapshot("keep", now).Health
	restored := newSourceHealth()
	restored.retain([]string{"keep", "remove"})
	restored.restore(map[string]sourceHealthState{"keep": *saved}, now)
	snapshot := restored.snapshot("keep", now)
	if snapshot.State != "quarantined" || snapshot.ConsecutiveFailures != 3 || snapshot.UnitConnectionMbps != 8 || snapshot.SampleSeconds != 40 {
		t.Fatalf("restored=%+v health=%+v", snapshot, snapshot.Health)
	}
	restored.retain([]string{"keep", "new"})
	if snapshot := restored.snapshot("keep", now); snapshot.ConsecutiveFailures != 3 || snapshot.UnitConnectionMbps != 8 {
		t.Fatalf("reload lost state=%+v", snapshot)
	}
	if _, exists := restored.performance["remove"]; exists {
		t.Fatal("removed metrics retained")
	}
	expired := newSourceHealth()
	expired.retain([]string{"keep"})
	expired.restore(map[string]sourceHealthState{"keep": *saved}, now.Add(61*time.Second))
	if snapshot := expired.snapshot("keep", now.Add(61*time.Second)); snapshot.ActiveSeconds != 0 {
		t.Fatal("restored stale rate sample")
	}
}

func TestSourceFileAcceptsFallbackAndHealthButRejectsPrivateSchemes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sources.json")
	data := `{"generation":"v2","sources":["https://primary.example/file"],"fallback_sources":["https://local.example/file"],"health":{"https://primary.example/file":{"consecutive_failures":3,"quarantine_level":1,"retry_after":"2026-10-19T12:00:00Z"}}}`
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	config, err := loadSourceConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(config.FallbackSources) != 1 || config.Health["https://primary.example/file"].ConsecutiveFailures != 3 {
		t.Fatalf("config=%+v", config)
	}
	config.FallbackSources = []string{"file:///private"}
	if err := validateSourceConfig(config); err == nil {
		t.Fatal("invalid fallback accepted")
	}
}

func TestLongStreamingRequestReportsProgressAndRotatesWithoutHTTPFailure(t *testing.T) {
	started := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started <- struct{}{}
		ticker := time.NewTicker(25 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-r.Context().Done():
				return
			case <-ticker.C:
				if _, err := w.Write(make([]byte, 4096)); err != nil {
					return
				}
				w.(http.Flusher).Flush()
			}
		}
	}))
	defer server.Close()
	health := newSourceHealth()
	health.retain([]string{server.URL})
	choice := sourceChoice{url: server.URL, epoch: health.epoch(server.URL)}
	var total atomic.Int64
	result := make(chan error, 1)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go func() {
		_, err := downloadSelectedSource(ctx, server.Client(), server.URL, "test", &total, health, choice, 1, []string{server.URL})
		result <- err
	}()
	<-started
	deadline := time.Now().Add(3 * time.Second)
	for health.snapshot(server.URL, time.Now()).WindowBytes == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if snapshot := health.snapshot(server.URL, time.Now()); snapshot.WindowBytes == 0 || snapshot.ActiveSeconds <= 0 {
		t.Fatalf("no progress before EOF: %+v", snapshot)
	}
	health.mu.Lock()
	health.performanceForLocked(server.URL).slowRetryAfter = time.Now().Add(time.Minute)
	health.mu.Unlock()
	select {
	case err := <-result:
		if !errors.Is(err, errSourceRotated) {
			t.Fatalf("rotation error=%v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("slow long stream not cancelled")
	}
	if total.Load() == 0 || health.snapshot(server.URL, time.Now()).ConsecutiveFailures != 0 {
		t.Fatal("intentional rotation counted as HTTP failure")
	}
}

func TestLongRequestRejoinsBestThreeAfterNewMeasurements(t *testing.T) {
	health := newSourceHealth()
	urls := []string{"fast", "second", "third", "old"}
	health.retain(urls)
	now := time.Unix(1000, 0)
	for i, url := range urls {
		addPerformanceSeconds(health, url, now, 40, 1, int64(10-i)*1_000_000)
	}
	health.lastExplore = now.Add(40 * time.Second)
	choice := sourceChoice{url: "old", epoch: health.epoch("old")}
	if !health.shouldRotate(choice, 1, urls, now, now.Add(40*time.Second)) {
		t.Fatal("long stream stayed on a source outside best three")
	}
	if health.snapshot("old", now.Add(40*time.Second)).ConsecutiveFailures != 0 {
		t.Fatal("rank reassignment counted as source failure")
	}
}

func TestStalledStreamingSourceStopsBeforeLineTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(make([]byte, 4096))
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer server.Close()
	health := newSourceHealth()
	health.retain([]string{server.URL})
	choice := sourceChoice{url: server.URL, epoch: health.epoch(server.URL)}
	var total atomic.Int64
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	received, err := downloadSelectedSource(ctx, server.Client(), server.URL, "stall-test", &total, health, choice, 1, []string{server.URL})
	if !errors.Is(err, errSourceStalled) {
		t.Fatalf("stall error=%v", err)
	}
	if received != 4096 || total.Load() != 4096 {
		t.Fatalf("stall bytes received=%d counted=%d", received, total.Load())
	}
	snapshot := health.snapshot(server.URL, time.Now())
	if snapshot.Health.StalledRequests != 1 {
		t.Fatalf("stall metrics=%+v", snapshot.Health)
	}
	health.failedCurrent(server.URL, choice.epoch, time.Now(), err.Error(), false)
	if snapshot := health.snapshot(server.URL, time.Now()); snapshot.ConsecutiveFailures != 1 || snapshot.Health.FailedRequests != 1 {
		t.Fatalf("stall fault=%+v", snapshot)
	}
}

func TestShutdownReportsBytesAfterLastPeriodicStatus(t *testing.T) {
	var hits atomic.Int32
	waiting := make(chan struct{}, 1)
	const bodySize = 1024 * 1024
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) == 1 {
			_, _ = w.Write(make([]byte, bodySize))
			return
		}
		waiting <- struct{}{}
		<-r.Context().Done()
	}))
	defer server.Close()
	output := &lockedBuffer{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- run(ctx, options{workerID: "final", lineID: "a", connections: 1, maxConnections: 1, readTimeoutSeconds: 5,
			statusIntervalSeconds: 60, urls: []string{server.URL}, statusWriter: output})
	}()
	select {
	case <-waiting:
	case <-time.After(3 * time.Second):
		t.Fatal("first complete download did not finish")
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown did not complete")
	}
	var final *statusEvent
	for _, line := range strings.Split(strings.TrimSpace(output.String()), "\n") {
		var event statusEvent
		if err := json.Unmarshal([]byte(line), &event); err != nil {
			t.Fatal(err)
		}
		if event.Type == "status" {
			copy := event
			final = &copy
		}
	}
	if final == nil || final.Bytes != bodySize || final.Connections != 0 {
		t.Fatalf("missing final byte snapshot=%+v output=%s", final, output.String())
	}
}

func TestFullRemotePoolCanKeepLocalFallbackSources(t *testing.T) {
	config := sourceFileConfig{FallbackSources: []string{"https://fallback.example/file"}}
	for i := 0; i < 100; i++ {
		config.Sources = append(config.Sources, fmt.Sprintf("https://source%d.example/download", i))
	}
	if err := validateSourceConfig(config); err != nil {
		t.Fatal(err)
	}
	config.Sources = append(config.Sources, "https://overflow.example/file")
	if err := validateSourceConfig(config); err == nil {
		t.Fatal("more than 100 remote sources accepted")
	}
}
