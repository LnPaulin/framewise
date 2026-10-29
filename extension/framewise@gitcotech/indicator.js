// The Framewise icon at the top right, with a menu of everything it does.
// Like a menu-bar app, it is there while the Framewise app runs and goes away
// when it quits (extension.js watches the app). Choosing an item asks the
// app over D-Bus; the clipboard list opens right here. "Show in the top bar"
// in Framewise Settings hides it.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Slider} from 'resource:///org/gnome/shell/ui/slider.js';

import * as audio from './audio.js';
import {prettyKeys, shortcutFor} from './keys.js';

const APP_ID = 'com.gitcotech.Framewise';
const APP_PATH = '/com/gitcotech/Framewise';

// [app action, label, icon, shortcut id]
const ITEMS = [
    ['record', 'Record Screen', 'media-record-symbolic', 'framewise-record'],
    ['screenshot', 'Take Screenshot', 'applets-screenshooter-symbolic', 'framewise-screenshot'],
    ['text', 'Capture Text', 'edit-copy-symbolic', 'framewise-text'],
    ['scroll', 'Scrolling Screenshot', 'go-down-symbolic', 'framewise-scroll'],
    ['color', 'Pick a Color', 'color-select-symbolic', 'framewise-color'],
    ['dictate', 'Dictate', 'audio-input-microphone-symbolic', 'framewise-dictate'],
    ['draw', 'Draw on Screen', 'document-edit-symbolic', 'framewise-draw'],
    ['clipboard', 'Clipboard History', 'edit-paste-symbolic', 'framewise-clipboard'],
    ['camera', 'Show Camera', 'camera-web-symbolic', 'framewise-camera'],
];
const CAMERA_TITLE = 'framewise camera';

