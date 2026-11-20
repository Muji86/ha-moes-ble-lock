"""List the locks the Tuya IoT platform knows, without printing any keys (read-only).

    .venv/bin/python tools/cloud_keys.py

Reads secrets/tuya_iot.json: {"access_id": "...", "access_secret": "..."}
(Tuya IoT platform → Cloud → your project → Overview → Authorization key).
If secrets/lock.json exists, says whether the fetched values match it.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuya_ble_lock.cloud import find_locks  # noqa: E402

SECRETS = Path(__file__).resolve().parent.parent / "secrets"


async def main() -> None:
    cfg = json.loads((SECRETS / "tuya_iot.json").read_text())
    known = json.loads((SECRETS / "lock.json").read_text()) if (SECRETS / "lock.json").exists() else {}
    try:  # python.org builds on macOS ship without CA certificates
        import certifi
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        connector = None
    async with aiohttp.ClientSession(connector=connector) as session:
        endpoint, locks = await find_locks(session, cfg["access_id"], cfg["access_secret"])
    print(f"data centre: {endpoint or 'none with locks'}; {len(locks)} lock(s)")
    for lk in locks:
        line = (f"  {lk.name:26} id={lk.device_id} cat={lk.category} "
                f"local_key={'yes' if lk.local_key else 'NO'} check_code={'yes' if lk.check_code else 'NO'}")
        if lk.device_id == known.get("device_id"):
            line += (f"  | vs lock.json: key {'OK' if lk.local_key == known['local_key'] else 'DIFFERENT'},"
                     f" uuid {'OK' if lk.uuid == known['uuid'] else 'DIFFERENT'},"
                     f" code {'OK' if lk.check_code == known['check_code'] else 'DIFFERENT'}")
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
