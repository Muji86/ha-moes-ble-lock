"""Read-only probe: log in to Tuya via the Smart Life QR code and check which lock keys come back.

Same login as Home Assistant's built-in Tuya integration (no developer account).
Only GET requests are made: list homes and their devices. Nothing is sent to any device.

    .venv/bin/python tools/cloud_probe.py [USER_CODE]

User code: Smart Life app → Me → ⚙ Settings → Account and Security → User Code.
Raw responses are saved to secrets/ (private, git-ignored); keys are never printed.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import qrcode
from tuya_sharing import LoginControl, Manager

CLIENT_ID = "HA_3y9q4ak7g4ephrvke"  # Home Assistant's public Tuya client id
SCHEMA = "haauthorize"
ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
LOGIN_FILE = SECRETS / "cloud_login.json"
RAW_FILE = SECRETS / "cloud_probe.json"


def login(user_code: str) -> dict:
    ctl = LoginControl()
    resp = ctl.qr_code(CLIENT_ID, SCHEMA, user_code)
    if not resp.get("success"):
        sys.exit(f"QR request failed: {resp.get('msg') or resp}")
    token = resp["result"]["qrcode"]
    data = f"tuyaSmart--qrLogin?token={token}"
    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.print_ascii(invert=True)
    qrcode.make(data).save(SECRETS / "login_qr.png")
    print("\nScan this with the Smart Life app (＋ → Scan, top right of Home), then confirm.")
    print(f"(Also saved as {SECRETS / 'login_qr.png'})")
    for _ in range(90):
        time.sleep(2)
        ok, info = ctl.login_result(token, CLIENT_ID, user_code)
        if ok:
            info["user_code"] = user_code
            LOGIN_FILE.write_text(json.dumps(info, indent=2))
            print("Logged in.")
            return info
    sys.exit("Timed out waiting for the QR scan.")


def main() -> None:
    lock = json.loads((SECRETS / "lock.json").read_text())
    if LOGIN_FILE.exists() and len(sys.argv) < 2:
        info = json.loads(LOGIN_FILE.read_text())
        print("Reusing saved login.")
    else:
        code = sys.argv[1] if len(sys.argv) > 1 else input("Smart Life user code: ").strip()
        info = login(code)

    token = {k: info[k] for k in ("t", "uid", "expire_time", "access_token", "refresh_token")
             if k in info} or info.get("token_info", {})
    mgr = Manager(CLIENT_ID, info["user_code"], info["terminal_id"], info["endpoint"], token)
    api = mgr.customer_api

    raw: dict = {"homes": api.get("/v1.0/m/life/users/homes")}
    homes = raw["homes"].get("result") or []
    raw["devices"] = {}
    for h in homes:
        hid = str(h.get("ownerId") or h.get("homeId") or h.get("id"))
        raw["devices"][hid] = api.get("/v1.0/m/life/ha/home/devices", {"homeId": hid})
    RAW_FILE.write_text(json.dumps(raw, indent=2, default=str))
    print(f"\n{len(homes)} home(s); raw responses saved to {RAW_FILE}")

    devices = [d for r in raw["devices"].values() for d in (r.get("result") or [])]
    print(f"{len(devices)} device(s):")
    for d in devices:
        print(f"  {d.get('name')!r:30} id={d.get('id')} cat={d.get('category')} "
              f"pid={d.get('product_id') or d.get('productId')} "
              f"local_key={'yes' if d.get('local_key') or d.get('localKey') else 'no'} "
              f"uuid={'yes' if d.get('uuid') else 'no'}")

    mine = next((d for d in devices if d.get("id") == lock["device_id"]), None)
    print("\n--- this lock ---")
    if not mine:
        print("Not found in the device list.")
    else:
        lk = mine.get("local_key") or mine.get("localKey")
        print("local_key matches lock.json:", lk == lock["local_key"])
        print("uuid matches lock.json:     ", mine.get("uuid") == lock["uuid"])
        print("fields returned:", ", ".join(sorted(mine)))
        print("status codes:", [s.get("code") for s in mine.get("status", [])])
    blob = json.dumps(raw)
    print("check code appears anywhere in responses:", lock["check_code"] in blob)


if __name__ == "__main__":
    main()
