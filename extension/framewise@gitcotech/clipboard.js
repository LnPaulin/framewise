// Clipboard history: while Framewise runs, every copy (text, pictures, copied files) is kept in
// ~/.local/share/framewise/clipboard, and Shift+Super+V opens a searchable
// list that pastes the chosen entry into the window you were using.
//
// On Wayland only the Shell can see every copy, which is why this lives in
// the extension rather than the app. Copies a password manager marks as
// secret are never kept. Settings come from framewise's settings.json.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GdkPixbuf from 'gi://GdkPixbuf';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {vbox} from './compat.js';

const DATA_DIR = GLib.build_filenamev([GLib.get_user_data_dir(), 'framewise', 'clipboard']);
const HISTORY_FILE = GLib.build_filenamev([DATA_DIR, 'history.json']);
const SETTINGS_FILE = GLib.build_filenamev([GLib.get_user_config_dir(), 'framewise', 'settings.json']);

const DEFAULTS = {
    clipboard_history: true,
    clipboard_limit: 100,          // unpinned entries kept; 0 = no limit
    clipboard_images: true,        // keep pictures and copied files too
    clipboard_retention_days: 0,   // 0 = keep until the limit pushes them out
    clipboard_skip_sensitive: false,
};
const MAX_TEXT = 1000000;               // characters
const MAX_IMAGE_BYTES = 40 * 1024 * 1024;
const MAX_ROWS = 300;                   // shown at once; search reaches the rest
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp', 'image/tiff'];
// Password managers (KeePassXC and others) mark secrets with this type.
const SECRET_HINTS = ['x-kde-passwordManagerHint', 'application/x-kde-passwordManagerHint'];
const TERMINALS = ['terminal', 'ptyxis', 'kgx', 'console', 'kitty', 'alacritty', 'konsole', 'tilix',
    'wezterm', 'foot', 'ghostty', 'xterm', 'blackbox', 'terminator'];

const CLIPBOARD = St.ClipboardType.CLIPBOARD;

function readJson(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? JSON.parse(new TextDecoder().decode(bytes)) : null;
    } catch (e) {
        return null;
    }
}

function newId() {
    return `${Date.now().toString(36)}${Math.floor(Math.random() * 1e6).toString(36)}`;
}

