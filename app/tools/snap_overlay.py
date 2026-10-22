"""Shows the area selector briefly and saves what the screen looks like.

Run from app/: python3 -m tools.snap_overlay OUT.png
Uses the remembered screen share, so no dialog appears.
"""

import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gst  # noqa: E402

from framewise import portal, ui  # noqa: E402

Gst.init(None)
out = sys.argv[1]
app = Adw.Application(application_id="com.gitcotech.Framewise")


def snap(cap, selector):
    pipe = Gst.parse_launch(
        f"pipewiresrc fd={cap.fd} path={cap.stream.node_id} num-buffers=10 ! "
        f"vapostproc ! video/x-raw,format=BGRx ! videoconvert ! "
        f"videoscale ! video/x-raw,width=1720,height=720 ! pngenc snapshot=false ! "
        f"multifilesink location={out}")
    print("snapping", flush=True)
    pipe.set_state(Gst.State.PLAYING)
    bus = pipe.get_bus()
    bus.timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    selector._close(None)
    cap.close()
    app.quit()
    return False


def on_activate(a):
    a.hold()
    ui.install_css()

    def ready(cap):
        print("capture ready", cap.stream, flush=True)
        sel = ui.AreaSelector(a, ui.monitor_for(cap.stream), lambda r: None)
        GLib.timeout_add(1500, snap, cap, sel)

    portal.open_capture("monitor", "embedded", ready, lambda m: (print(m), a.quit()))


app.connect("activate", on_activate)
app.run([])
