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
JSON_CHECK_WORKERS = 3
JSON_CHECK_REQUESTS = 10  # sequential requests per worker, on one kept-alive connection


def json_endpoint_check(ip, failure):
    """Hammer /json/devices concurrently and verify every response is well-formed.

    serveJson() serialises itself with a `servingJson` flag and returns 429 when busy.
    A missing early-return there sends two responses down one socket. The extra one is
    left in the buffer, so every later response on that connection is off by one.

    That shift is invisible if you only check "is this valid JSON" — the stale response
    is a perfectly good 200. So each worker reuses one keep-alive connection (to expose
    the shift at all) and alternates two endpoints with different shapes: /json has no
    "devices" key, /json/devices does. A shifted stream answers the wrong question.
    """

    def worker(n):
        conn = http.client.HTTPConnection(ip, timeout=10)
        for i in range(JSON_CHECK_REQUESTS):
            path = "/json/devices" if i % 2 else "/json"
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 429:
                continue
            if resp.status != 200:
                raise AssertionError(f"worker {n} req {i}: GET {path} -> HTTP {resp.status}")
            try:
                doc = json.loads(body)
            except ValueError as e:
                raise AssertionError(
                    f"worker {n} req {i}: GET {path} -> body is not valid JSON ({e}); "
                    f"{len(body)} bytes, starts {body[:80]!r}"
                )
            if ("devices" in doc) != path.endswith("/devices"):
                raise AssertionError(
                    f"worker {n} req {i}: GET {path} answered with the wrong document "
                    f"(keys {sorted(doc)}) — a stale response is queued on this "
                    f"connection, i.e. something sent two responses to one request"
                )
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

    errors = []
    threads = [
        threading.Thread(target=lambda n=n: _run(worker, n, errors)) for n in range(JSON_CHECK_WORKERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        failure.append(f"/json misbehaved under concurrent load: {errors[0]}")
    else:
        total = JSON_CHECK_WORKERS * JSON_CHECK_REQUESTS
        print(f"[hil] /json check passed ({total} concurrent requests to {ip})", flush=True)


def _run(fn, n, errors):
    try:
        fn(n)
    except Exception as e:  # noqa: BLE001 - any failure here is a test failure
        errors.append(str(e))


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
