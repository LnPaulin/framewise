"""The computer's sound outputs, for Settings (the same list the Framewise
menu shows: outputs whose connector reports nothing plugged in are left out).
Read from PipeWire with pw-dump; the extension's audio.js does the same."""

import json
import subprocess


def short_names(descriptions):
    """'Meteor Lake-P HD Audio Controller Speaker' -> 'Speaker': the words all
    the computer's own outputs start with are left out."""
    words = [d.split(" ") for d in descriptions]
    if len(words) < 2:
        return descriptions
    common = 0
    while all(len(w) > common + 1 and w[common] == words[0][common] for w in words):
        common += 1
    return [" ".join(w[common:]) for w in words]


def parse(objects):
    """pw-dump's objects -> [(node name, label)]."""
    def props(o):
        return ((o or {}).get("info") or {}).get("props") or {}
    by_id = {o.get("id"): o for o in objects}

    def unplugged(o):
        p = props(o)
        device = (by_id.get(p.get("device.id")) or {}).get("info") or {}
        routes = (device.get("params") or {}).get("EnumRoute") or []
        mine = [r for r in routes if r.get("direction") == "Output"
                and p.get("card.profile.device") in (r.get("devices") or [])]
        return bool(mine) and all(r.get("available") == "no" for r in mine)

    sinks = [(props(o).get("node.name"), props(o).get("node.description") or props(o).get("node.name"))
             for o in objects if props(o).get("media.class") == "Audio/Sink" and not unplugged(o)]
    built_in = [i for i, (name, _d) in enumerate(sinks) if (name or "").startswith("alsa_output.pci")]
    short = short_names([sinks[i][1] for i in built_in])
    out = [list(s) for s in sinks]
    for i, label in zip(built_in, short):
        out[i][1] = label or out[i][1]
    return [tuple(s) for s in out if s[0]]


def outputs():
    try:
        dump = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
        return parse(json.loads(dump))
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
