#!/usr/bin/env python3
"""Extract ATT/GATT traffic for one BLE peer from an Android btsnoop_hci.log.

Usage: python3 btsnoop_att.py <btsnoop_hci.log> <MAC>

Prints: connections to the peer, the discovered GATT table, and every
ATT PDU (with timestamps) exchanged with it. Read-only analysis.
"""
import struct
import sys
from datetime import datetime, timezone

EPOCH_DELTA = 0x00DCDDB30F2F8000  # btsnoop µs epoch (year 0) -> unix µs

ATT_OPS = {
    0x01: "ERROR", 0x02: "MTU_REQ", 0x03: "MTU_RSP", 0x04: "FIND_INFO_REQ",
    0x05: "FIND_INFO_RSP", 0x08: "READ_BY_TYPE_REQ", 0x09: "READ_BY_TYPE_RSP",
    0x0A: "READ_REQ", 0x0B: "READ_RSP", 0x10: "READ_BY_GROUP_REQ",
    0x11: "READ_BY_GROUP_RSP", 0x12: "WRITE_REQ", 0x13: "WRITE_RSP",
    0x1B: "NOTIFY", 0x1D: "INDICATE", 0x1E: "CONFIRM", 0x52: "WRITE_CMD",
}
PROPS = [(0x02, "READ"), (0x04, "WRITE_NR"), (0x08, "WRITE"), (0x10, "NOTIFY"),
         (0x20, "INDICATE"), (0x01, "BCAST"), (0x40, "SIGNED"), (0x80, "EXT")]


def uuid_str(b: bytes) -> str:
    if len(b) == 2:
        return f"0x{struct.unpack('<H', b)[0]:04X}"
    h = b[::-1].hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def records(path):
    d = open(path, "rb").read()
    assert d[:8] == b"btsnoop\0", "not a btsnoop file"
    p = 16
    while p + 24 <= len(d):
        _ol, il, flags, _drops, ts = struct.unpack(">IIIIq", d[p:p + 24])
        yield (ts - EPOCH_DELTA) / 1e6, flags, d[p + 24:p + 24 + il]
        p += 24 + il


def main(path, mac):
    addr = bytes.fromhex(mac.replace(":", ""))[::-1]
    handles = set()
    partial = {}
    pdus = []  # (t, dir, att bytes, conn handle)
    for t, flags, pk in records(path):
        if not pk:
            continue
        typ = pk[0]
        if typ == 0x04 and len(pk) > 3 and pk[1] == 0x3E:  # LE meta event
            sub = pk[3]
            if sub in (0x01, 0x0A) and pk[4] == 0 and pk[9:15] == addr:
                h = struct.unpack("<H", pk[5:7])[0] & 0x0FFF
                handles.add(h)
                print(f"[{fmt(t)}] CONNECTED handle=0x{h:03X} role={'central' if pk[7]==0 else 'peripheral'}")
        elif typ == 0x04 and len(pk) > 6 and pk[1] == 0x05:  # disconnect complete
            h = struct.unpack("<H", pk[4:6])[0] & 0x0FFF
            if h in handles:
                print(f"[{fmt(t)}] DISCONNECTED handle=0x{h:03X} reason=0x{pk[6]:02X}")
                handles.discard(h)
        elif typ == 0x02 and len(pk) >= 5:
            hf = struct.unpack("<H", pk[1:3])[0]
            h, pb = hf & 0x0FFF, (hf >> 12) & 0x3
            if h not in handles:
                continue
            data = pk[5:]
            direction = "<-" if flags & 1 else "->"
            key = (h, direction)
            if pb == 0x01:  # continuation
                if key in partial:
                    partial[key] += data
            else:
                partial[key] = data
            buf = partial.get(key, b"")
            if len(buf) >= 4:
                ln, cid = struct.unpack("<HH", buf[:4])
                if len(buf) >= 4 + ln:
                    if cid == 0x0004:
                        pdus.append((t, direction, buf[4:4 + ln], h))
                    partial.pop(key, None)

    # GATT table from discovery responses
    services, chars, descs = [], [], {}
    for _t, _d, a, _h in pdus:
        op = a[0]
        if op == 0x11:
            ln = a[1]
            for i in range(2, len(a), ln):
                e = a[i:i + ln]
                s, en = struct.unpack("<HH", e[:4])
                services.append((s, en, uuid_str(e[4:])))
        elif op == 0x09:
            ln = a[1]
            for i in range(2, len(a), ln):
                e = a[i:i + ln]
                if ln in (7, 21):
                    decl, props, vh = struct.unpack("<HBH", e[:5])
                    chars.append((decl, props, vh, uuid_str(e[5:])))
        elif op == 0x05:
            step = 4 if a[1] == 1 else 18
            for i in range(2, len(a), step):
                e = a[i:i + step]
                descs[struct.unpack("<H", e[:2])[0]] = uuid_str(e[2:])

    names = {}
    print("\n=== GATT table (from the app's own discovery) ===")
    for s, en, u in sorted(set(services)):
        print(f"Service {u}  handles 0x{s:04X}-0x{en:04X}")
        for decl, props, vh, cu in sorted(set(chars)):
            if s <= decl <= en:
                p = "|".join(n for b, n in PROPS if props & b)
                names[vh] = cu
                print(f"   char {cu}  value=0x{vh:04X}  [{p}]")
                for dh, du in sorted(descs.items()):
                    if vh < dh <= en and du not in ("0x2803",) and dh not in [c[0] for c in chars]:
                        nxt = min([c[0] for c in chars if c[0] > vh] + [en + 1])
                        if dh < nxt:
                            print(f"      desc {du} at 0x{dh:04X}")
                            names[dh] = f"{du} of {cu[:8]}"

    print("\n=== ATT traffic (excluding discovery) ===")
    t0 = pdus[0][0] if pdus else 0
    for t, d, a, h in pdus:
        op = a[0]
        if op in (0x04, 0x05, 0x08, 0x09, 0x10, 0x11):
            continue
        name = ATT_OPS.get(op, f"OP_{op:02X}")
        detail = ""
        if op in (0x12, 0x52, 0x1B, 0x1D, 0x0A):
            vh = struct.unpack("<H", a[1:3])[0]
            detail = f"h=0x{vh:04X} ({names.get(vh, '?')[:13]}) len={len(a)-3} {a[3:].hex()}"
        elif op == 0x0B:
            detail = f"len={len(a)-1} {a[1:].hex()}"
        elif op == 0x01:
            detail = f"req=0x{a[1]:02X} h=0x{struct.unpack('<H', a[2:4])[0]:04X} code=0x{a[4]:02X}"
        elif op in (0x02, 0x03):
            detail = f"mtu={struct.unpack('<H', a[1:3])[0]}"
        print(f"[{fmt(t)} +{t-t0:7.2f}s] {d} {name:10} {detail}")


def fmt(t):
    return datetime.fromtimestamp(t, timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
