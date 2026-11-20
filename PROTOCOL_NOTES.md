# MOES BLE cabinet lock — protocol notes

Device: Tuya product `ES830` (`0m1ajdjg`), category `jtmspro`, MAC `DC:23:51:xx:xx:xx`.
Credentials: `secrets/lock.json` (device_id, uuid, local_key). Keep private.
Source: HCI snoop capture 2026-11-17 21:17–21:19 (Smart Life open → unlock, auto-lock after 5 s).
Tools: `tools/btsnoop_att.py <log> <MAC>` (ATT view), `tools/tuya_decrypt.py <log> secrets/lock.json` (decrypted view).

**Status: every message in the capture decrypts with a valid CRC using the cloud `local_key`.**
This is standard Tuya BLE, protocol version 4.2 (device fw 1.2, hw 0.1).

## Transport

| Handle | UUID | Props |
|---|---|---|
| svc 0x000E–0x0013 | `0xFD50` | |
| 0x0010 | `00000001-0000-1001-8001-00805f9b07d0` | WRITE, WRITE_NR — app uses Write Without Response |
| 0x0012 | `00000002-0000-1001-8001-00805f9b07d0` | NOTIFY |
| 0x0013 | CCCD of 0x0012 | write `0100` |

- No BLE SMP pairing / link encryption. MTU 247 (lock requests). Lock drops idle link after ~60 s.

## Frame

```
[pkt# varint][total-len varint, pkt 0 only][ver byte, pkt 0 only][sec flag][IV 16][AES-128-CBC ct]
```
- ver byte: `0x20` phone→lock, `0x40` lock→phone. IV random per message; plaintext zero-padded to 16.
- sec flag 4: key = `MD5(local_key[:6])` — used only for DEVICE_INFO.
- sec flag 5: key = `MD5(local_key[:6] + srand)`, `srand` = device-info data[6:12]. New every connection.

Plaintext: `seq u32 BE | resp_to u32 | code u16 | len u16 | data | crc16 u16` (CRC-16/MODBUS: init 0xFFFF, poly 0xA001).
Each side keeps its own `seq` counter from 1; responses set `resp_to` to the request's seq.

## Session handshake (verified)

1. → `0x0000` DEVICE_INFO, data `00f3`  (key 4)
2. ← DEVICE_INFO, 96 bytes: ver 1.2, protocol 4.2, flags 0x05, bound=1, srand[6:12], device_id and MAC inside
3. → `0x0001` PAIR (key 5): `uuid(16 ASCII) + local_key[:6] + device_id padded to 22 + 00 01`
4. ← PAIR result `02` (= already bound; normal for an existing pairing). Lock also sends `0x8011` time request.
5. → `0x0003` DEVICE_STATUS (empty) → ← status `00`, then a full DP report (below)
6. → time reply to `0x8011`: 13-char ASCII ms timestamp + `0000` (timezone ×100, i16)

## DP messages (protocol v4)

- `0x0027` command →: `00 | sn u32 | DPs`; result ←: `00 | sn u32 | result u8` (0 = ok)
- `0x8006` report ←: `00 | sn u32 | flags u16 | DPs`
- `0x8007` timed report ←: `00 | sn u32 | flags u16 | 01 | unix_ts u32 | DPs`
- Ack every report →, same function code: `00 | sn u32 | 00 00 | 00`
- DP entry: `id u8 | type u8 | len u16 BE | value` (type 0 raw, 1 bool, 2 value, 4 enum)

## Cloud data model (`secrets/model.json`, model e1kipfqo)

Declared DPs: 1/2/3 unlock method create/delete/modify, **8 residual_electricity**, 15 unlock_card,
**19 unlock_ble**, 21 alarm_lock (enum incl. low_battery, pry, tongue_bad…), 31 beep_volume (mute/normal),
**33 automatic_lock**, **47 lock_motor_state**, 54 synch_method, 61 remote_no_dp_key, 62 unlock_phone_remote,
68 special_function, **69 record** (fetch stored unlock records), **70 check_code_set**
(configures the BLE unlock check code), **71 ble_unlock_check**, 73 remote_pd_setkey_check.

Implications:
- **No lock command DP exists.** Locking is only via auto-lock (DP 33 + delay DP 36).
- DP 69 sent on connect = "fetch records", not an unlock attempt.
- The 8-char code in DP 71 is set into the lock via DP 70 → it is stored on the lock and
  likely only changes if the app re-sends DP 70. Watch for DP 70 in captures.
- The lock also reports DPs not in the model (28, 32, 34, 36, 40, 58, 76, 77, 89, 90).

## Data points seen

| DP | Type | Seen | Probable meaning |
|---|---|---|---|
| 8 | value | 66 | **battery %** (residual_electricity) |
| 47 | bool | 0 → 1 → 0 | **lock_motor_state** (1 = unlocked). Real-time on unlock and auto-lock |
| 33 | bool | 1 | automatic_lock on |
| 36 | value | 5 | auto_lock_time = 5 s |
| 19 | value | 1, 0 | unlock_ble record (member id), sent as timed report |
| 71 | raw | cmd | **ble_unlock_check — the unlock command** |
| 69 | raw | cmd | sent on connect and once more; result ok, no motor change |
| 28,31,32,34,40,58,76,77,89,90 | various | | settings/other, TBD |

