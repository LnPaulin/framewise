// Remembers where the camera bubble was put and puts it back there whenever
// it appears again (a new window would otherwise open in the middle of the
// screen: Wayland apps can't place their own windows, but the Shell can).
// The spot is kept in ~/.local/share/framewise/camera-position.json.

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
    start() {
        this._windows = new Map();     // window -> [signal ids]
        this._createdId = global.display.connect('window-created', (_d, win) => this._watch(win));
        for (const actor of global.get_window_actors())
            this._watch(actor.meta_window);
    }

    stop() {
        if (this._createdId)
            global.display.disconnect(this._createdId);
        this._createdId = 0;
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
            ids.push(win.connect('position-changed', () => this._remember(win)));
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
}

function workArea(index) {
    const ws = global.workspace_manager.get_active_workspace();
    return ws.get_work_area_for_monitor(Math.max(0, index));
}
