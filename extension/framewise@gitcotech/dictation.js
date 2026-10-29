// Dictation, GNOME Shell side: the "Listening" pill at the top of the
// screen, and typing the recognized text into the focused window. The app
// records and recognizes the speech; this only shows and pastes.
//
// Neither takes the keyboard focus, so the text lands where the person was
// typing. Pasting goes through the clipboard (any character, any keyboard
// layout); what was copied before stays in the clipboard history.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {TERMINALS} from './clipboard.js';
import {prettyKeys, shortcutFor} from './keys.js';

export const DictationPill = GObject.registerClass(
class FramewiseDictationPill extends St.Button {
    _init(onClick) {
        super._init({style_class: 'framewise-dictation', reactive: true, can_focus: false,
            track_hover: true});
        const box = new St.BoxLayout({style_class: 'framewise-dictation-box'});
        this._dot = new St.Label({text: '●', style_class: 'framewise-dictation-dot',
            y_align: Clutter.ActorAlign.CENTER});
        this._label = new St.Label({style_class: 'framewise-dictation-label',
            y_align: Clutter.ActorAlign.CENTER});
        this._hint = new St.Label({style_class: 'framewise-dictation-hint',
            y_align: Clutter.ActorAlign.CENTER});
        box.add_child(this._dot);
        box.add_child(this._label);
        box.add_child(this._hint);
        this.set_child(box);
        this.connect('clicked', () => onClick());
        Main.layoutManager.addTopChrome(this);
        this.connect('notify::width', () => this._place());
    }

    setState(state) {
        this._dot.remove_all_transitions();
        this._dot.opacity = 255;
        if (state === 'writing') {
            this._label.text = 'Writing…';
            this._hint.text = '';
            this._hint.hide();
            this.remove_style_class_name('listening');
        } else {
            this._label.text = 'Listening…';
            const keys = prettyKeys(shortcutFor('framewise-dictate'));
            this._hint.text = keys ? `${keys} or click to finish` : 'Click to finish';
            this._hint.show();
            this.add_style_class_name('listening');
        }
        // A slow pulse while listening, a quicker one while writing.
        this._dot.ease({
            opacity: 70,
            duration: state === 'writing' ? 350 : 800,
            mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
            autoReverse: true,
            repeatCount: -1,
        });
        this._place();
    }

    _place() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor)
            return;
        this.set_position(Math.round(monitor.x + (monitor.width - this.width) / 2),
            monitor.y + Main.panel.height + 12);
    }

    destroy() {
        this._dot.remove_all_transitions();
        Main.layoutManager.removeChrome(this);
        super.destroy();
    }
});

export class Typist {
    constructor() {
        // Made now: a virtual keyboard drops the first keys sent right after
        // it is created.
        const backend = global.stage.context?.get_backend?.() ?? Clutter.get_default_backend();
        this._keyboard = backend.get_default_seat().create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._timer = 0;
    }

    // Pastes text into the focused window. False when no window has the focus.
    type(text) {
        const win = global.display.focus_window;
        if (!win || !text)
            return false;
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
        const names = [win.get_wm_class(), win.get_gtk_application_id(), win.get_sandboxed_app_id?.()]
            .filter(Boolean).join(' ').toLowerCase();
        const terminal = TERMINALS.some(t => names.includes(t));
        if (this._timer)
            GLib.source_remove(this._timer);
        // Give the clipboard a moment to reach the app before it pastes.
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 120, () => {
            this._timer = 0;
            if (global.display.focus_window === win)
                this._paste(terminal);
            return GLib.SOURCE_REMOVE;
        });
        return true;
    }

    _paste(shift) {
        const k = this._keyboard;
        const t = GLib.get_monotonic_time();
        const keys = shift ? [Clutter.KEY_Control_L, Clutter.KEY_Shift_L] : [Clutter.KEY_Control_L];
        keys.forEach(key => k.notify_keyval(t, key, Clutter.KeyState.PRESSED));
        k.notify_keyval(t, Clutter.KEY_v, Clutter.KeyState.PRESSED);
        k.notify_keyval(t, Clutter.KEY_v, Clutter.KeyState.RELEASED);
        keys.reverse().forEach(key => k.notify_keyval(t, key, Clutter.KeyState.RELEASED));
    }

    destroy() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._keyboard?.run_dispose();
        this._keyboard = null;
    }
}
