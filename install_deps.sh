#!/usr/bin/env bash
# Install the USB transport dependencies for Bookloader.
set -euo pipefail

say() { printf '\n==> %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO="${ID:-unknown}"
    FAMILY="${ID_LIKE:-}"
else
    DISTRO="unknown"
    FAMILY=""
fi

say "Detected distribution: $DISTRO"

case "$DISTRO" in
    debian|ubuntu|linuxmint|pop|raspbian|elementary|zorin|kali)
        $SUDO apt update
        $SUDO apt install -y python3-tk libimobiledevice-utils ifuse usbmuxd
        ;;
    fedora|rhel|centos|rocky|alma)
        $SUDO dnf install -y python3-tk libimobiledevice-utils ifuse usbmuxd
        ;;
    arch|manjaro|endeavouros)
        $SUDO pacman -S --needed tk libimobiledevice ifuse usbmuxd
        ;;
    opensuse*|sles)
        $SUDO zypper install -y python3-tk libimobiledevice-tools ifuse usbmuxd
        ;;
    *)
        if [[ "$FAMILY" == *debian* ]]; then
            $SUDO apt update
            $SUDO apt install -y python3-tk libimobiledevice-utils ifuse usbmuxd
        else
            echo "Unsupported distribution. Install manually:"
            echo "  - python3-tk (tkinter)"
            echo "  - libimobiledevice-utils + ifuse + usbmuxd"
            echo "  or just run: python3 -m venv .venv && .venv/bin/pip install pymobiledevice3 websocket-client"
            exit 1
        fi
        ;;
esac

say "Installing Python tools into a local virtual environment (.venv):"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install pymobiledevice3 websocket-client

say "Done. Run: ./run.sh"
echo "On the iPhone, enable Settings > Safari > Advanced > Web Inspector."
