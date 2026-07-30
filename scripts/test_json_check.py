#!/usr/bin/env python3
"""Prove json_endpoint_check catches the serveJson double-send AND tolerates load-shedding.

Raw-socket mocks (BaseHTTPRequestHandler can't send two responses to one request, which
is the whole point):
  buggy   - pre-fix serveJson: when busy, writes the 429 AND the 200 JSON (the bug)
  fixed   - post-fix serveJson: when busy, writes only the 429
  flaky   - correct load-shedding a real constrained node does: drops keep-alive
            connections mid-body, and refuses with 429 when it can't afford the buffer.
            MUST pass — refusing to serve is fine.
  oom     - the low-heap null-body bug: under pressure returns a 200 with body `null`
            instead of refusing. MUST be caught — a 200 that lies about success is the
            whole point of the check (this is what ESPresense#2428 fixes in firmware).
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hil_monitor  # noqa: E402
from hil_monitor import json_endpoint_check  # noqa: E402

hil_monitor.JSON_CHECK_DELAY_SECS = 0  # don't wait 15s in a test

INFO = b'{"room":"hil","ver":"test"}'
DEVICES = b'{"room":"hil","ver":"test","devices":[]}'


def resp(status, reason, body):
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    )


def serve(sock, mode, stop):
    serving = threading.Lock()
    counter = [0]

    def handle(conn):
        conn.settimeout(5)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                for req in data.split(b"\r\n\r\n")[:-1]:  # one response per pipelined request
                    body = DEVICES if b"/json/devices" in req else INFO
                    counter[0] += 1

                    if mode == "flaky":
                        # Correct load-shedding — refusing to serve, never a false 200:
                        if counter[0] % 4 == 0:
                            conn.close()  # drop keep-alive mid-stream -> IncompleteRead/reset
                            return
                        if counter[0] % 7 == 0:
                            # firmware's low-heap refusal (ESPresense#2428): 429, not a false 200
                            conn.sendall(resp(429, "Too Many Requests", b'{"error":"low memory"}'))
                            continue

                    if mode == "oom" and counter[0] % 3 == 0:
                        # The bug: a 200 that lies — buffer failed, doc serialized as null.
                        conn.sendall(resp(200, "OK", b"null"))
                        continue

                    if mode == "stale503" and counter[0] % 3 == 0:
                        # Wrong/old firmware: low-heap refusal as 503 instead of 429.
                        conn.sendall(resp(503, "Service Unavailable", b'{"error":"low memory"}'))
                        continue

                    busy = not serving.acquire(blocking=False)
                    if busy:
                        conn.sendall(resp(429, "Too Many Requests", b"Too Many Requests"))
                        if mode == "buggy":
                            conn.sendall(resp(200, "OK", body))  # the bug: no early return
                        continue
                    try:
                        time.sleep(0.02)  # widen the window so workers actually collide
                        conn.sendall(resp(200, "OK", body))
                    finally:
                        serving.release()
        except OSError:
            pass
        finally:
            conn.close()

    while not stop.is_set():
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def run(mode):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    sock.settimeout(1)
    stop = threading.Event()
    threading.Thread(target=serve, args=(sock, mode, stop), daemon=True).start()

    bug = []
    json_endpoint_check(f"127.0.0.1:{sock.getsockname()[1]}", bug)
    stop.set()
    sock.close()
    return bug


buggy = run("buggy")
assert buggy, "detector MISSED the double-send bug"
print(f"buggy server  -> detected: {buggy[0]}")

fixed = run("fixed")
assert not fixed, f"detector false-positived on correct server: {fixed}"
print("fixed server  -> clean")

flaky = run("flaky")
assert not flaky, f"detector false-positived on load-shedding node: {flaky}"
print("flaky server  -> clean (drops + 429 tolerated)")

oom = run("oom")
assert oom, "detector MISSED the low-heap 200-null body"
print(f"oom server    -> detected: {oom[0]}")

stale503 = run("stale503")
assert stale503, "detector MISSED a 503 (wrong/old firmware — must be 429)"
print(f"stale503 srv  -> detected: {stale503[0]}")

print("\nOK: catches double-send + 200-null, tolerates 429+drops, rejects 503, no false positives.")
