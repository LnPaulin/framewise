"""PDF tools: merge, pick pages, compress, rotate, pictures → PDF, PDF → pictures.

Built on what Ubuntu already has: Poppler's command-line tools (pdfinfo,
pdfunite, pdfseparate, pdftoppm) and Ghostscript, plus Cairo for pictures.
Merging and picking pages copy the pages as they are (nothing is
re-compressed); compressing and rotating rewrite the file with Ghostscript.
Results are new files next to the first one; nothing is ever overwritten.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
COMPRESS = {   # level -> (Ghostscript preset, label)
    "smaller": ("/ebook", "Smaller (150 dpi pictures, good on screen)"),
    "smallest": ("/screen", "Smallest (72 dpi pictures, for email)"),
}
TIMEOUT = 600


class PdfError(Exception):
    pass


def available():
    return all(shutil.which(t) for t in ("pdfinfo", "pdfunite", "pdfseparate", "pdftoppm", "gs"))


def is_pdf(path):
    return Path(path).suffix.lower() == ".pdf"


def is_image(path):
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def _run(cmd, what):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise PdfError(f"{what} failed: {e}") from e
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip().splitlines()
        message = detail[-1] if detail else "unknown error"
        if "password" in (out.stderr or "").lower() or "encrypt" in (out.stderr or "").lower():
            message = "the PDF is protected with a password"
        raise PdfError(f"{what} failed: {message}")
    return out.stdout


def page_count(path):
    out = _run(["pdfinfo", str(path)], "Reading the PDF")
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    if not m:
        raise PdfError("This file doesn't look like a PDF")
    return int(m.group(1))


def output_path(first, suffix_text, ext=".pdf", folder=None):
    """'Name (merged).pdf' next to the first file, never replacing one."""
    first = Path(first)
    folder = Path(folder) if folder else first.parent
    stem = f"{first.stem} ({suffix_text})" if suffix_text else first.stem
    path = folder / f"{stem}{ext}"
    n = 2
    while path.exists():
        path = folder / f"{stem} {n}{ext}"
        n += 1
    return path


def parse_pages(text, count):
    """'1-3, 5, 8-' -> [1, 2, 3, 5, 8, 9, ...] (1-based, in the order given)."""
    pages = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", part)
        if not m:
            raise PdfError(f"“{part}” isn't a page or a range like 2-5")
        if m.group(3):
            a = b = int(m.group(3))
        else:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else count
        if a < 1 or b > count or a > b:
            raise PdfError(f"Pages {part} aren't in this PDF (it has {count} page{'s' if count != 1 else ''})")
        pages.extend(range(a, b + 1))
    if not pages:
        raise PdfError("Type the pages to keep, for example 1-3, 7")
    return pages


# ---- the tools --------------------------------------------------------------------------

def merge(files, dest):
    """PDFs and pictures, in this order, into one PDF."""
    with tempfile.TemporaryDirectory(prefix="framewise-pdf-") as tmp:
        parts = []
        for i, f in enumerate(files):
            if is_image(f):
                part = Path(tmp) / f"image-{i}.pdf"
                images_to_pdf([f], part)
                parts.append(str(part))
            else:
                parts.append(str(f))
        if len(parts) == 1:
            shutil.copyfile(parts[0], dest)
        else:
            _run(["pdfunite", *parts, str(dest)], "Merging")
    return Path(dest)


def pick_pages(src, pages, dest):
    """The given pages (1-based, any order, repeats allowed) as a new PDF, unchanged."""
    with tempfile.TemporaryDirectory(prefix="framewise-pdf-") as tmp:
        _run(["pdfseparate", str(src), str(Path(tmp) / "p%d.pdf")], "Splitting")
        parts = [str(Path(tmp) / f"p{n}.pdf") for n in pages]
        if len(parts) == 1:
            shutil.copyfile(parts[0], dest)
        else:
            _run(["pdfunite", *parts, str(dest)], "Joining the pages")
    return Path(dest)


def split_all(src, folder):
    """Every page as its own PDF in folder: 'Name page 1.pdf'..."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(src).stem
    _run(["pdfseparate", str(src), str(folder / f"{stem} page %d.pdf")], "Splitting")
    return folder


def _gs(src, dest, *options):
    # The output comes first: a PostScript instruction (-c) opens the device.
    _run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
          "-dCompatibilityLevel=1.5", f"-sOutputFile={dest}", *options, str(src)], "Rewriting the PDF")


def compress(src, dest, level="smaller"):
    """Returns (path, saved_bytes); raises PdfError when it wouldn't get smaller."""
    preset = COMPRESS[level][0]
    _gs(src, dest, f"-dPDFSETTINGS={preset}", "-dDetectDuplicateImages=true")
    before, after = Path(src).stat().st_size, Path(dest).stat().st_size
    if after >= before * 0.97:
        Path(dest).unlink(missing_ok=True)
        raise PdfError("This PDF is already as small as it gets (it has no large pictures to shrink)")
    return Path(dest), before - after


def rotate(src, dest, degrees):
    """Turns every page clockwise by 90, 180 or 270 degrees."""
    turns = {90: 3, 180: 2, 270: 1}[degrees]     # Ghostscript's page orientation codes
    _gs(src, dest, "-dAutoRotatePages=/None", "-c", f"<</Orientation {turns}>> setpagedevice", "-f")
    return Path(dest)


def images_to_pdf(images, dest):
    """One picture per page, each page the picture's size. JPEG photos are
    kept as they are (no quality lost)."""
    import cairo
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf
    surface = None
    try:
        for path in images:
            original = GdkPixbuf.Pixbuf.new_from_file(str(path))
            upright = original.get_option("orientation") in (None, "1")
            pix = original.apply_embedded_orientation() or original    # phone photos taken sideways
            w, h = pix.get_width(), pix.get_height()
            pw, ph = w * 72 / 96, h * 72 / 96                        # pixels at 96 dpi, in points
            if surface is None:
                surface = cairo.PDFSurface(str(dest), pw, ph)
            else:
                surface.set_size(pw, ph)
            cr = cairo.Context(surface)
            cr.scale(72 / 96, 72 / 96)
            img = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
            ic = cairo.Context(img)
            Gdk.cairo_set_source_pixbuf(ic, pix, 0, 0)
            ic.paint()
            img.flush()
            if Path(path).suffix.lower() in (".jpg", ".jpeg") and upright:
                img.set_mime_data(cairo.MIME_TYPE_JPEG, Path(path).read_bytes())
            cr.set_source_surface(img, 0, 0)
            cr.paint()
            surface.show_page()
    except Exception as e:  # noqa: BLE001 - any unreadable picture
        raise PdfError(f"A picture couldn't be read: {e}") from e
    finally:
        if surface is not None:
            surface.finish()
    return Path(dest)


def to_images(src, folder, dpi=150):
    """Every page as a PNG in folder: 'Name-1.png'..."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _run(["pdftoppm", "-png", "-r", str(dpi), str(src), str(folder / Path(src).stem)], "Making pictures")
    return folder
