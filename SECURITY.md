# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/App-Builders-Gang/logitech-layout-auto-switcher/security/advisories/new)
rather than opening a public issue. Expect an initial response within a week.

## What this software actually does

Useful context for assessing risk:

- It sends HID++ output reports to a Logitech vendor-defined HID collection
  (usage page `0xFF00`) — the same interface Logi Options+ uses. The only write
  it performs is `setHostPlatform`, the value the Fn+O / Fn+P chord already sets.
- It runs entirely as the logged-in user. **No administrator or root rights are
  required or requested.**
- It makes **no network connections** at runtime. The installer downloads the
  project from GitHub over HTTPS; the agent itself never talks to the network.
- It reads no keystrokes. The HID++ collection carries device control messages,
  not typed input, and no keyboard usage page is ever opened.
- It writes two files: a rotating log and a small JSON state file holding
  device-index hints. Neither contains credentials.

## Installer scripts

The one-line installers download and execute code from this repository. If that
is not acceptable in your environment, clone the repo, read `install.ps1` /
`install.sh`, and run them locally — both work identically either way.

## Supported versions

Fixes land on `main` and in the next tagged release. Only the latest release is
supported.
