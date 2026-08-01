#!/usr/bin/env python3
"""Prove heap_verdict fails on a real decline and stays quiet on healthy noise.

The shapes come from ESPresense#2309: the S3 slid 85KB -> 33KB over ~6h with the
fingerprint count flat, while the C3 sat at ~110KB for the same window. One of those
must fail the build and the other must not.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hil_monitor import HEAP_TREND_MIN_SECS, heap_verdict  # noqa: E402

HOURS_6 = 6 * 3600


def samples(values, fingerprints=29):
    return [(i * 60.0, v, v // 3, fingerprints) for i, v in enumerate(values)]


def ramp(start, end, n):
    step = (end - start) / (n - 1)
    return [int(start + step * i) for i in range(n)]


def wobble(level, n):
    """Healthy heap is noisy, not flat — alternate around the level."""
    return [level + (2000 if i % 2 else -2000) for i in range(n)]


# The S3: a steady slide with the fingerprint count flat. Must fail.
verdict = heap_verdict(samples(ramp(85000, 33000, 360)), HOURS_6)
assert verdict and "fell" in verdict, f"leak not caught: {verdict!r}"
assert "fingerprints 29->29" in verdict, verdict
print(f"leak      -> {verdict}")

# The C3: noisy but level. Must pass.
verdict = heap_verdict(samples(wobble(110000, 360)), HOURS_6)
assert verdict is None, f"healthy node failed: {verdict!r}"
print("healthy   -> pass")

# A dip that recovers is not a leak — the tail is what matters, and the median at each
# edge keeps a single ugly sample from deciding the build.
dip = wobble(110000, 150) + ramp(110000, 60000, 60) + ramp(60000, 108000, 150)
verdict = heap_verdict(samples(dip), HOURS_6)
assert verdict is None, f"transient dip failed the build: {verdict!r}"
print("dip       -> pass")

# A 25% floor means a shallow slide is tolerated; anything past it is not.
assert heap_verdict(samples(ramp(100000, 80000, 360)), HOURS_6) is None, "20% should pass"
assert heap_verdict(samples(ramp(100000, 70000, 360)), HOURS_6), "30% should fail"
print("threshold -> 20% passes, 30% fails")

# A 3-minute PR run cannot say anything about a multi-hour slope, so it must not try.
assert heap_verdict(samples(ramp(85000, 33000, 360)), 180) is None, "short run must not judge"
assert heap_verdict(samples(ramp(85000, 33000, 8)), HOURS_6) is None, "too few samples to judge"
print("short run -> skipped")

# Firmware without /json/tele must fail loudly. A heap check that silently samples nothing
# and still reports PASS is how #2309 survived an 8h soak in the first place.
verdict = heap_verdict([], HOURS_6, ["missing"])
assert verdict and "missing" in verdict, f"old firmware not flagged: {verdict!r}"
print(f"no endpoint -> {verdict}")

# But a node the runner simply cannot reach is not a firmware fault — still a skip.
assert heap_verdict([], HOURS_6) is None, "unreachable node must not fail the build"
assert heap_verdict([], 180, ["missing"]) is None, "short run still must not judge"
print("unreachable -> skipped")

print("all heap trend checks passed")
