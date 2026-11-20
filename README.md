# MOES Tuya BLE Lock for Home Assistant

<img src="custom_components/moes_ble_lock/brand/icon.png" width="96" align="right" alt="">

Local Bluetooth control of the MOES ES830 Bluetooth cabinet lock (Tuya BLE, service `0xFD50`).
Setup fetches the keys from your Tuya account once. After that it needs no cloud and no
Smart Life app.
It talks to the lock directly, through a Home Assistant Bluetooth adapter or an ESPHome
Bluetooth proxy.

- **Lock**: unlock opens the latch. Lock means auto-lock on, so it locks straight away.
- **Auto-lock** switch: turning it off holds the latch open until you turn it back on.
- **Battery**, **Signal strength** and **Connected** diagnostic sensors.
- Real-time state while connected, a regular background refresh, automatic reconnection and
  a re-authentication prompt if the keys stop working.

Smart Life keeps working alongside it. The lock accepts **one Bluetooth connection at a
time**, so whichever side connects first wins; Home Assistant lets go 60 s after its last use
(adjustable).

> Tested with one lock: MOES ES830, Tuya product `0m1ajdjg`, category `jtmspro`, protocol 4.2.
> Other Tuya BLE locks with the same category may work; please open an issue either way.

## What you need

1. A Bluetooth adapter or an [ESPHome Bluetooth proxy](https://esphome.io/projects/?type=bluetooth)
   **within about 5–10 m of the lock**. The signal should be −80 dBm or better.
   The **Signal strength** sensor helps you place the proxy.
2. The lock already paired in **Smart Life** or **Tuya Smart**.
3. A free **Tuya developer account** with your Smart Life account linked to it. Setup uses it
   once to fetch each lock's keys. After that, everything is local. See below.

## Install

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/Muji86/ha-moes-ble-lock`,
   type **Integration**.
2. Search for **MOES Tuya BLE Lock**, select **Download**, then restart Home Assistant.

### Manual

Copy `custom_components/moes_ble_lock` into your Home Assistant `config/custom_components/`
folder, then restart.

## Add the lock

1. **Close Smart Life** on every phone, or turn off Bluetooth, so the lock is free.
2. Home Assistant usually discovers it on its own: **Settings → Devices & services →
   Discovered**. If not, select **Add integration** and search for **MOES Tuya BLE Lock**.
3. Choose **Log in to my Tuya developer account (automatic)** and enter your project's
   **Access ID** and **Access Secret**.
   Home Assistant then:
   - finds which of your locks this is, from the identity it broadcasts
   - fetches its local key and unlock check code
   - logs in to the lock to check them (it does **not** unlock)
   - names it as in Smart Life
4. **Adding more locks:** the account is remembered, so each lock is matched and added as soon as
   you select it under **Discovered**.

If a lock is re-paired in Smart Life, it gets a new key. Home Assistant notices and fetches the
new key from the account.

### Setting up the Tuya developer account (free, once)

1. Sign up at [platform.tuya.com](https://platform.tuya.com) and create a **Cloud project**:
   development method **Smart Home**, and the **data center that matches your Smart Life
   account's region** (UK and Europe: Central Europe).
2. In the project: **Devices → Link App Account → Add App Account**. Scan the QR code with
   Smart Life (**Me →** scan icon at the top). Your locks appear under **All Devices**.
3. **Overview → Authorization Key** shows the **Access ID** and **Access Secret** to enter
   in Home Assistant.

The free IoT Core plan has to be active when you add a lock. If setup reports a subscription or
permission error, renew it under **Cloud → Cloud Services**. Locks already set up don't need it.

### Entering the keys by hand (alternative)

Choose **Enter the keys myself**. You need four values:

- **Device ID, UUID and Local key:** from the API Explorer, **Device Management → Query Device
  Details** (`/v2.0/cloud/thing/{id}`). Other endpoints return a different `local_key` that
  these locks reject.
- **Check code:** 8 characters, the last bytes of the lock's `ble_unlock_check` status. Or read
  it from a phone Bluetooth log of one unlock from Smart Life:
  1. Android **Developer options → Enable Bluetooth HCI snoop log**. Turn Bluetooth off and on.
  2. Unlock the lock once in Smart Life.
  3. Run `adb bugreport bugreport.zip` and extract `btsnoop_hci.log`.
  4. Put the other values in a `lock.json`:
     `{"device_id": "...", "uuid": "...", "local_key": "...", "check_code": "", "mac": "DC:23:51:AA:BB:CC"}`
  5. Run `python tools/tuya_decrypt.py btsnoop_hci.log lock.json`, after
     `pip install cryptography`. It prints the code at the end.

## Options

**Settings → Devices & services → MOES Tuya BLE Lock → Configure**:

| Option | Default | Meaning |
|---|---|---|
| Stay connected after activity | 60 s | `0` = always connected: instant updates, but uses more lock battery and blocks Smart Life |
| Refresh every | 30 min | Each refresh briefly connects to read the state and battery |

## Troubleshooting

- **Unavailable or slow commands**: check **Signal strength**. Below about −85 dBm, move a
  proxy closer. The reading only updates while Home Assistant isn't connected, because the lock
  stops advertising during a connection.
- **"The lock isn't advertising"**: Smart Life (on any phone) is probably connected to it.
- **"Rejected these keys"**: the lock was re-paired; fetch the new local key.

## How it works

The protocol write-up is in [PROTOCOL_NOTES.md](PROTOCOL_NOTES.md): framing, AES keys,
handshake, data points and the unlock command. The protocol code is plain Python with no Home
Assistant dependency:

```
tuya_ble_lock/protocol.py   sans-IO protocol (framing, AES, handshake, DPs)
tuya_ble_lock/client.py     async bleak client
tuya_ble_lock/cloud.py      read-only Tuya developer-platform client (setup only)
tuya_ble_lock/__main__.py   command-line tool: scan / status / watch / unlock / lock / hold-open
tests/                      offline tests (replay captured frames; need the owner's keys, else skipped)
tools/                      btsnoop parser, offline decryptor, advert listener, icon generator
custom_components/moes_ble_lock/   the Home Assistant integration (vendors tuya_ble_lock in lib/)
```

Command-line use, with the four values in `secrets/lock.json`:

```bash
python3 -m venv .venv && .venv/bin/pip install bleak cryptography pytest
.venv/bin/python -m tuya_ble_lock status        # log in and read the state; nothing moves
.venv/bin/python -m tuya_ble_lock unlock --yes  # momentary unlock (auto-locks again)
```

After changing `tuya_ble_lock/`, run `tools/sync_lib.sh` to update the integration's copy.

## Disclaimer

This is an unofficial project, not affiliated with MOES or Tuya. You use it at your own risk. It's a
cabinet lock, so don't rely on it for anything safety-critical.