export const Indicator = GObject.registerClass(
class FramewiseIndicator extends PanelMenu.Button {
    _init(extension, onClipboard, isRecording) {
        super._init(0.5, 'Framewise', false);
        this._isRecording = isRecording;
        const gicon = Gio.icon_new_for_string(`${extension.path}/icons/framewise-symbolic.svg`);
        this.add_child(new St.Icon({gicon, style_class: 'system-status-icon'}));

        this._items = {};
        for (const [action, label, icon, shortcut] of ITEMS) {
            const item = new PopupMenu.PopupImageMenuItem(label, icon);
            // The menu's own text colour, faded: readable in light and dark menus.
            const keys = new St.Label({style_class: 'framewise-menu-keys', x_expand: true,
                x_align: Clutter.ActorAlign.END, y_align: Clutter.ActorAlign.CENTER, opacity: 140});
            item.add_child(keys);
            item.connect('activate', () => {
                if (action === 'clipboard')
                    onClipboard();
                else
                    this._run(action);
            });
            this.menu.addMenuItem(item);
            this._items[action] = {item, keys, label, shortcut};
        }
        // Apps playing sound, each with the device it plays on (filled when the menu opens).
        this._sound = new PopupMenu.PopupMenuSection();
        this._soundRound = 0;
        this.menu.addMenuItem(this._sound);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const settings = new PopupMenu.PopupImageMenuItem('Settings', 'emblem-system-symbolic');
        settings.connect('activate', () => this._run('preferences'));
        this.menu.addMenuItem(settings);
        // Quit closes the app; this icon goes with it.
        this._quitSeparator = new PopupMenu.PopupSeparatorMenuItem();
        this.menu.addMenuItem(this._quitSeparator);
        this._quit = new PopupMenu.PopupImageMenuItem('Quit Framewise', 'application-exit-symbolic');
        this._quit.connect('activate', () => this._run('quit-app'));
        this.menu.addMenuItem(this._quit);

        this.menu.connect('open-state-changed', (_m, open) => {
            if (open) {
                this._refresh();
                this._refreshSound();
            }
        });
    }

    // Shortcuts can change in Settings, and Record becomes Stop while recording.
    _refresh() {

        for (const [action, {item, keys, label, shortcut}] of Object.entries(this._items)) {
            keys.text = prettyKeys(shortcutFor(shortcut));
            if (action === 'record')
                item.label.text = this._isRecording() ? 'Stop Recording' : label;
            if (action === 'camera') {
                const open = global.display.list_all_windows().some(w => w.get_title() === CAMERA_TITLE);
                item.label.text = open ? 'Hide Camera' : label;
            }
        }
    }

    // ---- per-app sound output (audio.js) ---------------------------------------

    async _refreshSound() {
        const round = ++this._soundRound;
        let state = null;
        try {
            state = audio.available() ? await audio.list() : null;
        } catch (e) {
            state = null;
        }
        if (round !== this._soundRound)
            return;                     // the menu was opened again meanwhile
        this._sound.removeAll();
        if (!state || !state.apps.length)
            return;                     // nothing playing
        this._sound.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Apps Playing Sound'));
        const chosen = audio.remembered();
        for (const app of state.apps) {
            const current = state.sinks.find(s => s.id === app.sinkId);
            const item = new PopupMenu.PopupSubMenuMenuItem(app.name, true);
            item.icon.gicon = Gio.ThemedIcon.new_with_default_fallbacks(app.icon || 'audio-speakers-symbolic');
            const where = new St.Label({text: current?.label ?? '', style_class: 'framewise-menu-keys',
                y_align: Clutter.ActorAlign.CENTER, opacity: 140});
            item.insert_child_above(where, item.label);
            item.menu.addMenuItem(this._volumeItem(app));
            if (state.sinks.length < 2) {
                this._sound.addMenuItem(item);
                continue;               // one device: only the volume to set
            }
            item.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Play On'));
            for (const sink of state.sinks) {
                const choice = new PopupMenu.PopupMenuItem(sink.label);
                choice.setOrnament(sink.id === app.sinkId ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
                choice.connect('activate', () => this._sendApp(app, sink));
                item.menu.addMenuItem(choice);
            }
            const follow = new PopupMenu.PopupMenuItem('Same as Other Apps');
            follow.setOrnament(chosen[app.key] ? PopupMenu.Ornament.NONE : PopupMenu.Ornament.CHECK);
            follow.connect('activate', () => this._sendApp(app, null));
            item.menu.addMenuItem(follow);
            this._sound.addMenuItem(item);
        }
    }

    // A volume slider for one app, up to 150% (past 100% boosts a quiet app).
    _volumeItem(app) {
        const item = new PopupMenu.PopupBaseMenuItem({activate: false});
        item.add_child(new St.Icon({icon_name: 'audio-volume-high-symbolic', style_class: 'popup-menu-icon'}));
        const slider = new Slider(0);
        slider.maximum_value = audio.MAX_VOLUME;
        slider.overdrive_start = 1;
        slider.x_expand = true;
        const percent = new St.Label({style_class: 'framewise-menu-keys', y_align: Clutter.ActorAlign.CENTER,
            opacity: 160, style: 'min-width: 3.2em; text-align: right;'});
        item.add_child(slider);
        item.add_child(percent);
        let ready = false;
        let timer = 0;
        audio.getVolume(app).then(v => {
            slider.value = Math.min(audio.MAX_VOLUME, v);
            percent.text = `${Math.round(v * 100)}%`;
            ready = true;
        }).catch(() => {
            ready = true;
        });
        slider.connect('notify::value', () => {
            percent.text = `${Math.round(slider.value * 100)}%`;
            if (!ready)
                return;
            if (timer)
                GLib.source_remove(timer);
            timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 60, () => {
                timer = 0;
                audio.setVolume(app, slider.value).catch(e => console.error(`framewise volume: ${e}`));
                return GLib.SOURCE_REMOVE;
            });
        });
        item.connect('destroy', () => timer && GLib.source_remove(timer));
        return item;
    }

    async _sendApp(app, sink) {
        try {
            if (sink)
                await audio.moveApp(app, sink);
            else
                await audio.followDefault(app);
            audio.remember(app.key, sink?.name ?? null);
        } catch (e) {
            Main.notify('Framewise', `Couldn't move ${app.name}'s sound: ${e.message}`);
        }
    }

    _appRunning() {
        try {
            const reply = Gio.DBus.session.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                'org.freedesktop.DBus', 'NameHasOwner', new GLib.Variant('(s)', [APP_ID]),
                new GLib.VariantType('(b)'), Gio.DBusCallFlags.NONE, 500, null);
            return reply.deepUnpack()[0];
        } catch (e) {
            return false;
        }
    }

    // Asks the app (starting it if needed) to run one of its actions.
    _run(action) {
        // Let the menu close first, so it isn't in a screenshot.
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 180, () => {
            Gio.DBus.session.call(APP_ID, APP_PATH, 'org.freedesktop.Application', 'ActivateAction',
                new GLib.Variant('(sava{sv})', [action, [], {}]), null, Gio.DBusCallFlags.NONE, -1, null,
                (conn, result) => {
                    try {
                        conn.call_finish(result);
                    } catch (e) {
                        Main.notify('Framewise', `Couldn't start Framewise: ${e.message}`);
                    }
                });
            return GLib.SOURCE_REMOVE;
        });
    }
});
