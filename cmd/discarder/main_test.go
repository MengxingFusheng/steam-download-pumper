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

	got, err := downloadOnce(context.Background(), newHTTPClient(5*time.Second), server.URL, "1")
	if err != nil {
		t.Fatalf("downloadOnce returned error: %v", err)
	}

	if got != 6 {
		t.Fatalf("downloadOnce bytes = %d, want 6", got)
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
