#!/usr/bin/env python3
"""
HIL serial monitor for ESPresense firmware.

Exit codes:
  0 = pass (duration elapsed cleanly)
  1 = crash detected
  2 = boot timeout (IP address not seen within 90s)
  3 = serial error
"""

import argparse
import sys
import time

import serial

CRASH_PATTERNS = [
    "Guru Meditation Error",
    "abort()",
    "Backtrace:",
    "TWDT",
    "Task watchdog got triggered",
    "rst:0xc",
]

BOOT_SUCCESS_PATTERN = "IP address:"
BOOT_TIMEOUT_SECS = 90


def main():
    parser = argparse.ArgumentParser(description="HIL serial monitor")
    parser.add_argument("--port", required=True, help="Serial device path")
    parser.add_argument("--duration", type=int, required=True, help="Monitor duration in seconds")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except serial.SerialException as e:
        print(f"ERROR: Could not open serial port {args.port}: {e}", file=sys.stderr)
        sys.exit(3)

    print(f"Monitoring {args.port} for {args.duration}s...")

    start = time.monotonic()
    booted = False

    try:
        while True:
            elapsed = time.monotonic() - start

            if elapsed >= args.duration:
                print(f"PASS: {args.duration}s elapsed cleanly.")
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

            for pattern in CRASH_PATTERNS:
                if pattern in line:
                    print(f"FAIL: Crash pattern detected: '{pattern}'")
                    sys.exit(1)

    finally:
        ser.close()


if __name__ == "__main__":
    main()
