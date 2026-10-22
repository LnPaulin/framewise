"""Camera effects: background blur, smooth skin and face light.

Two small models find what to change: MediaPipe's selfie segmentation
(you versus the background) and YuNet (faces, with eyes, nose and mouth
corners). They run in a background thread a few times a second on the
latest frame; every frame is then composed with the newest results,
smoothed over time so edges don't flicker.

  background blur  everything but you, blurred; soft edges at hair and shoulders
  smooth skin      edge-preserving smoothing on the face's skin only: eyes,
                   eyebrows and mouth stay sharp
  face light       a soft brightening centred on the face, like a ring light

Needs OpenCV and numpy (python3-opencv, python3-numpy). available() says
whether they are installed.
"""

import threading
import time
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:          # effects are offered only when these are installed
    cv2 = np = None

MODELS = Path(__file__).parent / "models"
SEG_MODEL = MODELS / "selfie_segmentation.onnx"
FACE_MODEL = MODELS / "face_detection_yunet_2023mar.onnx"
SEG_SIZE = 256               # the segmentation model's input (square)
FACE_WIDTH = 320             # frames are shrunk to this width to find faces
MAX_MODEL_RATE = 15          # model runs per second, at most


def available():
    return cv2 is not None and SEG_MODEL.exists() and FACE_MODEL.exists()


def _soft_ellipse(shape, center, axes, feather, scale=4):
    """A filled ellipse with a soft edge (float32 0..1) the size of shape.
    feather: how far the edge fades, as a fraction of the size. Worked out on a
    small grid and enlarged, which is much faster than blurring a big mask."""
    h, w = shape
    sh, sw = max(2, h // scale), max(2, w // scale)
    ys, xs = np.ogrid[0:sh, 0:sw]
    kx, ky = sw / w, sh / h
    dx = (xs + 0.5 - center[0] * kx) / max(1e-3, axes[0] * kx)
    dy = (ys + 0.5 - center[1] * ky) / max(1e-3, axes[1] * ky)
    d = np.sqrt(dx * dx + dy * dy)
    m = np.clip((1.0 + feather - d) / max(1e-3, 2 * feather), 0.0, 1.0).astype(np.float32)
    m = m * m * (3 - 2 * m)     # smoothstep: a gentle fade
    return cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)


