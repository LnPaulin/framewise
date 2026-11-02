// Scrolling screenshots, GNOME Shell side: scrolls the window under a spot
// (with a virtual mouse wheel) and shows a "Scrolling" pill outside the
// area being captured. Moving the mouse, or clicking the pill, stops it.
// The app captures and joins the pictures.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Mtk from 'gi://Mtk';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const MOVED = 80;          // pixels the pointer may drift (a bumped desk) before it counts as moved by the person
const SMOOTH_STEP = 5;   // smooth-scroll units per "click" (about 30 px in Files)

export class Scroller {
    // Made when the extension starts: a virtual device drops the first events
    // sent right after it is created.
    constructor() {
        this._timer = 0;
        const backend = global.stage.context?.get_backend?.() ?? Clutter.get_default_backend();
        this._pointer = backend.get_default_seat().create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
    }

    // A new capture: the pointer may be moved to the area again.
    reset() {
        this._placed = null;
    }

    // Moves the pointer to x, y and turns the wheel down `clicks` times.
    // False (and nothing done) when the person moved the mouse away since the
    // last scroll: they want it back, so the capture stops instead.
    scrollAt(x, y, clicks, smooth = false) {
        const [px, py] = global.get_pointer();
        if (this._placed && Math.hypot(px - this._placed[0], py - this._placed[1]) > MOVED)
            return false;
        this._placed = [x, y];
        this._pointer.notify_absolute_motion(GLib.get_monotonic_time(), x, y);
        if (this._timer)
            GLib.source_remove(this._timer);
        // The window under the pointer first learns that the pointer is there.
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 40, () => {
            this._timer = 0;
            const t = GLib.get_monotonic_time();
            for (let i = 0; i < clicks; i++) {
                if (smooth)
                    this._pointer?.notify_scroll_continuous(t + i, 0, SMOOTH_STEP, Clutter.ScrollSource.WHEEL, Clutter.ScrollFinishFlags.NONE);
                else
                    this._pointer?.notify_discrete_scroll(t + i, Clutter.ScrollDirection.DOWN, Clutter.ScrollSource.WHEEL);
            }
            return GLib.SOURCE_REMOVE;
        });
        return true;
    }

    destroy() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._pointer?.run_dispose();
        this._pointer = null;
    }
}

export const ScrollPill = GObject.registerClass(
class FramewiseScrollPill extends St.Button {
    _init(onClick) {
        super._init({style_class: 'framewise-dictation listening', reactive: true, can_focus: false,
            track_hover: true});
        const box = new St.BoxLayout({style_class: 'framewise-dictation-box'});
        this._dot = new St.Label({text: '●', style_class: 'framewise-dictation-dot', y_align: Clutter.ActorAlign.CENTER});
        box.add_child(this._dot);
        box.add_child(new St.Label({text: 'Scrolling…', style_class: 'framewise-dictation-label',
            y_align: Clutter.ActorAlign.CENTER}));
        box.add_child(new St.Label({text: 'Move the mouse to stop', style_class: 'framewise-dictation-hint',
            y_align: Clutter.ActorAlign.CENTER}));
        this.set_child(box);
        this.connect('clicked', () => onClick());
        Main.layoutManager.addTopChrome(this);
        this._dot.ease({opacity: 70, duration: 600, mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
            autoReverse: true, repeatCount: -1});
    }

    // Outside the captured area (or it would be in the picture): above it,
    // below it, or else in a corner of the screen it is on.
    placeOutside(x, y, w, h) {
        const [, pw] = this.get_preferred_width(-1);
        const [, ph] = this.get_preferred_height(pw);
        const monitor = Main.layoutManager.monitors[global.display.get_monitor_index_for_rect(
            new Mtk.Rectangle({x, y, width: Math.max(1, w), height: Math.max(1, h)}))]
            ?? Main.layoutManager.primaryMonitor;
        const gap = 12;
        const cx = Math.round(Math.min(Math.max(x + (w - pw) / 2, monitor.x + gap),
            monitor.x + monitor.width - pw - gap));
        const top = monitor.y + Main.panel.height;
        if (y - ph - gap >= top)
            this.set_position(cx, y - ph - gap);
        else if (y + h + gap + ph <= monitor.y + monitor.height)
            this.set_position(cx, y + h + gap);
        else
            this.set_position(monitor.x + monitor.width - pw - gap, top + gap);
    }

    destroy() {
        this._dot.remove_all_transitions();
        Main.layoutManager.removeChrome(this);
        super.destroy();
    }
});
