#!/usr/bin/env python3
"""Prove json_endpoint_check catches the serveJson double-send and passes a correct server.

Two mock servers on raw sockets (BaseHTTPRequestHandler can't send two responses to one
request, which is the whole point):
  buggy  - mimics pre-fix serveJson: when busy, writes the 429 AND the 200 JSON
  fixed  - mimics post-fix serveJson: when busy, writes only the 429
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hil_monitor import json_endpoint_check  # noqa: E402

import hil_monitor  # noqa: E402

hil_monitor.JSON_CHECK_DELAY_SECS = 0  # don't wait 15s in a test

INFO = b'{"room":"hil","ver":"test"}'
DEVICES = b'{"room":"hil","ver":"test","devices":[]}'


def resp(status, reason, body):
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    )


def serve(sock, buggy, stop):
    serving = threading.Lock()

    def handle(conn):
        conn.settimeout(5)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                for req in data.split(b"\r\n\r\n")[:-1]:  # one response per pipelined request
                    # serveJson picks the document from the URL, same as the firmware
                    body = DEVICES if b"/json/devices" in req else INFO
                    busy = not serving.acquire(blocking=False)
                    if busy:
                        conn.sendall(resp(429, "Too Many Requests", b"Too Many Requests"))
                        if not buggy:
                            continue
                        # the bug: no early return, so a second response follows
                        conn.sendall(resp(200, "OK", body))
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


def run(buggy):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    sock.settimeout(1)
    stop = threading.Event()
    threading.Thread(target=serve, args=(sock, buggy, stop), daemon=True).start()

    failure = []
    json_endpoint_check(f"127.0.0.1:{sock.getsockname()[1]}", failure)
    stop.set()
    sock.close()
    return failure


buggy = run(buggy=True)
assert buggy, "detector MISSED the double-send bug"
print(f"buggy server  -> detected: {buggy[0]}")

fixed = run(buggy=False)
assert not fixed, f"detector false-positived on correct server: {fixed}"
print("fixed server  -> clean")
print("\nOK: detector catches the bug and does not false-positive.")
