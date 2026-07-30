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
"""

import argparse
import http.client
import json
import re
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

IP_PATTERN = re.compile(r"IP address:\s*(\d+\.\d+\.\d+\.\d+)")
JSON_CHECK_DELAY_SECS = 15  # let the web server settle after boot
JSON_CHECK_WORKERS = 2      # enough to collide; a constrained node need not survive a stampede
JSON_CHECK_REQUESTS = 12    # sequential requests per worker, on one kept-alive connection


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
    asked for it. Refusing to serve under pressure is fine — 429 (busy), 503 (low heap),
    or dropping the connection at the TCP layer (IncompleteRead / reset / refused) are all
    acceptable load-shedding and are counted, logged, and reconnected past, never failed
    on. But a *200* that is null, truncated, unparseable, or the wrong document is the
    server lying about success — that is the double-send, or the low-heap null-body path
    (fixed by returning 503) — and it fails the build.
    """
    TOO_MANY = b"Too Many Requests"

    def worker(n, drops):
        conn = None
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

            # Refusing to serve is acceptable: 429 busy, 503 low-heap, anything non-200.
            if resp.status != 200:
                if resp.status not in (429, 503):
                    drops.append(f"HTTP {resp.status} on {path}")
                continue

            # From here a 200 must be a complete, correct object — anything less is a lie.

            # A 200 carrying the 429 text is the double-send caught red-handed.
            if TOO_MANY in body:
                conn.close()
                raise _Bug(f"GET {path} returned 200 with the 429 body {TOO_MANY!r} — "
                           f"two responses were sent for one request")

            try:
                doc = json.loads(body)
            except ValueError:
                conn.close()
                raise _Bug(f"GET {path} returned a 200 with an unparseable body "
                           f"({len(body)}B, starts {body[:60]!r}) — truncated or garbled")

            # A 200 that isn't a JSON object is the low-heap null-body path: the buffer
            # failed to allocate, the doc serialized as `null`, and it shipped as 200
            # instead of 503. That is the bug the 503 guard fixes.
            if not isinstance(doc, dict):
                conn.close()
                raise _Bug(f"GET {path} returned a 200 with non-object JSON ({body[:40]!r}) "
                           f"— low-heap serving should 503, not 200")

            # The double-send signature: a 200 whose document doesn't match the URL.
            if ("devices" in doc) != path.endswith("/devices"):
                conn.close()
                raise _Bug(f"GET {path} answered with the wrong document (keys "
                           f"{sorted(doc)}) — a stale response is queued on this "
                           f"connection, i.e. two responses were sent for one request")
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

    bugs, drops = [], []
    threads = [
        threading.Thread(target=lambda n=n: _run(worker, n, bugs, drops))
        for n in range(JSON_CHECK_WORKERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = JSON_CHECK_WORKERS * JSON_CHECK_REQUESTS
    shed = f" ({len(drops)}/{total} shed under load)" if drops else ""
    if bugs:
        bug.append(f"/json returned a bad 200: {bugs[0]}")
    else:
        print(f"[hil] /json check passed ({total} concurrent requests to {ip}){shed}", flush=True)


class _Bug(Exception):
    """The double-send signature was observed — the one hard failure."""


def _run(fn, n, bugs, drops):
    try:
        fn(n, drops)
    except _Bug as e:
        bugs.append(str(e))


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
                    # Background thread so the serial buffer keeps draining while we probe.
                    threading.Thread(
                        target=json_endpoint_check,
                        args=(match.group(1), json_failure),
                        daemon=True,
                    ).start()
                else:
                    print(f"[hil] /json check SKIPPED — no IP in {line!r}")

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
