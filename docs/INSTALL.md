# Installation

Install on **every computer that shares the keyboard**. Each machine asserts only
its own OS, so the two never disagree.

Requirements: Python 3.9 or newer, and a Logitech keyboard that supports HID++
feature `0x4531` or `0x4530` (run `logiswitch status` to check — no admin rights
needed anywhere).

---

## Windows

```powershell
irm https://raw.githubusercontent.com/App-Builders-Gang/logitech-layout-auto-switcher/main/install.ps1 | iex
```

The installer detects your Python (skipping the Microsoft Store stub), downloads
the project to `%LOCALAPPDATA%\LogiSwitch\app`, creates a virtualenv, prints what
it found, and registers a Scheduled Task named `LogiSwitch` that starts the agent
at logon as your user. It also removes the legacy `MXSwitch` task if you ran an
earlier version.

From a clone instead:

```powershell
git clone https://github.com/App-Builders-Gang/logitech-layout-auto-switcher.git
cd logitech-layout-auto-switcher
.\install.ps1
```

If PowerShell refuses to run the local script:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Verify:

```powershell
.\.venv\Scripts\python -m logiswitch service-status
Get-Content "$env:LOCALAPPDATA\LogiSwitch\logiswitch.log" -Tail 20
```

Uninstall: `.\install.ps1 -Uninstall`, or without a clone:

```powershell
$env:LOGISWITCH_UNINSTALL='1'; irm https://raw.githubusercontent.com/App-Builders-Gang/logitech-layout-auto-switcher/main/install.ps1 | iex
```

---

## macOS

```bash
curl -fsSL https://raw.githubusercontent.com/App-Builders-Gang/logitech-layout-auto-switcher/main/install.sh | bash
```

Pipe to `bash`, not `sh` — the script uses bash features and a shebang is ignored
when a script is piped.

From a clone instead:

```bash
git clone https://github.com/App-Builders-Gang/logitech-layout-auto-switcher.git
cd logitech-layout-auto-switcher
chmod +x install.sh && ./install.sh
```

Either way it registers a launchd LaunchAgent (`com.appbuildersgang.logiswitch`) in your own
`~/Library/LaunchAgents`, with `RunAtLoad` and `KeepAlive`.

Verify:

```bash
./.venv/bin/python -m logiswitch service-status
tail -20 ~/Library/Logs/logiswitch.log
```

Uninstall: `./install.sh --uninstall`, or without a clone:

```bash
curl -fsSL https://raw.githubusercontent.com/App-Builders-Gang/logitech-layout-auto-switcher/main/install.sh | bash -s -- --uninstall
```

### If macOS blocks HID access

The agent talks to a vendor-defined HID collection, not to a keyboard usage page,
so it normally needs no privacy permission. If `logiswitch status` fails with a
permission error anyway, add the interpreter to **System Settings → Privacy &
Security → Input Monitoring**. The installer prints the exact path:

```bash
./.venv/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.executable))'
```

Device *discovery* uses IOKit service matching, which never opens a device and so
never triggers a permission prompt on its own.

---

## Using a KVM

Switch the KVM to the machine you are installing on first, so the receiver is
actually enumerated there and the installer can confirm it found the keyboard.
Then repeat on the other machine.

Nothing needs configuring for the KVM itself. Moving the dongle re-enumerates it
on the newly active host, which is the event the agent subscribes to.

---

## Pinning the target OS

By default each machine targets its own operating system. Override it when the
host OS is not what the keyboard should be set to:

```bash
logiswitch install --os macos
```

Valid values: `windows`, `macos`, `linux`, `android`, `ios`, `chrome`
(plus aliases `win`, `mac`, `pc`, `osx`, `chromeos`).

---

## Running without installing a service

```bash
logiswitch watch            # foreground, Ctrl-C to stop
logiswitch watch --once     # apply once and exit — good for your own scheduler
logiswitch watch -v         # debug logging
```

---

## Verifying it works end to end

1. `logiswitch status` on both machines — each should list the keyboard, its
   platform table, and the platform it is currently on.
2. Switch the KVM (or Easy-Switch channel) and watch the log on the machine you
   moved to. Expect one `device arrived` followed by one `switched … to …`.
3. Check the keyboard physically: on macOS the key left of the spacebar should be
   **Command** (⌘C copies); on Windows it should be **Alt**.

## Where things live

| | Windows | macOS |
|---|---|---|
| Log | `%LOCALAPPDATA%\LogiSwitch\logiswitch.log` | `~/Library/Logs/logiswitch.log` |
| State | `%LOCALAPPDATA%\LogiSwitch\state.json` | `~/Library/Application Support/logiswitch/state.json` |
| Service | Scheduled Task `LogiSwitch` | `~/Library/LaunchAgents/com.appbuildersgang.logiswitch.plist` |
