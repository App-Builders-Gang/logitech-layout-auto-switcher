# How Logitech's Fn+O / Fn+P actually works — HID++ feature 0x4531

A reverse-engineering write-up of the Logitech HID++ 2.0 `MULTIPLATFORM` feature,
with the raw frames captured from a Logi Bolt receiver and an MX Keys S.

Everything below was measured on real hardware, not inferred from documentation.

## The short version

Holding **Fn+O** (macOS/iOS) or **Fn+P** (Windows/Android) on an MX Keys does not
just toggle a local key map. It writes a firmware value that is equally reachable
over Logitech's HID++ 2.0 protocol:

| | |
|---|---|
| Feature | `0x4531` `MULTIPLATFORM` |
| Function | `3` — `setHostPlatform(hostIndex, platformIndex)` |
| Transport | plain HID output reports on the receiver's vendor collection |

That means any program that can write a HID report can switch the layout — no
driver, no kernel extension, no administrator rights, and no Linux dependency.
Measured time for the switch to take effect: **326 ms**.

## Finding the endpoint

Logitech receivers expose a vendor-defined HID collection alongside the normal
keyboard and mouse collections:

| Item | Value |
|---|---|
| Vendor id | `0x046D` |
| Usage page | `0xFF00` |
| Usage `0x0001` | short reports, report id `0x10`, 7 bytes |
| Usage `0x0002` | long reports, report id `0x11`, 20 bytes |

On Windows these appear as two separate device paths (`&Col01`, `&Col02`) sharing
an interface number. macOS may expose a single interface backing both report ids.

Product ids seen in the wild: `C548`/`C547` (Logi Bolt), `C52B` (Unifying),
`C52F`/`C534` (Nano), `C539`/`C53A`/`C53D`/`C541` (Lightspeed). None of this
matters for support — the code matches on the usage page, not a product table.

## Frame layout

Both report sizes share one layout:

```
[reportID, deviceIndex, featureIndex, (funcIndex << 4) | swId, p0, p1, p2, ...]
```

* **deviceIndex** — `0x01`–`0x06` for receiver slots; **`0xFF` for a device
  connected directly** over Bluetooth or a USB cable, and for the receiver itself.
* **featureIndex** — *not* the feature id. Each device maps feature ids onto its
  own small index space; you must resolve it at runtime (below).
* **swId** — a non-zero software id nibble echoed back in the reply. It is what
  distinguishes *your* response from an unsolicited event, because HID++ 2.0
  notifications always carry `swId = 0`.

Error replies:

```
HID++ 2.0:  [id, dev, 0xFF, featureIndex, funcByte, errorCode]
HID++ 1.0:  [id, dev, 0x8F, subId,        address,  errorCode]
```

## Step 1 — resolve the feature index

Feature `0x0000` (root) function `0` maps a feature id to this device's index.

```
→  10 05 00 0E 45 31 00        getFeature(0x4531) on device 5
←  11 05 00 0E 10 00 00 ...    feature index = 0x10
```

A returned index of `0` means the device does not implement the feature. On the
test hardware the MX Keys S answered `0x10`; the MX Master 3S answered `0`, which
is how a mouse is ruled out in a single round trip and never asked again.

## Step 2 — read the platform table

Function `0` (`getFeatureInfos`) then function `1` (`getPlatformDescriptor`) per
entry. Captured from the MX Keys S:

```
getFeatureInfos → 03 00 04 04 03 00 01 00 ...
                  capability mask 0x0300, 4 platforms, 4 descriptors
```

| Descriptor | Platform index | OS mask | Meaning |
|---|---|---|---|
| 0 | **0** | `0x1500` | Android + Linux + **Windows** |
| 1 | **1** | `0x2000` | **macOS** |
| 2 | 2 | `0x4000` | iOS |
| 3 | 3 | `0x0800` | ChromeOS |

The 16-bit OS mask bits:

| Bit | OS | Bit | OS |
|---|---|---|---|
| `0x0001` | Tizen | `0x0800` | ChromeOS |
| `0x0100` | Windows | `0x1000` | Android |
| `0x0200` | Windows Embedded | `0x2000` | macOS |
| `0x0400` | Linux | `0x4000` | iOS |
| | | `0x8000` | WebOS |

This table is firmware-constant, so it is read once per session and cached. Not
caching it was the single biggest performance bug in the first implementation:
it turned every "is the layout still right?" check into six round trips instead
of one.

## Step 3 — read and write the live platform

