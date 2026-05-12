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
"""

import argparse
import re
import sys
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

            if not line:
                continue

            print(line, flush=True)

            if not booted and BOOT_SUCCESS_PATTERN in line:
                booted = True
                print(f"[hil] Boot confirmed at {elapsed:.1f}s")

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
