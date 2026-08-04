# Changelog

## 2.3.0 — 2026-08-04

Running alongside Logi Options+, correctly. Measured on macOS with Options+
2.6.941708 and the agent side by side: **5,952 frames from Options+ against 349
from us**, every one of them a root ping across the receiver's device slots, and
**not a single host-platform write** in any log on the machine. It polls; it does
not compete for the setting. Three things were wrong on our side.

### Fixed

- **Another program's traffic was still blamed on the receiver.** 2.2.1 tried to fix
  this by asking whether a frame carried a software id we never issue -- but Options+
  walks the same 1–15 range this project rotates through, so that test could only
  ever catch Solaar's reserved `0x0B`. Against Options+ it was unreachable code, and
  844 warnings in seventeen minutes announced that the receiver was missing its 1.2 s
  deadline on a completely healthy machine.

  A reply is now ours if and only if it answers a request we recorded sending. That
  is exact, needs no guessing about anyone's software id, and keeps a genuine
  straggler reported as one. The tests replay the real captured traffic.

- **The agent would have handed the keyboard to a program on its own computer.** It
  stops writing when a peer is present and this machine has been idle -- the right
  bargain between two machines sharing a keyboard over a KVM. But both peer detectors
  conclude "another machine" from evidence a local rival satisfies equally well: a
  platform set by "host software" says only that. An idle Mac running Options+ could
  therefore stand down and stay down until somebody typed, which is exactly when the
  layout needs to already be right. Nobody is typing on Options+.

  Turn-taking is now suspended while Logitech software is running locally, and
  `doctor` says so under `sharing`. Behaviour with none running is unchanged.

- **Close re-checking eased off on the same bad evidence**, which is backwards: it
  exists for firmware that drops the setting every few seconds.

### Added

- Software ids are now chosen away from those another program is currently using.
  Nothing reserves them, and today only the *feature* differs between our traffic and
  Options+'s -- luck, not design.
- A short wait for quiet before transmitting into another program's burst, and a
  `receiver_busy` counter for the receiver's own "resource error" replies (2,381 of
  them in the measured window).

### Changed

- The claim that Options+ "wins" a conflict is gone from the README and
  `docs/PROTOCOL.md`. It was never measured. What is documented instead is what the
  traces show, and how to avoid the conflict: point both at the same OS.
- `doctor` no longer prints a bare "open failed" above the report that explains it.

## 2.2.2 — 2026-08-04

### Fixed

- **Desktop notifications never worked on Windows.** The PowerShell script wrapped
  `[Windows.UI.Notifications.ToastNotificationManager, ...]` across two lines with a
  backtick, for no reason beyond keeping the Python source narrow. PowerShell will
  not continue a type literal: it answered "Missing ] at end of attribute or type
  literal" and every toast failed, on every Windows machine, since notifications
  shipped. `logiswitch notify-test` reported the failure correctly, which is how it
  was found.

  The script is now assembled so every emitted statement is on one physical line,
  and the tests check that rather than merely checking the text is present -- on
  Windows they hand the script to PowerShell's own parser instead of guessing.

## 2.2.1 — 2026-08-04

### Fixed

- **Another program's traffic was reported as the device answering late.** On a
  machine running Logi Options+ the agent logged 215 orphan replies against 212
  requests, warning that the device was missing the 1.2 s deadline. It was not: the
  frames carried software id `0x0B`, which this project never issues, and 151 of
  them were `HOSTS_INFO.getHostInfo`, a function it does not call. A shared HID
  collection delivers another program's replies to us as well. They are now counted
  and reported at INFO as what they are.

## 2.2.0 — 2026-08-04

Taking turns between machines did not actually engage. Everything needed to
notice a competing machine was being computed and then not used.

### Fixed

- **A second machine was never detected, so nobody ever stood down.** Peer
  detection waited to catch another host's `setHostPlatform` *reply* on the wire.
  On a shared receiver that is unreliable: two machines running 2.1.0 saw each
  other's `getHostPlatform` reads constantly and each other's writes almost never,
  so both kept correcting the layout and the log recorded no peer at all.

  The conclusive evidence was already there and unused — the platform now reads as
  a value we did not write, set by "host software", which can only mean software on
  another machine. That is what peer detection uses. It also survives session
  rebuilds now: the agent remembers what it last wrote per device, because a fresh
  device object after a reconnect cannot otherwise tell its own past writes from
  somebody else's, and reconnects are exactly what a KVM hop produces.

- **A stuck `fn` blocked real corrections.** Fn was treated as a modifier that a
  platform switch could strand, so the agent waited for it to be released. macOS
  reports the flag for function-row and media keys, not just for a held key, so it
  looked held for thirty seconds at a time on a keyboard nobody was touching. Fn is
  not on the row a platform switch remaps and cannot be stranded by one, so it is
  no longer considered.

- **`doctor` blamed the wrong thing for a device it could not open.** Running it
  while the agent is up cannot open the receiver — only one process can hold it —
  and the report said this was the macOS Input Monitoring permission, sending
  people to change a setting that was never the problem. It now recognises its own
  agent and says how to get the full dump instead.

- **A trace test assumed a finer clock than Windows has.** A zero-length window
  cannot distinguish "just now" from "already past" when the monotonic clock
  advances every 15 ms; four of five Windows jobs failed on it.

### Added

