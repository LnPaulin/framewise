"""Reading QR codes in a picture (Text Capture tries this before reading text).

Uses OpenCV (python3-opencv, installed with Framewise unless left out). QR
codes on screen are often small (a corner of a web page, a paused video),
so a picture is also tried enlarged. Without OpenCV, nothing is found and
Text Capture reads text as before.
"""

import re
from pathlib import Path

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MIN_SIDE = 400        # smaller pictures are tried enlarged too


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def _detectors(cv2):
    out = []
    if hasattr(cv2, "QRCodeDetectorAruco"):
        out.append(cv2.QRCodeDetectorAruco())      # finds more codes, at more angles
    out.append(cv2.QRCodeDetector())
    return out


def read_codes(path):
    """The text of every QR code in the picture at path, in reading order
    (top to bottom, then left to right), without repeats. [] if none."""
    cv2 = _cv2()
    if cv2 is None or not Path(path).is_file():
        return []
    image = cv2.imread(str(path))
    if image is None:
        return []
    tries = [image]
    h, w = image.shape[:2]
    if min(h, w) < MIN_SIDE:
        k = max(2, int(MIN_SIDE * 2 / max(1, min(h, w))))
        tries.append(cv2.resize(image, (w * k, h * k), interpolation=cv2.INTER_NEAREST))
    for picture in tries:
        for detector in _detectors(cv2):
            try:
                ok, texts, points, _ = detector.detectAndDecodeMulti(picture)
            except cv2.error:
                continue
            if not ok or texts is None:
                continue
            found = [(p[:, 1].min(), p[:, 0].min(), t) for t, p in zip(texts, points) if t]
            if found:
                seen, out = set(), []
                for _y, _x, text in sorted(found, key=lambda f: (round(f[0] / 20), f[1])):
                    if text not in seen:
                        seen.add(text)
                        out.append(text)
                return out
    return []


def is_link(text):
    """Only web links get an "Open link" button (never file: or other schemes)."""
    return bool(URL_RE.match(text.strip()))
