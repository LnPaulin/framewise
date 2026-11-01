"""Reading QR codes in a picture (Text Capture tries this before reading text).

Uses OpenCV (python3-opencv, installed with Framewise unless left out). QR
codes on screen are often small (a corner of a web page, a paused video),
so a picture is also tried enlarged, with every reader OpenCV has. Without OpenCV, nothing is found and
Text Capture reads text as before.
"""

import re
from pathlib import Path

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MAX_SIDE = 5000       # pictures aren't enlarged past this (time and memory)


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def _detectors(cv2):
    """Functions picture -> [(text, corner points)], best first."""
    out = []
    if hasattr(cv2, "wechat_qrcode_WeChatQRCode"):
        # WeChat's reader (OpenCV's extra modules): the best with small codes.
        wechat = cv2.wechat_qrcode_WeChatQRCode()

        def read_wechat(picture):
            texts, points = wechat.detectAndDecode(picture)
            return list(zip(texts, points))
        out.append(read_wechat)
    for detector in ([cv2.QRCodeDetectorAruco()] if hasattr(cv2, "QRCodeDetectorAruco") else []) + [cv2.QRCodeDetector()]:
        def read(picture, detector=detector):
            ok, texts, points, _ = detector.detectAndDecodeMulti(picture)
            return list(zip(texts, points)) if ok and texts is not None else []
        out.append(read)
    return out


def _sizes(image, cv2):
    """The picture as it is, then enlarged: codes on screen are often a few
    pixels per square (a zoomed-out page), which readers miss at 1:1."""
    h, w = image.shape[:2]
    yield image, 1
    for k in (2, 3):
        if max(h, w) * k > MAX_SIDE:
            break
        yield cv2.resize(image, (w * k, h * k), interpolation=cv2.INTER_CUBIC), k


def read_codes(path):
    """The text of every QR code in the picture at path, in reading order
    (top to bottom, then left to right), without repeats. [] if none.
    What every reader finds is put together: each one misses codes another finds."""
    cv2 = _cv2()
    if cv2 is None or not Path(path).is_file():
        return []
    image = cv2.imread(str(path))
    if image is None:
        return []
    found = {}                       # text -> (y, x) of its top-left corner
    sizes = list(_sizes(image, cv2))
    for i, read in enumerate(_detectors(cv2)):
        # Enlarged pictures go to the first reader only; the others are slow
        # there and find nothing more (they're for codes seen at an angle).
        for picture, k in (sizes if i == 0 else sizes[:1]):
            try:
                results = read(picture)
            except cv2.error:
                continue
            for text, points in results:
                if text and text not in found:
                    pts = points.reshape(-1, 2)
                    found[text] = (pts[:, 1].min() / k, pts[:, 0].min() / k)
    return [t for t, _ in sorted(found.items(), key=lambda f: (round(f[1][0] / 20), f[1][1]))]


def is_link(text):
    """Only web links get an "Open link" button (never file: or other schemes)."""
    return bool(URL_RE.match(text.strip()))
