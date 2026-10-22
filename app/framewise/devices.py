"""Lists the microphones and cameras the user can choose from.

One device monitor runs for the whole app, from the first time it's asked:
it notices devices plugged in or out, and GStreamer's device providers are
shared, so starting and stopping several monitors could crash the app.
"""

from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

_monitor = None
_listeners = []


@dataclass
class Device:
    id: str        # microphone: PipeWire/Pulse source name; camera: /dev/videoN
    name: str
    mjpeg: bool = False   # cameras: can send compressed 1280x720 frames


def _props(dev):
    p = dev.get_properties()
    get = (lambda k: p.get_string(k) if p and p.has_field(k) else None)
    return get


def _get_monitor():
    global _monitor
    if _monitor is None:
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Audio/Source", None)
        monitor.add_filter("Video/Source", None)
        monitor.get_bus().add_watch(GLib.PRIORITY_DEFAULT, _on_message)
        monitor.start()
        _monitor = monitor
    return _monitor


def _on_message(_bus, msg):
    if msg.type in (Gst.MessageType.DEVICE_ADDED, Gst.MessageType.DEVICE_REMOVED):
        for callback in list(_listeners):
            callback()
    return True


def watch(callback):
    """callback() whenever a microphone or camera is plugged in or out
    (several times for one device: wait a moment before acting)."""
    _get_monitor()
    _listeners.append(callback)


def unwatch(callback):
    if callback in _listeners:
        _listeners.remove(callback)


def shutdown():
    """Stops the device monitor (when the app quits)."""
    global _monitor
    if _monitor is not None:
        _monitor.get_bus().remove_watch()
        _monitor.stop()
        _monitor = None
    _listeners.clear()


def _scan(kind):
    return [d for d in (_get_monitor().get_devices() or []) if d.has_classes(kind)]


def _clean(name):
    # "HP 5MP Camera: HP 5MP Camera" -> "HP 5MP Camera",
    # "EMEET SmartCam C960 AF: EMEET S" -> "EMEET SmartCam C960 AF"; drop "(V4L2)" tags.
    name = name.replace(" (V4L2)", "")
    if ": " in name:
        a, b = name.split(": ", 1)
        if b.startswith(a) or a.startswith(b):
            name = max(a, b, key=len)
    return name


def microphones():
    """Real inputs only: speaker 'monitor' sources are left out."""
    found, seen = [], set()
    for dev in _scan("Audio/Source"):
        get = _props(dev)
        node = get("node.name")
        if not node or get("device.class") == "monitor" or node in seen:
            continue
        seen.add(node)
        found.append(Device(node, dev.get_display_name()))
    return found


def cameras():
    """Colour cameras only: infrared (greyscale-only) face-unlock cameras are left out."""
    found = {}
    for dev in _scan("Video/Source"):
        get = _props(dev)
        path = get("api.v4l2.path") or get("device.path")
        caps = dev.get_caps()
        text = caps.to_string() if caps else ""
        if not path or ("GRAY8" in text and "image/jpeg" not in text and "YUY2" not in text
                        and "NV12" not in text):
            continue
        cam = Device(path, _clean(dev.get_display_name()), "image/jpeg" in text)
        if path not in found or len(cam.name) < len(found[path].name):
            found[path] = cam
    return sorted(found.values(), key=lambda d: d.id)


def match_camera(cams, device_id, name=None):
    """The chosen camera among cams, or None. A camera plugged back in can get
    a new address (/dev/video4 -> /dev/video6), so the name decides when the
    address is gone or now belongs to another camera."""
    by_id = next((c for c in cams if c.id == device_id), None)
    if by_id is not None and (not name or by_id.name == name):
        return by_id
    by_name = next((c for c in cams if name and c.name == name), None)
    return by_name or by_id


def find_camera(device_id=None, name=None):
    """The chosen camera if it is still plugged in, else the first one."""
    cams = cameras()
    return match_camera(cams, device_id, name) or (cams[0] if cams else None)


def output_delay():
    """Seconds between a sound leaving the computer and reaching your ears,
    on the current speakers or headphones (as PipeWire reports it).

    Bluetooth headphones add about 0.15-0.3 s; wired ones and laptop
    speakers almost nothing. Recordings delay the captured system sound by
    this much, so music lines up with a voice singing along to what was heard.
    0.0 when it can't be told.
    """
    import json
    import subprocess
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout
        objects = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0
    # The default output, from PipeWire's "default" metadata.
    sink = None
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Metadata" and obj.get("props", {}).get("metadata.name") == "default":
            for entry in obj.get("metadata", []):
                if entry.get("key") == "default.audio.sink":
                    value = entry.get("value")
                    sink = value.get("name") if isinstance(value, dict) else None
    if not sink:
        return 0.0
    for obj in objects:
        info = obj.get("info") or {}
        if (info.get("props") or {}).get("node.name") != sink:
            continue
        params = info.get("params") or {}
        latency = 0
        for p in params.get("Latency", []) + params.get("ProcessLatency", []):
            if p.get("direction", "Input") == "Input":
                latency = max(latency, int(p.get("minNs", 0) or 0))
        # Implausible values are ignored rather than shifting the sound a lot.
        seconds = latency / 1e9
        return seconds if 0.0 < seconds < 1.0 else 0.0
    return 0.0