Function `2` (`getHostPlatform`) and function `3` (`setHostPlatform`), both taking
a host index where **`0xFF` means "whichever host is active right now"**.

```
→  11 05 10 2E FF                        getHostPlatform(current)
←  11 05 10 2E 00 01 01 02 00 ...        host 0, status 1, platform 1, source 2

→  11 05 10 3E FF 00                     setHostPlatform(current, 0)
←  11 05 10 3E ...                       ack

→  11 05 10 2E FF                        read back
←  11 05 10 2E 00 01 00 03 00 ...        platform 0, source 3
```

`platform_source` values observed: `1` and `2` before any write, and `3` after a
write from host software — a useful tell for *who* last set the platform.

### The platform is stored per Easy-Switch host

Reading each host index separately shows the state is per-channel, not global:

| Host index | Status | Platform | Source |
|---|---|---|---|
| 0 (Easy-Switch channel 1 — the Bolt dongle) | 1 | 1 → macOS | 2 |
| 1 (Easy-Switch channel 2) | 1 | 1 → macOS | 1 |
| 2 (Easy-Switch channel 3) | 0 | 255 (unpaired) | 0 |

This is exactly why a KVM setup needs help. Two computers sharing one dongle share
**one** Easy-Switch host, so the per-host platform value cannot distinguish them.
Two computers on two *different* channels would each keep their own setting.

### Changing platform re-enumerates the device

Writing a new platform makes the keyboard re-publish its HID report descriptors.
Open handles go stale for roughly a second afterwards, so any client must expect
its next request to fail and reconnect. The agent tears the session down
immediately after a successful change for this reason.

## Older hardware: 0x4530 DUALPLATFORM

Devices predating `0x4531` expose `0x4530` instead, with only two buckets:

| Value | Meaning |
|---|---|
| `0x00` | iOS / macOS |
| `0x01` | Android / Windows |

Read with function `0`, written with function `2`. Falling back to it is what
extends support to older Craft and K-series keyboards.

## Why Logi Options+ does not already solve this

Options+ **does** drive this feature. Measured on Windows with
`logioptionsplus_agent.exe` running: setting the keyboard to macOS mode reverted
to Windows in **under 0.5 s**, every time. Stopping that process made the change
persist indefinitely (checked to 30 s); restarting it snapped Windows mode back.

And yet, with Options+ installed on *both* machines, a KVM switch still left the
keyboard in the wrong layout in both directions.

### What actually triggers it — reverse-engineered, macOS 2.6.941708

An earlier version of this document asserted that *"Options+ reverts a platform
change it observes"*. That is **wrong on macOS**, and the real trigger is narrower
and more interesting. Established by decompiling `logioptionsplus_agent` and by
Frida-instrumenting the running process:

| Experiment | Result |
|---|---|
| Platform forced to Windows under a **running** Options+, 35 s | **No reaction.** Layout stayed wrong. |
| Same, with the Options+ window **open**, 45 s | **No reaction.** |
| Platform forced to Windows, then Options+ agent **restarted** | **Corrected to macOS ~7 s after start**, twice, reproducibly |

The captured write, from a hook on `devio::Feature4531MultiPlatform::setHostPlatform`:

```
t=5.10  GetHostPlatform(host 0), (host 1), (host 2)     read all three Easy-Switch slots
t=5.39  feature_1815_hosts_infos::start()
t=6.73  _update_hosts_info(refresh=1, notify=1)
t=7.06  GetHostPlatform(host 0), (host 1), (host 2)
t=7.11  hosts_info::set_platform(host=0, platform=1, force=0)
t=7.11  ---> setHostPlatform(host=0, platform=1)   rc=1
t=7.34  GetHostPlatform(host 0), (host 1), (host 2)     read-back
```

Note it addresses a **concrete Easy-Switch host index (0), not `0xFF`** — the same
conclusion this project reached the hard way (see `docs/RESOURCES.md`).

So the enforcement is real but **edge-triggered on device registration**, not
level-triggered on the platform value. A KVM switch changes which computer owns the
dongle without restarting the other machine's Options+, so nothing re-asserts, and
the keyboard keeps whatever layout the other computer left it in.

### The setting is not what does it

The UI option *"Always keep the keyboard in Mac layout"* is `keepKeyboardInOsLayout`,
persisted per-profile in `settings.db` under
`card.attribute = "KEYBOARD_SETTINGS"`, and delivered to the agent over the route
`/keyboard/<device>/keyboard_oslayout` as a `logi.protocol.keyboard.KeyboardSettings`
protobuf. Its only agent-side consumer is
`feature_4531_multi_platform::on_set_os_keyboard_layout_handler`, whose logic is:

