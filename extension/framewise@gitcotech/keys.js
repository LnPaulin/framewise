// Framewise's keyboard shortcuts, as set in GNOME (Settings > Keyboard >
// Custom Shortcuts, where Framewise adds them): reading them for the menu,
// and recognising them while a panel holds the keyboard (GNOME doesn't run
// shortcuts then).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';

const KEYS_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys';
const KEYS_BASE = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/';

// The accelerator of one of Framewise's shortcuts ('<Shift><Super>s'), or ''.
export function shortcutFor(id) {
    try {
        const paths = new Gio.Settings({schema_id: KEYS_SCHEMA}).get_strv('custom-keybindings');
        const path = `${KEYS_BASE}${id}/`;
        if (!paths.includes(path))
            return '';
        return new Gio.Settings({schema_id: `${KEYS_SCHEMA}.custom-keybinding`, path}).get_string('binding');
    } catch (e) {
        return '';
    }
}

// '<Shift><Super>s' -> 'Shift+Super+S'
export function prettyKeys(accel) {
    if (!accel)
        return '';
    const mods = [...accel.matchAll(/<([^>]+)>/g)].map(m => m[1])
        .map(m => ({Primary: 'Ctrl', Control: 'Ctrl', Ctrl: 'Ctrl', Alt: 'Alt', Shift: 'Shift',
            Super: 'Super', Meta: 'Super'})[m] ?? m);
    const key = accel.replace(/<[^>]+>/g, '');
    const name = key.length === 1 ? key.toUpperCase() : key;
    return [...mods, name].join('+');
}

// Whether a key press is the accelerator (same key, same modifiers).
export function matches(event, accel) {
    if (!accel)
        return false;
    const want = new Set([...accel.matchAll(/<([^>]+)>/g)].map(m => m[1].toLowerCase())
        .map(m => ({primary: 'control', ctrl: 'control', meta: 'super'})[m] ?? m));
    const key = accel.replace(/<[^>]+>/g, '').toLowerCase();
    const name = (Clutter.keyval_name(event.get_key_symbol()) ?? '').toLowerCase();
    if (!key || name !== key)
        return false;
    const state = event.get_state();
    const has = {
        shift: (state & Clutter.ModifierType.SHIFT_MASK) !== 0,
        control: (state & Clutter.ModifierType.CONTROL_MASK) !== 0,
        alt: (state & Clutter.ModifierType.MOD1_MASK) !== 0,
        super: (state & (Clutter.ModifierType.SUPER_MASK | Clutter.ModifierType.MOD4_MASK)) !== 0,
    };
    return Object.keys(has).every(m => has[m] === want.has(m));
}
