#!/bin/bash
# Installs Framewise on Ubuntu, with automatic updates:
#
#   wget -qO- https://lnpaulin.github.io/framewise/install.sh | sudo bash
#
# It adds the Framewise APT repository and its signing key (the same two
# files the Framewise package itself installs), then installs Framewise
# with apt, which also brings what it needs (GStreamer, ffmpeg, Tesseract...).
# New versions then arrive in Software Updater.
#
# Key fingerprint: F69B 9995 8DF3 DE44 9A33  CABF 2B8D 94D1 92EA DC4F
# Source: https://github.com/LnPaulin/framewise

set -euo pipefail

SITE="https://lnpaulin.github.io/framewise/apt"
ROOT="${FRAMEWISE_ROOT:-}"   # for testing only: install the two files under another folder

main() {
    if [[ -z "$ROOT" && $EUID -ne 0 ]]; then
        echo "Run it with sudo:  wget -qO- https://lnpaulin.github.io/framewise/install.sh | sudo bash" >&2
        exit 1
    fi
    if ! command -v apt-get >/dev/null; then
        echo "Framewise needs Ubuntu (or another apt-based system with GNOME)." >&2
        exit 1
    fi

    echo "Adding the Framewise update repository..."
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    fetch "$SITE/framewise.gpg" "$TMP/framewise.gpg"
    fetch "$SITE/framewise.sources" "$TMP/framewise.sources"
    install -d -m 755 "$ROOT/etc/apt/keyrings" "$ROOT/etc/apt/sources.list.d"
    install -m 644 "$TMP/framewise.gpg" "$ROOT/etc/apt/keyrings/framewise.gpg"
    install -m 644 "$TMP/framewise.sources" "$ROOT/etc/apt/sources.list.d/framewise.sources"

    echo "Installing Framewise..."
    apt-get update
    apt-get install -y framewise

    echo
    echo "Framewise is installed. Open it from the app grid, then log out and back in"
    echo "once to turn on its top bar menu, clipboard history and recording controls."
    echo "Updates will arrive in Software Updater."
}

fetch() {
    if command -v wget >/dev/null; then
        wget -qO "$2" "$1"
    else
        curl -fsSLo "$2" "$1"
    fi
}

main "$@"
