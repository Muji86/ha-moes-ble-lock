"""Sans-IO implementation of the Tuya BLE protocol (v4) as observed on the MOES ES830.

Nothing in this module touches Bluetooth. `Session` turns intents into frames to
write, and turns notification bytes into events (plus any replies the protocol
requires, such as acks and time responses). See PROTOCOL_NOTES.md for the
captures this is based on.
"""
from __future__ import annotations

import hashlib
import os
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SERVICE_UUID = "0000fd50-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "00000001-0000-1001-8001-00805f9b07d0"
CHAR_NOTIFY = "00000002-0000-1001-8001-00805f9b07d0"

VERSION_BYTE = 0x20  # what Smart Life puts in outgoing frames
SEC_LOGIN = 4  # key = MD5(local_key[:6])
SEC_SESSION = 5  # key = MD5(local_key[:6] + srand)


class Func(IntEnum):
    DEVICE_INFO = 0x0000
    PAIR = 0x0001
    DEVICE_STATUS = 0x0003
    SEND_DPS_V4 = 0x0027
    RECV_DP_V4 = 0x8006
    RECV_TIME_DP_V4 = 0x8007
    TIME1_REQ = 0x8011
    TIME2_REQ = 0x8012


class DpType(IntEnum):
    RAW = 0
    BOOL = 1
    VALUE = 2
    STRING = 3
    ENUM = 4
    BITMAP = 5


# Data points (see secrets/model.json and PROTOCOL_NOTES.md)
DP_BATTERY = 8
DP_UNLOCK_BLE_RECORD = 19
DP_ALARM = 21
DP_BEEP_VOLUME = 31
DP_AUTO_LOCK = 33
DP_AUTO_LOCK_TIME = 36
DP_MOTOR_STATE = 47  # True = open
DP_RECORD = 69
DP_CHECK_CODE_SET = 70
DP_BLE_UNLOCK_CHECK = 71

ALARMS = [
    "wrong_finger", "wrong_password", "wrong_card", "wrong_face", "tongue_bad",
    "too_hot", "unclosed_time", "tongue_not_out", "pry", "key_in", "low_battery",
    "power_off", "shock", "defense", "wrong_unlock",
]

PAIR_OK = 0
PAIR_ALREADY_BOUND = 2


class ProtocolError(Exception):
    """Malformed or unexpected data from the lock."""


class AuthError(ProtocolError):
    """The lock rejected our credentials (wrong local_key / uuid / device_id)."""


# --------------------------------------------------------------------------- helpers

