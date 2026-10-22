// Where you type, while a recording runs: each character typed in the
// focused window is noted with the text cursor's place on the screen, so
// the editor's zoom can look at what you type. Uses the accessibility
// system (AT-SPI), like GNOME's own magnifier; apps that don't share their
// text cursor (some Electron/Chromium apps) are simply not followed.

import Atspi from 'gi://Atspi';
import GLib from 'gi://GLib';
import St from 'gi://St';

const INSERT = 'object:text-changed:insert';
const MAX_TYPED = 3;          // characters per event: more is output, not typing
const TOPLEVEL = new Set([Atspi.Role.FRAME, Atspi.Role.DIALOG, Atspi.Role.WINDOW]);

export class TypingTracker {
    constructor() {
        this._samples = null;
        this._listener = null;
    }

    start() {
        this._samples = [];
        try {
            const status = Atspi.init();          // 0: started now, 1: already running
            if (status !== 0 && status !== 1)
                return;
            this._listener = Atspi.EventListener.new(event => this._onInsert(event));
            this._listener.register(INSERT);
        } catch (e) {
            console.warn(`framewise: typing can't be followed: ${e.message}`);
            this._listener = null;
        }
    }

    // [[monotonic_us, x, y], ...] in screen coordinates.
    stop() {
        try {
            this._listener?.deregister(INSERT);
        } catch (e) {}
        this._listener = null;
        const out = this._samples ?? [];
        this._samples = null;
        return out;
    }

    _onInsert(event) {
        if (!this._samples || this._samples.length > 200000)
            return;
        // detail2 is how many characters came in: typing is one (or a few).
        if (event.detail2 < 1 || event.detail2 > MAX_TYPED)
            return;
        try {
            const where = this._caretOnScreen(event.source);
            if (where)
                this._samples.push([GLib.get_monotonic_time(), Math.round(where[0]), Math.round(where[1])]);
        } catch (e) {}
    }

    _caretOnScreen(accessible) {
        // Only the focused text field of the focused window (as the magnifier).
        let iter = accessible, window = null;
        while (iter) {
            const role = iter.get_role();
            if (role === Atspi.Role.APPLICATION) {
                if (iter.get_name() === 'gnome-shell')
                    return null;
                break;
            }
            if (TOPLEVEL.has(role))
                window = iter;
            iter = iter.get_parent();
        }
        if (!window || !window.get_state_set().contains(Atspi.StateType.ACTIVE) ||
            !accessible.get_state_set().contains(Atspi.StateType.FOCUSED))
            return null;
        const text = accessible.get_text_iface();
        if (!text)
            return null;
        const offset = text.get_caret_offset();
        let box = text.get_character_extents(offset, Atspi.CoordType.WINDOW);
        let after = false;
        if (box.width === 0 && box.height === 0 && offset > 0) {
            // At the end of the text: the character just typed, then step past it.
            box = text.get_character_extents(offset - 1, Atspi.CoordType.WINDOW);
            after = true;
        }
        if (box.width === 0 && box.height === 0)
            return null;
        const focus = global.display.focus_window;
        if (!focus)
            return null;
        const rect = focus.get_client_content_rect();
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const x = rect.x + scale * (box.x + (after ? box.width : 0));
        const y = rect.y + scale * (box.y + box.height / 2);
        return [x, y];
    }
}
