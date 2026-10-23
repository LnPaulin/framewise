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

import {prettyKeys, shortcutFor} from './keys.js';

const APP_ID = 'com.gitcotech.Framewise';
const APP_PATH = '/com/gitcotech/Framewise';

// [app action, label, icon, shortcut id]
const ITEMS = [
    ['record', 'Record Screen', 'media-record-symbolic', 'framewise-record'],
    ['screenshot', 'Take Screenshot', 'applets-screenshooter-symbolic', 'framewise-screenshot'],
    ['text', 'Capture Text', 'edit-copy-symbolic', 'framewise-text'],
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
            if (open)
                this._refresh();
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
