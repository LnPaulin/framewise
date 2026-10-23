// Framewise's helper inside GNOME Shell. Brought to you by @gitcotech.
//
// On Wayland a normal app can't keep a window above all others, see every
// copy, or put an icon in the top bar. GNOME Shell can, so this extension:
//   - shows the Framewise menu in the top bar (indicator.js)
//   - keeps the clipboard history (clipboard.js)
//   - shows recording controls (time, pause, stop) and a border around the
//     recorded area, a countdown and the screenshot preview, when the app asks
// The extension never records anything itself.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Mtk from 'gi://Mtk';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

import {ClipboardHistory, ClipboardPicker} from './clipboard.js';
import {Indicator} from './indicator.js';
import {BubblePlacement} from './placement.js';
import {vbox} from './compat.js';
import {CallerGuard} from './guard.js';
import {TypingTracker} from './typing.js';

const BUS_NAME = 'com.gitcotech.Framewise.Shell';
const OBJECT_PATH = '/com/gitcotech/Framewise/Shell';
const VERSION = '1.9';
const APP_ID = 'com.gitcotech.Framewise';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot');
Gio._promisify(Shell.Screenshot.prototype, 'screenshot_window');

const IFACE = `
<node>
  <interface name="com.gitcotech.Framewise.Shell">
    <method name="Ping">
      <arg type="s" direction="out" name="version"/>
    </method>
    <method name="ShowArea">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
    </method>
    <method name="ShowScreenBorder">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
    </method>
    <method name="ShowWindowBorder">
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="b" direction="out" name="found"/>
    </method>
    <method name="HideArea"/>
    <method name="StartPointerTrack"/>
    <method name="StopPointerTrack">
      <arg type="s" direction="out" name="samples"/>
    </method>
    <method name="KeepAbove">
      <arg type="s" direction="in" name="title"/>
      <arg type="b" direction="out" name="found"/>
    </method>
    <method name="ShowControls"/>
    <method name="UpdateControls">
      <arg type="d" direction="in" name="elapsed"/>
      <arg type="b" direction="in" name="paused"/>
    </method>
    <method name="HideControls"/>
    <method name="ShowCountdown">
      <arg type="i" direction="in" name="centerX"/>
      <arg type="i" direction="in" name="centerY"/>
      <arg type="i" direction="in" name="seconds"/>
    </method>
    <method name="HideCountdown"/>
    <method name="Screenshot">
      <arg type="b" direction="in" name="include_cursor"/>
      <arg type="s" direction="in" name="path"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="ScreenshotWindow">
      <arg type="t" direction="in" name="window_id"/>
      <arg type="b" direction="in" name="include_cursor"/>
      <arg type="s" direction="in" name="path"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="ListWindows">
      <arg type="s" direction="out" name="windows"/>
    </method>
    <method name="ShowShot">
      <arg type="s" direction="in" name="path"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="i" direction="in" name="atX"/>
      <arg type="i" direction="in" name="atY"/>
      <arg type="u" direction="in" name="seconds"/>
      <arg type="s" direction="in" name="note"/>
    </method>
    <method name="HideShot"/>
    <method name="ShowClipboard"/>
    <method name="BubbleCorner">
      <arg type="s" direction="out" name="corner"/>
    </method>
    <method name="SetBubbleAspect">
      <arg type="d" direction="in" name="ratio"/>
      <arg type="i" direction="in" name="margin"/>
    </method>
    <signal name="BubbleCornerChanged">
      <arg type="s" name="corner"/>
    </signal>
    <signal name="ShotAction">
      <arg type="s" name="action"/>
    </signal>
    <signal name="PauseClicked"/>
    <signal name="StopClicked"/>
    <signal name="CountdownCancelled"/>
  </interface>
</node>`;