// Text that looks like a password, a key or a token (only when that option is on).
export function looksSensitive(text) {
    const t = text.trim();
    if (!t || t.includes('\n') || t.length < 8 || t.length > 200)
        return false;
    if (/^(sk-|ghp_|gho_|github_pat_|glpat-|xox[abp]-|AKIA|AIza|eyJ)[A-Za-z0-9_\-.]{8,}/.test(t))
        return true;
    if (/\s/.test(t) || /^https?:\/\//.test(t) || t.length > 64)
        return false;
    const kinds = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter(r => r.test(t)).length;
    return kinds >= 3 && !/^[\w.+-]+@[\w-]+\.[\w.]+$/.test(t);
}

export function timeAgo(ms, now = Date.now()) {
    const s = Math.max(0, (now - ms) / 1000);
    if (s < 60)
        return 'now';
    if (s < 3600)
        return `${Math.floor(s / 60)} min`;
    if (s < 86400)
        return `${Math.floor(s / 3600)} h`;
    return `${Math.floor(s / 86400)} d`;
}

// ---- the history -----------------------------------------------------------------

export class ClipboardHistory {
    constructor() {
        this.entries = [];       // newest first; {id, kind, time, pinned, text | image,mime,hash,w,h | files}
        this.paused = false;
        this.recording = false;  // only while the Framewise app runs
        this._saveId = 0;
        this._settings = {...DEFAULTS};
        this._listeners = new Set();
    }

    start() {
        GLib.mkdir_with_parents(DATA_DIR, 0o700);
        this._loadSettings();
        const saved = readJson(HISTORY_FILE);
        // Only well-formed entries; a picture's name must be a plain file name
        // here (the file is deleted with its entry, so never follow a path).
        this.entries = Array.isArray(saved?.entries) ? saved.entries.filter(e => e && e.id && e.kind &&
            (e.kind !== 'image' || (typeof e.image === 'string' && /^[A-Za-z0-9]+\.[a-z]{2,4}$/.test(e.image)))) : [];
        this._trim();
        this._settingsMonitor = Gio.File.new_for_path(SETTINGS_FILE).monitor_file(Gio.FileMonitorFlags.NONE, null);
        this._settingsMonitor.connect('changed', () => this._loadSettings());
        this._selection = global.display.get_selection();
        this._ownerId = this._selection.connect('owner-changed', (_sel, type, source) => {
            if (type === Meta.SelectionType.SELECTION_CLIPBOARD && source)
                this._captureSoon();
        });
        // Entries also age out while nothing new is copied.
        this._hourly = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, 3600, () => {
            if (this._trim())
                this._changed();
            return GLib.SOURCE_CONTINUE;
        });
    }

    stop() {
        if (this._ownerId)
            this._selection.disconnect(this._ownerId);
        this._ownerId = 0;
        this._settingsMonitor?.cancel();
        this._settingsMonitor = null;
        for (const id of [this._hourly, this._captureId])
            if (id)
                GLib.source_remove(id);
        this._hourly = this._captureId = 0;
        if (this._saveId) {
            GLib.source_remove(this._saveId);
            this._saveId = 0;
            this._save();
        }
        this._listeners.clear();
    }

    setRecording(on) {
        this.recording = on;
    }

    get enabled() {
        return !!this._settings.clipboard_history;
    }

    onChange(fn) {
        this._listeners.add(fn);
        return () => this._listeners.delete(fn);
    }

    _loadSettings() {
        const s = readJson(SETTINGS_FILE) ?? {};
        this._settings = {...DEFAULTS};
        for (const key of Object.keys(DEFAULTS))
            if (key in s && s[key] !== null)
                this._settings[key] = s[key];
        if (this._trim())
            this._changed();
    }

    // ---- capturing ---------------------------------------------------------

    _captureSoon() {
        // Apps often set the clipboard several times in a row; read the last.
        if (this._captureId)
            GLib.source_remove(this._captureId);
        this._captureId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 80, () => {
            this._captureId = 0;
            this._capture();
            return GLib.SOURCE_REMOVE;
        });
    }

    _capture() {
        if (!this.enabled || this.paused || !this.recording)
            return;
        const types = this._selection.get_mimetypes(Meta.SelectionType.SELECTION_CLIPBOARD) ?? [];
        if (!types.length || types.some(t => SECRET_HINTS.includes(t)))
            return;
        const clip = St.Clipboard.get_default();
        const media = !!this._settings.clipboard_images;
        const hasText = types.some(t => t.startsWith('text/plain') || t === 'UTF8_STRING' || t === 'STRING');
        if (media && (types.includes('x-special/gnome-copied-files') || types.includes('text/uri-list'))) {
            const mime = types.includes('x-special/gnome-copied-files') ? 'x-special/gnome-copied-files' : 'text/uri-list';
            clip.get_content(CLIPBOARD, mime, (_c, bytes) => {
                const files = this._parseFiles(bytes);
                if (files.length)
                    this._add({kind: 'files', files});
                else if (hasText)
                    this._captureText(clip);
            });
            return;
        }
        if (hasText) {
            this._captureText(clip);
            return;
        }
        const imageMime = IMAGE_TYPES.find(t => types.includes(t));
        if (media && imageMime)
            clip.get_content(CLIPBOARD, imageMime, (_c, bytes) => this._captureImage(bytes, imageMime));
    }

    _captureText(clip) {
        clip.get_text(CLIPBOARD, (_c, text) => {
            if (!text || !text.trim() || text.length > MAX_TEXT)
                return;
            if (this._settings.clipboard_skip_sensitive && looksSensitive(text))
                return;
            this._add({kind: 'text', text});
        });
    }

    _parseFiles(bytes) {
        if (!bytes || bytes.get_size() === 0)
            return [];
        const lines = new TextDecoder().decode(bytes.get_data()).split(/\r?\n/)
            .map(l => l.trim()).filter(l => l && !l.startsWith('#') && l !== 'copy' && l !== 'cut');
        const files = [];
        for (const line of lines) {
            if (!line.startsWith('file://'))
                return [];     // web links are kept as text
            try {
                files.push(GLib.filename_from_uri(line)[0]);
            } catch (e) {
                return [];
            }
        }
        return files;
    }

    _captureImage(bytes, mime) {
        if (!bytes || bytes.get_size() === 0 || bytes.get_size() > MAX_IMAGE_BYTES)
            return;
        const hash = GLib.compute_checksum_for_bytes(GLib.ChecksumType.SHA256, bytes);
        const same = this.entries.find(e => e.kind === 'image' && e.hash === hash);
        if (same) {
            this._add({kind: 'image', hash});
            return;
        }
        const ext = mime.split('/')[1].replace('jpeg', 'jpg');
        const name = `${newId()}.${ext}`;
        const path = GLib.build_filenamev([DATA_DIR, name]);
        try {
            GLib.file_set_contents_full(path, bytes.get_data(), GLib.FileSetContentsFlags.NONE, 0o600);
        } catch (e) {
            return;
        }
        let w = 0, h = 0;
        try {
            [, w, h] = GdkPixbuf.Pixbuf.get_file_info(path);
        } catch (e) {}
        this._add({kind: 'image', image: name, mime, hash, w, h});
    }

    // ---- changing the list ----------------------------------------------------

    _sameAs(a, b) {
        if (a.kind !== b.kind)
            return false;
        if (a.kind === 'text')
            return a.text === b.text;
        if (a.kind === 'image')
            return a.hash === b.hash;
        return JSON.stringify(a.files) === JSON.stringify(b.files);
    }

    _add(entry) {
        const old = this.entries.findIndex(e => this._sameAs(e, entry));
        let item;
        if (old >= 0) {
            // Copied again: move it to the top, keeping its pin.
            [item] = this.entries.splice(old, 1);
            item.time = Date.now();
        } else {
            item = {id: newId(), time: Date.now(), pinned: false, ...entry};
        }
        this.entries.unshift(item);
        this._trim();
        this._changed();
    }

    // Drops what the limit and the retention window no longer allow.
    _trim() {
        const limit = Number(this._settings.clipboard_limit) || 0;
        const days = Number(this._settings.clipboard_retention_days) || 0;
        const oldest = days > 0 ? Date.now() - days * 86400000 : 0;
        let kept = 0;
        const removed = [];
        this.entries = this.entries.filter(e => {
            if (e.pinned)
                return true;
            if ((limit > 0 && kept >= limit) || e.time < oldest) {
                removed.push(e);
                return false;
            }
            kept++;
            return true;
        });
        removed.forEach(e => this._deleteFiles(e));
        return removed.length > 0;
    }

    _deleteFiles(entry) {
        if (entry.kind !== 'image' || !entry.image || this.entries.some(e => e.image === entry.image))
            return;
        try {
            Gio.File.new_for_path(GLib.build_filenamev([DATA_DIR, entry.image])).delete(null);
        } catch (e) {}
    }

    imagePath(entry) {
        return GLib.build_filenamev([DATA_DIR, entry.image]);
    }

    remove(id) {
        const i = this.entries.findIndex(e => e.id === id);
        if (i < 0)
            return;
        const [entry] = this.entries.splice(i, 1);
        this._deleteFiles(entry);
        this._changed();
    }

    togglePin(id) {
        const entry = this.entries.find(e => e.id === id);
        if (entry) {
            entry.pinned = !entry.pinned;
            this._trim();
            this._changed();
        }
    }

    clearUnpinned() {
        const gone = this.entries.filter(e => !e.pinned);
        this.entries = this.entries.filter(e => e.pinned);
        gone.forEach(e => this._deleteFiles(e));
        this._changed();
    }

    // Puts an entry back on the clipboard. False if its content is gone.
    copy(entry) {
        const clip = St.Clipboard.get_default();
        if (entry.kind === 'text') {
            clip.set_text(CLIPBOARD, entry.text);
        } else if (entry.kind === 'image') {
            try {
                const [ok, data] = GLib.file_get_contents(this.imagePath(entry));
                if (!ok)
                    return false;
                clip.set_content(CLIPBOARD, entry.mime || 'image/png', new GLib.Bytes(data));
            } catch (e) {
                return false;
            }
        } else {
            const files = entry.files.filter(f => GLib.file_test(f, GLib.FileTest.EXISTS));
            if (!files.length)
                return false;
            const uris = files.map(f => GLib.filename_to_uri(f, null)).join('\r\n');
            clip.set_content(CLIPBOARD, 'text/uri-list', new GLib.Bytes(new TextEncoder().encode(`${uris}\r\n`)));
        }
        return true;
    }

    _changed() {
        if (!this._saveId) {
            this._saveId = GLib.timeout_add(GLib.PRIORITY_LOW, 800, () => {
                this._saveId = 0;
                this._save();
                return GLib.SOURCE_REMOVE;
            });
        }
        for (const fn of this._listeners)
            fn();
    }

    _save() {
        try {
            GLib.mkdir_with_parents(DATA_DIR, 0o700);
            const json = JSON.stringify({version: 1, entries: this.entries});
            GLib.file_set_contents_full(HISTORY_FILE, new TextEncoder().encode(json),
                GLib.FileSetContentsFlags.CONSISTENT, 0o600);
        } catch (e) {
            console.error(`framewise: could not save the clipboard history: ${e.message}`);
        }
    }
}

