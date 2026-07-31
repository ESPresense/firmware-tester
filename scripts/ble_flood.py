#!/usr/bin/env python3
"""Flood the bench with BLE advertisements from a fresh random address every time.

Why: ESPresense fingerprints a *static random* address by MAC (BleFingerprint.cpp,
ID_TYPE_RAND_STATIC_MAC when the top two bits of the MSB are set), so every rotation
here costs the node a new fingerprint slot against a pool of 100-200. That is the
churn a real room full of phones produces, compressed — the load under which
ESPresense#2309's slow heap decline shows up in hours instead of days.

The adapter is driven raw over HCI_CHANNEL_USER, which means BlueZ is not involved and
does not need to be installed: the kernel hands us the controller exclusively and
nothing else can fight us for it. Requires root (CAP_NET_ADMIN) and an adapter that no
other process has powered up.

  ble_flood.py --index 0 --rate 40
  ble_flood.py --index 0 --flag /var/lock/woodpecker/bleflood.on   # only while flag exists
  ble_flood.py --selftest                                          # no hardware needed
"""

import argparse
import ctypes
import fcntl
import os
import secrets
import socket
import struct
import time

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_USER = 1
HCIDEVDOWN = 0x400448CA

HCI_COMMAND_PKT = 0x01

OCF_RESET = 0x0C03
OCF_LE_SET_RANDOM_ADDRESS = 0x2005
OCF_LE_SET_ADV_PARAMETERS = 0x2006
OCF_LE_SET_ADV_DATA = 0x2008
OCF_LE_SET_ADV_ENABLE = 0x200A

ADV_NONCONN_IND = 0x03
OWN_ADDR_TYPE_RANDOM = 0x01
ADV_INTERVAL = 0x0020  # 20 ms — the BLE minimum, so a packet lands on all 3 channels


def hci_command(opcode, params=b""):
    """Frame one HCI command packet: type, opcode (LE), parameter length, parameters."""
    if len(params) > 255:
        raise ValueError(f"HCI parameters too long: {len(params)}")
    return struct.pack("<BHB", HCI_COMMAND_PKT, opcode, len(params)) + params


def random_static_address():
    """A valid BLE static random address, in the little-endian order HCI wants.

    Spec: the two most significant bits must both be 1, and the remaining 46 bits must
    not be all-zeros or all-ones. The MSB is the *last* byte on the wire. Those top bits
    are also exactly what makes ESPresense classify it as ID_TYPE_RAND_STATIC_MAC rather
    than discarding it as a resolvable private address it cannot resolve.
    """
    while True:
        addr = bytearray(secrets.token_bytes(6))
        addr[5] |= 0xC0
        rest = int.from_bytes(addr, "little") & ((1 << 46) - 1)
        if rest not in (0, (1 << 46) - 1):
            return bytes(addr)


def advertising_payload(name=b"HIL-FLOOD"):
    """A minimal but realistic non-connectable advert: flags + complete local name."""
    fields = bytes([2, 0x01, 0x06]) + bytes([len(name) + 1, 0x09]) + name
    if len(fields) > 31:
        raise ValueError(f"advertising payload too long: {len(fields)}")
    return bytes([len(fields)]) + fields.ljust(31, b"\x00")


def adv_parameters():
    return struct.pack(
        "<HHBBB6sBB",
        ADV_INTERVAL, ADV_INTERVAL,
        ADV_NONCONN_IND,
        OWN_ADDR_TYPE_RANDOM,
        0x00,            # peer address type (unused for undirected)
        b"\x00" * 6,     # peer address (unused)
        0x07,            # all three advertising channels
        0x00,            # no filtering — anyone may scan
    )


def open_adapter(index):
    """Take exclusive raw control of hciN.

    The kernel only allows HCI_CHANNEL_USER on a *down* adapter, which is also what
    guarantees exclusivity: if this succeeds, nothing else is driving the controller.
    """
    ctl = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    try:
        fcntl.ioctl(ctl.fileno(), HCIDEVDOWN, index)
    except OSError:
        # Already down is the normal case (no bluetoothd). A real problem — missing
        # adapter, no privileges — surfaces with a clearer message on bind() below.
        pass
    finally:
        ctl.close()

    sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    # CPython's bind() for BTPROTO_HCI cannot set hci_channel, so build sockaddr_hci
    # ourselves: { sa_family, hci_dev, hci_channel }, all u16.
    addr = struct.pack("<HHH", AF_BLUETOOTH, index, HCI_CHANNEL_USER)
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.bind(sock.fileno(), ctypes.c_char_p(addr), len(addr)) != 0:
        err = ctypes.get_errno()
        sock.close()
        raise OSError(err, f"bind(hci{index}, HCI_CHANNEL_USER) failed: {os.strerror(err)}. "
                           f"Is bluetoothd holding the adapter, or is this not root?")
    sock.setblocking(False)
    return sock


def send(sock, opcode, params=b""):
    sock.sendall(hci_command(opcode, params))
    drain(sock)


def drain(sock):
    """Consume controller events. Unread events eventually stall the socket."""
    while True:
        try:
            sock.recv(1024)
        except BlockingIOError:
            return
        except OSError:
            return


