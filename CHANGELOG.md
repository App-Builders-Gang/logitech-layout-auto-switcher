# Changelog

## 2.0.4 — 2026-07-31

### Added

- **`logiswitch update`** — each installation can now bring itself up to the
  latest release without re-running the installer. `update` fetches the release
  wheel from GitHub over HTTPS (standard library only — no PyPI account or extra
  dependency), stops the running agent, installs the wheel into the same virtualenv
  with pip, and restarts the agent. `update --check` reports availability without
  changing anything; `selfupdate` is an alias. Works on Windows and macOS with no
  administrator rights.
  The stop-before-install ordering matters on Windows: a running process locks the
  files pip must replace, so the agent is stopped first and restarted afterwards —
  even on failure, so a botched update never leaves the machine without an agent.
  The command arrives in this release; to pick it up the first time, re-run the
  install one-liner once.

### Fixed

- A property test for `normalise_os` assumed every canonical OS name (such as
  `tizen`, `webos`, `winemb`) was also a key of the alias map. Hypothesis proved
  otherwise. The test now asserts the real invariant: `normalise_os` either
  rejects the input or returns a name present in the OS-mask table.

## 2.0.3 — 2026-07-31

### Fixed

- **Upgrading on Windows left the old build running.** `schtasks /Create /F`
  replaces a task's registration but does not stop an instance that is already
  running, so re-installing over a live agent kept the previous process resident
  until the next logon — it went on using the settings it started with while the
  new code sat unused on disk. Caught after 2.0.2: the freshly installed agent
  reported `reassert=600s`, the 2.0.1 default, when 2.0.2 had already lowered it
  to 20 s. Registration now ends any running instance before replacing the task.

  This is the Windows counterpart of the `launchctl bootout` race fixed on macOS
  in 2.0.1, and it has the same symptom: the installer says it succeeded while
  the machine keeps running the old build.

### Added

- `tests/test_service_windows.py` — drives registration against a scripted
  `schtasks` and asserts the ordering (`/End` before `/Create`, `/Run` last), so
  the sequence is pinned rather than assumed.

- **Reconfiguring logging leaked a file handle.** `setup_logging` cleared the
  handler list without closing the handlers first, orphaning the rotating file
  handler's open descriptor on every call. Found by turning `ResourceWarning`
  into a test failure.

### Internal

The test suite was hardened rather than merely extended: **165 tests, up from 69**,
and the harness now fails on the things that used to slip through silently.

- **Leak detection is automatic.** An autouse fixture fails any test that leaves a
  reader, worker or watcher thread running, or a HID handle open, so a leak is
  attributed to the test that caused it instead of surfacing later as an
  unrelated flake.
- **Logger state is restored between tests.** Running the CLI sets
  `propagate = False` on the package logger; without isolation any later test
  using `caplog` would have silently recorded nothing.
- **Property-based tests** (Hypothesis) cover the wire format — frame building,
  response matching, error decoding and OS-mask decoding — including the
  guarantee that arbitrary bytes from a shared receiver never crash the matchers.
- **Robustness and concurrency tests**: malformed and truncated frames, a
  notification listener that raises, concurrent requests that must not cross-talk,
  closing a transport mid-request, and repeated concurrent closes.
- **The CLI is tested** — exit codes, output and argument handling — where it
  previously had no coverage at all.
- `pytest` now runs with `--strict-markers`, `--strict-config`, `xfail_strict`,
  warnings as errors, and a per-test timeout so a deadlock fails loudly instead of
  hanging CI.
- CI gained a `ruff format` check, a combined-coverage gate across both operating
  systems, a wider Python matrix (3.9–3.13), least-privilege permissions, job
  timeouts, a clean-environment wheel install check, CodeQL, Dependabot and a
  pre-commit config.

- Four agent tests waited a fixed 400 ms for the agent to establish a session
  before asserting, which is not long enough on a loaded macOS CI runner and made
  two of them fail intermittently. Waiting on the session was not enough either:
  it is established at the end of session building, still a whole device scan
  before any platform is read, so a test could flip the platform while the
  agent's own first pass was in flight and the agent would correctly put it back.
  The agent now counts completed passes, and the tests wait on that — which makes
  them deterministic rather than merely slower. The failure was self-inflicted in
  a second way too: with no session established the agent deliberately treats
  *any* device chatter as "something came back", so asserting too early changed
  the very behaviour under test.

## 2.0.2 — 2026-07-31

Switching a keyboard to another machine and back left the layout wrong for up to
ten minutes. Traced on real hardware (MX Keys S on a Logi Bolt receiver): now
corrected in **1.1 s**.

### Fixed

- **A returning device was never noticed.** An Easy-Switch move leaves the
  receiver plugged in, so macOS reports no device change and the receiver forwards
  no HID++ 1.0 `0x41` connect notification — the two things the agent listened
  for. What the keyboard actually sends on reconnect is an ordinary HID++ 2.0
  event (`11 05 0e 00`: feature `0x4220`, lock-key state), and byte 2 of a 2.0
  frame is a feature index that can never equal `0x41`. The agent now treats any
  unsolicited frame from a device it drives as "I am back", which also covers the
  `0x4531` platform-change event a keyboard emits when something else moves it.
  Chatter from devices it does not drive is ignored, so a mouse cannot trigger it.
- **The retry after a return inherited the wrong backoff.** A device announces
  itself a moment before it will answer requests — a scan one second after the
  announcement still finds nothing — so the check that follows usually fails. That
  failure used to inherit the 30 s ceiling reached while the device was away,
  turning a return into a 32 s wait. A reconnect now restarts the backoff, and the
  ceiling is 10 s rather than 30 s.
- **The safety re-check was the only backstop and ran every 600 s.** It is now
  20 s (`--reassert` tunes it, 0 disables). Idle cost goes from nothing to about
  45 ms of CPU per minute; the alternative is a layout that stays wrong.

### Added

- The log now says how long nothing answered, so a slow round trip is measurable
  without a debug build: `device(s) answering again after 1.1s away`.

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