// ---- the picker ------------------------------------------------------------------

function entryLabel(entry) {
    if (entry.kind === 'text') {
        const lines = entry.text.split('\n').map(l => l.replace(/\s+/g, ' ').trim()).filter(Boolean);
        const shown = lines.slice(0, 2).map(l => (l.length > 160 ? `${l.slice(0, 160)}…` : l));
        if (lines.length > 2)
            shown[1] += `  (+${lines.length - 2} ${lines.length === 3 ? "line" : "lines"})`;
        return shown.join('\n');
    }
    if (entry.kind === 'image')
        return entry.w ? `Picture  ${entry.w} × ${entry.h}` : 'Picture';
    const names = entry.files.map(f => GLib.path_get_basename(f));
    const head = names.slice(0, 3).join(', ');
    return names.length > 3 ? `${head} and ${names.length - 3} more` : head;
}

function searchText(entry) {
    if (entry.kind === 'text')
        return entry.text.toLowerCase();
    if (entry.kind === 'image')
        return 'picture image';
    return `file ${entry.files.join(' ').toLowerCase()}`;
}

export class ClipboardPicker {
    constructor(history) {
        this._history = history;
        this._rows = [];
        this._selected = 0;
        this._clearArmed = false;
        // Made now: a virtual keyboard drops the first keys sent right after
        // it is created, which would lose the first paste.
        const backend = global.stage.context?.get_backend?.() ?? Clutter.get_default_backend();
        this._keyboard = backend.get_default_seat().create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
    }

