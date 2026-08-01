#!/usr/bin/env python3
"""
HIL serial monitor for ESPresense firmware.

Exit codes:
  0 = pass (duration elapsed cleanly)
  1 = crash detected
  2 = boot timeout (IP address not seen within 90s)
  3 = serial error
  4 = no scan results seen
  5 = firmware MQTT reconnect limit reached
  6 = /json endpoint misbehaved under concurrent load
  7 = node restarted mid-run
  8 = free heap declined over the run
"""

import argparse
import http.client
import json
import re
import statistics
import sys
import threading
import time

import serial

CRASH_PATTERNS = [
    "Guru Meditation Error",
    "abort()",
    "Backtrace:",
    "TWDT",
    "Task watchdog got triggered",
]

BOOT_SUCCESS_PATTERN = "IP address:"
BOOT_TIMEOUT_SECS = 90
MQTT_RECONNECT_LIMIT_PATTERN = "Too many reconnect attempts; Restarting"
DEFAULT_SCAN_RESULT_PATTERN = r"^\s*\d+\s+\w+\s+\|"

# setup() prints this on every boot. Seeing it *after* boot was confirmed means the node
# went down and came back — a silent restart loop otherwise passes the whole window,
# because every other check here is satisfied by the fresh boot (ESPresense#2309).
REBOOT_PATTERN = "Pre-Setup Free Mem:"
# The firmware's own low-heap watchdog announcing a restart. Same failure, better message.
OOM_RESTART_PATTERN = "Out of memory for"

IP_PATTERN = re.compile(r"IP address:\s*(\d+\.\d+\.\d+\.\d+)")
JSON_CHECK_DELAY_SECS = 15  # let the web server settle after boot
JSON_CHECK_WORKERS = 3      # concurrent connections — enough to reliably collide on serveJson
JSON_CHECK_REQUESTS = 12    # sequential requests per worker, on one kept-alive connection

# Heap trend, sampled from /json/tele. Deliberately not /json: that endpoint refuses with
# 429 when it cannot afford a 12KB document, so heap numbers hung off it would vanish
# exactly as a node degraded — the samples would stop before the decline they are meant to
# measure and the verdict below would compare a healthier window. /json/tele is
# allocation-free and answers at any heap level.
#
# A leak shows as freeHeap sliding while fingerprints stays flat; a fragmentation problem
# shows as maxHeap sliding while freeHeap holds; fingerprint churn shows as both moving
# with the device count. All three are printed so the log answers which without a re-run.
HEAP_TELE_PATH = "/json/tele"
HEAP_SAMPLE_SECS = 60
HEAP_SETTLE_SECS = 120        # ignore the post-boot allocation burst
HEAP_TREND_MIN_SECS = 1800    # below this the window is too short for a slope to mean anything
HEAP_TREND_EDGE = 5           # samples used for the median at each end
HEAP_DECLINE_FRAC = 0.25      # fail if the tail lost more than this much of the baseline


