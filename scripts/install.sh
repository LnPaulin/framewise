#!/usr/bin/env bash
# Installs Framewise for the current user (no sudo needed). Brought to you by @gitcotech.
#   ~/.local/bin/framewise                                 the command
#   ~/.local/share/applications/com.gitcotech.Framewise.desktop   one app icon,
#       with Record, Screenshot, Copy text, Clipboard history and Settings
#       on its right-click menu
#   ~/.local/share/icons/hicolor/...                       the icons
#   the GNOME Shell extension framewise@gitcotech          top-bar menu, clipboard history
#   keyboard shortcuts: Shift+Super+R / S / T / V / C (kept if already set; change them
#       in Framewise Settings)
# It also replaces an install made under the old name (capturekit), keeping
# settings, data and shortcut keys.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$HOME/.local/bin"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$SHARE/applications"
ICONS="$SHARE/icons/hicolor"
APP_ID="com.gitcotech.Framewise"
EXT_UUID="framewise@gitcotech"
EXT_DIR="$SHARE/gnome-shell/extensions"
mkdir -p "$BIN" "$APPS" "$ICONS/scalable/apps" "$ICONS/symbolic/apps" "$EXT_DIR"

# ---- the old name ------------------------------------------------------------------
OLD_EXT="capturekit@capturekit"
if [[ -e "$EXT_DIR/$OLD_EXT" ]]; then
    gnome-extensions disable "$OLD_EXT" 2>/dev/null || true
    rm -f "$EXT_DIR/$OLD_EXT"
    echo "Removed the old capturekit extension."
fi
rm -f "$BIN/capturekit" "$APPS"/io.github.capturekit*.desktop \
      "$SHARE/dbus-1/services/io.github.capturekit.service"
# Moves ~/.config/capturekit and ~/.local/share/capturekit (done on import).
PYTHONPATH="$ROOT/app" python3 -c "import framewise.state"

# ---- the command -------------------------------------------------------------------
cat > "$BIN/framewise" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$ROOT/app\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -P -m framewise "\$@"
EOF
chmod +x "$BIN/framewise"

# ---- icons -------------------------------------------------------------------------
cp "$ROOT/data/icons/$APP_ID.svg" "$ICONS/scalable/apps/"
cp "$ROOT/data/icons/$APP_ID-symbolic.svg" "$ICONS/symbolic/apps/"
gtk-update-icon-cache -q -t "$ICONS" 2>/dev/null || true

# ---- one app icon, with the tools on its right-click menu ---------------------------
cat > "$APPS/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Framewise
GenericName=Screen Recorder and Screenshots
Comment=Record the screen, take and mark up screenshots, copy text, clipboard history
Keywords=record;screencast;screenshot;capture;ocr;text;clipboard;history;
Exec=$BIN/framewise
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Video;Recorder;Graphics;Utility;
StartupNotify=true
DBusActivatable=true
Actions=record;screenshot;text;clipboard;camera;preferences;

[Desktop Action record]
Name=Record Screen
Exec=$BIN/framewise record

[Desktop Action screenshot]
Name=Take a Screenshot
Exec=$BIN/framewise screenshot

[Desktop Action text]
Name=Capture Text
Exec=$BIN/framewise text

[Desktop Action clipboard]
Name=Clipboard History
Exec=$BIN/framewise clipboard

[Desktop Action camera]
Name=Toggle Camera
Exec=$BIN/framewise camera

[Desktop Action preferences]
Name=Settings
Exec=$BIN/framewise settings
EOF

# Lets GNOME start the app from the icon, the top-bar menu or a notification.
mkdir -p "$SHARE/dbus-1/services"
cat > "$SHARE/dbus-1/services/$APP_ID.service" <<EOF
[D-BUS Service]
Name=$APP_ID
Exec=$BIN/framewise --gapplication-service
EOF
update-desktop-database "$APPS" 2>/dev/null || true

# "Start at login" already on: point it at this install.
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.desktop"
if [[ -f "$AUTOSTART" ]]; then
    PYTHONPATH="$ROOT/app" python3 -c "from framewise.settings_window import set_autostart; set_autostart(True)"
fi

# ---- shortcuts ---------------------------------------------------------------------
PYTHONPATH="$ROOT/app" python3 -c "
from framewise import shortcuts
print('\n'.join(shortcuts.setup()))"

# ---- GNOME Shell extension ---------------------------------------------------------
# GNOME only notices a new extension after logging out and back in.
ln -sfn "$ROOT/extension/$EXT_UUID" "$EXT_DIR/$EXT_UUID"
if gnome-extensions enable "$EXT_UUID" 2>/dev/null; then
    echo "Extension enabled."
else
    # Not known to GNOME yet: add it to the enabled list for the next login.
    current=$(gsettings get org.gnome.shell enabled-extensions)
    if [[ "$current" != *"$EXT_UUID"* ]]; then
        if [[ "$current" == "@as []" || "$current" == "[]" ]]; then
            gsettings set org.gnome.shell enabled-extensions "['$EXT_UUID']"
        else
            gsettings set org.gnome.shell enabled-extensions "${current%]}, '$EXT_UUID']"
        fi
    fi
    echo "Extension installed. Log out and back in to turn it on."
fi
echo "Framewise is installed. Brought to you by @gitcotech."
