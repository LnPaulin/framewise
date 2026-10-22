"""Records the screen for a few seconds to check the capture path.

Usage: python3 -m tools.probe_capture OUT.mkv [seconds]   (run from app/)
Prints the format PipeWire hands over and how many frames arrived.
"""

import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from framewise import portal  # noqa: E402

Gst.init(None)
out = sys.argv[1]
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 8
loop = GLib.MainLoop()
frames = {"n": 0, "first": None, "last": None}


def on_ready(cap):
    s = cap.stream
    print(f"stream node={s.node_id} size={s.width}x{s.height} type={s.source_type}", flush=True)
    desc = (f"pipewiresrc name=src fd={cap.fd} path={s.node_id} do-timestamp=true "
            f"keepalive-time=1000 always-copy=false ! "
            f"vapostproc ! video/x-raw(memory:VAMemory),format=NV12 ! "
            f"vah264enc rate-control=cqp qpi=16 qpp=18 qpb=18 key-int-max=120 target-usage=2 ! "
            f"h264parse ! matroskamux ! filesink location={out}")
    pipe = Gst.parse_launch(desc)
    src = pipe.get_by_name("src")

    def count(_pad, _info):
        t = time.monotonic()
        frames["n"] += 1
        frames["first"] = frames["first"] or t
        frames["last"] = t
        return Gst.PadProbeReturn.OK

    srcpad = src.get_static_pad("src")
    srcpad.add_probe(Gst.PadProbeType.BUFFER, count)
    bus = pipe.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print("ERROR", err.message, dbg, flush=True)
            pipe.set_state(Gst.State.NULL)
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            pipe.set_state(Gst.State.NULL)
            caps = srcpad.get_current_caps()
            print("caps:", caps.to_string() if caps else None)
            span = (frames["last"] or 0) - (frames["first"] or 0)
            print(f"frames={frames['n']} over {span:.1f}s "
                  f"= {frames['n'] / span if span else 0:.1f} fps", flush=True)
            cap.close()
            loop.quit()

    bus.connect("message", on_msg)
    pipe.set_state(Gst.State.PLAYING)

    def stop():
        caps = srcpad.get_current_caps()
        print("caps:", caps.to_string() if caps else None, flush=True)
        pipe.send_event(Gst.Event.new_eos())
        return False

    GLib.timeout_add(int(seconds * 1000), stop)


def on_error(msg):
    print("portal error:", msg, flush=True)
    loop.quit()


portal.open_capture("monitor", "embedded", on_ready, on_error)
loop.run()