```cpp
void feature_4531_multi_platform::_update_platform(bool refresh) {
    if (refresh && !platforms_info.update(true))  { log("platforms info update failed"); return; }
    if (!this->keep_in_os_layout /* +0x17c */)    { log("use system layout is not on"); return; }
    if (host->platform == this->desired_platform) { log("platform is same as before"); return; }
    platforms_info.set_platform(this->host_index, this->desired_platform, false);
}
```

**That entire class is dead in this build.** Its only construction site,
`devio_interface::_setup_features`, is gated on a `dynamic_cast` to
`IFeature4531MultiPlatform` that returns null at that point in start-up, so the
object is never created — confirmed dynamically: hooks on its constructor and on
`_update_platform` never fired once across every experiment above, while the write
came from `feature_1815_hosts_infos` instead.

The live writer is `feature_1815_hosts_infos::_update_hosts_info`, which re-asserts
an **OS-derived** platform when the device's reported platform differs from the one
it computed — it never reads `keepKeyboardInOsLayout` at all. Toggling that option
therefore has no effect on platform enforcement in 2.6.941708.

The 0.5 s revert recorded on Windows above was measured on a different build and OS
and has **not** been reproduced on macOS; treat the mechanism as verified only for
macOS 2.6.941708.

That gap is the entire reason this project exists: it fires on device *arrival* —
the event Options+ ignores — and targets the same value Options+ wants, so the
two cooperate rather than fight. Pointed at different platforms on the same host
they do fight, and the agent says so once corrections pass `REVERT_THRESHOLD` in
a `REVERT_WINDOW` — counted over a window rather than consecutively, because each
correction is followed by a check that reads back as correct, so an "in a row"
counter is reset by the agent's own success and never climbs.

### What sharing the receiver actually costs

Measured on macOS, Options+ 2.6.941708 running alongside the agent, ~20 minutes
of trace plus every rotated log on the machine:

| | |
|---|---|
| Frames from Options+ vs from us | 5,952 vs 349 |
| What it sends | `feat0x00 fn1` root pings across dev1–6 and dev255 |
| Cadence | a sweep pair every ~17 s, ~50 frames/min |
| `resource error (0x09)` | 2,381 — the receiver refusing work under its own bursts |
| Software ids it stamps | 0x1–0x9, incrementing |
| `setHostPlatform` writes | **0** while running; exactly **one** per agent start, if the platform is wrong |

Two consequences the code depends on:

**Third-party frames cannot be identified by software id.** Options+ walks the same
1–15 range this project rotates through, so "an id we never issue" only ever
catches Solaar's reserved `0x0B`. A reply is ours if and only if it answers a
request we recorded sending — which is what `Transport._was_ours` checks.

**A platform set by "host software" does not prove another machine.** Source code
`3` says only that software wrote it, and Options+ satisfies that as well as a peer
across a KVM. So the agent refuses to stand down while a local rival is running:
yielding is a bargain between machines, and there is nobody typing on Options+.

## Recorded hardware profile

Windows host, 2026-07-31. Logi Bolt `046D:C548`, HID++ collections on interface 2.

| Index | Device | HID++ | `0x4531` |
|---|---|---|---|
| 1 | MX Master 3S | 4.5 | not supported |
| 5 | **MX Keys S** | 4.5 | feature index `0x10` |

Slots 2–4 answer HID++ 1.0 `resource error`, 6 `connection request failed`,
7 `unknown device`. A sleeping device can take **over 350 ms** to answer a ping,
which is why discovery fans out all seven indices in one window rather than
walking them serially.

This exact byte sequence is replayed by `tests/fakehid.py`, so the protocol layer
is covered by unit tests on machines with no Logitech hardware attached.

## References

* [Solaar](https://github.com/pwr-Solaar/Solaar) — the Linux implementation; its
  `multiplatform` setting is the same feature
* [Logitech `cpg-docs`](https://github.com/Logitech/cpg-docs/tree/master/hidpp20) — official HID++ 2.0 feature list
* [Linux kernel patch adding 0x4531 support](https://lkml.iu.edu/hypermail/linux/kernel/2605.1/02813.html)
* [HID++ 1.0 specification for Unifying receivers](https://lekensteyn.nl/files/logitech/logitech_hidpp10_specification_for_Unifying_Receivers.pdf)
