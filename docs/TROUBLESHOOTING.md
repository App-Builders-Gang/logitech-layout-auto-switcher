# Troubleshooting

Start here:

```bash
logiswitch status     # what is attached and what it is set to
logiswitch probe      # full HID++ dump — attach this to a bug report
logiswitch watch -v   # run in the foreground with debug logging
```

---

## "no Logitech HID++ receiver or device found on this host"

* If you use a **KVM**, switch it to this machine — the dongle is physically on
  the other computer right now.
* Bluetooth keyboards must be on the Easy-Switch channel paired to *this* machine.
* On macOS, check Input Monitoring (see [INSTALL.md](INSTALL.md#if-macos-blocks-hid-access)).

---

## The device is listed but says "cannot switch layout"

The device does not implement `0x4531` or `0x4530`. Mice generally do not, and
neither do keyboards with a single fixed layout. Run `logiswitch probe` — if the
device *should* support it, that output is what to open an issue with.

---

## The layout keeps flipping back

The log says so after several corrections in five minutes:

```
WARNING corrected the platform 5 times in the last 5 minutes ...
```

Work through the causes in this order — the obvious one is usually not it:

1. **Another machine running logiswitch.** By far the most common cause when a
   keyboard is shared. See *The layout flips back and forth between two machines*
   below; the log names the other machine's software id.
2. **Logi Options+.** It enforces the platform it believes the host should have.
   Worth ruling out, but *confirm it before blaming it* — quit it and watch the log
   for five minutes. The warning only names Options+ when it is actually running.
3. **The write not taking.** If the log says `did NOT switch`, the device accepted
   the command and ignored it; that is a firmware issue, not contention.

`logiswitch doctor` reports the counters for all three.

---

## Nothing happens when I switch the KVM

1. Confirm the agent is running: `logiswitch service-status`.
2. Check the log for `watching for device changes via cfgmgr32` (Windows) or
   `via iokit` (macOS). If it says **`via polling`**, native notifications failed
   to register and it fell back — still works, just up to 2 s slower.
3. Some KVMs keep USB permanently attached to both hosts and only switch video,
   and an Easy-Switch move never disconnects the receiver at all. Either way the
   OS reports nothing. The agent then relies on the device speaking up when it
   reconnects, and on the re-check interval (default 20 s) as the backstop; tune
   it with `logiswitch watch --reassert 10`.

   The log states the recovery time directly:

   ```
   12:50:18 INFO nothing is answering; waiting for a device to come back
   12:50:20 INFO switched MX Keys S to macos (platform 1)
   12:50:20 INFO device(s) answering again after 1.1s away
   ```

---

## It works, then stops after the keyboard sleeps

Expected and handled: a sleeping device cannot answer, so the agent retries with
backoff (2 s → 30 s) and also listens for the receiver's device-connection
notification, which fires the moment the keyboard wakes. If you see this *not*
recover, run with `-v` and open an issue with the log.

---

## Windows: the Scheduled Task exists but the agent is not running

```powershell
Get-ScheduledTaskInfo -TaskName LogiSwitch
Get-Content "$env:LOCALAPPDATA\LogiSwitch\logiswitch.log" -Tail 40
```

`LastTaskResult` of `267009` means "currently running" — that is success, not an
error.

---

## macOS: the LaunchAgent will not stay loaded

```bash
launchctl print gui/$(id -u)/com.appbuildersgang.logiswitch
tail -40 ~/Library/Logs/logiswitch.log
tail -40 ~/Library/Logs/logiswitch.launchd.log   # crashes before logging starts
```

A non-zero exit with no log usually means the venv's Python moved. Re-run
`./install.sh`.

---

## macOS: `install failed: Bootstrap failed: 5: Input/output error`

launchd still had the old agent registered when the installer tried to load the new
one. The installer now waits for the teardown and retries, so re-running
`./install.sh` is the fix. If it persists, clear the label by hand and try again:

```bash
launchctl bootout gui/$(id -u)/com.appbuildersgang.logiswitch
launchctl enable gui/$(id -u)/com.appbuildersgang.logiswitch
```

Ignore launchctl's own "Try re-running the command as root" hint — this is a
per-user LaunchAgent and root targets a different domain.

---

## A modifier key is stuck down (⌘, Alt, Shift…)

Switching the platform **remaps the bottom row**: the key left of the space bar is
Command on a macOS platform and Alt on a Windows one. If the platform changes
between a key's press and its release, the host never sees a release for the
modifier it registered — so it stays down, and everything you type afterwards is a
shortcut.

logiswitch guards against causing this: before writing a new platform it asks the OS
which modifiers are held, and waits for the chord to finish. Reading that state
needs no permission on either platform (`CGEventSourceFlagsState` on macOS,
`GetAsyncKeyState` on Windows) — it is a snapshot, not an event tap.

**To clear a stuck modifier now:** tap the key once. Nothing needs restarting.

What the log will tell you:

```
holding off: command held                      (DEBUG — the guard working)
command has been held for 30s -- that is a stuck modifier, not typing
the layout changed while command was held down -- that can strand the modifier
```

The last line should never appear. If it does, the guard was bypassed and it is the
evidence for how the modifier got stuck. `logiswitch doctor` reports the
`stuck_modifiers` and `switched_while_held` counters.

If a modifier is *genuinely* held for more than 30 seconds the correction goes ahead
anyway — at that point the key is jammed rather than in use, and refusing to fix the
layout on its account helps nobody.

---

## No notifications appear

Send one on demand:

```bash
logiswitch notify-test
```

If that prints `sent` and you still saw nothing, the notification is being
*blocked*, not failing:

* **macOS** — an `osascript` notification is attributed to **Script Editor**, so
  that is what has to be allowed: System Settings → Notifications → Script Editor.
  Focus and Do Not Disturb also hide them.
* **Windows** — Settings → System → Notifications, and check Focus Assist.

If it appears when you run it by hand but never from the background agent, confirm
the agent was not installed with notifications off:

```bash
# macOS: look for --no-notify in the arguments
plutil -p ~/Library/LaunchAgents/com.appbuildersgang.logiswitch.plist | grep -A6 ProgramArguments
```

Re-run `logiswitch install` to turn them back on.

**Seeing too few is usually correct.** Notifications are throttled per kind. If the
layout is being corrected repeatedly you get one "switched" message and then one
"keeps reverting" message, not one per correction — the number that were hidden is
appended to the next message of that kind.

---

## The layout flips back and forth between two machines

Two computers sharing one keyboard, each running logiswitch, each wanting its own OS.
On a KVM they share a single receiver, so the keyboard has **one** platform slot and
they overwrite each other.

Current versions take turns: whichever machine you are typing on keeps the keyboard,
and the others stand down after `--active-window` seconds (20 by default). Old builds
do not, and the log says so explicitly:

```
another machine is running an OLD logiswitch (software id 0x0E) and setting this
keyboard's platform. Old builds do not take turns -- update logiswitch on that machine
```

`0x0E` was this project's fixed software id before the versions that arbitrate, so
that line means exactly what it says: upgrade the other machine.

To confirm from the log which machine you are reading — they otherwise look identical:

```
logiswitch agent starting on <hostname>: target=macos reassert=20s
steady on <hostname>: ... | peer sw0x0E (standing down)
```

If one machine should never touch the layout, install it in observe-only mode:

```bash
logiswitch install --observe
```

---

## Reporting a bug

Include:

* `logiswitch probe` output (it contains no secrets — device names and HID++
  feature tables only)
* the last ~50 log lines with `-v` enabled
* OS version, keyboard model, and how it is connected (receiver / Bluetooth / cable)
* whether Logi Options+ is installed and running

---

## Sending a diagnosis (especially with two machines)

One command packs everything:

```bash
logiswitch bundle
```

It writes `logiswitch-diagnostics-<hostname>-<timestamp>.zip` to your home directory
containing the agent log and its rotations, the frame trace, the device dump, the
installed service definition, and the environment. Run it on **both** machines when
they are sharing a keyboard — the hostname is in the filename precisely so the two
can be told apart.

It contains no keystrokes (this project never sees any) and no credentials. It does
contain the machine's hostname and its Logitech device names.

For a full device dump, stop the agent first — only one process can hold the
receiver, and normally that is the agent:

```bash
launchctl bootout gui/$(id -u)/com.appbuildersgang.logiswitch   # macOS
schtasks /End /TN LogiSwitch                                    # Windows
logiswitch bundle
logiswitch install    # start it again
```

`bundle` says so itself when the agent has the device, rather than blaming the
macOS Input Monitoring permission for it.