def json_endpoint_check(ip, bug):
    """Detect the serveJson() double-send under concurrent load.

    serveJson() serialises itself with a `servingJson` flag and returns 429 when busy.
    A missing early-return there sends TWO responses down one socket for one request.
    The extra one is left in the buffer, so every later response on that connection is
    off by one — and each shifted response is still a valid 200. That is the only thing
    this check is looking for, and it is the only thing that fails the build.

    To surface the shift, each worker reuses one keep-alive connection and alternates two
    endpoints with different shapes: /json has no "devices" key, /json/devices does. A
    shifted stream answers the wrong question, or hands back the 429 text as a 200 body.

    The rule is simple: a 200 must be a complete, correct JSON object for the URL that
    asked for it. Refusing to serve under pressure is fine — a 429 (busy or low heap) is
    the designed refusal and is silently accepted; anything odder that still isn't the bug
    (a dropped TCP connection, an unexpected non-200/429 status) is counted into the
    "shed under load" tally, logged, and reconnected past, never failed on. But a *200*
    that is null, truncated, unparseable, or the wrong document is the server lying about
    success — the double-send, or the low-heap null-body path (fixed in firmware by
    refusing with 429) — and it fails the build.
    """
    TOO_MANY = b"Too Many Requests"

    def worker(n, drops):
        conn = None
        try:
            for i in range(JSON_CHECK_REQUESTS):
                path = "/json/devices" if i % 2 else "/json"
                try:
                    if conn is None:
                        conn = http.client.HTTPConnection(ip, timeout=10)
                    conn.request("GET", path)
                    resp = conn.getresponse()
                    body = resp.read()
                except (OSError, http.client.HTTPException) as e:
                    # Connection-level hiccup: the node shed load. Not the bug. Reconnect.
                    drops.append(str(e))
                    if conn is not None:
                        conn.close()
                    conn = None
                    continue

                # The firmware refuses with 429 (busy or low-heap) — the one accepted
                # non-200, and silently accepted (not counted as shedding). A 503 means
                # wrong/old firmware: the low-heap path was deliberately changed from 503
                # to 429 (ESPresense#2428), so a 503 on a direct connection is a contract
                # violation, not shedding.
                if resp.status != 200:
                    if resp.status == 503:
                        raise _Bug(f"GET {path} returned 503 — firmware must refuse with 429, not 503")
                    if resp.status != 429:
                        drops.append(f"HTTP {resp.status} on {path}")
                    continue

                # From here a 200 must be a complete, correct object — anything less is a lie.

                # A 200 carrying the 429 text is the double-send caught red-handed.
                if TOO_MANY in body:
                    raise _Bug(f"GET {path} returned 200 with the 429 body {TOO_MANY!r} — "
                               f"two responses were sent for one request")

                try:
                    doc = json.loads(body)
                except ValueError as e:
                    raise _Bug(f"GET {path} returned a 200 with an unparseable body "
                               f"({len(body)}B, starts {body[:60]!r}) — truncated or garbled") from e

                # A 200 that isn't a JSON object is the low-heap null-body path: the buffer
                # failed to allocate, the doc serialized as `null`, and it shipped as 200
                # instead of 429. That is the bug the low-heap guard fixes.
                if not isinstance(doc, dict):
                    raise _Bug(f"GET {path} returned a 200 with non-object JSON ({body[:40]!r}) "
                               f"— low-heap serving should refuse (429), not 200")

                # The double-send signature: a 200 whose document doesn't match the URL.
                if ("devices" in doc) != path.endswith("/devices"):
                    raise _Bug(f"GET {path} answered with the wrong document (keys "
                               f"{sorted(doc)}) — a stale response is queued on this "
                               f"connection, i.e. two responses were sent for one request")
        finally:
            if conn is not None:
                conn.close()

    time.sleep(JSON_CHECK_DELAY_SECS)

    # A device we cannot route to is not a firmware failure — say so and move on.
    try:
        probe = http.client.HTTPConnection(ip, timeout=10)
        probe.request("GET", "/json")
        probe.getresponse().read()
        probe.close()
    except OSError as e:
        print(f"[hil] /json check SKIPPED — {ip} unreachable from the runner ({e})", flush=True)
        return

    bugs, drops, crashes = [], [], []
    threads = [
        threading.Thread(target=lambda n=n: _run(worker, n, bugs, drops, crashes))
        for n in range(JSON_CHECK_WORKERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = JSON_CHECK_WORKERS * JSON_CHECK_REQUESTS
    notes = []
    if drops:
        notes.append(f"{len(drops)}/{total} shed under load")
    if crashes:
        notes.append(f"{len(crashes)} worker crash(es): {crashes[0]}")
    suffix = f" ({'; '.join(notes)})" if notes else ""
    if bugs:
        bug.append(f"/json contract violation: {bugs[0]}")
    elif crashes:
        # A checker crash isn't a firmware failure, but it must not read as a clean pass.
        bug.append(f"/json check harness error: {crashes[0]}")
    else:
        print(f"[hil] /json check passed ({total} concurrent requests to {ip}){suffix}", flush=True)


def heap_sampler(ip, samples, stop, problems):
    """Poll /json/tele every HEAP_SAMPLE_SECS and record freeHeap/maxHeap/fingerprints.

    Read-only and deliberately gentle — one request a minute is nothing next to the
    concurrent burst json_endpoint_check already fires. Failures to sample are skipped
    rather than recorded: a missed poll is a network hiccup, not a heap measurement, and
    inventing a zero here would read as a catastrophic leak.
    """
    stop.wait(HEAP_SETTLE_SECS)
    while not stop.is_set():
        try:
            conn = http.client.HTTPConnection(ip, timeout=10)
            conn.request("GET", HEAP_TELE_PATH)
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            if resp.status == 404:
                # Firmware predates the endpoint. Recorded rather than ignored: a heap check
                # that quietly samples nothing is worse than no check, because the run still
                # reports PASS.
                if "missing" not in problems:
                    problems.append("missing")
                    print(f"[hil] {HEAP_TELE_PATH} returned 404 — firmware too old", flush=True)
                return
            if resp.status == 200:
                doc = json.loads(body)
                # A null/non-object body is the low-heap path (see json_endpoint_check) — the
                # exact moment this sampler must not die on `None.get(...)`. Skip the sample.
                if isinstance(doc, dict):
                    free, mx = doc.get("freeHeap"), doc.get("maxHeap")
                    if isinstance(free, int) and isinstance(mx, int):
                        fp = doc.get("fingerprints")
                        samples.append((time.monotonic(), free, mx, fp))
                        print(f"[hil] heap freeHeap={free} maxHeap={mx} fingerprints={fp}", flush=True)
        except (OSError, http.client.HTTPException, ValueError):
            pass  # a missed sample is not a measurement — see docstring
        stop.wait(HEAP_SAMPLE_SECS)


def heap_verdict(samples, duration, problems=()):
    """Return a failure string if free heap trended down over the run, else None."""
    if duration < HEAP_TREND_MIN_SECS:
        return None  # a 3-minute PR run says nothing about a multi-hour slope
    if "missing" in problems:
        # Not a heap failure, but the run cannot claim to have checked heap either.
        return (f"{HEAP_TELE_PATH} is missing from this firmware, so heap was never "
                f"sampled over {format_duration(int(duration))}")
    if len(samples) < HEAP_TREND_EDGE * 2:
        # Unreachable-from-runner looks the same as a quiet network; neither is a firmware
        # fault, so this stays a skip. A 404 is handled above precisely because it is one.
        print(f"[hil] heap trend SKIPPED — only {len(samples)} samples", flush=True)
        return None

    def edges(index):
        head = statistics.median(s[index] for s in samples[:HEAP_TREND_EDGE])
        tail = statistics.median(s[index] for s in samples[-HEAP_TREND_EDGE:])
        return head, tail

    free0, free1 = edges(1)
    max0, max1 = edges(2)
    fp0, fp1 = samples[0][3], samples[-1][3]
    span = samples[-1][0] - samples[0][0]  # actual sampled window, not the full monitor duration
    summary = (f"freeHeap {free0:.0f}->{free1:.0f}, maxHeap {max0:.0f}->{max1:.0f}, "
               f"fingerprints {fp0}->{fp1}, over {format_duration(int(span))}")

    if free1 < free0 * (1 - HEAP_DECLINE_FRAC):
        lost = (1 - free1 / free0) * 100
        return f"Free heap fell {lost:.0f}% ({summary})"
    print(f"[hil] heap trend OK ({summary})", flush=True)
    return None


class _Bug(Exception):
    """A /json contract violation that must fail the build."""


def _run(fn, n, bugs, drops, crashes):
    try:
        fn(n, drops)
    except _Bug as e:
        bugs.append(str(e))
    except Exception as e:  # noqa: BLE001 - never let a worker die silently and still "pass"
        # A checker crash is not load-shedding; track it apart so it can't hide in the tally.
        crashes.append(f"worker {n}: {type(e).__name__}: {e}")
        print(f"[hil] /json worker {n} crashed: {type(e).__name__}: {e}", flush=True)


def format_duration(seconds):
    """Format seconds into a human-readable string like '8h', '2h30m', '45m', '90s'."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h{minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def main():
    parser = argparse.ArgumentParser(description="HIL serial monitor")
    parser.add_argument("--port", required=True, help="Serial device path")
    parser.add_argument("--duration", type=int, required=True, help="Monitor duration in seconds")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument(
        "--scan-pattern",
        default=DEFAULT_SCAN_RESULT_PATTERN,
        help="Regex that identifies a BLE scan result line",
    )
    parser.add_argument(
        "--allow-no-scan",
        action="store_true",
        help="Pass even if no scan result lines are seen",
    )
    args = parser.parse_args()
    try:
        scan_result_pattern = re.compile(args.scan_pattern)
    except re.error as e:
        parser.error(f"Invalid --scan-pattern: {e}")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except serial.SerialException as e:
        print(f"ERROR: Could not open serial port {args.port}: {e}", file=sys.stderr)
        sys.exit(3)

    print(f"Monitoring {args.port} for {format_duration(args.duration)}...")

    start = time.monotonic()
    booted = False
    saw_scan_result = False
    json_failure = []  # appended to by the /json check thread
    heap_samples = []   # appended to by the heap sampler thread
    heap_problems = []  # ditto, for "the check could not run" conditions
    stop_sampling = threading.Event()

    try:
        while True:
            elapsed = time.monotonic() - start

            if elapsed >= args.duration:
                if not booted:
                    print(
                        f"FAIL: Boot was not confirmed before the "
                        f"{format_duration(args.duration)} monitor window elapsed."
                    )
                    sys.exit(2)
                if not args.allow_no_scan and not saw_scan_result:
                    print(
                        f"FAIL: No scan results matching /{args.scan_pattern}/ were seen in "
                        f"{format_duration(args.duration)}."
                    )
                    sys.exit(4)
                stop_sampling.set()
                decline = heap_verdict(heap_samples, elapsed, heap_problems)
                if decline:
                    print(f"FAIL: {decline}")
                    sys.exit(8)
                print(f"PASS: {format_duration(args.duration)} elapsed cleanly.")
                sys.exit(0)

            if not booted and elapsed > BOOT_TIMEOUT_SECS:
                print(f"FAIL: Boot timeout — '{BOOT_SUCCESS_PATTERN}' not seen within {BOOT_TIMEOUT_SECS}s.")
                sys.exit(2)

            try:
                line = ser.readline().decode("utf-8", errors="replace").rstrip()
            except serial.SerialException as e:
                print(f"ERROR: Serial read error: {e}", file=sys.stderr)
                sys.exit(3)

            if json_failure:
                print(f"FAIL: {json_failure[0]}")
                sys.exit(6)

            if not line:
                continue

            print(line, flush=True)

            if not booted and BOOT_SUCCESS_PATTERN in line:
                booted = True
                print(f"[hil] Boot confirmed at {elapsed:.1f}s")
                match = IP_PATTERN.search(line)
                if match:
                    # Background threads so the serial buffer keeps draining while we probe.
                    threading.Thread(
                        target=json_endpoint_check,
                        args=(match.group(1), json_failure),
                        daemon=True,
                    ).start()
                    threading.Thread(
                        target=heap_sampler,
                        args=(match.group(1), heap_samples, stop_sampling, heap_problems),
                        daemon=True,
                    ).start()
                else:
                    print(f"[hil] /json check SKIPPED — no IP in {line!r}")

            # A restart after boot resets every other signal here — fresh boot, fresh heap,
            # fresh scan results — so without this the window passes while the node is
            # actually power-cycling every few hours (ESPresense#2309 on the S3).
            elif booted and (REBOOT_PATTERN in line or OOM_RESTART_PATTERN in line):
                why = ("firmware low-heap watchdog fired" if OOM_RESTART_PATTERN in line
                       else "unexpected restart")
                print(f"FAIL: Node restarted {elapsed:.0f}s into the run — {why}: {line.strip()!r}")
                sys.exit(7)

            if MQTT_RECONNECT_LIMIT_PATTERN in line:
                print(f"FAIL: Firmware MQTT reconnect limit reached: '{MQTT_RECONNECT_LIMIT_PATTERN}'")
                sys.exit(5)

            if not saw_scan_result and scan_result_pattern.search(line):
                saw_scan_result = True
                print(f"[hil] First scan result confirmed at {elapsed:.1f}s")

            for pattern in CRASH_PATTERNS:
                if pattern in line:
                    error_start_time = time.monotonic()
                    while time.monotonic() - error_start_time < 5:
                        try:
                            error_line = ser.readline().decode("utf-8", errors="replace").rstrip()
                            if error_line:
                                print(error_line, flush=True)
                        except serial.SerialException as e:
                            print(f"ERROR: Serial read error during crash capture: {e}", file=sys.stderr)
                            break
                    print(f"FAIL: Crash pattern detected: '{pattern}'")
                    sys.exit(1)

    finally:
        ser.close()


if __name__ == "__main__":
    main()