    get isOpen() {
        return !!this._backdrop;
    }

    open() {
        if (this._backdrop) {
            this.close();
            return;
        }
        this._target = global.display.focus_window;
        const monitor = global.display.get_current_monitor();
        const area = Main.layoutManager.getWorkAreaForMonitor(monitor);

        this._backdrop = new St.Widget({reactive: true, x: 0, y: 0,
            width: global.stage.width, height: global.stage.height});
        this._panel = vbox({style_class: 'framewise-clip',
            reactive: true, width: Math.min(620, area.width - 40)});
        this._backdrop.add_child(this._panel);

        const top = new St.BoxLayout({style_class: 'framewise-clip-top'});
        this._search = new St.Entry({style_class: 'framewise-clip-search', hint_text: 'Search clipboard history',
            can_focus: true, x_expand: true});
        this._search.set_primary_icon(new St.Icon({icon_name: 'edit-find-symbolic', icon_size: 16}));
        top.add_child(this._search);
        this._pauseButton = new St.Button({style_class: 'framewise-clip-tool', can_focus: false});
        this._pauseButton.connect('clicked', () => {
            this._history.paused = !this._history.paused;
            this._updateFooter();
        });
        top.add_child(this._pauseButton);
        this._panel.add_child(top);

        this._list = vbox({style_class: 'framewise-clip-list'});
        this._scroll = new St.ScrollView({style_class: 'framewise-clip-scroll', x_expand: true, y_expand: true,
            hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC});
        this._scroll.set_child(this._list);
        this._scroll.style = `max-height: ${Math.round(area.height * 0.62)}px;`;
        this._panel.add_child(this._scroll);

        const foot = new St.BoxLayout({style_class: 'framewise-clip-foot'});
        this._hint = new St.Label({style_class: 'framewise-clip-hint', x_expand: true,
            y_align: Clutter.ActorAlign.CENTER});
        foot.add_child(this._hint);
        this._clearButton = new St.Button({style_class: 'framewise-clip-tool', label: 'Clear', can_focus: false});
        this._clearButton.connect('clicked', () => this._clear());
        foot.add_child(this._clearButton);
        this._panel.add_child(foot);

        Main.uiGroup.add_child(this._backdrop);
        this._grab = Main.pushModal(this._backdrop);
        this._backdrop.connect('button-press-event', (_a, event) => {
            const actor = global.stage.get_event_actor(event);
            if (!this._panel.contains(actor))
                this.close();
            return Clutter.EVENT_PROPAGATE;
        });
        this._search.clutter_text.connect('text-changed', () => {
            this._selected = 0;
            this._fill();
        });
        this._search.clutter_text.connect('key-press-event', (_a, event) => this._key(event));
        global.stage.set_key_focus(this._search.clutter_text);
        this._unwatch = this._history.onChange(() => this._fill());

        this._fill();
        this._updateFooter();
        const [, natH] = this._panel.get_preferred_height(this._panel.width);
        this._panel.set_position(area.x + Math.round((area.width - this._panel.width) / 2),
            area.y + Math.max(20, Math.round((area.height - natH) / 3)));
        this._panel.opacity = 0;
        this._panel.scale_x = this._panel.scale_y = 0.97;
        this._panel.set_pivot_point(0.5, 0.2);
        this._panel.ease({opacity: 255, scale_x: 1, scale_y: 1, duration: 120,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD});
    }

