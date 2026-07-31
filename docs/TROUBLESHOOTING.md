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

The log says it plainly after three reverts:

```
WARNING the platform keeps reverting -- another process is fighting us.
        Logi Options+ enforces its own host OS on this collection; quit or
        uninstall it if the layout will not stay on macos.
```

Logi Options+ enforces the platform it believes the host should have, and it wins
because it reacts to the change. This only happens when Options+ and logiswitch
disagree — normally both want the host's own OS and they cooperate fine.

Fix by making them agree (`logiswitch install --os <what Options+ wants>`), or by
quitting Options+ on that machine.

---

## Nothing happens when I switch the KVM

1. Confirm the agent is running: `logiswitch service-status`.
2. Check the log for `watching for device changes via cfgmgr32` (Windows) or
   `via iokit` (macOS). If it says **`via polling`**, native notifications failed
   to register and it fell back — still works, just up to 2 s slower.
3. Some KVMs keep USB permanently attached to both hosts and only switch video.
   If your dongle never actually disconnects there is no arrival event to react
   to. The safety heartbeat (default 600 s) will still correct it eventually;
   lower it with `logiswitch watch --reassert 30`.

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
```

A non-zero exit with no log usually means the venv's Python moved. Re-run
`./install.sh`.

---

## Reporting a bug

Include:

* `logiswitch probe` output (it contains no secrets — device names and HID++
  feature tables only)
* the last ~50 log lines with `-v` enabled
* OS version, keyboard model, and how it is connected (receiver / Bluetooth / cable)
* whether Logi Options+ is installed and running
