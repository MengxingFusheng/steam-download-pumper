package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestValidateOptionsRejectsMoreThanTwelveConnections(t *testing.T) {
	for _, opts := range []options{
		{connections: 13, maxConnections: 13, urls: []string{"http://example.test/file"}},
		{connections: 1, maxConnections: 13, urls: []string{"http://example.test/file"}},
	} {
		if err := validateOptions(&opts); err == nil || !strings.Contains(err.Error(), "12") {
			t.Fatalf("expected hard-cap error, got %v", err)
		}
	}
}

func TestCountingWriterReportsBytesBeforeRequestCompletes(t *testing.T) {
	var total atomic.Int64
	writer := countingWriter{total: &total}
	n, err := writer.Write(make([]byte, 4096))
	if err != nil || n != 4096 || total.Load() != 4096 {
		t.Fatalf("n=%d total=%d err=%v", n, total.Load(), err)
	}
}

func TestStatusEventIsNewlineDelimitedJSON(t *testing.T) {
	var output bytes.Buffer
	err := writeStatus(&output, statusEvent{
		Type: "status", LineID: "line-1", BindIP: "192.168.1.233", Bytes: 42, Connections: 2,
	})
	if err != nil || !strings.HasSuffix(output.String(), "\n") {
		t.Fatalf("output=%q err=%v", output.String(), err)
	}
	var decoded statusEvent
	if err := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &decoded); err != nil || decoded.Bytes != 42 {
		t.Fatalf("decoded=%+v err=%v", decoded, err)
	}
}

func TestDownloadOnceDiscardsBodyAndReturnsByteCount(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("abcdef"))
	}))
	defer server.Close()

	got, err := downloadOnce(context.Background(), newHTTPClient(5*time.Second, ""), server.URL, "1")
	if err != nil {
		t.Fatalf("downloadOnce returned error: %v", err)
	}

	if got != 6 {
		t.Fatalf("downloadOnce bytes = %d, want 6", got)
	}
}

func TestNewHTTPClientAcceptsBindIP(t *testing.T) {
	client := newHTTPClient(5*time.Second, "127.0.0.1")

	if client == nil {
		t.Fatal("newHTTPClient returned nil")
	}
}

func TestHTTPClientDoesNotApplyHeaderTimeoutToStreamingBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		for range 8 {
			_, _ = w.Write([]byte("x"))
			if flusher, ok := w.(http.Flusher); ok {
				flusher.Flush()
			}
			time.Sleep(10 * time.Millisecond)
		}
	}))
	defer server.Close()

	got, err := downloadOnce(context.Background(), newHTTPClient(25*time.Millisecond, ""), server.URL, "1")
	if err != nil {
		t.Fatalf("streaming response was interrupted: %v", err)
	}
	if got != 8 {
		t.Fatalf("downloadOnce bytes = %d, want 8", got)
	}
}

func TestRunReconnectsShortFilesUntilCanceled(t *testing.T) {
	var hits atomic.Int32
	ctx, cancel := context.WithCancel(context.Background())
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if hits.Add(1) >= 3 {
			cancel()
		}
		_, _ = w.Write([]byte("chunk"))
	}))
	defer server.Close()

	err := run(ctx, options{
		workerID:             "7",
		minSessionSeconds:    1,
		restartJitterSeconds: 0,
		readTimeoutSeconds:   5,
		urls:                 []string{server.URL},
	})
	if err != nil {
		t.Fatalf("run returned error: %v", err)
	}
	if hits.Load() < 3 {
		t.Fatalf("hits = %d, want at least 3", hits.Load())
	}
}

func TestSourceHealthSharesFailureCooldownAcrossConnections(t *testing.T) {
	health := newSourceHealth()
	now := time.Now()
	result := health.failed("http://bad.test/file", now, "timeout", false)

	if result.RetryIn != time.Second {
		t.Fatalf("first retry delay = %s, want 1s", result.RetryIn)
	}
	if allowed, _, _ := health.claim("http://bad.test/file", now.Add(500*time.Millisecond)); allowed {
		t.Fatal("failed source became ready before cooldown elapsed")
	}
	if recovered, snapshot := health.succeeded("http://bad.test/file"); !recovered {
		t.Fatal("recovered did not report the prior failure")
	} else if snapshot.State != "healthy" || snapshot.ConsecutiveFailures != 0 {
		t.Fatalf("recovered snapshot = %+v", snapshot)
	}
	if allowed, _, probe := health.claim("http://bad.test/file", now); !allowed || probe {
		t.Fatal("recovered source remained in cooldown")
	}
}

