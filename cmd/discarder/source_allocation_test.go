package main

import (
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
