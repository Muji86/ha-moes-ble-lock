# MOES Tuya BLE Lock for Home Assistant

<img src="custom_components/moes_ble_lock/brand/icon.png" width="96" align="right" alt="">

Local Bluetooth control of the MOES ES830 Bluetooth cabinet lock (Tuya BLE, service `0xFD50`).
It needs no cloud, no Tuya IoT subscription and no Smart Life app once it's set up.
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
3. Four values for the lock: **Device ID**, **UUID** and **Local key** (from the Tuya IoT
   platform, below) and an 8-character **check code** (from a phone Bluetooth log, below).

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
   Tuya locks advertise as `TY`. If you have several, match the MAC address in Smart Life
   (lock → ✎ → **Device information**).
3. Enter the four values. Setup logs in to the lock to check them; it does **not** unlock.

## Getting the keys

### Device ID, UUID and Local key (Tuya IoT platform, free)

1. Sign up at [platform.tuya.com](https://platform.tuya.com) and create a **Cloud project**:
   development method **Smart Home**, and the **data center that matches your Smart Life
   account's region**.
2. In the project: **Devices → Link App Account → Add App Account**. Scan the QR code with
   Smart Life (**Me → ⊞ scan icon**). Your devices now appear under **All Devices**; copy the
   lock's **Device ID**.
3. **Cloud → API Explorer → Device Management → Query Device Details**, enter the Device ID
   and select **Submit Request**. The result holds `local_key` and `uuid`.

You only need the account during setup. Re-pairing the lock in Smart Life gives it a new
local key; Home Assistant will ask for the new one.

### Check code (one Bluetooth log from an Android phone)

The lock only accepts an unlock that carries an 8-character code. Smart Life gets it from
the Tuya cloud and sends it in every unlock. So far the simplest way to read it is to log
one unlock:

1. Android **Settings → About phone →** tap **Build number** 7× to enable Developer options.
2. **Developer options → Enable Bluetooth HCI snoop log → Enabled**. Turn Bluetooth off and
   on again.
3. Open Smart Life and **unlock the lock once**.
4. Get the log onto a computer with [ADB](https://developer.android.com/tools/adb):
   `adb bugreport bugreport.zip`. Then extract `FS/data/log/bt/btsnoop_hci.log`. The path
   differs on some phones; search the zip for `btsnoop_hci.log`.
5. Create `lock.json` with what you have so far:

   ```json
   {"device_id": "...", "uuid": "...", "local_key": "...", "check_code": "", "mac": "DC:23:51:AA:BB:CC"}
   ```

6. Run the decryptor. It only reads the file and prints the code at the end:

   ```bash
   python3 -m venv .venv && .venv/bin/pip install cryptography
   .venv/bin/python tools/tuya_decrypt.py btsnoop_hci.log lock.json
   ```

7. Turn the snoop log off again.

The code has stayed the same across many unlocks. It is probably tied to your account, not
to each unlock.

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
tuya_ble_lock/__main__.py   command-line tool: scan / status / watch / unlock / lock / hold-open
tests/                      offline tests (replay captured frames; need the owner's keys, else skipped)
tools/                      btsnoop parser, offline decryptor, advert listener, icon generator
custom_components/moes_ble_lock/   the Home Assistant integration (vendors protocol.py + client.py in lib/)
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