def flood_requested(flag_dir, max_age):
    """True while any HIL step is asking for load.

    A directory rather than a single on/off file because the four device steps run in
    parallel and finish at different times — one step exiting must not cut the flood out
    from under the other three. Each step owns one file and removes it on exit.

    Entries older than max_age are ignored so a hard-killed container cannot leave the
    bench advertising forever, which would fill every ESPresense node in range with junk.
    """
    if not flag_dir:
        return True
    now = time.time()
    try:
        with os.scandir(flag_dir) as entries:
            for entry in entries:
                try:
                    if now - entry.stat().st_mtime < max_age:
                        return True
                except FileNotFoundError:
                    continue  # step cleaned up mid-scan
    except FileNotFoundError:
        return False
    return False


def flood(sock, rate, flag_dir, max_age, stop_after):
    """Rotate the advertised address forever (or until the flag clears / time runs out).

    Order matters: the controller rejects LE Set Random Address while advertising is
    enabled, so each rotation is disable -> re-address -> enable.
    """
    send(sock, OCF_RESET)
    time.sleep(0.1)
    send(sock, OCF_LE_SET_ADV_PARAMETERS, adv_parameters())
    send(sock, OCF_LE_SET_ADV_DATA, advertising_payload())

    interval = 1.0 / rate
    started = time.monotonic()
    rotations = 0
    reported = started
    advertising = False

    while True:
        if stop_after and time.monotonic() - started >= stop_after:
            break
        if not flood_requested(flag_dir, max_age):
            if advertising:
                send(sock, OCF_LE_SET_ADV_ENABLE, b"\x00")
                advertising = False
                print(f"[flood] paused — no requests in {flag_dir}", flush=True)
            time.sleep(2)
            continue
        if not advertising and flag_dir:
            print(f"[flood] resumed — request present in {flag_dir}", flush=True)

        cycle = time.monotonic()
        send(sock, OCF_LE_SET_ADV_ENABLE, b"\x00")
        send(sock, OCF_LE_SET_RANDOM_ADDRESS, random_static_address())
        send(sock, OCF_LE_SET_ADV_ENABLE, b"\x01")
        advertising = True
        rotations += 1

        now = time.monotonic()
        if now - reported >= 30:
            print(f"[flood] {rotations} unique addresses in {now - started:.0f}s "
                  f"({rotations / (now - started):.1f}/s)", flush=True)
            reported = now
        time.sleep(max(0.0, interval - (now - cycle)))

    send(sock, OCF_LE_SET_ADV_ENABLE, b"\x00")
    print(f"[flood] stopped after {rotations} unique addresses", flush=True)


def selftest():
    """Check the packet framing and address rules without touching an adapter."""
    pkt = hci_command(OCF_LE_SET_ADV_ENABLE, b"\x01")
    assert pkt == b"\x01\x0a\x20\x01\x01", pkt.hex()

    assert hci_command(OCF_RESET) == b"\x01\x03\x0c\x00"

    for _ in range(2000):
        addr = random_static_address()
        assert len(addr) == 6
        # The MSB is the last byte on the wire; ESPresense keys ID_TYPE_RAND_STATIC_MAC
        # off exactly this test, so if it ever fails the flood stops being fingerprinted.
        assert addr[5] & 0xC0 == 0xC0, addr.hex()
        rest = int.from_bytes(addr, "little") & ((1 << 46) - 1)
        assert rest not in (0, (1 << 46) - 1)

    assert len({random_static_address() for _ in range(5000)}) == 5000, "addresses repeat"

    payload = advertising_payload()
    assert len(payload) == 32 and payload[0] == 14, payload.hex()
    assert len(adv_parameters()) == 15, len(adv_parameters())

    try:
        hci_command(OCF_LE_SET_ADV_DATA, b"\x00" * 256)
        raise AssertionError("oversized parameters must be rejected")
    except ValueError:
        pass

    # The gate decides whether the bench sprays junk MACs across the house, so both
    # directions matter: it must come on for a live step and go off for a dead one.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        assert not flood_requested(d, 3600), "empty dir must not request load"
        assert not flood_requested(os.path.join(d, "gone"), 3600), "missing dir must not request"

        live = os.path.join(d, "esp32s3")
        open(live, "w").close()
        assert flood_requested(d, 3600), "a step's request file must turn the flood on"

        # A step killed hard leaves its file behind; staleness must not flood forever.
        os.utime(live, (0, 0))
        assert not flood_requested(d, 3600), "stale request must be ignored"

        os.remove(live)
        assert not flood_requested(d, 3600), "removed request must turn the flood off"
    assert flood_requested(None, 3600), "ungated mode must always advertise"

    print("selftest OK")


def main():
    p = argparse.ArgumentParser(description="BLE advertisement flood with unique addresses")
    p.add_argument("--index", type=int, default=0, help="hciN adapter index")
    p.add_argument("--rate", type=float, default=40.0, help="address rotations per second")
    p.add_argument("--flag-dir", help="only advertise while this directory holds a request file")
    p.add_argument("--max-age", type=float, default=9 * 3600,
                   help="ignore request files older than this many seconds")
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    p.add_argument("--selftest", action="store_true", help="verify framing, no hardware")
    args = p.parse_args()

    if args.selftest:
        selftest()
        return
    if args.rate <= 0:
        p.error("--rate must be positive")

    sock = open_adapter(args.index)
    print(f"[flood] hci{args.index} claimed, rotating at {args.rate}/s"
          + (f", gated on {args.flag_dir}" if args.flag_dir else ""), flush=True)
    try:
        flood(sock, args.rate, args.flag_dir, args.max_age, args.seconds)
    except KeyboardInterrupt:
        send(sock, OCF_LE_SET_ADV_ENABLE, b"\x00")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