    close() {
        this._unwatch?.();
        this._unwatch = null;
        if (this._grab) {
            Main.popModal(this._grab);
            this._grab = null;
        }
        this._backdrop?.destroy();
        this._backdrop = null;
        this._rows = [];
    }

    _visibleEntries() {
        const query = this._search.text.trim().toLowerCase();
        const all = this._history.entries;
        const match = query ? all.filter(e => searchText(e).includes(query)) : all;
        // Pinned first, each part newest first.
        return [...match.filter(e => e.pinned), ...match.filter(e => !e.pinned)].slice(0, MAX_ROWS);
    }

    _fill() {
        if (!this._list)
            return;
        this._list.destroy_all_children();
        this._rows = [];
        const entries = this._visibleEntries();
        if (!entries.length) {
            let text = this._search.text.trim() ? 'No matches' : 'Clipboard history is empty';
            if (!this._history.enabled)
                text = 'Clipboard history is off. Turn it on in Framewise Settings.';
            this._list.add_child(new St.Label({text, style_class: 'framewise-clip-empty',
                x_align: Clutter.ActorAlign.CENTER}));
        }
        let section = null;
        const now = Date.now();
        entries.forEach((entry, i) => {
            const part = entry.pinned ? 'Pinned' : 'Recent';
            if (part !== section) {
                section = part;
                if (entries.some(e => e.pinned))
                    this._list.add_child(new St.Label({text: part, style_class: 'framewise-clip-section'}));
            }
            const row = this._row(entry, i, now);
            this._list.add_child(row);
            this._rows.push({row, entry});
        });
        this._selected = Math.min(this._selected, Math.max(0, this._rows.length - 1));
        this._highlight();
    }

