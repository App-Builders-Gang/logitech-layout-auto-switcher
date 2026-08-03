# HID++ resources, and what this project took from them

Where the protocol knowledge in this repository comes from, and — more usefully —
the two places where someone else had already hit a wall we were about to walk
into. If you are about to "simplify" something here, read this first.

## Load-bearing decisions taken from other projects

### Do not address the current host as `0xFF`

`HOST_CURRENT = 0xFF` is what the spec says means "whichever host is active", and
on most devices it works. On the MX Keys S it does not. Solaar carries this comment
in `lib/logitech_receiver/settings_templates.py`:

> can't just use the first byte = 0xFF (for current host) because of a bug in the
> firmware of the MX Keys S

and a commit, [`get and use current host in K375sFnSwap to work around bug in MX
Keys S firmware`](https://github.com/pwr-Solaar/Solaar/commit/ce42e27), whose
workaround is to resolve the concrete index first:

```python
def find_current_host(self, device):
    if not self.prefix:
        response = device.feature_request(_F.HOSTS_INFO, 0x00)  # 0x1815
        self.prefix = response[3:4] if response else b"\xff"
```

We do the same in `HidppDevice.current_host()` and address that index in
`set_platform`, `current_platform` and `host_platform_detail`.

**Why it matters here:** a `setHostPlatform` addressed to `0xFF` on affected
firmware is *acknowledged and then discarded*. The agent writes, reads back the old
value, writes again, forever. On the machine this was written against that produced
a correction every twelve seconds for two days straight — 2178 of them — and quitting
Logi Options+ changed nothing, which ruled out the obvious suspect. `0xFF` is now
only the fallback for devices with no 0x1815.

### Rotate the software id, but never use `0x0B`

Solaar pins every request to `SOLAAR_SOFTWARE_ID = 0x0B` deliberately, so that
cooperating userspace HID++ clients sharing a device can each pick a distinct value
and filter their own traffic out of the shared stream.

We rotate instead, because our architecture differs: dedicated reader threads and a
single response sink mean a reply that arrives after its deadline can be handed to
whichever request is waiting next. A fixed id makes that straggler byte-identical to
the awaited reply. Rotating makes it distinguishable; the abandoned-request tracking
in `transport.py` then makes it *rejected*, which is what actually closes the race.

But there is no reason to impersonate another client while doing it, so `0x0B` is
excluded from `SW_IDS`. If you add another well-known id to that exclusion list,
note it here.

## Reference implementations

- **[Solaar](https://github.com/pwr-Solaar/Solaar)** — Python, Linux, GPL-2.0. The
  mature reference for this feature set. Read `lib/logitech_receiver/base.py` for
  the transport and `settings_templates.py` for `Multiplatform` (0x4531) and
  `DualPlatform` (0x4530). The most useful source in this list.
- **[logiops](https://github.com/PixlOne/logiops)** — C++, Linux. Its
  [HID++ 2.0 wiki page](https://github.com/PixlOne/logiops/wiki/HIDPP--2.0) is a
  clear plain-English framing description.
- **[libratbag](https://github.com/libratbag/libratbag)** — C. See the
  [hidpp20 feature list](https://github.com/libratbag/libratbag/wiki/hidpp20-Features).
- **[cvuchener/hidpp](https://github.com/cvuchener/hidpp)** — C++ command-line HID++
  tools, handy for poking at a device by hand.
- **[OpenLogi](https://github.com/AprilNEA/OpenLogi)** — Rust, MIT/Apache-2.0, a
  local-first Logi Options+ replacement (GUI + agent + CLI). It does **not**
  implement 0x4531 — it covers DPI `0x2201`, SmartShift `0x2111`, scroll `0x2121`
  and RGB `0x8070`/`0x8080` — so it is not an alternative to this tool. Worth
  reading for its architecture (the agent owns all device I/O, the GUI is a pure IPC
  client, config is plain TOML) and for its install note, which matches our
  experience: *"Quit Logi Options+ first — the two applications fight over HID++
  access."* Depends on the [`hidpp` crate](https://crates.io/crates/hidpp).

## Protocol specifications

- **[Logitech `cpg-docs`](https://github.com/Logitech/cpg-docs/tree/master/hidpp20)**
  — Logitech's own published HID++ 2.0 feature documentation. Note that `0x4530`
  Dual Platform is documented but **`0x4531` MULTIPLATFORM is not in the public
  index**, which is why Solaar and libratbag are the de-facto reference for it.
- **[HID++ 2.0 draft specification (2012)](https://lekensteyn.nl/files/logitech/logitech_hidpp_2.0_specification_draft_2012-06-04.pdf)**
  and the [HID++ 1.0 excerpt](https://lekensteyn.nl/files/logitech/logitech_hidpp10_specification_for_Unifying_Receivers.pdf)
  — older, but the clearest statement of the framing and error model.

## Is there an open-source driver?

**Linux: yes.**
[`drivers/hid/hid-logitech-hidpp.c`](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-logitech-hidpp.c)
is in the mainline kernel, and a [December 2025
patch](https://lkml.org/lkml/2025/12/15/698) adds feature 0x4531 with a
`hidpp_platform` module parameter and device IDs for the MX Keys S. It is static —
one platform chosen at module load, not per-host and not re-applied when a device
returns — so it solves a different problem from this tool.

**macOS and Windows: no, and none is needed.** HID++ is an application-level
protocol carried over ordinary HID reports. Solaar, OpenLogi, logiops and this
project all speak it from userspace over hidapi. A macOS kext or DriverKit dext
would be a large amount of work for no capability that userspace lacks. The
architecture here is already the right one.

## Platform permissions

Opening the vendor collection needs permission that enumerating it does not:

- **macOS** — Input Monitoring, under System Settings → Privacy & Security. Without
  it `hid_open_path` fails with a bare `open failed` while the device still
  enumerates normally, which reads as a hardware fault and is not one. `logiswitch
  doctor` detects this case and names it. Note this applies to whatever binary is
  running: the installed agent having permission does not give your terminal it.
- **Linux** — a udev rule for the hidraw node.
- **Windows** — no permission, but another process may hold the device exclusively.
