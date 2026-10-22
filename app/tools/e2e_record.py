"""End-to-end check: record the remembered screen with a pause, then inspect.

Run from app/: python3 -m tools.e2e_record OUT_DIR
Records ~4 s, pauses 2 s, records ~3 s more, stops, then reports the saved
file and the take's timestamps (frame spacing, backwards jumps, track ends).
"""

import glob
import json
import os
import subprocess
import sys
import time

from framewise.export import TAKES_DIR
from framewise.state import settings

out = sys.argv[1]
settings.set("folder", out)
settings.set("last_mode", "screen")
settings.set("countdown", 3)

before = set(glob.glob(f"{out}/*.mp4"))
app = subprocess.Popen([sys.executable, "-m", "framewise", "record", "--now"],
                       stderr=subprocess.PIPE, text=True)


def cmd(*a):
    subprocess.run([sys.executable, "-m", "framewise", "record", *a])


time.sleep(3.6 + 4)
cmd("--pause")
time.sleep(2)
cmd("--pause")
time.sleep(3)
cmd("--stop")
try:
    _, err = app.communicate(timeout=30)
except subprocess.TimeoutExpired:
    app.kill()
    _, err = app.communicate()
    print("APP DID NOT EXIT")
print("app log:", err.strip() or "(empty)")

new = sorted(set(glob.glob(f"{out}/*.mp4")) - before)
if not new:
    print("NO FILE SAVED")
    sys.exit(1)


def probe(path, *args):
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args, path],
        capture_output=True, text=True).stdout)


f = new[-1]
info = probe(f, "-show_entries", "stream=codec_name,width,height,avg_frame_rate,duration:format=duration")
print("saved:", os.path.basename(f))
for s in info["streams"]:
    print("  ", s)
print("   format duration", info["format"]["duration"])

take = max(glob.glob(f"{TAKES_DIR}/*/take.mkv"), key=os.path.getmtime)
pk = probe(take, "-show_entries", "packet=stream_index,pts_time")["packets"]
by = {}
for p in pk:
    by.setdefault(p["stream_index"], []).append(float(p["pts_time"]))
for idx, ts in sorted(by.items()):
    ts.sort()
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    back = sum(1 for p, q in zip(by[idx], by[idx][1:]) if q < p)
    print(f"take stream {idx}: {len(ts)} packets, {ts[0]:.3f}..{ts[-1]:.3f}s, "
          f"max gap {max(gaps) if gaps else 0:.3f}s, backwards {back}")