    _row(entry, index, now) {
        const box = new St.BoxLayout({style_class: 'framewise-clip-row-box', x_expand: true});
        box.add_child(new St.Label({text: index < 9 ? `${index + 1}` : '', style_class: 'framewise-clip-num',
            y_align: Clutter.ActorAlign.CENTER}));
        if (entry.kind === 'image') {
            const thumb = new St.Bin({style_class: 'framewise-clip-thumb', y_align: Clutter.ActorAlign.CENTER});
            const uri = Gio.File.new_for_path(this._history.imagePath(entry)).get_uri();
            thumb.set_style(`background-image: url("${uri}");`);
            box.add_child(thumb);
        } else if (entry.kind === 'files') {
            box.add_child(new St.Icon({icon_name: entry.files.length > 1 ? 'folder-documents-symbolic' : 'text-x-generic-symbolic',
                icon_size: 20, style_class: 'framewise-clip-icon', y_align: Clutter.ActorAlign.CENTER}));
        }
        const label = new St.Label({text: entryLabel(entry), style_class: 'framewise-clip-text', x_expand: true,
            y_align: Clutter.ActorAlign.CENTER});
        label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        box.add_child(label);
        box.add_child(new St.Label({text: timeAgo(entry.time, now), style_class: 'framewise-clip-time',
            y_align: Clutter.ActorAlign.CENTER}));
        const pin = new St.Button({style_class: `framewise-clip-icon-button${entry.pinned ? ' pinned' : ''}`,
            child: new St.Icon({icon_name: 'view-pin-symbolic', icon_size: 14}),
            accessible_name: entry.pinned ? 'Unpin' : 'Pin', y_align: Clutter.ActorAlign.CENTER});
        pin.connect('clicked', () => this._history.togglePin(entry.id));
        box.add_child(pin);
        const del = new St.Button({style_class: 'framewise-clip-icon-button',
            child: new St.Icon({icon_name: 'edit-delete-symbolic', icon_size: 14}),
            accessible_name: 'Remove', y_align: Clutter.ActorAlign.CENTER});
        del.connect('clicked', () => this._history.remove(entry.id));
        box.add_child(del);

        const row = new St.Button({style_class: 'framewise-clip-row', child: box, x_expand: true,
            can_focus: false, track_hover: true});
        row.connect('clicked', () => this._choose(index, true));
        row.connect('notify::hover', () => {
            if (row.hover && this._selected !== index) {
                this._selected = index;
                this._highlight(false);
            }
        });
        return row;
    }