func TestSourceCircuitBreakerQuarantinesAndEscalatesFailedProbes(t *testing.T) {
	health := newSourceHealth()
	url := "http://bad.test/file"
	now := time.Date(2026, 7, 20, 8, 0, 0, 0, time.UTC)

	for attempt, expected := range []time.Duration{time.Second, 2 * time.Second, 10 * time.Minute} {
		allowed, _, probe := health.claim(url, now)
		if !allowed || probe {
			t.Fatalf("attempt %d claim allowed=%v probe=%v", attempt+1, allowed, probe)
		}
		result := health.failed(url, now, "timeout", false)
		if result.RetryIn != expected {
			t.Fatalf("attempt %d retry = %s, want %s", attempt+1, result.RetryIn, expected)
		}
		now = now.Add(expected)
	}

	allowed, _, probe := health.claim(url, now)
	if !allowed || !probe {
		t.Fatalf("first half-open claim allowed=%v probe=%v", allowed, probe)
	}
	if anotherAllowed, _, _ := health.claim(url, now); anotherAllowed {
		t.Fatal("second worker claimed the same half-open probe")
	}
	result := health.failed(url, now, "still unavailable", true)
	if result.RetryIn != 30*time.Minute || result.State != "quarantined" {
		t.Fatalf("failed first probe = %+v", result)
	}

	now = now.Add(30 * time.Minute)
	allowed, _, probe = health.claim(url, now)
	if !allowed || !probe {
		t.Fatalf("second half-open claim allowed=%v probe=%v", allowed, probe)
	}
	result = health.failed(url, now, "still unavailable", true)
	if result.RetryIn != 60*time.Minute {
		t.Fatalf("failed second probe retry = %s, want 60m", result.RetryIn)
	}

	now = now.Add(60 * time.Minute)
	allowed, _, probe = health.claim(url, now)
	if !allowed || !probe {
		t.Fatalf("third half-open claim allowed=%v probe=%v", allowed, probe)
	}
	result = health.failed(url, now, "still unavailable", true)
	if result.RetryIn != 60*time.Minute {
		t.Fatalf("failed third probe retry = %s, want capped 60m", result.RetryIn)
	}
}

func TestSourceCircuitBreakerRecoveryClearsQuarantine(t *testing.T) {
	health := newSourceHealth()
	url := "http://recovered.test/file"
	now := time.Date(2026, 7, 20, 8, 0, 0, 0, time.UTC)

	for _, advance := range []time.Duration{time.Second, 2 * time.Second, 10 * time.Minute} {
		if allowed, _, _ := health.claim(url, now); !allowed {
			t.Fatal("source was unavailable before failure")
		}
		health.failed(url, now, "timeout", false)
		now = now.Add(advance)
	}
	if allowed, _, probe := health.claim(url, now); !allowed || !probe {
		t.Fatalf("recovery probe allowed=%v probe=%v", allowed, probe)
	}
	recovered, snapshot := health.succeeded(url)
	if !recovered || snapshot.State != "healthy" || snapshot.ConsecutiveFailures != 0 || snapshot.RetryIn != 0 {
		t.Fatalf("recovered snapshot = %+v", snapshot)
	}
	if allowed, _, probe := health.claim(url, now); !allowed || probe {
		t.Fatalf("healthy claim after recovery allowed=%v probe=%v", allowed, probe)
	}
}

func TestWorkerDoesNotRestartWhenAllSourcesAreQuarantined(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 3500*time.Millisecond)
	defer cancel()
	urls := []string{
		server.URL + "/one",
		server.URL + "/two",
		server.URL + "/three",
		server.URL + "/four",
	}
	var total atomic.Int64
	var current atomic.Value
	current.Store("")
	err := runWorker(ctx, options{
		workerID:             "quarantine-test",
		lineID:               "line-1",
		minSessionSeconds:    1,
		restartJitterSeconds: 0,
		readTimeoutSeconds:   1,
		urls:                 urls,
	}, 1, newSourceHealth(), &total, &current, &statusSink{})

	if err != nil {
		t.Fatalf("worker exited instead of waiting for source probes: %v", err)
	}
}

func TestWorkerKeepsHealthyTrafficWhileFailedSourceIsQuarantined(t *testing.T) {
	var failedHits atomic.Int32
	failed := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		failedHits.Add(1)
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer failed.Close()
	var healthyHits atomic.Int32
	healthy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		healthyHits.Add(1)
		_, _ = w.Write(make([]byte, 32*1024))
	}))
	defer healthy.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 3500*time.Millisecond)
	defer cancel()
	var total atomic.Int64
	var current atomic.Value
	current.Store("")
	err := runWorker(ctx, options{
		workerID:             "mixed-source-test",
		lineID:               "line-1",
		minSessionSeconds:    1,
		restartJitterSeconds: 0.002,
		readTimeoutSeconds:   1,
		urls:                 []string{failed.URL, healthy.URL},
	}, 1, newSourceHealth(), &total, &current, &statusSink{})

	if err != nil {
		t.Fatalf("worker returned error: %v", err)
	}
	if got := failedHits.Load(); got != 3 {
		t.Fatalf("failed source requests = %d, want exactly 3 before quarantine", got)
	}
	if healthyHits.Load() < 10 || total.Load() == 0 {
		t.Fatalf("healthy hits=%d total_bytes=%d", healthyHits.Load(), total.Load())
	}
}
