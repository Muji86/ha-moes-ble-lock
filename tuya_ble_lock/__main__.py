"""Command line for testing the lock.

  python -m tuya_ble_lock scan              # passive: list nearby Tuya FD50 devices
  python -m tuya_ble_lock status            # connect, authenticate, read state (no movement)
  python -m tuya_ble_lock watch             # status, then print live reports until Ctrl-C
  python -m tuya_ble_lock unlock --yes      # momentary unlock
  python -m tuya_ble_lock lock --yes        # auto-lock on (locks now)
  python -m tuya_ble_lock hold-open --yes   # auto-lock off (opens and stays open)

Close Smart Life first: the lock accepts one connection at a time.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys
import time

from bleak import BleakScanner

from . import protocol as p
from .client import LockState, TuyaBleLock

MOVING = {"unlock", "lock", "hold-open"}


ADDRESS_CACHE = pathlib.Path("secrets/.ble_address")


async def find_lock(address: str | None, attempts: int = 3, timeout: float = 10.0):
    """Find the lock. On macOS addresses are CoreBluetooth UUIDs, so we match on the FD50
    service and remember the address that worked last time."""
    if not address and ADDRESS_CACHE.exists():
        address = ADDRESS_CACHE.read_text().strip()
    def match(dev, adv) -> bool:
        if address:
            return dev.address.upper() == address.upper()
        return True  # scanner is already filtered to FD50 by the OS

    for attempt in range(1, attempts + 1):
        # Returns as soon as the lock is seen (usually < 1 s) instead of waiting out a full scan
        dev = await BleakScanner.find_device_by_filter(
            match, timeout=timeout, service_uuids=[p.SERVICE_UUID])
        if dev:
            if not address:
                ADDRESS_CACHE.write_text(dev.address)
            return dev
        print(f"lock not advertising (attempt {attempt}/{attempts}); "
              "is Smart Life connected to it?")
    raise SystemExit("lock not found. Force-stop Smart Life on the phone and try again.")


def show(state: LockState) -> None:
    print(f"  latch: {'OPEN' if state.open else 'locked' if state.open is not None else '?'}"
          f"   battery: {state.battery}%   auto-lock: {state.auto_lock}"
          f" ({state.auto_lock_time}s)" + (f"   alarm: {state.alarm}" if state.alarm else ""))


async def main() -> int:
    ap = argparse.ArgumentParser(prog="tuya_ble_lock")
    ap.add_argument("command", choices=["scan", "status", "watch", *sorted(MOVING)])
    ap.add_argument("--secrets", default="secrets/lock.json")
    ap.add_argument("--address", help="BLE address (Linux) or CoreBluetooth UUID (macOS)")
    ap.add_argument("--yes", action="store_true", help="confirm a command that moves the lock")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING)

    if a.command == "scan":
        found = await BleakScanner.discover(timeout=15.0, return_adv=True,
                                            service_uuids=[p.SERVICE_UUID])
        if not found:
            print("no Tuya FD50 devices found (is Smart Life connected to it?)")
        for dev, adv in found.values():
            if True:  # noqa: SIM108
                print(f"{dev.address}  name={adv.local_name!r}  rssi={adv.rssi}"
                      f"  service_data={ {k[4:8]: v.hex() for k, v in adv.service_data.items()} }"
                      f"  mfr={ {hex(k): v.hex() for k, v in adv.manufacturer_data.items()} }")
        return 0

    if a.command in MOVING and not a.yes:
        print(f"'{a.command}' moves the lock; re-run with --yes to confirm.")
        return 2

    creds = p.Credentials.from_dict(json.loads(pathlib.Path(a.secrets).read_text()))
    t0 = time.monotonic()
    device = await find_lock(a.address)
    t1 = time.monotonic()
    lock = TuyaBleLock(device, creds,
                       on_update=(show if a.command == "watch" else None))
    print(f"found in {t1 - t0:.1f}s, connecting…")
    await lock.connect()
    info = lock._session.device_info
    print(f"authenticated in {time.monotonic() - t1:.1f}s "
          f"(fw {info.device_version}, protocol {info.protocol_version})")
    show(lock.state)
    try:
        if a.command == "watch":
            print("watching; Ctrl-C to stop (the lock may drop idle links after ~60 s)")
            while lock.is_connected:
                await asyncio.sleep(1)
        elif a.command == "unlock":
            await lock.unlock()
            print("unlock accepted")
            await asyncio.sleep(3)
            show(lock.state)
        elif a.command == "lock":
            await lock.lock()
            await asyncio.sleep(1.5)
            show(lock.state)
        elif a.command == "hold-open":
            await lock.hold_open()
            await asyncio.sleep(1.5)
            show(lock.state)
    finally:
        await lock.disconnect()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