    _highlight(scroll = true) {
        this._rows.forEach(({row}, i) => {
            if (i === this._selected)
                row.add_style_pseudo_class('selected');
            else
                row.remove_style_pseudo_class('selected');
        });
        const current = this._rows[this._selected]?.row;
        if (scroll && current) {
            const adj = this._scroll.vadjustment;
            const box = current.get_allocation_box();
            const page = adj.page_size;
            if (page > 0) {
                if (box.y1 < adj.value)
                    adj.value = box.y1;
                else if (box.y2 > adj.value + page)
                    adj.value = box.y2 - page;
            } else {
                // Not laid out yet: try again once it is.
                GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                    if (this._rows[this._selected]?.row === current && this._scroll.vadjustment.page_size > 0)
                        this._highlight();
                    return GLib.SOURCE_REMOVE;
                });
            }
        }
    }

    _updateFooter() {
        if (!this._hint)
            return;
        this._pauseButton.label = this._history.paused ? 'Resume' : 'Pause';
        this._hint.text = this._history.paused
            ? 'Paused: new items are not saved.'
            : 'Enter paste  ·  Ctrl+C copy  ·  Ctrl+P pin  ·  Ctrl+Del remove  ·  Ctrl+1–9';
        this._clearButton.label = this._clearArmed ? 'Clear Unpinned?' : 'Clear';
    }

    _clear() {
        if (!this._clearArmed) {
            this._clearArmed = true;
            this._updateFooter();
            GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 4, () => {
                this._clearArmed = false;
                this._updateFooter();
                return GLib.SOURCE_REMOVE;
            });
            return;
        }
        this._clearArmed = false;
        this._history.clearUnpinned();
        this._updateFooter();
    }

    _key(event) {
        const sym = event.get_key_symbol();
        const ctrl = (event.get_state() & Clutter.ModifierType.CONTROL_MASK) !== 0;
        const count = this._rows.length;
        const move = d => {
            if (count) {
                this._selected = Math.max(0, Math.min(count - 1, this._selected + d));
                this._highlight();
            }
            return Clutter.EVENT_STOP;
        };
        switch (sym) {
        case Clutter.KEY_Escape:
            this.close();
            return Clutter.EVENT_STOP;
        case Clutter.KEY_Down:
            return move(1);
        case Clutter.KEY_Up:
            return move(-1);
        case Clutter.KEY_Page_Down:
            return move(8);
        case Clutter.KEY_Page_Up:
            return move(-8);
        case Clutter.KEY_Return:
        case Clutter.KEY_KP_Enter:
            this._choose(this._selected, true);
            return Clutter.EVENT_STOP;
        }
        if (ctrl) {
            if (sym >= Clutter.KEY_1 && sym <= Clutter.KEY_9) {
                this._choose(sym - Clutter.KEY_1, true);
                return Clutter.EVENT_STOP;
            }
            const entry = this._rows[this._selected]?.entry;
            if ((sym === Clutter.KEY_c || sym === Clutter.KEY_C) && !this._search.clutter_text.get_selection()) {
                this._choose(this._selected, false);
                return Clutter.EVENT_STOP;
            }
            if ((sym === Clutter.KEY_p || sym === Clutter.KEY_P) && entry) {
                this._history.togglePin(entry.id);
                return Clutter.EVENT_STOP;
            }
            if ((sym === Clutter.KEY_Delete || sym === Clutter.KEY_BackSpace) && entry) {
                this._history.remove(entry.id);
                return Clutter.EVENT_STOP;
            }
        }
        return Clutter.EVENT_PROPAGATE;
    }

    // Copies the entry, closes, and (paste) types Ctrl+V into the window that
    // had the focus before the list opened.
    _choose(index, paste) {
        const entry = this._rows[index]?.entry;
        if (!entry)
            return;
        const target = this._target;
        const ok = this._history.copy(entry);
        this.close();
        if (!ok) {
            Main.notify('Framewise', 'This item is no longer available (the file was moved or deleted).');
            return;
        }
        if (!paste || !target)
            return;
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 120, () => {
            if (global.display.focus_window === target)
                this._sendPaste(this._isTerminal(target));
            return GLib.SOURCE_REMOVE;
        });
    }

    _isTerminal(win) {
        const names = [win.get_wm_class(), win.get_gtk_application_id(), win.get_sandboxed_app_id?.()]
            .filter(Boolean).join(' ').toLowerCase();
        return TERMINALS.some(t => names.includes(t));
    }

    _sendPaste(shift) {
        const k = this._keyboard;
        if (!k)
            return;
        const t = GLib.get_monotonic_time();
        const keys = shift ? [Clutter.KEY_Control_L, Clutter.KEY_Shift_L] : [Clutter.KEY_Control_L];
        keys.forEach(key => k.notify_keyval(t, key, Clutter.KeyState.PRESSED));
        k.notify_keyval(t, Clutter.KEY_v, Clutter.KeyState.PRESSED);
        k.notify_keyval(t, Clutter.KEY_v, Clutter.KeyState.RELEASED);
        keys.reverse().forEach(key => k.notify_keyval(t, key, Clutter.KeyState.RELEASED));
    }

    destroy() {
        this.close();
        this._keyboard?.run_dispose();
        this._keyboard = null;
    }
}
