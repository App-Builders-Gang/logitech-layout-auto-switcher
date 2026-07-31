# Recorded hardware profiles

Raw `logiswitch probe` results from real devices. These are what
[`tests/fakehid.py`](../tests/fakehid.py) replays, so adding a profile here and a
matching `FakeDevice` extends test coverage to that hardware.

To contribute one, run `logiswitch probe` and open a
[device report](https://github.com/App-Builders-Gang/logitech-layout-auto-switcher/issues/new?template=device-report.yml).

---

## MX Keys S + MX Master 3S via Logi Bolt — Windows 11

Windows 11 Pro 26200, 2026-07-31. Receiver `046D:C548` behind a TESmart DKS202-P24 KVM.

HID++ vendor collections, both on interface 2:

| Collection | Usage page | Usage | Report |
|---|---|---|---|
| `MI_02&Col01` | `0xFF00` | `0x0001` | short, id `0x10`, 7 bytes |
| `MI_02&Col02` | `0xFF00` | `0x0002` | long, id `0x11`, 20 bytes |

Devices found by the fan-out scan of indices 1–6 + `0xFF`:

| Index | Device | HID++ | Platform feature |
|---|---|---|---|
| 1 | MX Master 3S | 4.5 | none — correctly skipped |
| 5 | **MX Keys S** | 4.5 | `0x4531` at feature index `0x10` |

Slots 2–4 answer HID++ 1.0 `resource error`, 6 `connection request failed`,
7 `unknown device`.

### MX Keys S platform table

`getFeatureInfos` → `03 00 04 04 03 00 01 00 …`
(capability mask `0x0300`, 4 platforms, 4 descriptors)

| Descriptor | Platform index | OS mask | OS |
|---|---|---|---|
| 0 | 0 | `0x1500` | android / linux / windows |
| 1 | 1 | `0x2000` | macos |
| 2 | 2 | `0x4000` | ios |
| 3 | 3 | `0x0800` | chrome |

`getHostPlatform` per host:

| Host | Status | Platform | Source |
|---|---|---|---|
| 0 (Easy-Switch channel 1, the dongle) | 1 | varies | 2, then 3 after a write |
| 1 (Easy-Switch channel 2) | 1 | 1 → macos | 1 |
| 2 (Easy-Switch channel 3) | 0 | 255 (unpaired) | 0 |

### Measurements on this hardware

| | |
|---|---|
| Platform switch (`setHostPlatform` → verified read-back) | 326 ms |
| Steady-state check with warm caches | 15 ms |
| Fan-out scan of all 7 indices | 836 ms |
| Endpoint enumeration | 96 ms |
| Transport open | 25 ms |
| Transport close (bounded by the reader-thread join) | ~500 ms |
| Agent CPU over 60 s idle | 0 ms |
| 300 fake + 20 real open/close cycles | no thread growth, RSS flat |

A sleeping device can take over 350 ms to answer a ping, which is why discovery
fans out rather than walking slots serially.

---

## macOS — not yet recorded

Pending. Run `logiswitch probe` on a Mac with a Logitech receiver attached and
open a device report; the macOS device-notification watcher in particular has not
been exercised on real Apple hardware yet.