class Effects:
    """Applies the effects to BGRA frames. Call process() for every frame."""

    def __init__(self):
        self.blur = 0.0          # 0..1
        self.smooth = 0.0        # 0..1
        self.light = 0.0         # 0..1
        self._seg = cv2.dnn.readNetFromONNX(str(SEG_MODEL))
        self._faces_net = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (FACE_WIDTH, FACE_WIDTH), 0.75)
        self._latest = None      # the newest frame, for the model thread
        self._mask = None        # person mask, SEG_SIZE x SEG_SIZE, smoothed over time
        self._faces = []         # [(x, y, w, h, landmarks 5x2)] in frame pixels, smoothed
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._models_loop, daemon=True)
        self._thread.start()

    @property
    def active(self):
        return self.blur > 0.01 or self.smooth > 0.01 or self.light > 0.01

    def stop(self):
        self._running = False
        self._wake.set()

    # ---- the models, in their own thread ------------------------------------------------

    def _models_loop(self):
        tick = 0
        while self._running:
            self._wake.wait(0.5)
            self._wake.clear()
            frame = self._latest
            if frame is None or not self.active:
                continue
            started = time.monotonic()
            bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            if self.blur > 0.01 or self.light > 0.01:
                blob = cv2.dnn.blobFromImage(bgr, 1 / 255.0, (SEG_SIZE, SEG_SIZE), swapRB=True)
                self._seg.setInput(blob)
                mask = self._seg.forward()[0, 0].astype(np.float32)
                with self._lock:
                    # Blend with the last mask: steadier edges, still quick to follow.
                    self._mask = mask if self._mask is None else self._mask * 0.35 + mask * 0.65
            if (self.smooth > 0.01 or self.light > 0.01) and tick % 2 == 0:
                self._find_faces(bgr)
            tick += 1
            # Leave the CPU some room: at most MAX_MODEL_RATE runs a second.
            rest = 1.0 / MAX_MODEL_RATE - (time.monotonic() - started)
            if rest > 0:
                time.sleep(rest)

    def _find_faces(self, bgr):
        h, w = bgr.shape[:2]
        k = w / FACE_WIDTH
        small = cv2.resize(bgr, (FACE_WIDTH, max(1, int(h / k))), interpolation=cv2.INTER_AREA)
        self._faces_net.setInputSize((small.shape[1], small.shape[0]))
        _ok, found = self._faces_net.detect(small)
        faces = []
        if found is not None:
            for f in found[:3]:
                box = f[:4] * k
                marks = f[4:14].reshape(5, 2) * k
                faces.append((box, marks))
        with self._lock:
            old = self._faces
            smoothed = []
            for box, marks in faces:
                # Follow the nearest face from last time smoothly (no jitter).
                near = min(old, key=lambda o: np.abs(o[0][:2] - box[:2]).sum(), default=None)
                if near is not None and np.abs(near[0][:2] - box[:2]).sum() < box[2]:
                    box = near[0] * 0.5 + box * 0.5
                    marks = near[1] * 0.5 + marks * 0.5
                smoothed.append((box, marks))
            self._faces = smoothed

    # ---- every frame -----------------------------------------------------------------------

    def process(self, frame):
        """frame: HxWx4 uint8 BGRA. Returns the frame with the effects (a new array)."""
        self._latest = frame
        self._wake.set()
        if not self.active:
            return frame
        with self._lock:
            mask = None if self._mask is None else self._mask.copy()
            faces = list(self._faces)
        out = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        if self.blur > 0.01 and mask is not None:
            out = self._blur_background(out, mask)
        for box, marks in faces:
            if self.smooth > 0.01:
                self._smooth_skin(out, box, marks)
            if self.light > 0.01:
                self._face_light(out, box, mask)
        return cv2.cvtColor(out, cv2.COLOR_BGR2BGRA)

    def _blur_background(self, bgr, mask):
        h, w = bgr.shape[:2]
        sw, sh = max(1, w // 4), max(1, h // 4)
        # Work on a quarter-size copy: fast, and the blur hides the detail anyway.
        small = cv2.resize(bgr, (sw, sh), interpolation=cv2.INTER_LINEAR).astype(np.float32)
        person = cv2.resize(mask, (sw, sh), interpolation=cv2.INTER_LINEAR)
        person = np.clip((person - 0.3) / 0.45, 0.0, 1.0)
        # Blur only the background's own colours ("normalized" blur), so the
        # person doesn't bleed a halo into it.
        sigma = 1.5 + 6.0 * self.blur
        room = 1.0 - person
        num = cv2.GaussianBlur(small * room[..., None], (0, 0), sigma)
        den = cv2.GaussianBlur(room, (0, 0), sigma)[..., None] + 1e-3
        back = cv2.resize(np.clip(num / den, 0, 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_LINEAR)
        edge = cv2.GaussianBlur(person, (0, 0), 0.8)
        weight = cv2.resize(edge, (w, h), interpolation=cv2.INTER_LINEAR)
        return cv2.blendLinear(bgr, back, weight, 1.0 - weight)

    def _face_region(self, shape, box, grow):
        h, w = shape[:2]
        x, y, bw, bh = box
        cx, cy = x + bw / 2, y + bh / 2
        rw, rh = bw * grow / 2, bh * grow / 2
        x0, y0 = int(max(0, cx - rw)), int(max(0, cy - rh * 1.1))
        x1, y1 = int(min(w, cx + rw)), int(min(h, cy + rh))
        return x0, y0, x1, y1

    def _smooth_skin(self, bgr, box, marks):
        """Changes bgr in place."""
        x0, y0, x1, y1 = self._face_region(bgr.shape, box, 1.35)
        if x1 - x0 < 24 or y1 - y0 < 24:
            return
        crop = np.ascontiguousarray(bgr[y0:y1, x0:x1])
        ch, cw = crop.shape[:2]
        s = self.smooth
        # Edge-preserving smoothing on a half-size copy (fast): evens out skin,
        # keeps outlines.
        half = cv2.resize(crop, (max(8, cw // 2), max(8, ch // 2)), interpolation=cv2.INTER_AREA)
        half = cv2.bilateralFilter(half, 7, 20 + 45 * s, 5)
        smooth = cv2.resize(half, (cw, ch), interpolation=cv2.INTER_LINEAR)
        # Where: skin-coloured parts of the face oval...
        ycc = cv2.cvtColor(half, cv2.COLOR_BGR2YCrCb)
        skin = cv2.inRange(ycc, (0, 133, 77), (255, 178, 130)).astype(np.float32) / 255.0
        skin = cv2.resize(cv2.GaussianBlur(skin, (0, 0), 2), (cw, ch), interpolation=cv2.INTER_LINEAR)
        bx, by, bw, bh = box
        where = skin * _soft_ellipse((ch, cw), (bx + bw / 2 - x0, by + bh / 2 - y0),
                                     (bw * 0.52, bh * 0.62), 0.15)
        # ...but not the eyes and mouth, which stay sharp.
        size = bw * 0.11
        for i, (mx, my) in enumerate(marks):
            if i == 2:          # the nose can be smoothed
                continue
            axes = (size * 1.6, size * 1.0) if i < 2 else (size * 1.5, size * 0.85)
            where *= 1.0 - _soft_ellipse((ch, cw), (mx - x0, my - y0), axes, 0.5)
        weight = np.ascontiguousarray(where * (0.4 + 0.55 * s))
        bgr[y0:y1, x0:x1] = cv2.blendLinear(smooth, crop, weight, 1.0 - weight)

    def _face_light(self, bgr, box, mask):
        """Changes bgr in place. Only the person is lit, never the room."""
        x0, y0, x1, y1 = self._face_region(bgr.shape, box, 2.4)
        if x1 - x0 < 24 or y1 - y0 < 24 or mask is None:
            return
        h, w = bgr.shape[:2]
        crop = np.ascontiguousarray(bgr[y0:y1, x0:x1])
        ch, cw = crop.shape[:2]
        bx, by, bw, bh = box
        # How dark the face is decides how much light it needs.
        fx0, fy0 = int(max(0, bx - x0)), int(max(0, by - y0))
        face = crop[fy0:fy0 + int(bh), fx0:fx0 + int(bw)]
        level = float(face.mean()) if face.size else 128.0
        gain = 1.0 + self.light * max(0.08, min(0.55, (150.0 - level) / 150.0 + 0.12))
        lit = cv2.convertScaleAbs(crop, alpha=gain, beta=4 * self.light)
        glow = _soft_ellipse((ch, cw), (bx + bw / 2 - x0, by + bh / 2 - y0), (bw * 0.8, bh * 0.95), 0.6)
        # The person mask, cut to the same place.
        mh, mw = mask.shape
        mx0, my0 = int(x0 * mw / w), int(y0 * mh / h)
        mx1, my1 = max(mx0 + 1, int(np.ceil(x1 * mw / w))), max(my0 + 1, int(np.ceil(y1 * mh / h)))
        person = cv2.resize(mask[my0:my1, mx0:mx1], (cw, ch), interpolation=cv2.INTER_LINEAR)
        person = np.clip((person - 0.3) / 0.45, 0.0, 1.0)
        # A wide, soft edge: no bright outline along the shoulders.
        person = cv2.GaussianBlur(person, (0, 0), max(2.0, cw * 0.02))
        weight = np.ascontiguousarray(glow * person)
        bgr[y0:y1, x0:x1] = cv2.blendLinear(lit, crop, weight, 1.0 - weight)
