"""Offline tests: replay frames captured from Smart Life (capture 1, 2026-11-17)."""
import json
import pathlib

import pytest

from tuya_ble_lock import protocol as p

SECRETS = pathlib.Path(__file__).parent.parent / "secrets" / "lock.json"

# Raw ATT values exactly as captured
DEVICE_INFO_RSP = bytes.fromhex(
    "008101400410494531a330338cd9efb4df3446adead88add6df1a3aaf205c99f11344845e683124a15dbba"
    "08d710043b64be0c6e9cd79b994d53fc8078e2b47278a9e8fd6ab2e2ed7faa99e6eb80e0775d71e88514ee"
    "806bf1ce411265163a28b22ff24ab907d7b01db85ff310fc7632fb044ccbfbad58b55a15fa6291a75e6e51"
    "eefeb7e7")
PAIR_REQ = bytes.fromhex(
    "005120053a5db989afcb3224929e77181924a51119abf04c689e50ffb272dcd7d5409a7e8c290955b52774"
    "deba12b0737d51ce58e0f11ebcfaa185724180460cfb1cc1ac21e854664f316d86366cd0bbacbc7feb")
UNLOCK_CMD = bytes.fromhex(
    "0041200597eb7fd9f1026a160046ac304299ecb230a67af764286f83486cf596759d74021ff1948cd0a2c3"
    "2e1ecd50cff77c491be630b452effef480a3f464e56e593112")
REPORT_OPEN = bytes.fromhex(
    "0031400548814509622813245da7da37eabeef02a678c65f1368d156af1895a7c79551773a08f0c8ec18f5"
    "dd756a5d69ce1471de")


@pytest.fixture
def session():
    if not SECRETS.exists():
        pytest.skip("needs secrets/lock.json")
    creds = p.Credentials.from_dict(json.loads(SECRETS.read_text()))
    s = p.Session(creds, mtu=247)
    s.feed(DEVICE_INFO_RSP)
    return s


def body_of(frame):
    return p.FrameAssembler().feed(frame)


def test_device_info_gives_session_key(session):
    info = session.device_info
    assert info.protocol_version == "4.2"
    assert info.bound
    assert info.srand.hex() == "7a170b120e43"


def test_pair_request_is_byte_identical(session):
    captured = session._decrypt(body_of(PAIR_REQ))
    session._seq = captured.seq - 1
    ours = session.pair_request()
    assert session._decrypt(body_of(ours[0])) == captured


def test_unlock_command_byte_identical(session):
    body = body_of(UNLOCK_CMD)
    captured = session._decrypt(body)
    session._seq, session._dp_sn = captured.seq - 1, 2
    sn, _ = session.unlock_command(ts=0x6AFCC47A)
    assert sn == 3
    msg = p.Message(captured.seq, 0, p.Func.SEND_DPS_V4, captured.data)
    # same plaintext + same IV => same ciphertext as Smart Life sent
    assert p.encrypt_body(msg, session.session_key, 5, iv=body[1:17]) == body
    expected = p.build_unlock_payload(session.creds.check_code, 0x6AFCC47A)
    assert captured.data == b"\x00" + (3).to_bytes(4, "big") + p.Dp(71, p.DpType.RAW, expected).encode()


def test_report_parsed_and_acked(session):
    res = session.feed(REPORT_OPEN)
    (ev,) = res.events
    assert isinstance(ev, p.DpReport)
    assert ev.dps == [p.Dp(47, p.DpType.BOOL, True)]
    ack = session._decrypt(body_of(res.replies[0]))
    assert ack.code == p.Func.RECV_DP_V4
    assert ack.data == bytes.fromhex("000000002b000000")  # what Smart Life sent


def test_fragmentation_roundtrip():
    body = bytes(range(200))
    frames = p.split_frames(body, mtu=23)
    assert all(len(f) <= 20 for f in frames)
    asm = p.FrameAssembler()
    out = [asm.feed(f) for f in frames]
    assert out[-1] == body and all(o is None for o in out[:-1])


def test_vendored_lib_in_sync():
    root = pathlib.Path(__file__).parent.parent
    for name in ("protocol.py", "client.py"):
        assert (root / "tuya_ble_lock" / name).read_bytes() == \
            (root / "custom_components/moes_ble_lock/lib" / name).read_bytes(), \
            f"{name} out of sync: run tools/sync_lib.sh"
