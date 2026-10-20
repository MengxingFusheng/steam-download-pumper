package main

import "time"

// Connections move in pairs only after the measured total rate improves. A
// high single-connection score alone must not move an entire line to a capped host.
type sourceAllocation struct {
	targetMbps      float64
	connections     int
	leader          string
	trial           string
	trialSlots      int
	trialBaseline   float64
	measuredAt      time.Time
	measuredBytes   int64
	mbps            float64
	underTargetFrom time.Time
	trialRetryAfter map[string]time.Time
}

func (health *sourceHealth) explorationIntervalLocked(now time.Time) time.Duration {
	if !health.allocation.underTargetFrom.IsZero() && now.Sub(health.allocation.underTargetFrom) >= time.Minute {
		return sourceEvaluationInterval
	}
	return sourceExploreInterval
}

func (allocation *sourceAllocation) delayTrial(rawURL string, now time.Time) {
	if allocation.trialRetryAfter == nil {
		allocation.trialRetryAfter = make(map[string]time.Time)
	}
	allocation.trialRetryAfter[rawURL] = now.Add(slowSourceRetry)
}

func (health *sourceHealth) allocationPoolLocked(primary, fallback []string, now time.Time) []string {
	allocation := &health.allocation
	if len(health.rankedLocked(primary, now)) == 0 || (allocation.targetMbps > 0 &&
		!allocation.underTargetFrom.IsZero() && now.Sub(allocation.underTargetFrom) >= time.Minute) {
		return combinedSources(primary, fallback)
	}
	// Keep an adopted fallback until a measured trial proves another source better.
	for _, rawURL := range fallback {
		if allocation.leader == rawURL {
			return combinedSources(primary, fallback)
		}
	}
	return primary
}

func (health *sourceHealth) preferredLocked(connectionID int, now time.Time) string {
	allocation := &health.allocation
	if allocation.trial != "" && connectionID <= allocation.trialSlots && health.availableLocked(allocation.trial, now) {
		return allocation.trial
	}
	return allocation.leader
}

func (health *sourceHealth) evaluateAllocationLocked(now time.Time) {
	allocation := &health.allocation
	var total int64
	for _, performance := range health.performance {
		total += performance.totalBytes
	}
	if allocation.measuredAt.IsZero() {
		allocation.measuredAt, allocation.measuredBytes = now, total
		return
	}
	elapsed := now.Sub(allocation.measuredAt).Seconds()
	if elapsed < sourceEvaluationInterval.Seconds() {
		return
	}
	allocation.mbps = float64(max(int64(0), total-allocation.measuredBytes)) * 8 / elapsed / 1e6
	allocation.measuredAt, allocation.measuredBytes = now, total
	if allocation.targetMbps > 0 && allocation.mbps < allocation.targetMbps*0.9 {
		if allocation.underTargetFrom.IsZero() {
			allocation.underTargetFrom = now
		}
	} else {
		allocation.underTargetFrom = time.Time{}
	}
	if allocation.trial != "" {
		if !health.availableLocked(allocation.trial, now) || allocation.mbps < allocation.trialBaseline*0.9 {
			allocation.delayTrial(allocation.trial, now)
			allocation.trial, allocation.trialSlots = "", 0
			return
		}
		if allocation.mbps >= allocation.trialBaseline*1.05 || (allocation.targetMbps > 0 && allocation.mbps >= allocation.targetMbps*0.9) {
			if allocation.trialSlots >= max(1, allocation.connections) {
				allocation.leader, allocation.trial = allocation.trial, ""
				allocation.trialSlots = 0
			} else {
				allocation.trialSlots = min(max(1, allocation.connections), allocation.trialSlots+2)
				allocation.trialBaseline = allocation.mbps
			}
		} else {
			// No benefit: restore the previous allocation and explore again later.
			allocation.delayTrial(allocation.trial, now)
			allocation.trial, allocation.trialSlots = "", 0
		}
		return
	}
	if allocation.targetMbps > 0 && allocation.mbps >= allocation.targetMbps*0.9 {
		return
	}
	best := allocation.leader
	leaderRate := performanceRate(health.performance[best], now, sourceEvaluationInterval)
	for rawURL, performance := range health.performance {
		_, _, sampled := performance.window(now, performanceWindow)
		if sampled < 5 || !health.availableLocked(rawURL, now) || now.Before(allocation.trialRetryAfter[rawURL]) {
			continue
		}
		if performanceRate(performance, now, sourceEvaluationInterval) > max(leaderRate*1.05,
			performanceRate(health.performance[best], now, sourceEvaluationInterval)) {
			best = rawURL
		}
	}
	if allocation.leader != "" && best != allocation.leader {
		allocation.trial, allocation.trialSlots = best, min(2, max(1, allocation.connections))
		allocation.trialBaseline = allocation.mbps
	}
}
