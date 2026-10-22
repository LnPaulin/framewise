// Remembers where the camera bubble was put and puts it back there whenever
// it appears again (a new window would otherwise open in the middle of the
// screen: Wayland apps can't place their own windows, but the Shell can).
// The spot is kept in ~/.local/share/framewise/camera-position.json.
//
// It also tells the app which corner of the screen the bubble is nearest
// (its resize handle and the Corner shape's curve face the other way, into
// the screen), and after a resize trims the window to the camera's shape,
// keeping that screen corner in place.

import GLib from 'gi://GLib';
import Mtk from 'gi://Mtk';

const TITLE = 'framewise camera';
const FILE = GLib.build_filenamev([GLib.get_user_data_dir(), 'framewise', 'camera-position.json']);

function load() {
    try {
        const [ok, bytes] = GLib.file_get_contents(FILE);
        if (!ok)
            return null;
        const p = JSON.parse(new TextDecoder().decode(bytes));
        return Number.isFinite(p.x) && Number.isFinite(p.y) ? p : null;
    } catch (e) {
        return null;
    }
}

function save(x, y) {
    try {
        GLib.mkdir_with_parents(GLib.path_get_dirname(FILE), 0o700);
        GLib.file_set_contents(FILE, new TextEncoder().encode(JSON.stringify({x, y})));
    } catch (e) {}
}

export class BubblePlacement {
    // onCorner('top-left' | 'top-right' | 'bottom-left' | 'bottom-right')
    constructor(onCorner) {
        this._onCorner = onCorner;
        this.corner = 'bottom-right';
        this._aspect = null;           // [width / height, margin] of the camera picture
    }

    start() {
        this._windows = new Map();     // window -> [signal ids]
        this._createdId = global.display.connect('window-created', (_d, win) => this._watch(win));
        this._grabEndId = global.display.connect('grab-op-end', (_d, win) => {
            if (win && win.get_title() === TITLE)
                this._fit(win);
        });
        for (const actor of global.get_window_actors())
            this._watch(actor.meta_window);
    }

    stop() {
        if (this._createdId)
            global.display.disconnect(this._createdId);
        this._createdId = 0;
        if (this._grabEndId)
            global.display.disconnect(this._grabEndId);
        this._grabEndId = 0;
        for (const [win, ids] of this._windows)
            ids.forEach(id => win.disconnect(id));
        this._windows.clear();
        if (this._saveId)
            GLib.source_remove(this._saveId);
        this._saveId = 0;
    }

    _watch(win) {
        if (!win || this._windows.has(win))
            return;
        const ids = [];
        // The title can arrive just after the window: check on both.
        const check = () => {
            if (win.get_title() !== TITLE || win._framewisePlaced)
                return;
            win._framewisePlaced = true;
            this._place(win);
            ids.push(win.connect('position-changed', () => {
                this._remember(win);
                this._updateCorner(win);
            }));
            ids.push(win.connect('size-changed', () => this._updateCorner(win)));
            this._updateCorner(win);
        };
        ids.push(win.connect('notify::title', check));
        ids.push(win.connect('shown', check));
        ids.push(win.connect('unmanaged', () => {
            ids.forEach(id => win.disconnect(id));
            this._windows.delete(win);
        }));
        this._windows.set(win, ids);
        check();
    }

    _place(win) {
        const spot = load();
        if (!spot)
            return;
        // Back where it was, kept fully on a screen that still exists.
        const rect = win.get_frame_rect();
        const w = rect.width || 260, h = rect.height || 260;
        const index = global.display.get_monitor_index_for_rect(
            new Mtk.Rectangle({x: spot.x, y: spot.y, width: w, height: h}));
        const area = workArea(index);
        const x = Math.max(area.x, Math.min(spot.x, area.x + area.width - w));
        const y = Math.max(area.y, Math.min(spot.y, area.y + area.height - h));
        this._placing = true;
        win.move_frame(true, x, y);
        // Some windows get placed again right after mapping: repeat once.
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 120, () => {
            if (win.get_compositor_private())
                win.move_frame(true, x, y);
            this._placing = false;
            return GLib.SOURCE_REMOVE;
        });
    }

    _remember(win) {
        if (this._placing)
            return;
        if (this._saveId)
            GLib.source_remove(this._saveId);
        this._saveId = GLib.timeout_add(GLib.PRIORITY_LOW, 400, () => {
            this._saveId = 0;
            const r = win.get_frame_rect();
            if (win.get_compositor_private())
                save(r.x, r.y);
            return GLib.SOURCE_REMOVE;
        });
    }

    // The camera picture's width / height, and the transparent margin around it
    // (for its shadow), so a resized window can be trimmed to fit it.
    setAspect(ratio, margin) {
        this._aspect = ratio > 0.1 && ratio < 10 ? [ratio, Math.max(0, Math.min(100, margin))] : null;
        const win = global.display.list_all_windows().find(w => w.get_title() === TITLE);
        if (win)
            this._fit(win);
    }

    _updateCorner(win) {
        const r = win.get_frame_rect();
        const area = workArea(win.get_monitor());
        const right = r.x + r.width / 2 >= area.x + area.width / 2;
        const bottom = r.y + r.height / 2 >= area.y + area.height / 2;
        const corner = `${bottom ? 'bottom' : 'top'}-${right ? 'right' : 'left'}`;
        if (corner !== this.corner) {
            this.corner = corner;
            this._onCorner?.(corner);
        }
    }

    // Window and picture shapes differ after a free resize: shrink the window
    // to the picture, keeping the corner nearest the screen edge where it is.
    _fit(win) {
        if (!this._aspect || win.is_fullscreen())
            return;
        const [ratio, margin] = this._aspect;
        const r = win.get_frame_rect();
        let w = r.width - 2 * margin, h = r.height - 2 * margin;
        if (w < 8 || h < 8)
            return;
        if (w / h > ratio)
            w = Math.round(h * ratio);
        else
            h = Math.round(w / ratio);
        const width = w + 2 * margin, height = h + 2 * margin;
        if (Math.abs(width - r.width) < 2 && Math.abs(height - r.height) < 2)
            return;
        const x = this.corner.endsWith('right') ? r.x + r.width - width : r.x;
        const y = this.corner.startsWith('bottom') ? r.y + r.height - height : r.y;
        win.move_resize_frame(true, x, y, width, height);
    }
}

function workArea(index) {
    const ws = global.workspace_manager.get_active_workspace();
    return ws.get_work_area_for_monitor(Math.max(0, index));
}
