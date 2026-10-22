#!/usr/bin/env bash
# Removes the development install made by scripts/install.sh (the links to this
# checkout in your home folder), so an installed .deb is the one that runs.
# Your settings, recordings, screenshots, clipboard history and shortcuts stay.
set -euo pipefail

SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_ID="com.gitcotech.Framewise"
EXT="framewise@gitcotech"

rm -f "$HOME/.local/bin/framewise"
rm -f "$SHARE/applications/$APP_ID.desktop" "$SHARE/dbus-1/services/$APP_ID.service"
rm -f "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg" "$SHARE/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
# Only the link to this checkout: the .deb's copy lives in /usr/share.
if [[ -L "$SHARE/gnome-shell/extensions/$EXT" ]]; then
    rm -f "$SHARE/gnome-shell/extensions/$EXT"
fi
update-desktop-database "$SHARE/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$SHARE/icons/hicolor" 2>/dev/null || true
echo "Development install removed. Settings and data are kept."
echo "Quit Framewise (top-right menu) and start it again, or log out and back in."
