// Only the Framewise app may use this extension's D-Bus interface.
//
// The interface can take screenshots, follow the pointer and list windows:
// things GNOME otherwise allows only with the person's consent. So every
// call (except Ping) must come from the process that owns the app's D-Bus
// name, and that process must be Framewise ("python3 ... -m framewise").
// GNOME guards its own screenshot service the same way (by bus name). A
// sandboxed app (Flatpak, Snap) can't own that name, so it is refused.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const OPEN = new Set(['Ping']);

function callBus(method, args, type) {
    return new Promise((resolve, reject) => {
        Gio.DBus.session.call('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            method, new GLib.Variant(type, args), null, Gio.DBusCallFlags.NONE, 2000, null,
            (conn, res) => {
                try {
                    resolve(conn.call_finish(res).deepUnpack()[0]);
                } catch (e) {
                    reject(e);
                }
            });
    });
}

function isFramewise(pid) {
    try {
        const [ok, bytes] = GLib.file_get_contents(`/proc/${pid}/cmdline`);
        if (!ok)
            return false;
        const args = new TextDecoder().decode(bytes).split('\0');
        const m = args.indexOf('-m');
        return m >= 0 && args[m + 1] === 'framewise' && /python3/.test(GLib.path_get_basename(args[0]));
    } catch (e) {
        return false;
    }
}

export class CallerGuard {
    constructor(appId) {
        this._appId = appId;
        this._trusted = new Set();     // unique bus names already checked
    }

    // The app's name changed hands (started, quit): check again next time.
    reset() {
        this._trusted.clear();
    }

    async allowed(method, sender) {
        if (OPEN.has(method))
            return true;
        if (this._trusted.has(sender))
            return true;
        try {
            const owner = await callBus('GetNameOwner', [this._appId], '(s)');
            if (owner !== sender)
                return false;
            const pid = await callBus('GetConnectionUnixProcessID', [sender], '(s)');
            if (!isFramewise(pid))
                return false;
        } catch (e) {
            return false;
        }
        this._trusted.add(sender);
        return true;
    }

    // An object for Gio.DBusExportedObject.wrapJSObject: every method checks
    // the caller, then runs the real one on impl.
    wrap(ifaceXml, impl) {
        const info = Gio.DBusNodeInfo.new_for_xml(ifaceXml).interfaces[0];
        const proxy = {};
        for (const method of info.methods) {
            const name = method.name;
            const outSig = `(${method.out_args.map(a => a.signature).join('')})`;
            const hasOut = method.out_args.length > 0;
            proxy[`${name}Async`] = async (params, invocation) => {
                if (!await this.allowed(name, invocation.get_sender())) {
                    invocation.return_dbus_error('org.freedesktop.DBus.Error.AccessDenied',
                        'Only the Framewise app can use this.');
                    return;
                }
                if (impl[`${name}Async`]) {
                    impl[`${name}Async`](params, invocation);
                    return;
                }
                try {
                    const result = impl[name](...params);
                    invocation.return_value(hasOut ? new GLib.Variant(outSig,
                        method.out_args.length === 1 ? [result] : result) : null);
                } catch (e) {
                    invocation.return_dbus_error('org.gnome.gjs.JSError', String(e));
                }
            };
        }
        return proxy;
    }
}