def crc16(data: bytes) -> int:
    """CRC-16/MODBUS (init 0xFFFF, reflected poly 0xA001)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf):
            raise ProtocolError("truncated varint")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, pos


def _aes(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    c = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


TUYA_COMPANY_ID = 0x07D0


def uuid_from_advertisement(service_data: bytes, manufacturer_data: bytes) -> str | None:
    """The device's Tuya uuid, decrypted from its advert (no keys needed).

    service_data: the FD50 service data; manufacturer_data: company 0x07D0's data (without the
    company id). key = iv = MD5(service_data); uuid = AES-CBC decrypt of manufacturer_data[4:20].
    """
    if len(manufacturer_data) < 20 or not service_data:
        return None
    key = hashlib.md5(service_data).digest()
    raw = _aes(key, key, manufacturer_data[4:20], encrypt=False)
    try:
        uuid = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return uuid if uuid.isalnum() else None


def check_code_from_status(status: dict[str, object]) -> str | None:
    """The 8-char BLE unlock check code from a lock's cloud status (raw DPs are base64).

    DP 73 remote_pd_setkey_check is `.... <code8> ...` (code at bytes 4..12);
    DP 71 ble_unlock_check (last report) is `0001 ffff <code8> 01 <ts> 0000`.
    """
    import base64

    for code in ("remote_pd_setkey_check", "ble_unlock_check"):
        value = status.get(code)
        if not isinstance(value, str) or not value:
            continue
        try:
            raw = base64.b64decode(value)
        except ValueError:
            continue
        cand = raw[4:12]
        if len(cand) == 8 and cand.isascii() and cand.decode().isalnum():
            return cand.decode()
    return None


# --------------------------------------------------------------------------- data types

@dataclass(frozen=True)
class Credentials:
    local_key: str
    uuid: str
    device_id: str
    check_code: str  # 8-char BLE unlock check code (configured via DP 70 by the app)

    @property
    def key6(self) -> bytes:
        return self.local_key[:6].encode()

    @property
    def login_key(self) -> bytes:
        return hashlib.md5(self.key6).digest()

    @classmethod
    def from_dict(cls, d: dict) -> "Credentials":
        return cls(d["local_key"], d["uuid"], d["device_id"], d["check_code"])


@dataclass(frozen=True)
class Dp:
    id: int
    type: DpType
    value: bytes | int | bool | str

    def encode(self) -> bytes:
        if self.type == DpType.RAW:
            raw = bytes(self.value)
        elif self.type == DpType.BOOL:
            raw = b"\x01" if self.value else b"\x00"
        elif self.type == DpType.VALUE:
            raw = struct.pack(">i", int(self.value))
        elif self.type == DpType.ENUM:
            raw = struct.pack(">B", int(self.value))
        elif self.type == DpType.STRING:
            raw = str(self.value).encode()
        else:  # BITMAP
            raw = int(self.value).to_bytes(4, "big")
        return struct.pack(">BBH", self.id, self.type, len(raw)) + raw


def parse_dps(buf: bytes) -> list[Dp]:
    out, i = [], 0
    while i + 4 <= len(buf):
        dp_id, typ, ln = buf[i], buf[i + 1], struct.unpack(">H", buf[i + 2:i + 4])[0]
        raw = buf[i + 4:i + 4 + ln]
        if len(raw) != ln:
            raise ProtocolError("truncated DP")
        i += 4 + ln
        t = DpType(typ) if typ in DpType._value2member_map_ else DpType.RAW
        if t == DpType.BOOL:
            val: bytes | int | bool | str = raw != b"\x00"
        elif t == DpType.VALUE:
            val = int.from_bytes(raw, "big", signed=True)
        elif t in (DpType.ENUM, DpType.BITMAP):
            val = int.from_bytes(raw, "big")
        elif t == DpType.STRING:
            val = raw.decode(errors="replace")
        else:
            val = raw
        out.append(Dp(dp_id, t, val))
    return out


@dataclass(frozen=True)
class Message:
    seq: int
    resp_to: int
    code: int
    data: bytes

    def pack(self) -> bytes:
        head = struct.pack(">IIHH", self.seq, self.resp_to, self.code, len(self.data)) + self.data
        raw = head + struct.pack(">H", crc16(head))
        return raw + b"\x00" * (-len(raw) % 16)

    @classmethod
    def unpack(cls, plain: bytes) -> "Message":
        if len(plain) < 14:
            raise ProtocolError("message too short")
        seq, resp_to, code, ln = struct.unpack(">IIHH", plain[:12])
        if 14 + ln > len(plain):
            raise ProtocolError("bad length")
        (crc,) = struct.unpack(">H", plain[12 + ln:14 + ln])
        if crc != crc16(plain[:12 + ln]):
            raise ProtocolError("CRC mismatch (wrong key?)")
        return cls(seq, resp_to, code, plain[12:12 + ln])


def encrypt_body(msg: Message, key: bytes, flag: int, iv: bytes | None = None) -> bytes:
    iv = iv or os.urandom(16)
    return bytes([flag]) + iv + _aes(key, iv, msg.pack(), True)


def split_frames(body: bytes, mtu: int, version: int = VERSION_BYTE) -> list[bytes]:
    """Split an encrypted body into ATT-sized frames ([pkt#][len][ver] header on pkt 0)."""
    room = max(mtu - 3, 20)
    frames, pos, num = [], 0, 0
    while pos < len(body) or num == 0:
        head = encode_varint(num)
        if num == 0:
            head += encode_varint(len(body)) + bytes([version])
        take = room - len(head)
        frames.append(head + body[pos:pos + take])
        pos += take
        num += 1
    return frames


class FrameAssembler:
    def __init__(self) -> None:
        self._expected = 0
        self._total = 0
        self._buf = b""

    def feed(self, frame: bytes) -> bytes | None:
        num, pos = decode_varint(frame)
        if num == 0:
            self._total, pos = decode_varint(frame, pos)
            pos += 1  # version byte
            self._buf = b""
        elif num != self._expected:
            self._buf, self._expected = b"", 0
            raise ProtocolError(f"unexpected frame #{num}")
        self._buf += frame[pos:]
        self._expected = num + 1
        if len(self._buf) >= self._total:
            body, self._buf, self._expected = self._buf[:self._total], b"", 0
            return body
        return None


# --------------------------------------------------------------------------- events

@dataclass(frozen=True)
class DeviceInfo:
    device_version: str
    protocol_version: str
    bound: bool
    srand: bytes


@dataclass(frozen=True)
class PairResult:
    result: int


@dataclass(frozen=True)
class StatusResult:
    result: int


@dataclass(frozen=True)
class DpCommandResult:
    sn: int
    result: int


@dataclass(frozen=True)
class DpReport:
    sn: int
    dps: list[Dp]
    timestamp: int | None = None


@dataclass
class FeedResult:
    events: list = field(default_factory=list)
    replies: list[bytes] = field(default_factory=list)  # frames to write back


# --------------------------------------------------------------------------- session

def build_unlock_payload(check_code: str, ts: int | None = None) -> bytes:
    """DP 71 value, copied from Smart Life: ffff 0001 <code> 01 <unix ts> 0001."""
    code = check_code.encode()
    if len(code) != 8:
        raise ValueError("check code must be 8 characters")
    ts = int(time.time()) if ts is None else ts
    return b"\xff\xff\x00\x01" + code + b"\x01" + struct.pack(">I", ts) + b"\x00\x01"


def _utc_offset_hundredths() -> int:
    offset = -(time.altzone if time.localtime().tm_isdst > 0 else time.timezone)
    return int(offset / 36)


class Session:
    """State for one BLE connection. Create a new one for every connection."""

    def __init__(self, creds: Credentials, mtu: int = 23) -> None:
        self.creds = creds
        self.mtu = mtu
        self.session_key: bytes | None = None
        self.device_info: DeviceInfo | None = None
        self._seq = 0
        self._dp_sn = 0
        self._assembler = FrameAssembler()

    # ---- outgoing
    def _frames(self, code: int, data: bytes = b"", resp_to: int = 0) -> list[bytes]:
        self._seq += 1
        msg = Message(self._seq, resp_to, code, data)
        if code == Func.DEVICE_INFO:
            body = encrypt_body(msg, self.creds.login_key, SEC_LOGIN)
        else:
            if self.session_key is None:
                raise ProtocolError("no session key yet; device info not received")
            body = encrypt_body(msg, self.session_key, SEC_SESSION)
        return split_frames(body, self.mtu)

    def device_info_request(self) -> list[bytes]:
        return self._frames(Func.DEVICE_INFO, struct.pack(">H", 0x00F3))

    def pair_request(self) -> list[bytes]:
        c = self.creds
        data = c.uuid.encode() + c.key6 + c.device_id.encode().ljust(22, b"\x00") + b"\x00\x01"
        return self._frames(Func.PAIR, data)

    def status_request(self) -> list[bytes]:
        return self._frames(Func.DEVICE_STATUS)

    def dp_command(self, dps: list[Dp]) -> tuple[int, list[bytes]]:
        self._dp_sn += 1
        data = b"\x00" + struct.pack(">I", self._dp_sn) + b"".join(d.encode() for d in dps)
        return self._dp_sn, self._frames(Func.SEND_DPS_V4, data)

    def unlock_command(self, ts: int | None = None) -> tuple[int, list[bytes]]:
        payload = build_unlock_payload(self.creds.check_code, ts)
        return self.dp_command([Dp(DP_BLE_UNLOCK_CHECK, DpType.RAW, payload)])

    def auto_lock_command(self, enabled: bool) -> tuple[int, list[bytes]]:
        return self.dp_command([Dp(DP_AUTO_LOCK, DpType.BOOL, enabled)])

    # ---- incoming
    def _decrypt(self, body: bytes) -> Message:
        flag, iv, ct = body[0], body[1:17], body[17:]
        if flag == SEC_LOGIN:
            key = self.creds.login_key
        elif flag == SEC_SESSION and self.session_key:
            key = self.session_key
        else:
            raise ProtocolError(f"unsupported security flag {flag}")
        if len(ct) % 16:
            raise ProtocolError("ciphertext not block aligned")
        return Message.unpack(_aes(key, iv, ct, False))

    def feed(self, frame: bytes) -> FeedResult:
        res = FeedResult()
        body = self._assembler.feed(frame)
        if body is None:
            return res
        msg = self._decrypt(body)
        code, d = msg.code, msg.data

        if code == Func.DEVICE_INFO:
            if len(d) < 12:
                raise ProtocolError("short device info")
            srand = d[6:12]
            self.session_key = hashlib.md5(self.creds.key6 + srand).digest()
            self.device_info = DeviceInfo(f"{d[0]}.{d[1]}", f"{d[2]}.{d[3]}", d[5] != 0, srand)
            res.events.append(self.device_info)
        elif code == Func.PAIR:
            res.events.append(PairResult(d[0] if d else -1))
        elif code == Func.DEVICE_STATUS:
            res.events.append(StatusResult(d[0] if d else -1))
        elif code == Func.SEND_DPS_V4:
            sn = struct.unpack(">I", d[1:5])[0]
            res.events.append(DpCommandResult(sn, d[5]))
        elif code in (Func.RECV_DP_V4, Func.RECV_TIME_DP_V4):
            sn = struct.unpack(">I", d[1:5])[0]
            flags = d[5:7]
            if code == Func.RECV_TIME_DP_V4:
                ts = struct.unpack(">I", d[8:12])[0]
                dps = parse_dps(d[12:])
            else:
                ts, dps = None, parse_dps(d[7:])
            res.events.append(DpReport(sn, dps, ts))
            ack = b"\x00" + struct.pack(">I", sn) + flags + b"\x00"
            res.replies += self._frames(code, ack, resp_to=msg.seq)
        elif code == Func.TIME1_REQ:
            data = str(int(time.time() * 1000)).encode() + struct.pack(">h", _utc_offset_hundredths())
            res.replies += self._frames(code, data, resp_to=msg.seq)
        elif code == Func.TIME2_REQ:
            t = time.localtime()
            data = struct.pack(">BBBBBBBh", t.tm_year % 100, t.tm_mon, t.tm_mday, t.tm_hour,
                               t.tm_min, t.tm_sec, t.tm_wday, _utc_offset_hundredths())
            res.replies += self._frames(code, data, resp_to=msg.seq)
        # anything else is ignored (logged by the caller if wanted)
        return res