### Unlock (DP 71), observed

```
→ ffff 0001 "<code8>" 01 <unix_ts u32> 0001
← dp71 echo: 0001 ffff "<code8>" 01 <same ts> 0000   (+ dp19 timed report)
← dp47 = 1 (≈1.5 s later)   ...   dp47 = 0 (auto-lock 5 s later)
```
DP 69 carries `ffff 0001 "<code8>" 00` (same 8-char code, no timestamp).
The 8 ASCII characters look like a per-account check code from the cloud. Field meanings
(`ffff`, `0001`, the `01`, the trailing `0001`) are unconfirmed.

## Capture 2 (21:34–21:45): auto-lock toggling — verified

- `dp33=0` (auto-lock off) → lock immediately reports `dp47=1` (open) and **stays open**.
- `dp33=1` (auto-lock on) → lock immediately reports `dp47=0` (locked).
- DP 71 while already open (auto-lock off): command ok, dp19 record, but no dp47 change.
- So DP 33 behaves as a **hold-open / lock** control. This is the only way to lock on demand.
- Same 8-char check code (unchanged) in all 8 unlocks across 20 min; DP 70 never sent.
- Each connection: new `srand`, same PAIR payload, lock answers `02` (already bound).
- Battery reported 66–70 across connections (noisy reading).

### Commands for the client

| Action | Command | Expected result |
|---|---|---|
| Unlock (momentary) | dp71 raw `ffff 0001 <code 8 ASCII> 01 <unix_ts u32> 0001` | dp47=1, then dp47=0 after dp36 s |
| Hold open | dp33 bool 0 | dp33=0, dp47=1 |
| Lock / normal mode | dp33 bool 1 | dp33=1, dp47=0 |

## Open questions
- Does the check code survive days/battery changes? Re-check in a later capture (watch for DP 70).
- Meaning of the fixed DP 71 fields `ffff`, `0001`, trailing `0001` (copy verbatim for now).

## Live verification (2026-11-17, Mac mini + bleak 3.1, `python -m tuya_ble_lock`)

| Command | Result |
|---|---|
| status | auth ok (fw 1.2, proto 4.2), state + battery read — repeated OK |
| unlock | accepted, dp47 → open — repeated OK |
| hold-open (dp33=0) | dp33=false, dp47 → open |
| lock (dp33=1) | dp33=true, dp47 → locked |

Practical notes:
- The lock stops advertising while any central is connected; Smart Life reconnecting in the
  background blocks us (and vice versa). Scans sometimes miss it for a few seconds after a disconnect.
- First connect attempt occasionally times out; a retry succeeds.
- Smart Life kept working throughout; pairing unaffected.

## Advertising / range (2026-11-17, Mac mini, 90 s passive listen)

- Advert: name `TY`, service UUID `FD50`, service data `FD50: <12 bytes>`,
  manufacturer `0x07D0: <20 bytes>`.
- Only 2 adverts heard in 90 s, at **−96 / −91 dBm** (edge of reception); gap between them 1.5 s.
  → the lock advertises roughly every ~1.5 s; the Mac is simply too far away and misses most.
- Slow discovery and occasional connect timeouts are a **range** problem, not lock behaviour.
- For HA: the receiving adapter (Pi 5 or an ESP32 proxy) should be close enough for ≥ about −80 dBm.

## HA Bluetooth reception (2026-11-19)

- ESP32 proxy (ESPHome bluetooth-proxy 26.10.1, active, flags 255)
  placed near the cabinet: lock heard at **−78 dBm**, and HA now uses the proxy as the best source.
  The proxy also receives the scan response (name `TY`, manufacturer 0x07D0 data).
- Pi built-in adapter: −93 to −97 dBm (unusable for connections).

## Cloud provisioning findings (2026-11-20)

- **Advert → UUID, no keys needed:** `key = MD5(FD50 service data)`, then
  `AES-128-CBC(key, iv=key).decrypt(manufacturer_data[4:20])` = the device's 16-char Tuya `uuid`.
  This identifies which cloud device a discovered lock is.
- **Check code is per lock** (7 locks on one account → 6 distinct codes visible, 1 lock never
  unlocked via BLE). It is in the cloud device status: `remote_pd_setkey_check` (DP 73) bytes
  4..12 for every lock, and the last `ble_unlock_check` (DP 71) report `0001 ffff <code> 01 <ts> 0000`.
- **Smart Life QR login (tuya-device-sharing-sdk, HA's Tuya client id)** returns id, name, uuid,
  product id and status (so the check code) — but its `local_key` is **not** the BLE key:
  login with it fails (no reply to DEVICE_INFO), while the Tuya IoT platform key works.
- Tuya IoT platform (`/v1.0/devices/{id}` local_key) is so far the only working source of the key.
