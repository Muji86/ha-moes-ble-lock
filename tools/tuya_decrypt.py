#!/usr/bin/env python3
"""Offline decryption of Tuya BLE (FD50) traffic from an Android btsnoop log.

Usage: python3 tuya_decrypt.py <btsnoop_hci.log> <secrets/lock.json>

Passive only: reads a capture file, never touches Bluetooth.
"""
import hashlib
import json
import struct
import sys
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from btsnoop_att import records  # noqa: E402

WRITE_H, NOTIFY_H = 0x0010, 0x0012

FUNCS = {
    0x0000: "DEVICE_INFO", 0x0001: "PAIR", 0x0002: "SEND_DPS", 0x0003: "DEVICE_STATUS",
    0x0004: "UNBIND", 0x0005: "DEVICE_RESET", 0x000C: "OTA_START", 0x0027: "SEND_DPS_V4",
    0x8001: "RECV_DP", 0x8002: "RECV_DP_TIME?", 0x8003: "RECV_DP_TIME", 0x8004: "RECV_SIGN_DP",
    0x8006: "RECV_DP_V4?", 0x8011: "TIME1_REQ", 0x8012: "TIME2_REQ",
}


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def varint(buf, i):
    val = shift = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return val, i


def aes_dec(key, iv, ct):
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return d.update(ct) + d.finalize()


def att_values(path, mac):
    """Yield (t, direction, value) for writes to 0x0010 and notifies on 0x0012."""
    addr = bytes.fromhex(mac.replace(":", ""))[::-1]
    handles, partial = set(), {}
    for t, flags, pk in records(path):
        if not pk:
            continue
        if pk[0] == 0x04 and len(pk) > 14 and pk[1] == 0x3E and pk[3] in (1, 0x0A) \
                and pk[4] == 0 and pk[9:15] == addr:
            handles.add(struct.unpack("<H", pk[5:7])[0] & 0xFFF)
            yield t, "--", None
        elif pk[0] == 0x02 and len(pk) >= 5:
            hf = struct.unpack("<H", pk[1:3])[0]
            h, pb = hf & 0xFFF, (hf >> 12) & 3
            if h not in handles:
                continue
            d = "<-" if flags & 1 else "->"
            key = (h, d)
            partial[key] = (partial.get(key, b"") + pk[5:]) if pb == 1 else pk[5:]
            buf = partial[key]
            if len(buf) < 4:
                continue
            ln, cid = struct.unpack("<HH", buf[:4])
            if len(buf) < 4 + ln:
                continue
            partial.pop(key)
            a = buf[4:4 + ln]
            if cid == 4 and a and a[0] in (0x12, 0x52, 0x1B) and len(a) > 3:
                vh = struct.unpack("<H", a[1:3])[0]
                if vh in (WRITE_H, NOTIFY_H):
                    yield t, d, a[3:]


DP_TYPES = {0: "raw", 1: "bool", 2: "value", 3: "string", 4: "enum", 5: "bitmap"}


def parse_dps(buf):
    out, i = [], 0
    while i + 4 <= len(buf):
        dp, typ, ln = buf[i], buf[i + 1], struct.unpack(">H", buf[i + 2:i + 4])[0]
        val = buf[i + 4:i + 4 + ln]
        i += 4 + ln
        v = int.from_bytes(val, "big") if typ in (1, 2, 4, 5) else val.hex()
        out.append(f"dp{dp}({DP_TYPES.get(typ, typ)})={v}")
    return " ".join(out)


def describe_dps(code, d, data):
    """V4 DP layouts as observed on this lock (see PROTOCOL_NOTES.md)."""
    sn = struct.unpack(">I", data[1:5])[0]
    if code == 0x0027 and d == "->":  # ver(1) sn(4) dps
        return f"CMD sn={sn} {parse_dps(data[5:])}"
    if code == 0x0027:  # ver sn result
        return f"CMD-RESULT sn={sn} result={data[5]}"
    if d == "->":  # ack: ver sn flags(2) result
        return f"ACK sn={sn} result={data[7]}"
    if code == 0x8006:  # ver sn flags(2) dps
        return f"REPORT sn={sn} flags={data[5:7].hex()} {parse_dps(data[7:])}"
    # 0x8007: ver sn flags(2) timetype(1) unix_ts(4) dps
    ts = struct.unpack(">I", data[8:12])[0]
    when = datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%H:%M:%S")
    return f"TIMED-REPORT sn={sn} at={when} {parse_dps(data[12:])}"


def main(path, secrets_path):
    s = json.load(open(secrets_path))
    lk6 = s["local_key"][:6].encode()
    login_key = hashlib.md5(lk6).digest()
    session_key = None
    frames = {}
    check_codes: set[str] = set()
    for t, d, v in att_values(path, s["mac"]):
        ts = datetime.fromtimestamp(t, timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]
        if v is None:
            print(f"\n[{ts}] ===== new connection =====")
            session_key, frames = None, {}
            continue
        pkt, i = varint(v, 0)
        if pkt == 0:
            total, i = varint(v, i)
            ver = v[i]
            i += 1
            frames[d] = [total, ver, v[i:]]
        elif d in frames:
            frames[d][2] += v[i:]
        else:
            continue
        total, ver, body = frames[d]
        if len(body) < total:
            continue
        del frames[d]
        flag, iv, ct = body[0], body[1:17], body[17:total]
        key = {4: login_key, 5: session_key}.get(flag)
        if key is None:
            print(f"[{ts}] {d} flag={flag:02x} (no key) {ct.hex()}")
            continue
        pt = aes_dec(key, iv, ct)
        seq, resp, code, ln = struct.unpack(">IIHH", pt[:12])
        data = pt[12:12 + ln]
        crc_ok = struct.unpack(">H", pt[12 + ln:14 + ln])[0] == crc16(pt[:12 + ln]) \
            if len(pt) >= 14 + ln else False
        name = FUNCS.get(code, f"0x{code:04X}")
        print(f"[{ts}] {d} k{flag} ver=0x{ver:02x} seq={seq:<3} resp_to={resp:<3} "
              f"{name:14} crc={'ok' if crc_ok else 'BAD'} len={ln} data={data.hex()}")
        if code in (0x0027, 0x8006, 0x8007) and crc_ok:
            print("      " + describe_dps(code, d, data))
            # DP 71 unlock from the app: ... ff ff 00 01 <8 ASCII chars> 01 <ts> ...
            j = data.find(b"\xff\xff\x00\x01")
            if code == 0x0027 and j >= 0 and data[j + 4:j + 12].isascii():
                check_codes.add(data[j + 4:j + 12].decode())
        if code == 0x0000 and d == "<-" and crc_ok:
            srand = data[6:12]
            session_key = hashlib.md5(lk6 + srand).digest()
            print(f"      device ver {data[0]}.{data[1]}  protocol {data[2]}.{data[3]}  "
                  f"flags=0x{data[4]:02x} bound={data[5]}  srand={srand.hex()}  "
                  f"hw {data[12]}.{data[13]}")


    if check_codes:
        print("\nBLE unlock check code(s) seen:", ", ".join(sorted(check_codes)))
    else:
        print("\nNo unlock command in this capture (unlock the lock from Smart Life while logging).")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
