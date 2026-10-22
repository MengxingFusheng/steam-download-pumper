"""Evidence for router reboots, independent of collection/process availability."""

TIMETICKS_MODULUS = 1 << 32


def router_rebooted(previous: int, current: int, elapsed: float) -> bool:
    """A kernel uptime restart within the observation gap, not a tick wrap.

    TimeTicks wrap after about 497 days. Small collection/clock differences
    must not turn continuous router uptime into a fabricated offline gap.
    A router's exact power-off instant is not available from uptime alone.
    """
    if not (0 <= previous < TIMETICKS_MODULUS and 0 <= current < TIMETICKS_MODULUS):
        return False
    if not 0 < elapsed < TIMETICKS_MODULUS / 100:
        return False
    expected = elapsed * 100
    tolerance = max(500, expected * 0.0001)
    progressed = (current - previous) % TIMETICKS_MODULUS
    return abs(progressed - expected) > tolerance and current < expected - tolerance
