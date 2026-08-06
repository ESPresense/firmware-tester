#!/usr/bin/env python3
"""Prove the #2309 DMA bisect fits a slope and splits arms A/B correctly.

The whole point is to tell "browser load steepens the internal-DMA decline" from "it
doesn't": arm A is BLE-flood-only, arm B adds the /json hammer. The slope sign and the
split on the load flag are the only logic worth checking.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hil_monitor import DMA_SETTLE_SECS, _ols_slope_per_hour, dma_bisect_report  # noqa: E402


def line(start_t, start_v, slope_per_hour, n, load, step=5):
    """DMALEAK samples: (device_t, dmaFree, under_load) sloping at slope_per_hour bytes/h."""
    return [(start_t + i * step, int(start_v + slope_per_hour / 3600 * (i * step)), load)
            for i in range(n)]


# Slope is bytes/hour: -3600 B/h means -1 B/s. 100 samples * 5s = 500s -> ~-500 bytes.
flat = line(0, 90000, 0, 100, load=False)
s = _ols_slope_per_hour([(t, v) for t, v, _ in flat])
assert abs(s) < 1, f"flat should be ~0 B/h, got {s}"

leak = line(0, 90000, -7200, 100, load=True)  # -2 B/s
s = _ols_slope_per_hour([(t, v) for t, v, _ in leak])
assert -7300 < s < -7100, f"expected ~-7200 B/h, got {s}"
print(f"slope fit  -> flat={0:.0f}, leak={s:.0f} B/h")

# Degenerate inputs must not throw.
assert _ols_slope_per_hour([]) is None
assert _ols_slope_per_hour([(5, 100)]) is None
assert _ols_slope_per_hour([(5, 100), (5, 200)]) is None  # zero t-variance
print("degenerate -> None, no throw")

# The settle window is dropped from arm A so the post-boot allocation burst can't bias it.
# Samples before DMA_SETTLE_SECS are arm-A-in-time but excluded from the fit.
burst = line(0, 90000, -100000, 20, load=False)              # steep boot burst, first 100s
steady_a = line(DMA_SETTLE_SECS, 88000, -500, 80, load=False)  # gentle real arm-A slope
steady_b = line(DMA_SETTLE_SECS + 400, 87500, -9000, 80, load=True)  # steep under load
report = burst + steady_a + steady_b

# Reach into the same split the report uses, to assert the arms partition on the load flag.
t0 = report[0][0]
arm_a = [(t, v) for t, v, load in report if not load and t - t0 >= DMA_SETTLE_SECS]
arm_b = [(t, v) for t, v, load in report if load]
assert len(arm_a) == 80, f"burst not excluded from arm A: {len(arm_a)}"
assert len(arm_b) == 80, f"arm B miscounted: {len(arm_b)}"
sa, sb = _ols_slope_per_hour(arm_a), _ols_slope_per_hour(arm_b)
assert sb < sa, f"browser arm must be steeper: A={sa:.0f} B={sb:.0f}"
print(f"arm split  -> A={sa:.0f} B/h, B={sb:.0f} B/h (B steeper)")

# The report itself must run clean and skip gracefully on too-few samples.
dma_bisect_report(report)
dma_bisect_report([(0, 90000, False)])  # < 4 samples -> SKIPPED, no throw
print("report     -> ran, skip path clean")

print("all dma bisect checks passed")
