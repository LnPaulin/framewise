"""Looks (presets) you save yourself, to start the next recording from.

A look keeps the visual choices: background and shape, the pointer, zoom on
click, and pictures shown over the video. Cuts, captions, blurs and sound
belong to one recording and stay out. Pictures are copied into the app's
own folder, so a look keeps working after its recording is gone.
"""

import shutil
import uuid
from pathlib import Path

from . import edit_model as em
from .state import DATA_DIR, settings

LOOKS_DIR = DATA_DIR / "looks"
POINTER_KEYS = ("show", "size", "smoothing", "rings")


def all_looks():
    return [lk for lk in (settings.get("looks") or []) if lk.get("id") and lk.get("name")]


def _keep_picture(src, folder):
    src = Path(src)
    if not src.exists():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / src.name
    n = 2
    while dest.exists():
        dest = folder / f"{src.stem}-{n}{src.suffix}"
        n += 1
    shutil.copy2(src, dest)
    return str(dest)


def save_look(name, doc, take_dir):
    """Stores the document's look under name (replacing one with that name)."""
    look_id = uuid.uuid4().hex[:12]
    folder = LOOKS_DIR / look_id
    style = em.clone(doc["style"])
    bg = style["background"]
    if bg.get("kind") == "image" and bg.get("image"):
        bg["image"] = _keep_picture(bg["image"], folder)
        if not bg["image"]:
            bg["kind"] = "none"
    images = []
    for im in doc.get("images", []):
        kept = _keep_picture(Path(take_dir) / im["file"], folder)
        if kept:
            images.append({"file": kept, "size": im["size"], "opacity": im["opacity"],
                           "anchor": im["anchor"]})
    look = {"id": look_id, "name": name.strip()[:40], "style": style,
            "pointer": {k: doc["pointer"][k] for k in POINTER_KEYS},
            "zoom": {"enabled": doc["zoom"]["enabled"], "amount": doc["zoom"]["amount"]},
            "images": images}
    looks = [lk for lk in all_looks() if lk["name"].lower() != look["name"].lower()]
    for old in all_looks():
        if old not in looks:
            shutil.rmtree(LOOKS_DIR / old["id"], ignore_errors=True)
    settings.set("looks", looks + [look])
    return look


def remove_look(look_id):
    settings.set("looks", [lk for lk in all_looks() if lk["id"] != look_id])
    shutil.rmtree(LOOKS_DIR / look_id, ignore_errors=True)


def apply_look(look, doc, duration, store_picture):
    """A new document with the look applied. store_picture(path) -> path inside the take."""
    doc = em.clone(doc)
    style = em.clone(look["style"])
    bg = style["background"]
    if bg.get("kind") == "image":
        if bg.get("image") and Path(bg["image"]).exists():
            bg["image"] = store_picture(bg["image"], absolute=True)
        else:
            bg["kind"] = "none"
    doc["style"] = style
    doc["pointer"].update({k: look["pointer"][k] for k in POINTER_KEYS if k in look["pointer"]})
    doc["zoom"].update(look.get("zoom") or {})
    # The look's pictures replace pictures that came from a look before;
    # pictures added by hand to this recording stay.
    doc["images"] = [im for im in doc["images"] if not im.get("from_look")]
    for im in look.get("images", []):
        if Path(im["file"]).exists():
            doc["images"].append({"id": em.new_id(), "start": 0.0, "end": duration,
                                  "file": store_picture(im["file"]), "size": im["size"],
                                  "opacity": im["opacity"], "anchor": im["anchor"],
                                  "from_look": True})
    return em.normalize(doc, duration)
