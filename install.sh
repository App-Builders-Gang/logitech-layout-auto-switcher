#!/usr/bin/env bash
# Logitech Layout Auto Switcher installer for macOS and Linux.
#
# One-liner (nothing cloned):
#   curl -fsSL https://raw.githubusercontent.com/App-Builders-Gang/logitech-layout-auto-switcher/main/install.sh | bash
#
# From a checkout:
#   ./install.sh
#
# Options (also work after `| bash -s --`):
#   --os <windows|macos|linux|android|ios|chrome>   pin the target OS
#   --uninstall                                     remove the service
#   --dir <path>                                    install location
#
# Environment: LOGISWITCH_OS, LOGISWITCH_HOME, LOGISWITCH_REF
#
# No sudo required.

set -euo pipefail

REPO="App-Builders-Gang/logitech-layout-auto-switcher"
REF="${LOGISWITCH_REF:-main}"
TARGET_OS="${LOGISWITCH_OS:-}"
INSTALL_DIR="${LOGISWITCH_HOME:-$HOME/.local/share/logiswitch}"
UNINSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --uninstall) UNINSTALL=1; shift ;;
        --os) TARGET_OS="${2:-}"; shift 2 ;;
        --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
        --ref) REF="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0" 2>/dev/null || echo "see $REPO"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m    %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$1" >&2; exit 1; }

# --- where are we running from? ----------------------------------------------
# Piped from curl, "$0" is bash/stdin and there is no checkout around us.
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    ROOT="$SCRIPT_DIR"
    FROM_CHECKOUT=1
else
    ROOT="$INSTALL_DIR/app"
    FROM_CHECKOUT=0
fi

VENV="$INSTALL_DIR/venv"
[[ $FROM_CHECKOUT -eq 1 ]] && VENV="$ROOT/.venv"
PY="$VENV/bin/python3"

# --- system detection ---------------------------------------------------------
detect_os() {
    case "$(uname -s)" in
        Darwin) echo macos ;;
        Linux)  echo linux ;;
        *)      die "unsupported system: $(uname -s). Windows users: see install.ps1" ;;
    esac
}

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# --- uninstall ----------------------------------------------------------------
if [[ $UNINSTALL -eq 1 ]]; then
    step "Removing the launch agent"
    if [[ -x "$PY" ]]; then
        "$PY" -m logiswitch uninstall || true
    else
        for label in com.appbuildersgang.logiswitch com.abd3lraouf.mxswitch; do
            plist="$HOME/Library/LaunchAgents/$label.plist"
            if [[ -f "$plist" ]]; then
                launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
                rm -f "$plist"
                ok "removed $label"
            fi
        done
    fi
    if [[ $FROM_CHECKOUT -eq 0 && -d "$INSTALL_DIR" ]]; then
        rm -rf "$INSTALL_DIR"
        ok "removed $INSTALL_DIR"
    fi
    ok "Done."
    exit 0
fi

SYSTEM="$(detect_os)"
step "Detected $SYSTEM ($(uname -m))"

step "Locating Python"
BASE_PYTHON="$(find_python)" || die "Python 3.9+ not found.
    macOS: xcode-select --install   (or install from https://python.org)
    Linux: install python3 and python3-venv from your package manager"
ok "$BASE_PYTHON ($("$BASE_PYTHON" -c 'import platform; print(platform.python_version())'))"

# --- fetch, if we were piped in ----------------------------------------------
if [[ $FROM_CHECKOUT -eq 0 ]]; then
    step "Downloading $REPO@$REF"
    mkdir -p "$INSTALL_DIR"
    rm -rf "$ROOT"
    mkdir -p "$ROOT"
    url="https://codeload.github.com/$REPO/tar.gz/$REF"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" | tar -xz -C "$ROOT" --strip-components=1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$url" | tar -xz -C "$ROOT" --strip-components=1
    else
        die "neither curl nor wget is available"
    fi
    [[ -f "$ROOT/pyproject.toml" ]] || die "download did not contain the project"
    ok "$ROOT"
fi

# --- install ------------------------------------------------------------------
if [[ ! -x "$PY" ]]; then
    step "Creating the virtualenv"
    "$BASE_PYTHON" -m venv "$VENV" || die "could not create a virtualenv (is python3-venv installed?)"
fi

step "Installing logiswitch"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet "$ROOT"
ok "$("$PY" -m logiswitch --version)"

step "Checking the keyboard is reachable"
if ! "$PY" -m logiswitch status; then
    warn "No supported device answered."
    warn "If you use a KVM, switch it to this machine and re-run."
    if [[ "$SYSTEM" == "macos" ]]; then
        warn "If that looked like a permissions error, add this binary to"
        warn "System Settings > Privacy & Security > Input Monitoring:"
        warn "  $("$PY" -c 'import os,sys; print(os.path.realpath(sys.executable))')"
    fi
    warn "Installing anyway; the agent retries on every device event."
fi

step "Registering the background agent"
if [[ -n "$TARGET_OS" ]]; then
    "$PY" -m logiswitch install --os "$TARGET_OS"
else
    "$PY" -m logiswitch install
fi

echo
printf '\033[32mInstalled.\033[0m\n'
echo "  status:    $PY -m logiswitch status"
echo "  service:   $PY -m logiswitch service-status"
if [[ "$SYSTEM" == "macos" ]]; then
    echo "  logs:      ~/Library/Logs/logiswitch.log"
else
    echo "  logs:      \${XDG_STATE_HOME:-~/.local/state}/logiswitch/logiswitch.log"
fi
if [[ $FROM_CHECKOUT -eq 0 ]]; then
    echo "  uninstall: curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | bash -s -- --uninstall"
else
    echo "  uninstall: ./install.sh --uninstall"
fi
