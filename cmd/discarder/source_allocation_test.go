package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestOrdinaryConnectionsConcentrateOnLeader(t *testing.T) {
	h := newSourceHealth()
	sources := []string{"fast", "second", "slow"}
	h.retain(sources)
	now := time.Unix(1000, 0)
	for i, u := range sources {
		addPerformanceSeconds(h, u, now, 40, 1, int64(3-i)*1_000_000)
	}
	now = now.Add(40 * time.Second)
	h.lastExplore = now
	for id := 1; id <= 12; id++ {
		c, _ := h.choose(sources, nil, id, id, now)
		if c.url != "fast" {
			t.Fatalf("connection %d got %s", id, c.url)
		}
	}
}

func TestTrialMovesOnlyTwoThenRollsBackWhenTotalFalls(t *testing.T) {
	h := newSourceHealth()
	h.retain([]string{"leader", "candidate"})
	now := time.Unix(1000, 0)
	h.allocation = sourceAllocation{leader: "leader", connections: 12, measuredAt: now, measuredBytes: 0}
	addPerformanceSeconds(h, "leader", now, 15, 10, 1_000_000)
	addPerformanceSeconds(h, "candidate", now, 15, 1, 2_000_000)
	h.evaluateAllocationLocked(now.Add(15 * time.Second))
	if h.allocation.trial != "candidate" || h.allocation.trialSlots != 2 {
		t.Fatalf("trial=%+v", h.allocation)
	}
	if h.preferredLocked(3, now.Add(15*time.Second)) != "leader" {
		t.Fatal("moved more than two connections")
	}
	h.evaluateAllocationLocked(now.Add(30 * time.Second))
	if h.allocation.trial != "" || h.allocation.leader != "leader" {
		t.Fatalf("rollback=%+v", h.allocation)
	}
	if !h.performance["candidate"].slowRetryAfter.After(now.Add(30 * time.Second)) {
		t.Fatal("trial retry was not delayed")
	}
}

func TestSlowPrimaryOpensFallbackExplorationAfterMinute(t *testing.T) {
	h := newSourceHealth()
	h.retain([]string{"primary", "fallback"})
	now := time.Unix(1000, 0)
	h.allocation = sourceAllocation{leader: "primary", targetMbps: 500, connections: 12, underTargetFrom: now}
	if got := h.allocationPoolLocked([]string{"primary"}, []string{"fallback"}, now.Add(59*time.Second)); len(got) != 1 {
		t.Fatal("fallback opened early")
	}
	if got := h.allocationPoolLocked([]string{"primary"}, []string{"fallback"}, now.Add(time.Minute)); len(got) != 2 {
		t.Fatal("slow healthy primary hid fallback")
	}
}

func TestRealWorkersKeepMostTrafficOnFastEndpoint(t *testing.T) {
	var fastBytes, slowBytes atomic.Int64
	handler := func(counter *atomic.Int64, size int, pause time.Duration) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			block := make([]byte, size)
			for n := 0; n < 200; n++ {
				select {
				case <-r.Context().Done():
					return
				default:
				}
				count, err := w.Write(block)
				counter.Add(int64(count))
				if err != nil {
					return
				}
				w.(http.Flusher).Flush()
				select {
				case <-r.Context().Done():
					return
				case <-time.After(pause):
				}
			}
		}
	}
	fast := httptest.NewServer(handler(&fastBytes, 32768, 10*time.Millisecond))
	defer fast.Close()
	slow := httptest.NewServer(handler(&slowBytes, 2048, 30*time.Millisecond))
	defer slow.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	err := run(ctx, options{connections: 12, maxConnections: 12, readTimeoutSeconds: 30, statusIntervalSeconds: 1,
		minSessionSeconds: 300, urls: []string{fast.URL + "/download?size=67108864", slow.URL + "/download?size=67108864"},
		targetMbps: 500, statusWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	if fastBytes.Load() == 0 {
		t.Fatal("fast endpoint was unused")
	}
	if slowBytes.Load() > fastBytes.Load()/10 {
		t.Fatalf("slow endpoint consumed ordinary allocation: fast=%d slow=%d", fastBytes.Load(), slowBytes.Load())
	}
}
