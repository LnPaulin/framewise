// Per-app output: which app plays sound on which device, and moving one.
//
// Reads PipeWire's state with pw-dump and moves an app by setting its
// streams' "target.object" (pw-metadata), as PipeWire's own tools do.
// WirePlumber doesn't keep that choice, so it is remembered here
// (~/.config/framewise/app-outputs.json) and applied again whenever the
// app starts playing (applyRemembered, run when GNOME sees a new sound).
//
// No GNOME Shell imports here, so this can be tried on its own with gjs.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

function run(argv) {
    return new Promise((resolve, reject) => {
        let proc;
        try {
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
        } catch (e) {
            reject(e);
            return;
        }
        proc.communicate_utf8_async(null, null, (p, res) => {
            try {
                const [, out] = p.communicate_utf8_finish(res);
                if (!p.get_successful())
                    throw new Error(`${argv[0]} failed`);
                resolve(out ?? '');
            } catch (e) {
                reject(e);
            }
        });
    });
}

// "Meteor Lake-P HD Audio Controller Speaker" -> "Speaker": the part the
// computer's outputs share is left out (they all start with the chip's name).
export function shortNames(descriptions) {
    const words = descriptions.map(d => d.split(' '));
    if (words.length < 2)
        return descriptions;
    let common = 0;
    while (words.every(w => w.length > common + 1 && w[common] === words[0][common]))
        common++;
    return words.map(w => w.slice(common).join(' '));
}

// pw-dump's objects -> {sinks: [{id, name, label}], apps: [{key, name, icon, streams, sinkId}]}
export function parse(objects) {
    const props = o => o?.info?.props ?? {};
    const byId = new Map(objects.map(o => [o.id, o]));
    // An output whose connector reports nothing plugged in (an HDMI port
    // without a screen) is left out, as GNOME's own sound menu does.
    const unplugged = o => {
        const p = props(o);
        const routes = byId.get(p['device.id'])?.info?.params?.EnumRoute ?? [];
        const mine = routes.filter(r => r.direction === 'Output' &&
            (r.devices ?? []).includes(p['card.profile.device']));
        return mine.length > 0 && mine.every(r => r.available === 'no');
    };
    const sinks = objects.filter(o => props(o)['media.class'] === 'Audio/Sink' && !unplugged(o))
        .map(o => ({id: o.id, name: props(o)['node.name'], description: props(o)['node.description'] ?? props(o)['node.name']}));
    // The computer's own outputs share a long prefix; Bluetooth and USB ones keep their names.
    const builtIn = sinks.filter(s => s.name?.startsWith('alsa_output.pci'));
    const short = shortNames(builtIn.map(s => s.description));
    builtIn.forEach((s, i) => {
        s.label = short[i] || s.description;
    });
    sinks.forEach(s => {
        s.label ??= s.description;
    });

    const links = objects.filter(o => o.type === 'PipeWire:Interface:Link').map(o => o.info ?? {});
    const sinkOf = id => {
        const link = links.find(l => l['output-node-id'] === id && sinks.some(s => s.id === l['input-node-id']));
        return link ? link['input-node-id'] : null;
    };
    const apps = new Map();
    for (const o of objects) {
        const p = props(o);
        if (p['media.class'] !== 'Stream/Output/Audio')
            continue;
        // PipeWire's own helpers (loopbacks, echo cancelling) aren't apps.
        if (!p['application.name'] && !p['application.process.binary'])
            continue;
        const name = p['application.name'] || p['application.process.binary'];
        const key = p['application.process.binary'] || name;
        if (!apps.has(key))
            apps.set(key, {key, name, icon: p['application.icon-name'] || null, streams: [], sinkId: null});
        const app = apps.get(key);
        app.streams.push(o.id);
        app.sinkId ??= sinkOf(o.id);
    }
    // The device everything else plays on (WirePlumber's "default" metadata).
    let defaultName = null;
    for (const o of objects) {
        if (o.type !== 'PipeWire:Interface:Metadata' || o.props?.['metadata.name'] !== 'default')
            continue;
        const entry = (o.metadata ?? []).find(m => m.key === 'default.audio.sink');
        defaultName = entry?.value?.name ?? null;
    }
    return {sinks, apps: [...apps.values()], defaultName};
}

export async function list() {
    return parse(JSON.parse(await run(['pw-dump'])));
}

// Sends every stream of the app to the sink (by its node name).
export async function moveApp(app, sink) {
    for (const id of app.streams)
        await run(['pw-metadata', String(id), 'target.object', sink.name, 'Spa:String']);
}

// ---- volume ---------------------------------------------------------------------------

export const MAX_VOLUME = 1.5;       // above 1 boosts a quiet app

export async function getVolume(app) {
    const out = await run(['wpctl', 'get-volume', String(app.streams[0])]);
    const m = /Volume:\s*([\d.]+)/.exec(out);
    return m ? parseFloat(m[1]) : 1;
}

export async function setVolume(app, volume) {
    const v = Math.max(0, Math.min(MAX_VOLUME, volume)).toFixed(3);
    for (const id of app.streams)
        await run(['wpctl', 'set-volume', String(id), v]);
}

// ---- switching the output for everything ---------------------------------------------

// Makes the next connected device the one everything plays on. Returns it.
export async function nextOutput() {
    const {sinks, defaultName} = await list();
    const next = nextSink(sinks, defaultName);
    if (next && next.name !== defaultName)
        await run(['wpctl', 'set-default', String(next.id)]);
    return next;
}

// The device after the current one, round and round (the first if none is current).
export function nextSink(sinks, currentName) {
    if (!sinks.length)
        return null;
    const i = sinks.findIndex(s => s.name === currentName);
    return sinks[(i + 1) % sinks.length];
}

// Back to whatever device the system uses for everything else.
export async function followDefault(app) {
    for (const id of app.streams)
        await run(['pw-metadata', '-d', String(id), 'target.object']);
}

// ---- remembered choices -------------------------------------------------------------

const MEMORY = GLib.build_filenamev([GLib.get_user_config_dir(), 'framewise', 'app-outputs.json']);

export function remembered() {
    try {
        const [ok, bytes] = GLib.file_get_contents(MEMORY);
        return ok ? JSON.parse(new TextDecoder().decode(bytes)) : {};
    } catch (e) {
        return {};
    }
}

export function remember(appKey, sinkName) {
    const all = remembered();
    if (sinkName)
        all[appKey] = sinkName;
    else
        delete all[appKey];
    GLib.mkdir_with_parents(GLib.path_get_dirname(MEMORY), 0o700);
    GLib.file_set_contents(MEMORY, JSON.stringify(all, null, 1));
}

// Sends apps with a remembered device there, if it is connected now
// (headphones that aren't plugged in are skipped: the app plays as usual).
export async function applyRemembered() {
    const all = remembered();
    if (!Object.keys(all).length)
        return;
    const {sinks, apps} = await list();
    for (const app of apps) {
        const sink = sinks.find(s => s.name === all[app.key]);
        if (sink && app.sinkId !== sink.id)
            await moveApp(app, sink);
    }
}

export function available() {
    return ['pw-dump', 'pw-metadata', 'wpctl'].every(p => !!GLib.find_program_in_path(p));
}
