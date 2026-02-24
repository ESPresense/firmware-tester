#!/usr/bin/env python3
"""Send WiFi credentials to an ESPresense device via Improv Serial protocol."""

import argparse
import sys
import time

import serial


def build_wifi_packet(ssid: str, password: str) -> bytes:
    """Build an Improv WiFi provisioning RPC packet."""
    ssid_bytes = ssid.encode("utf-8")
    pass_bytes = password.encode("utf-8")
    total_len = len(ssid_bytes) + len(pass_bytes) + 2

    # RPC data: totalLen, ssidLen, ssid, passLen, password
    rpc_data = (
        bytes([total_len, len(ssid_bytes)])
        + ssid_bytes
        + bytes([len(pass_bytes)])
        + pass_bytes
    )

    header = b"IMPROV"
    version = b"\x01"
    packet_type = b"\x03"  # RPC_Command
    rpc_command = b"\x01"  # Command_Wifi
    length = bytes([1 + len(rpc_data)])  # command type byte + data

    payload = header + version + packet_type + length + rpc_command + rpc_data
    checksum = sum(payload) & 0xFF
    return payload + bytes([checksum])


def parse_improv_packet(buf: bytes):
    """Try to parse an Improv packet from a buffer.

    Returns (packet_type, state_byte, consumed) or None.
    """
    idx = buf.find(b"IMPROV")
    if idx < 0 or len(buf) < idx + 10:
        return None

    version = buf[idx + 6]
    if version != 1:
        return None

    packet_type = buf[idx + 7]
    length = buf[idx + 8]
    end = idx + 9 + length + 1  # +1 for checksum

    if len(buf) < end:
        return None

    state = buf[idx + 9]
    return (packet_type, state, end)


def main():
    parser = argparse.ArgumentParser(description="Provision WiFi via Improv Serial")
    parser.add_argument("--port", required=True, help="Serial port device path")
    parser.add_argument("--ssid", required=True, help="WiFi SSID")
    parser.add_argument("--password", required=True, help="WiFi password")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout in seconds")
    args = parser.parse_args()

    print(f"Opening {args.port} at {args.baud} baud")
    ser = serial.Serial(args.port, args.baud, timeout=1)

    # Wait for device to boot after flash
    print("Waiting for device to boot...")
    time.sleep(5)
    ser.reset_input_buffer()

    # Send WiFi credentials
    packet = build_wifi_packet(args.ssid, args.password)
    print(f"Sending WiFi credentials (SSID: {args.ssid})")
    ser.write(packet)
    ser.flush()

    # Wait for provisioning state response
    buf = b""
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if ser.in_waiting:
            buf += ser.read(ser.in_waiting)
            while True:
                result = parse_improv_packet(buf)
                if not result:
                    break
                ptype, state, consumed = result
                buf = buf[consumed:]

                if ptype == 0x01:  # Current_State
                    state_names = {
                        0x02: "authorized",
                        0x03: "provisioning",
                        0x04: "provisioned",
                    }
                    print(f"State: {state_names.get(state, f'{state:#x}')}")
                    if state == 0x03:
                        print("Device accepted credentials, restarting...")
                        time.sleep(2)
                        ser.close()
                        return 0
                    if state == 0x04:
                        print("Device already provisioned!")
                        ser.close()
                        return 0
                elif ptype == 0x02:  # Error_State
                    print(f"Improv error: {state:#x}", file=sys.stderr)
                    ser.close()
                    return 1
        time.sleep(0.1)

    print("Timeout waiting for Improv response", file=sys.stderr)
    ser.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
