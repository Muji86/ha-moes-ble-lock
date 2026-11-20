"""Constants for the MOES Tuya BLE Lock integration."""
from __future__ import annotations

DOMAIN = "moes_ble_lock"

CONF_LOCAL_KEY = "local_key"
CONF_UUID = "uuid"
CONF_DEVICE_ID = "device_id"
CONF_CHECK_CODE = "check_code"

CONF_IDLE_TIMEOUT = "idle_timeout"
CONF_POLL_INTERVAL = "poll_interval"

# Stay connected this long after the last activity (live updates, instant commands),
# then release the lock so Smart Life can connect. 0 = stay connected permanently.
DEFAULT_IDLE_TIMEOUT = 60  # seconds
# How often to connect briefly to refresh state and battery when idle.
DEFAULT_POLL_INTERVAL = 30  # minutes

# Tuya IoT platform (developer account) credentials, kept so more locks can be added and
# changed keys re-fetched without typing anything. Only used during setup / re-auth.
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
