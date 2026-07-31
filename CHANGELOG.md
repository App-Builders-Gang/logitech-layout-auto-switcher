# Changelog

## 2.0.1 — 2026-07-31

Three macOS defects, all found in one reinstall on real Apple hardware.

### Fixed

- **Reinstalling over a running agent failed** with
  `install failed: launchctl failed (5): Bootstrap failed: 5: Input/output error`,
  leaving a plist on disk and nothing running. `launchctl bootout` returns as soon
  as SIGTERM is delivered, but the label stays registered in the domain until the
  process is really gone, and bootstrapping a still-registered label returns EIO.
  Registration now enables the label, boots it out, waits for it to leave the
  domain, and retries the bootstrap with backoff. Failures report launchctl's error
  plus a usable hint — launchctl's own "try re-running as root" advice is wrong for
  a per-user LaunchAgent.
- **The IOKit watcher never started on any Mac.** The terminate notification was
  registered as `IOServiceTerminated`; `IOKitKeys.h` spells it `IOServiceTerminate`,
  so `IOServiceAddMatchingNotification` returned `kIOReturnUnsupported` (0xE00002C7)
  and every macOS install silently fell back to the 2-second polling watcher. This
  is the "known limitation" from 2.0.0 — the watcher is now exercised on real
  hardware and switches on the device event.
- **Every log line was written twice.** The LaunchAgent redirected the process's
  stdout and stderr into the same file the agent's own rotating handler writes.
  launchd's capture now goes to `~/Library/Logs/logiswitch.launchd.log`, which holds
  only what escapes the logger, and the agent drops its console handler when it
  detects it is the managed service.

## 2.0.0 — 2026-07-31

Full rewrite around OS device notifications.

### Added

- **Event-driven core.** Device changes arrive from `CM_Register_Notification`
  (Windows, no window handle needed) and IOKit service matching (macOS). Device
  wake arrives from the receiver's own HID++ `0x41` notification. A polling
  watcher remains only as a fallback when native registration fails.
- **Dynamic device support.** Every Logitech interface exposing the HID++ vendor
  collection is enumerated and every device behind it is asked what it can do —
  no model allow-list. Adds directly-connected devices (Bluetooth / USB cable,
  HID++ index `0xFF`), the older `0x4530 DUALPLATFORM` feature, and driving
  *every* supported device rather than only the first.
- **`logiswitch` CLI** — `status`, `set`, `watch`, `probe`, `install`,
  `uninstall`, `service-status`.
- **One-line installers** for Windows and macOS/Linux that auto-detect the
  system, download, build a virtualenv and register a logon service.
- **59 tests** against a HID++ receiver simulator replaying bytes captured from
  real hardware, plus a live `CM_Register_Notification` round-trip on Windows.
- CI on Windows and macOS runners; tagged releases build sdist + wheel.
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the reverse-engineering write-up.

### Fixed

Defects found auditing v1:

- Wake notifications were lost: a pre-request drain and a notification poll
  fought over the same queue. Reads are now owned solely by reader threads.
- A partial transport open leaked the handle it had already acquired.
- Discovery walked device indices serially at 1200 ms each — up to 7.2 s per
  reconnect. It now fans out all seven indices in one window (836 ms measured).
- Every "is the layout still correct?" check re-read the firmware-constant
  platform table, costing ~6 HID++ round trips. Cached: **326 ms → 15 ms**.
- No signal handling, so `SIGTERM` from launchd leaked handles and threads.
- `time.sleep` inside the arrival path blocked the event loop.
- No top-level exception guard around the worker loop.

### Changed

- Package renamed `mxswitch` → `logiswitch`; the Windows installer removes the
  legacy `MXSwitch` scheduled task and the macOS installer removes the legacy
  `com.abd3lraouf.mxswitch` LaunchAgent.
- Idle behaviour: **0 ms CPU measured over 60 s**, no enumeration syscalls. The
  safety re-check is now 600 s (was 60 s) and can be disabled with `--reassert 0`.

### Known limitations

- The macOS device-notification watcher has not been exercised on real Apple
  hardware. If it fails to register, the agent falls back to polling.
- Linux has no native watcher; [Solaar](https://github.com/pwr-Solaar/Solaar)
  covers that platform well.

## 1.0.0

Initial working version: polled `hid.enumerate()` once a second, drove
`0x4531 MULTIPLATFORM` on the first supported device found.