function formatTime(seconds) {
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

const Controls = GObject.registerClass(
class FramewiseControls extends PanelMenu.Button {
    _init(onPause, onStop) {
        super._init(0.5, 'framewise recording', true);
        this.add_style_class_name('framewise-controls');

        const box = new St.BoxLayout({style_class: 'framewise-box', y_align: Clutter.ActorAlign.CENTER});
        this._dot = new St.Label({text: '●', style_class: 'framewise-dot', y_align: Clutter.ActorAlign.CENTER});
        this._time = new St.Label({text: '0:00', style_class: 'framewise-time', y_align: Clutter.ActorAlign.CENTER});
        this._pauseIcon = new St.Icon({icon_name: 'media-playback-pause-symbolic', style_class: 'system-status-icon'});
        this._pause = new St.Button({child: this._pauseIcon, style_class: 'framewise-button', can_focus: true});
        this._pause.connect('clicked', () => onPause());
        const stopIcon = new St.Icon({icon_name: 'media-playback-stop-symbolic', style_class: 'system-status-icon'});
        const stop = new St.Button({child: stopIcon, style_class: 'framewise-button framewise-stop', can_focus: true});
        stop.connect('clicked', () => onStop());

        box.add_child(this._dot);
        box.add_child(this._time);
        box.add_child(this._pause);
        box.add_child(stop);
        this.add_child(box);
    }

    update(elapsed, paused) {
        this._time.text = formatTime(elapsed);
        if (paused) {
            this._dot.add_style_class_name('paused');
            this._pauseIcon.icon_name = 'media-record-symbolic';
        } else {
            this._dot.remove_style_class_name('paused');
            this._pauseIcon.icon_name = 'media-playback-pause-symbolic';
        }
    }
});

export default class FramewiseExtension extends Extension {
    enable() {
        this._border = null;
        this._trackedWindow = null;
        this._windowSignals = null;
        this._controls = null;
        this._countdown = null;
        this._countdownLabel = null;
        this._clipHistory = new ClipboardHistory();
        this._clipHistory.start();
        this._placement = new BubblePlacement(corner =>
            this._dbus?.emit_signal('BubbleCornerChanged', new GLib.Variant('(s)', [corner])));
        this._placement.start();
        this._clipPicker = new ClipboardPicker(this._clipHistory);
        this._indicator = null;
        this._settingsFile = Gio.File.new_for_path(
            GLib.build_filenamev([GLib.get_user_config_dir(), 'framewise', 'settings.json']));
        this._settingsMonitor = this._settingsFile.monitor_file(Gio.FileMonitorFlags.NONE, null);
        this._settingsMonitor.connect('changed', () => this._syncIndicator());
        // Like a menu-bar app: the icon and clipboard history are there while
        // Framewise runs, and go away when it quits.
        this._appRunning = false;
        this._appWatch = Gio.bus_watch_name(Gio.BusType.SESSION, APP_ID, Gio.BusNameWatcherFlags.NONE,
            () => this._setAppRunning(true), () => this._setAppRunning(false));
        this._syncIndicator();
        // Every call is checked: only the Framewise app may use this interface.
        this._guard = new CallerGuard(APP_ID);
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this._guard.wrap(IFACE, this));
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null, null);
    }

    disable() {
        this._typing?.stop();
        this._typing = null;
        this._placement?.stop();
        this._placement = null;
        if (this._appWatch) {
            Gio.bus_unwatch_name(this._appWatch);
            this._appWatch = 0;
        }
        this._settingsMonitor?.cancel();
        this._settingsMonitor = null;
        this._indicator?.destroy();
        this._indicator = null;
        this._clipPicker?.destroy();
        this._clipPicker = null;
        this._clipHistory?.stop();
        this._clipHistory = null;
        this.HideShot();
        this._stopTrackTimer();
        this._track = null;
        this.HideArea();
        this.HideControls();
        this.HideCountdown();
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    _setAppRunning(running) {
        this._appRunning = running;
        this._guard?.reset();
        this._clipHistory?.setRecording(running);
        if (!running)
            this._clipPicker?.close();
        this._syncIndicator();
    }

    // The top-bar icon: while Framewise runs, unless "Show in the top bar"
    // is off in Settings.
    _syncIndicator() {
        let show = this._appRunning;
        try {
            const [, bytes] = this._settingsFile.load_contents(null);
            const saved = JSON.parse(new TextDecoder().decode(bytes));
            if (saved.topbar_icon === false)
                show = false;
        } catch (e) {}
        if (show && !this._indicator) {
            this._indicator = new Indicator(this, () => this._clipPicker?.open(), () => !!this._controls);
            Main.panel.addToStatusArea('framewise', this._indicator, 0, 'right');
        } else if (!show && this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }

    // ---- D-Bus methods --------------------------------------------------

    // Notes where the pointer is and whether the main button is down, 60 times
    // a second, while a recording runs. Wayland gives apps no global pointer
    // or clicks; the editor needs both for zoom on click. Times are the
    // system's monotonic clock in microseconds, which the app shares.
    StartPointerTrack() {
        this._stopTrackTimer();
        this._track = [];
        this._typing ??= new TypingTracker();
        this._typing.start();
        this._cursorTracker = global.backend.get_cursor_tracker();
        this._trackTimer = GLib.timeout_add(GLib.PRIORITY_HIGH, 16, () => {
            const [x, y, mods] = global.get_pointer();
            const down = (mods & Clutter.ModifierType.BUTTON1_MASK) ? 1 : 0;
            // GNOME hides the pointer while you type; the drawn one follows suit.
            const visible = this._cursorTracker?.get_pointer_visible() ? 1 : 0;
            this._track.push([GLib.get_monotonic_time(), Math.round(x), Math.round(y), down, visible]);
            // Safety net: stop after about two hours if nobody stops it.
            if (this._track.length > 60 * 60 * 120)
                this._stopTrackTimer();
            return GLib.SOURCE_CONTINUE;
        });
    }

    // {"pointer": [[us, x, y, down, visible], ...], "typing": [[us, x, y], ...]}
    StopPointerTrack() {
        this._stopTrackTimer();
        const typing = this._typing ? this._typing.stop() : [];
        const out = JSON.stringify({pointer: this._track ?? [], typing});
        this._track = null;
        return out;
    }

    _stopTrackTimer() {
        if (this._trackTimer) {
            GLib.source_remove(this._trackTimer);
            this._trackTimer = 0;
        }
    }

    // Keeps one of framewise's windows (the camera bubble) above all other
    // windows and on every workspace. Wayland apps can't ask for this.
    KeepAbove(title) {
        const win = global.display.list_all_windows().find(w => w.get_title() === title);
        if (!win)
            return false;
        win.make_above();
        win.stick();
        return true;
    }

    Ping() {
        return VERSION;
    }

    // The screen corner the camera bubble is nearest (BubbleCornerChanged
    // follows it as the bubble moves).
    BubbleCorner() {
        return this._placement?.corner ?? 'bottom-right';
    }

    SetBubbleAspect(ratio, margin) {
        this._placement?.setAspect(ratio, margin);
    }

    // A border just outside a selected area, so it never shows in the video.
    ShowArea(x, y, width, height) {
        const out = 5;
        this._showBorder(x - out, y - out, width + out * 2, height + out * 2);
    }

    // A border along the inside edges of a whole screen. It does show in the
    // capture, so the app trims those few pixels off when saving.
    ShowScreenBorder(x, y, width, height) {
        this._showBorder(x, y, width, height);
    }

    // A border around the window being recorded, following it as it moves.
    // The portal doesn't say which window was picked, so this finds the
    // window whose size matches the stream, preferring the focused one.
    ShowWindowBorder(width, height) {
        this.HideArea();
        const win = this._findWindow(width, height);
        if (!win)
            return false;
        this._trackedWindow = win;
        const update = () => {
            if (win.minimized) {
                this._border?.hide();
                return;
            }
            const r = win.get_frame_rect();
            const out = 5;
            this._showBorder(r.x - out, r.y - out, r.width + out * 2, r.height + out * 2);
        };
        this._windowSignals = [
            win.connect('position-changed', update),
            win.connect('size-changed', update),
            win.connect('notify::minimized', update),
            win.connect('unmanaged', () => this.HideArea()),
        ];
        update();
        return true;
    }

    _findWindow(width, height) {
        const fits = w => {
            const f = w.get_frame_rect();
            const b = w.get_buffer_rect();
            const near = (r, tol) => Math.abs(r.width - width) <= tol && Math.abs(r.height - height) <= tol;
            return near(f, 4) || near(b, 4) || near(f, 64);
        };
        const windows = global.display.get_tab_list(Meta.TabList.NORMAL_ALL, null)
            .filter(w => !w.minimized);
        // Tab list is most-recently-focused first.
        return windows.find(w => fits(w)) ?? null;
    }

    _showBorder(x, y, width, height) {
        if (!this._border) {
            this._border = new St.Widget({style_class: 'framewise-area', reactive: false});
            // GNOME 50 takes only these options; reactive: false is what lets
            // clicks pass through the border.
            Main.layoutManager.addTopChrome(this._border, {affectsStruts: false});
        }
        this._border.set_position(Math.round(x), Math.round(y));
        this._border.set_size(Math.round(width), Math.round(height));
        this._border.show();
    }

    HideArea() {
        if (this._trackedWindow && this._windowSignals) {
            for (const id of this._windowSignals)
                this._trackedWindow.disconnect(id);
        }
        this._trackedWindow = null;
        this._windowSignals = null;
        if (this._border) {
            Main.layoutManager.removeChrome(this._border);
            this._border.destroy();
            this._border = null;
        }
    }

    ShowControls() {
        if (this._controls)
            return;
        this._controls = new Controls(
            () => this._dbus.emit_signal('PauseClicked', null),
            () => this._dbus.emit_signal('StopClicked', null));
        Main.panel.addToStatusArea('framewise-controls', this._controls, 0, 'center');
    }

    UpdateControls(elapsed, paused) {
        this._controls?.update(elapsed, paused);
    }

    HideControls() {
        if (this._controls) {
            this._controls.destroy();
            this._controls = null;
        }
    }

    // A round badge with the seconds left, floating over the live screen.
    // A red ring sweeps around once per second and each number pops in.
    // The app sends the new number every second; a click cancels.
    ShowCountdown(centerX, centerY, seconds) {
        const size = 200;
        if (!this._countdown) {
            const button = new St.Button({
                style_class: 'framewise-countdown',
                width: size,
                height: size,
                x: centerX - size / 2,
                y: centerY - size / 2,
                reactive: true,
                opacity: 0,
            });
            const stack = new St.Widget({layout_manager: new Clutter.BinLayout(), width: size, height: size});
            this._ring = new St.DrawingArea({width: size, height: size});
            this._ring.connect('repaint', area => this._paintRing(area));
            const column = vbox({
                x_align: Clutter.ActorAlign.CENTER,
                y_align: Clutter.ActorAlign.CENTER,
            });
            this._countdownLabel = new St.Label({
                style_class: 'framewise-countdown-number',
                x_align: Clutter.ActorAlign.CENTER,
            });
            this._countdownLabel.set_pivot_point(0.5, 0.5);
            column.add_child(this._countdownLabel);
            column.add_child(new St.Label({
                text: 'Click to cancel',
                style_class: 'framewise-countdown-hint',
                x_align: Clutter.ActorAlign.CENTER,
            }));
            stack.add_child(this._ring);
            stack.add_child(column);
            button.set_child(stack);
            button.connect('clicked', () => this._dbus.emit_signal('CountdownCancelled', null));
            Main.layoutManager.addTopChrome(button, {affectsStruts: false});
            this._countdown = button;
            button.ease({opacity: 255, duration: 150, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
            this._ringTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 16, () => {
                this._ring?.queue_repaint();
                return GLib.SOURCE_CONTINUE;
            });
        }
        this._secondStarted = GLib.get_monotonic_time();
        const label = this._countdownLabel;
        label.text = String(seconds);
        label.remove_all_transitions();
        label.set_scale(1.4, 1.4);
        label.opacity = 0;
        label.ease({
            scale_x: 1, scale_y: 1, opacity: 255,
            duration: 280,
            mode: Clutter.AnimationMode.EASE_OUT_BACK,
        });
    }

    _paintRing(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 8;
        const elapsed = (GLib.get_monotonic_time() - (this._secondStarted ?? 0)) / 1e6;
        const progress = Math.min(1, Math.max(0, elapsed));
        cr.setLineWidth(6);
        cr.setLineCap(1);  // round
        cr.setSourceRGBA(1, 1, 1, 0.16);
        cr.arc(cx, cy, r, 0, 2 * Math.PI);
        cr.stroke();
        cr.setSourceRGBA(0.898, 0.282, 0.302, 1);
        const start = -Math.PI / 2;
        cr.arc(cx, cy, r, start, start + progress * 2 * Math.PI);
        cr.stroke();
        cr.$dispose();
    }

    // ---- screenshots ------------------------------------------------------

    // The whole desktop (every screen) as a PNG, with no dialog: the app
    // shows it frozen while the user chooses what to keep.
    async ScreenshotAsync([includeCursor, path], invocation) {
        try {
            const stream = Gio.File.new_for_path(path).replace(null, false, Gio.FileCreateFlags.NONE, null);
            await new Shell.Screenshot().screenshot(includeCursor, stream);
            stream.close(null);
            // Captured with the clipboard list on screen, if it was opened for that.
            this._clipPicker?.shotTaken();
            invocation.return_value(new GLib.Variant('(b)', [true]));
        } catch (e) {
            console.error(`framewise screenshot: ${e}`);
            invocation.return_value(new GLib.Variant('(b)', [false]));
        }
    }

    // One window on its own, even where others cover it. GNOME takes the
    // focused window, so this focuses it first.
    async ScreenshotWindowAsync([windowId, includeCursor, path], invocation) {
        try {
            const win = global.display.list_all_windows().find(w => w.get_id() === Number(windowId));
            if (!win)
                throw new Error('window not found');
            win.activate(global.get_current_time());
            await new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, 120, () => {
                resolve();
                return GLib.SOURCE_REMOVE;
            }));
            const stream = Gio.File.new_for_path(path).replace(null, false, Gio.FileCreateFlags.NONE, null);
            await new Shell.Screenshot().screenshot_window(true, includeCursor, stream);
            stream.close(null);
            invocation.return_value(new GLib.Variant('(b)', [true]));
        } catch (e) {
            console.error(`framewise window screenshot: ${e}`);
            invocation.return_value(new GLib.Variant('(b)', [false]));
        }
    }

    // Windows on the current workspace, top-most first, so the app can
    // highlight the one under the pointer while choosing.
    ListWindows() {
        const kinds = [Meta.WindowType.NORMAL, Meta.WindowType.DIALOG, Meta.WindowType.MODAL_DIALOG,
            Meta.WindowType.UTILITY];
        const ws = global.workspace_manager.get_active_workspace();
        const windows = global.display.sort_windows_by_stacking(
            global.display.list_all_windows().filter(w =>
                !w.minimized && kinds.includes(w.get_window_type()) &&
                (w.is_on_all_workspaces() || w.get_workspace() === ws)))
            .reverse();
        return JSON.stringify(windows.map(w => {
            const r = w.get_frame_rect();
            return {id: w.get_id(), x: r.x, y: r.y, w: r.width, h: r.height, title: w.get_title() ?? ''};
        }));
    }

    // A small card with the new screenshot in the corner of the screen it
    // was taken on. It fades away after a few seconds unless the pointer
    // is over it; every button reports back with ShotAction.
    ShowShot(path, width, height, atX, atY, seconds, note) {
        this.HideShot();
        const maxW = 280, maxH = 180;
        const k = Math.min(maxW / Math.max(1, width), maxH / Math.max(1, height), 1);
        const tw = Math.max(60, Math.round(width * k)), th = Math.max(40, Math.round(height * k));
        const card = vbox({
            style_class: 'framewise-shot',
            reactive: true, track_hover: true, opacity: 0,
        });
        const thumb = new St.Button({style_class: 'framewise-shot-thumb', width: tw, height: th,
            x_align: Clutter.ActorAlign.CENTER});
        const uri = Gio.File.new_for_path(path).get_uri();
        thumb.set_style(`background-image: url("${uri}"); background-size: cover;`);
        thumb.connect('clicked', () => this._shotAction('edit'));
        card.add_child(thumb);
        if (note)
            card.add_child(new St.Label({text: note, style_class: 'framewise-shot-note'}));
        const row = new St.BoxLayout({style_class: 'framewise-shot-row'});
        for (const [action, icon, label] of [
            ['edit', 'document-edit-symbolic', 'Edit'],
            ['pin', 'view-pin-symbolic', 'Pin'],
            ['delete', 'user-trash-symbolic', 'Delete'],
            ['close', 'window-close-symbolic', ''],
        ]) {
            const box = new St.BoxLayout({style_class: 'framewise-shot-button-box'});
            box.add_child(new St.Icon({icon_name: icon, icon_size: 16}));
            if (label)
                box.add_child(new St.Label({text: label, y_align: Clutter.ActorAlign.CENTER}));
            const button = new St.Button({style_class: 'framewise-shot-button', child: box,
                x_expand: !!label, accessible_name: label || 'Close'});
            button.connect('clicked', () => this._shotAction(action));
            row.add_child(button);
        }
        card.add_child(row);
        Main.layoutManager.addTopChrome(card, {affectsStruts: false});

        const index = global.display.get_monitor_index_for_rect(
            new Mtk.Rectangle({x: atX, y: atY, width: 1, height: 1}));
        const area = Main.layoutManager.getWorkAreaForMonitor(Math.max(0, index));
        const [, natW] = card.get_preferred_width(-1);
        const [, natH] = card.get_preferred_height(natW);
        card.set_position(area.x + area.width - natW - 24, area.y + area.height - natH - 24);
        card.translation_x = 40;
        card.ease({opacity: 255, translation_x: 0, duration: 220, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        this._shot = card;
        this._shotLeft = seconds;
        if (seconds > 0) {
            this._shotTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
                if (card.hover)
                    this._shotLeft = Math.max(this._shotLeft, 2);
                else if (--this._shotLeft <= 0) {
                    this._shotTimer = 0;
                    this._shotAction('timeout');
                    return GLib.SOURCE_REMOVE;
                }
                return GLib.SOURCE_CONTINUE;
            });
        }
    }

    _shotAction(action) {
        this.HideShot();
        this._dbus?.emit_signal('ShotAction', new GLib.Variant('(s)', [action]));
    }

    // Opens the clipboard history (or closes it when it is open).
    ShowClipboard() {
        this._clipPicker?.open();
    }

    HideShot() {
        if (this._shotTimer) {
            GLib.source_remove(this._shotTimer);
            this._shotTimer = 0;
        }
        if (this._shot) {
            const card = this._shot;
            this._shot = null;
            card.reactive = false;
            card.ease({
                opacity: 0, translation_x: 40, duration: 160, mode: Clutter.AnimationMode.EASE_IN_QUAD,
                onComplete: () => {
                    Main.layoutManager.removeChrome(card);
                    card.destroy();
                },
            });
        }
    }

    HideCountdown() {
        if (this._ringTimer) {
            GLib.source_remove(this._ringTimer);
            this._ringTimer = 0;
        }
        if (this._countdown) {
            const button = this._countdown;
            this._countdown = null;
            this._countdownLabel = null;
            this._ring = null;
            // Quick fade, finished well before the first frame is recorded.
            button.ease({
                opacity: 0,
                duration: 90,
                mode: Clutter.AnimationMode.EASE_IN_QUAD,
                onComplete: () => {
                    Main.layoutManager.removeChrome(button);
                    button.destroy();
                },
            });
        }
    }
}
