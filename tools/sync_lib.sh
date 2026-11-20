#!/bin/sh
# Copy the tested protocol/client into the HA integration (single source of truth: tuya_ble_lock/).
cd "$(dirname "$0")/.." || exit 1
cp tuya_ble_lock/protocol.py tuya_ble_lock/client.py custom_components/moes_ble_lock/lib/
printf '"""Vendored copy of tuya_ble_lock (protocol + client). Do not edit: run tools/sync_lib.sh."""\n' \
  > custom_components/moes_ble_lock/lib/__init__.py
echo "synced"
