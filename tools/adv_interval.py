#!/usr/bin/env python3
"""Passively measure how often the lock advertises. Sends nothing.

Usage: .venv/bin/python tools/adv_interval.py [seconds]

Scans WITHOUT an OS service filter (macOS drops adverts whose FD50 is only in
service data), and matches the lock by remembered address, FD50 anywhere, or name 'TY'.
"""
import asyncio
import pathlib
import statistics
import sys
import time

from bleak import BleakScanner

FD50 = "0000fd50-0000-1000-8000-00805f9b34fb"
CACHE = pathlib.Path("secrets/.ble_address")


async def main(duration: float) -> None:
    known = CACHE.read_text().strip().upper() if CACHE.exists() else None
    seen: list[float] = []
    kinds: dict[str, int] = {}
    t0 = time.monotonic()

    def cb(dev, adv):
        why = []
        if known and dev.address.upper() == known:
            why.append("addr")
        if FD50 in adv.service_uuids:
            why.append("uuid")
        if FD50 in adv.service_data:
            why.append("sdata")
        if adv.local_name == "TY":
            why.append("name")
        if not why:
            return
        t = time.monotonic() - t0
        key = "+".join(why)
        if key not in kinds:
            print(f"  new advert form [{key}]: name={adv.local_name!r} uuids={adv.service_uuids} "
                  f"sdata={ {k[4:8]: v.hex() for k, v in adv.service_data.items()} } "
                  f"mfr={ {hex(k): v.hex() for k, v in adv.manufacturer_data.items()} }")
        kinds[key] = kinds.get(key, 0) + 1
        seen.append(t)
        print(f"  +{t:6.2f}s  rssi={adv.rssi}  [{key}]  {dev.address[:8]}")

    print(f"listening {duration:.0f}s, unfiltered (known address: {known})…")
    async with BleakScanner(cb):
        await asyncio.sleep(duration)
    print(f"\nadvert forms seen: {kinds}")
    if len(seen) < 2:
        print(f"only {len(seen)} advertisement(s) seen")
        return
    gaps = [b - a for a, b in zip(seen, seen[1:])]
    print(f"{len(seen)} adverts; first after {seen[0]:.2f}s; gap median "
          f"{statistics.median(gaps):.2f}s, min {min(gaps):.2f}s, max {max(gaps):.2f}s")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 90))
