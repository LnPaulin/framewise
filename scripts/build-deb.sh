#!/usr/bin/env bash
# Builds dist/framewise_<version>_all.deb from this checkout.
# Brought to you by @gitcotech.
#
#   /usr/bin/framewise                          the command
#   /usr/lib/framewise/framewise/               the app (Python), with its small models
#   /usr/share/gnome-shell/extensions/framewise@gitcotech/
#   /usr/share/applications, dbus-1/services, icons, metainfo, doc
#   /etc/apt/sources.list.d/framewise.sources   the update channel (GitHub Pages)
#   /etc/apt/keyrings/framewise.gpg             the key its updates are signed with
#
# Install the result with:  sudo apt install ./dist/framewise_<version>_all.deb
# (apt, not dpkg -i, so the packages it needs are installed too).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(PYTHONPATH="$ROOT/app" python3 -c 'import framewise; print(framewise.__version__)')"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/framewise"
APP_ID="com.gitcotech.Framewise"
EXT="framewise@gitcotech"

# ---- files ---------------------------------------------------------------------------
install -d "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/lib/framewise" \
    "$PKG/usr/share/applications" "$PKG/usr/share/dbus-1/services" \
    "$PKG/usr/share/icons/hicolor/scalable/apps" "$PKG/usr/share/icons/hicolor/symbolic/apps" \
    "$PKG/usr/share/metainfo" "$PKG/usr/share/doc/framewise" \
    "$PKG/usr/share/gnome-shell/extensions/$EXT"

# The app, without caches or tests.
cp -r "$ROOT/app/framewise" "$PKG/usr/lib/framewise/"
find "$PKG/usr/lib/framewise" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$PKG/usr/lib/framewise" -name "*.pyc" -delete

install -m 755 "$ROOT/packaging/deb/framewise" "$PKG/usr/bin/framewise"
install -m 644 "$ROOT/packaging/deb/$APP_ID.desktop" "$PKG/usr/share/applications/"
install -m 644 "$ROOT/packaging/deb/$APP_ID.service" "$PKG/usr/share/dbus-1/services/"
install -m 644 "$ROOT/data/icons/$APP_ID.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$ROOT/data/icons/$APP_ID-symbolic.svg" "$PKG/usr/share/icons/hicolor/symbolic/apps/"
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$(date +%F)/" \
    "$ROOT/packaging/deb/$APP_ID.metainfo.xml" > "$PKG/usr/share/metainfo/$APP_ID.metainfo.xml"
install -m 644 "$ROOT/packaging/deb/copyright" "$PKG/usr/share/doc/framewise/copyright"
install -m 644 "$ROOT/README.md" "$PKG/usr/share/doc/framewise/README.md"

# The GNOME Shell extension (each person switches it on at their first run).
cp -r "$ROOT/extension/$EXT/." "$PKG/usr/share/gnome-shell/extensions/$EXT/"

# Everything readable by everyone; folders and the launcher executable.
chmod 755 "$PKG" "$PKG/DEBIAN"
find "$PKG/usr" -type d -exec chmod 755 {} +
find "$PKG/usr" -type f -exec chmod 644 {} +
chmod 755 "$PKG/usr/bin/framewise"

# Updates: the Framewise APT repository and its key. Both are conffiles, so
# "apt remove" keeps them (harmless) and "apt purge" removes them.
install -d -m 755 "$PKG/etc" "$PKG/etc/apt" "$PKG/etc/apt/sources.list.d" "$PKG/etc/apt/keyrings"
install -m 644 "$ROOT/packaging/deb/framewise.sources" "$PKG/etc/apt/sources.list.d/framewise.sources"
install -m 644 "$ROOT/packaging/apt/framewise-archive-keyring.gpg" "$PKG/etc/apt/keyrings/framewise.gpg"
printf '%s\n' /etc/apt/sources.list.d/framewise.sources /etc/apt/keyrings/framewise.gpg > "$PKG/DEBIAN/conffiles"
chmod 644 "$PKG/DEBIAN/conffiles"

# ---- control ---------------------------------------------------------------------------
SIZE="$(du -sk "$PKG/usr" "$PKG/etc" | awk '{s+=$1} END {print s}')"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$ROOT/packaging/deb/control" > "$PKG/DEBIAN/control"

mkdir -p "$OUT"
DEB="$OUT/framewise_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$DEB" >/dev/null
echo "Built $DEB ($(du -h "$DEB" | cut -f1))"