- **`logiswitch bundle`** — one archive with the agent log and its rotations, the
  frame trace, the device dump, the installed service definition and the
  environment, named after the machine that produced it. Two machines sharing a
  keyboard produce two half-stories and the missing half is always the interesting
  one; asking someone to find four rotated logs and a service definition in
  different places on each OS is how a bug report arrives incomplete. Nothing in it
  can fail the bundle: a probe that cannot reach the hardware becomes a note rather
  than losing the logs.

- **A separate notification for a competing machine**, so "another computer is also
  setting this layout" and "the layout keeps reverting" no longer share a cooldown
  and silence each other.

### Internal

**421 tests.** The suite no longer depends on how long ago somebody touched the
computer running it — an unattended run is idle by definition, and now that peer
detection works, agents were correctly standing down in the middle of tests that
expected them to keep writing.

## 2.1.0 — 2026-08-04

Chasing a keyboard that typed `Î` instead of `⌘⇧D`. The cause turned out to be a
second machine running an older build of this tool, and almost none of the work
below is the fix — it is everything that had to exist before the cause could be
seen at all.

### Fixed

- **A reply that missed its deadline could be handed to the next request.** Every
  request was stamped with the same software id (`SW_ID = 0x0E`), and the response
  sink matched on device, feature and function — all of which a late reply shares
  with the request that follows it. The agent then acted on a stale platform,
  concluded nothing needed changing, and logged success while the layout was wrong.
  Reproduced directly: the old code returns a stale `platform 1` as a fresh answer.
  Software ids now rotate, and a request that gives up is remembered so its answer
  is rejected when it finally arrives rather than given to whoever is waiting.

- **A failed read became a blind write.** A timed-out `getHostPlatform` set the
  current platform to "unknown", which never matches the target, so every timeout
  produced a write and reported `changed=True`. That manufactured corrections that
  never happened and drove the contention warning purely from a sleeping keyboard.

- **The contention warning could never fire.** It required *consecutive* changes,
  but every correction is followed three seconds later by a check that succeeds —
  the agent's own success reset the counter. A keyboard reverted every twelve
  seconds for two days produced 2178 "switched" lines and not one warning. Counted
  over a window now.

- **A platform write was never verified.** The reply says the command was accepted,
  not that the mode took. `ensure_os` now reads back, and a write the device
  contradicts is logged as a failure instead of announced as a switch.

- **Reconnecting dropped every device but one.** The device-index fast path returned
  as soon as a single hinted index answered, so a receiver with two keyboards had
  one of them quietly unmanaged after every reconnect. Invisible with one device.

- **Global flags before the subcommand were discarded.** `logiswitch -v status` ran
  without debug logging, because argparse copies a subparser's defaults over what
  the top level already parsed.

- **`0x41` was misread as a connect notification** whenever a HID++ 2.0 reply
  happened to carry feature index 0x41.

### Added

- **Desktop notifications** on macOS and Windows when the layout changes, when a
  switch will not stick, when it keeps reverting, when the wireless link is
  unstable, and when the host input source is not Latin. Throttled per kind: a
  keyboard reverting every twelve seconds produces one message and then one
  standing-condition message, not three hundred an hour. `logiswitch notify-test`
  checks they are permitted, because on macOS the failure is silent.

- **`logiswitch doctor`** — one report naming which of the three causes of wrong
  characters you have: firmware platform, host input source, or an unstable link.
  It also detects the macOS Input Monitoring failure, which otherwise looks like
  missing hardware.

- **Taking turns between machines.** Several computers sharing one keyboard through
  a KVM each want their own layout, and the keyboard has one platform slot. The
  machine being typed on now keeps the keyboard and the rest stand down — no
  configuration and no negotiation, because only one machine can be receiving
  keystrokes at a time. `--observe`, `--claim-host N` and `--active-window` cover
  the cases where the automatic behaviour is not wanted. An old build competing for
  the keyboard is detected by its fixed software id and named in the log.

- **Modifiers are no longer remapped mid-chord.** Changing the platform swaps the
  bottom row, so doing it between a key's press and its release strands the
  modifier. The agent now waits for the chord to finish, reading modifier state
  through APIs that need no permission on either platform.

- **A frame trace** kept in a bounded ring and flushed to disk when something
  anomalous happens, so an intermittent fault leaves evidence instead of nothing.

- **`docs/RESOURCES.md`** — the protocol references, and the two workarounds this
  project owes to Solaar, recorded with citations so they are not "simplified" away.

### Internal

**405 tests, up from 209.** The largest addition is a competing test environment:
ten machines sharing one receiver filled to its six-device limit, with the shared
receiver modelled faithfully so the agents discover each other the way they do
through a KVM. It covers fast user switching, thundering herds, mixed OS targets,
an unyielding old peer, and recovery from sleep, transport loss and a machine
disappearing. It found two bugs unit tests could not: arbitration was enforced only
in the worker loop, so `--once` bypassed it, and the reconnect fast path above.

## 2.0.5 — 2026-07-31

### Fixed

- **The `logiswitch` command was not on PATH after install.** The installer built
  the venv and registered the service but never exposed the entry point, so
  `logiswitch update`, `logiswitch status` and friends failed with "not recognized"
  unless invoked as `python -m logiswitch`. `logiswitch install` now adds the
  venv's Scripts directory to the persistent user PATH on Windows (broadcasting
  `WM_SETTINGCHANGE` so new terminals pick it up without a logoff) and symlinks
  the entry point into `~/.local/bin` on macOS. Existing installs pick this up by
  re-running the install one-liner once; the change is idempotent.

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
