package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

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
	delay := health.failed("http://bad.test/file", now)

	if delay != time.Second {
		t.Fatalf("first retry delay = %s, want 1s", delay)
	}
	if health.ready("http://bad.test/file", now.Add(500*time.Millisecond)) {
		t.Fatal("failed source became ready before cooldown elapsed")
	}
	if !health.recovered("http://bad.test/file") {
		t.Fatal("recovered did not report the prior failure")
	}
	if !health.ready("http://bad.test/file", now) {
		t.Fatal("recovered source remained in cooldown")
	}
}
