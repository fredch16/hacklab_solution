"""Local target detection, tracking and view scoring. OpenCV only: no cloud, no Claude.

One code path for every target. A target is a Reference: a crop of a real camera frame the
operator confirmed (a drawn box, or the saved Hack Lab sticker crop). Finding it in a frame is
the same for both: SIFT features + RANSAC homography (ORB missed the rotated sticker on real
footage; SIFT found it at ~40 ms per 960x720 frame), falling back to normalized template matching
for low-texture crops. The tracker re-detects from the reference on every frame (the camera only
moves in discrete hops, and after every hop identity must be re-established anyway), so there is
no short-term tracker ID to trust blindly after a large viewpoint change.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

MIN_INLIERS = 10          # homography inliers needed to accept a feature match
MIN_INLIER_RATIO = 0.3    # ...and their share of the ratio-test matches
TEMPLATE_MIN = 0.90       # TM_CCOEFF_NORMED for a template match: a different white label scored 0.81, the real sticker 0.96+
MIN_CONFIDENCE = 0.35     # below this an observation does not count as the target
LOST_AFTER = 5            # consecutive misses before the tracker reports TARGET_LOST
COVERED_RATIO = 0.5       # at a fixed pose: fewer than this share of the settled inlier count = partly covered
PATCH_MIN_SIDE = 200      # small crops are upscaled to this before feature extraction

cv2.setNumThreads(1)      # OpenCV otherwise spreads SIFT over every core and starves Station and the motion loop
_sift = cv2.SIFT_create()
_sift_frame = cv2.SIFT_create(nfeatures=1500)          # frames: the strongest features are plenty
_matcher = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=32))   # approximate NN: much faster
ROI_SCALE = 2.2           # live tracking searches a window this much larger than the last box first
ROI_MIN_PX = 160
ROI_SMALL_PX = 96


MAX_W = 960               # every frame is downscaled to at most this width before recognition
OVERLAY_W = 640           # ...and the live overlay works on this width


def decode_gray(jpeg: bytes, reduced: bool = False, full: bool = False) -> np.ndarray:
    """Grayscale frame downscaled to MAX_W (OVERLAY_W with `reduced`). 720p is not needed for recognition;
    at 640 px the small far-away sticker (wide, home) lost too many features on the real arm, so scored looks keep 960."""
    import framing
    wh = framing.jpeg_size(jpeg)
    limit = 10 ** 6 if full else OVERLAY_W if reduced else MAX_W
    flag = cv2.IMREAD_REDUCED_GRAYSCALE_2 if wh and wh[0] >= 2 * limit else cv2.IMREAD_GRAYSCALE
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), flag)
    if img is not None and img.shape[1] > limit:
        img = cv2.resize(img, (limit, round(img.shape[0] * limit / img.shape[1])), interpolation=cv2.INTER_AREA)
    if img is None:
        raise ValueError("not a decodable JPEG")
    return img


class Reference:
    """What the target looks like: a grayscale crop from a confirmed real frame."""

    def __init__(self, patch: np.ndarray, label: str, kind: str):
        if patch.ndim != 2 or min(patch.shape) < 8:
            raise ValueError("reference crop is too small")
        self.patch, self.label, self.kind = patch, label, kind
        import uuid
        self.uid = uuid.uuid4().hex          # stable across process boundaries (the worker gets pickled copies)
        scale = max(1.0, PATCH_MIN_SIDE / min(patch.shape))
        big = cv2.resize(patch, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale > 1 else patch
        kps, self.desc = _sift.detectAndCompute(big, None)
        self.pts = np.float32([(k.pt[0] / scale, k.pt[1] / scale) for k in kps]) if kps else np.zeros((0, 2), np.float32)


    @classmethod
    def from_frame(cls, gray: np.ndarray, box: list[float], label: str, kind: str) -> "Reference":
        h, w = gray.shape
        x1, y1, x2, y2 = (int(round(box[0] * w)), int(round(box[1] * h)), int(round(box[2] * w)), int(round(box[3] * h)))
        return cls(gray[max(0, y1):min(h, y2), max(0, x1):min(w, x2)].copy(), label, kind)

    def save(self, stem: Path):
        stem.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(stem.with_suffix(".png")), self.patch)
        stem.with_suffix(".json").write_text(json.dumps({"label": self.label, "kind": self.kind,
                                                         "size": [self.patch.shape[1], self.patch.shape[0]]}))

    @classmethod
    def load(cls, stem: Path) -> "Reference | None":
        png, meta = stem.with_suffix(".png"), stem.with_suffix(".json")
        if not (png.exists() and meta.exists()):
            return None
        info = json.loads(meta.read_text())
        patch = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
        return None if patch is None else cls(patch, info.get("label", "target"), info.get("kind", "manual"))


def _box_of(quad: np.ndarray, w: int, h: int) -> list[float]:
    x1, y1 = quad.min(axis=0)
    x2, y2 = quad.max(axis=0)
    return [float(np.clip(x1 / w, 0, 1)), float(np.clip(y1 / h, 0, 1)), float(np.clip(x2 / w, 0, 1)), float(np.clip(y2 / h, 0, 1))]


def _features(ref: Reference, gray: np.ndarray) -> dict | None:
    if ref.desc is None or len(ref.pts) < MIN_INLIERS:
        return None
    kps, desc = _sift_frame.detectAndCompute(gray, None)
    if desc is None or len(kps) < MIN_INLIERS:
        return None
    good = []
    for pair in _matcher.knnMatch(ref.desc, desc, k=2):
        if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
            good.append(pair[0])
    if len(good) < MIN_INLIERS:
        return None
    src = np.float32([ref.pts[m.queryIdx] for m in good])
    dst = np.float32([kps[m.trainIdx].pt for m in good])
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if H is None:
        return None
    inliers = int(mask.sum())
    ratio = inliers / len(good)
    if inliers < MIN_INLIERS or ratio < MIN_INLIER_RATIO:
        return None
    ph, pw = ref.patch.shape
    quad = cv2.perspectiveTransform(np.float32([[0, 0], [pw, 0], [pw, ph], [0, ph]]).reshape(-1, 1, 2), H).reshape(4, 2)
    if not cv2.isContourConvex(quad.astype(np.float32).reshape(-1, 1, 2)):
        return None
    h, w = gray.shape
    area = cv2.contourArea(quad) / (w * h)
    sides = [np.linalg.norm(quad[i] - quad[(i + 1) % 4]) for i in range(4)]
    if not (2e-4 < area < 0.9) or min(sides) < 4 or max(sides) / min(sides) > 6 * max(pw / ph, ph / pw):
        return None
    conf = min(1.0, inliers / 40) * 0.6 + min(1.0, ratio) * 0.4
    return {"box": _box_of(quad, w, h), "quad": (quad / [w, h]).round(4).tolist(), "confidence": round(conf, 3),
            "inliers": inliers, "method": "features", "evidence": f"{inliers} inliers of {len(good)} matches"}


def _template(ref: Reference, gray: np.ndarray) -> dict | None:
    h, w = gray.shape
    k = 480 / w if w > 480 else 1.0
    small = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else gray
    best = None
    for s in (0.6, 0.75, 0.9, 1.0, 1.15, 1.35, 1.6):
        tw, th = int(ref.patch.shape[1] * k * s), int(ref.patch.shape[0] * k * s)
        if tw < 8 or th < 8 or tw >= small.shape[1] or th >= small.shape[0]:
            continue
        tpl = cv2.resize(ref.patch, (tw, th), interpolation=cv2.INTER_AREA)
        if float(tpl.std()) < 4:            # a flat patch matches anything
            continue
        res = cv2.matchTemplate(small, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if best is None or score > best[0]:
            best = (score, loc, tw, th)
    if best is None or best[0] < TEMPLATE_MIN:
        return None
    score, (x, y), tw, th = best
    box = [x / k / w, y / k / h, (x + tw) / k / w, (y + th) / k / h]
    return {"box": [round(float(v), 4) for v in box], "quad": None, "confidence": round(min(1.0, (score - 0.55) / 0.4), 3),
            "inliers": None, "method": "template", "evidence": f"correlation {score:.2f}"}


def _full(ref: Reference, gray: np.ndarray) -> dict | None:
    hit = _features(ref, gray) or _template(ref, gray)
    return hit if hit and hit["confidence"] >= MIN_CONFIDENCE else None


def _small_box(box, w, h):
    return max((box[2] - box[0]) * w, (box[3] - box[1]) * h) <= ROI_SMALL_PX


def detect(ref: Reference, gray: np.ndarray, hint: list | None = None) -> dict | None:
    """The target in this frame, or None. {box, quad, confidence, method, evidence}.
    `hint`: the last box (0..1); the window around it is searched first, the whole frame only if that fails."""
    if hint is not None:
        h, w = gray.shape
        cx, cy = (hint[0] + hint[2]) / 2 * w, (hint[1] + hint[3]) / 2 * h
        half_w = max((hint[2] - hint[0]) * w * ROI_SCALE, ROI_MIN_PX) / 2
        half_h = max((hint[3] - hint[1]) * h * ROI_SCALE, ROI_MIN_PX) / 2
        x1, x2 = int(max(0, cx - half_w)), int(min(w, cx + half_w))
        y1, y2 = int(max(0, cy - half_h)), int(min(h, cy + half_h))
        if x2 - x1 >= 32 and y2 - y1 >= 32 and (x2 - x1) * (y2 - y1) < 0.8 * w * h:
            roi = gray[y1:y2, x1:x2]
            hit = None
            if _small_box(hint, w, h):
                enlarged = cv2.resize(roi, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                hit = _features(ref, enlarged)
                if hit:
                    hit["evidence"] += " (2× region)"
            hit = hit or _full(ref, roi)
            if hit is not None:
                bw, bh = (x2 - x1) / w, (y2 - y1) / h
                hit["box"] = [x1 / w + hit["box"][0] * bw, y1 / h + hit["box"][1] * bh,
                              x1 / w + hit["box"][2] * bw, y1 / h + hit["box"][3] * bh]
                if hit.get("quad"):
                    hit["quad"] = [[x1 / w + qx * bw, y1 / h + qy * bh] for qx, qy in hit["quad"]]
                hit["evidence"] += " (near the last position)"
                return hit
    return _full(ref, gray)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Tracker:
    """initialize(reference) / update(gray, frame_id, stamp) -> observation | None / lost()."""
    ref: Reference
    misses: int = 0
    history: list = field(default_factory=list)   # last observations (dicts), newest last

    def update(self, gray: np.ndarray, frame_id: int, stamp: float) -> dict | None:
        return self.record(detect(self.ref, gray, self.hint()), frame_id, stamp)

    def hint(self) -> list | None:
        """The last box while tracking is fresh (searched first), else None (whole frame)."""
        return self.history[-1]["box"] if self.history and self.misses < 2 else None

    def record(self, hit: dict | None, frame_id: int, stamp: float) -> dict | None:
        """Book-keeping for a detection made elsewhere (the vision worker process)."""
        if hit is None:
            self.misses += 1
            return None
        prev = self.history[-1] if self.history else None
        hit.update(frame_id=frame_id, stamp=stamp,
                   stable=bool(prev and frame_id - prev["frame_id"] <= 3 and _iou(prev["box"], hit["box"]) > 0.5))
        self.misses = 0
        self.history = (self.history + [hit])[-10:]
        return hit

    def lost(self) -> bool:
        return self.misses >= LOST_AFTER or not self.history

    def status(self) -> str:
        if not self.history and self.misses == 0:
            return "SEARCHING"
        if self.lost():
            return "TARGET_LOST"
        return "PARTLY_HIDDEN" if self.misses else "TRACKING"


# ---------------------------------------------------------------- view quality

WEIGHTS = {"composition": 0.30, "edge_clearance": 0.25, "target_size": 0.20, "sharpness": 0.15, "stability": 0.10}
DESIRED_CENTER = (0.5, 0.5)
DESIRED_OCCUPANCY = 0.08   # share of the frame the target should cover: legible, with context around it
MIN_VALID_FRAMES = 3       # of the settled frames, this many must contain the target


def frame_components(box: list[float], gray: np.ndarray) -> dict:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    comp = 1.0 - min(1.0, math.hypot(cx - DESIRED_CENTER[0], cy - DESIRED_CENTER[1]) / 0.5)
    edge = min(1.0, min(box[0], box[1], 1 - box[2], 1 - box[3]) / 0.12)
    occ = max(1e-6, (box[2] - box[0]) * (box[3] - box[1]))
    size = math.exp(-(math.log(occ / DESIRED_OCCUPANCY) ** 2) / (2 * 0.8 ** 2))
    h, w = gray.shape
    crop = gray[int(box[1] * h):max(int(box[3] * h), int(box[1] * h) + 2), int(box[0] * w):max(int(box[2] * w), int(box[0] * w) + 2)]
    sharp = 1.0 - math.exp(-float(cv2.Laplacian(crop, cv2.CV_64F).var()) / 300.0) if crop.size else 0.0
    return {"composition": comp, "edge_clearance": max(0.0, edge), "target_size": size, "sharpness": sharp,
            "center": [round(cx, 4), round(cy, 4)], "occupancy": round(occ, 4)}


def frame_score(hit: dict, gray: np.ndarray) -> float:
    """Single-frame quality, for frames taken while the arm moves (no stability term)."""
    c = frame_components(hit["box"], gray)
    return round(sum(WEIGHTS[k] * c[k] for k in WEIGHTS if k != "stability") + WEIGHTS["stability"] * 0.5, 4)


def score_view(observations: list[dict | None], grays: list[np.ndarray]) -> dict:
    """Median quality over settled frames. Hard gate first: the target must be seen (with enough
    confidence) in at least MIN_VALID_FRAMES of them, else the view is invalid."""
    seen = [(o, g) for o, g in zip(observations, grays) if o is not None]
    out = {"frames": len(observations), "seen": len(seen)}
    if len(seen) < MIN_VALID_FRAMES:
        return {**out, "valid": False, "score": None, "reason": f"target seen in {len(seen)} of {len(observations)} frames"}
    comps = [frame_components(o["box"], g) for o, g in seen]
    med = {k: float(np.median([c[k] for c in comps])) for k in WEIGHTS if k != "stability"}
    centers = np.array([c["center"] for c in comps])
    med["stability"] = 1.0 - min(1.0, float(centers.std(axis=0).max()) / 0.03)
    score = sum(WEIGHTS[k] * med[k] for k in WEIGHTS)
    best = max(seen, key=lambda og: og[0]["confidence"])[0]
    return {**out, "valid": True, "score": round(score, 4), "components": {k: round(v, 3) for k, v in med.items()},
            "box": best["box"], "confidence": round(float(np.median([o["confidence"] for o, _ in seen])), 3),
            "center": comps[0]["center"], "occupancy": float(np.median([c["occupancy"] for c in comps])),
            "inliers": int(np.median([o["inliers"] or 0 for o, _ in seen])),
            "method": best["method"], "evidence": best["evidence"]}


# ---------------------------------------------------------------- vision worker process entry points
# The arm server runs these in a separate process so image processing never delays its event loop
# (Station joint data and the 30 Hz motion commands share that loop).

def _worker_init():
    import os
    try:
        os.nice(5)
    except OSError:
        pass


def look(ref: Reference, jpeg: bytes, reduced: bool = False, hint: list | None = None) -> dict | None:
    """Detect in one JPEG (near `hint` first); also attach a single-frame score."""
    import framing

    size = framing.jpeg_size(jpeg) if hint is not None else None
    native = size is not None and _small_box(hint, *size)
    gray = decode_gray(jpeg, reduced, full=native)
    hit = detect(ref, gray, hint)
    if hit is None and not native:
        gray = decode_gray(jpeg, full=True)
        hit = detect(ref, gray, hint)
        native = True
    if hit is not None:
        if native:
            hit["evidence"] += " (full resolution)"
        hit["frame_score"] = frame_score(hit, gray)
    return hit


def look_many(ref: Reference, jpegs: list[bytes]) -> tuple[list, dict]:
    """Settled frames at full size: the detections and the view score."""
    bank = _bank if any(r.uid == ref.uid for r in _bank) else [ref]
    grays = [decode_gray(j) for j in jpegs]
    hits = [max((detect(r, g) for r in bank), key=lambda h: h["confidence"] if h else -1) for g in grays]
    if sum(h is not None for h in hits) < MIN_VALID_FRAMES:      # small / far away: full resolution
        grays = [decode_gray(j, full=True) for j in jpegs]
        hits = [max((detect(r, g) for r in bank), key=lambda h: h["confidence"] if h else -1) for g in grays]
    if sum(h is not None for h in hits) < MIN_VALID_FRAMES and _vit is not None:   # textureless / new angle:
        hits = [track(ref, j, redetect=False) for j in jpegs]                       # the learned tracker's view
        grays = [decode_gray(j) for j in jpegs]
    return hits, score_view(hits, grays)


# ---------------------------------------------------------------- learned tracking (worker-process state)
#
# OpenCV's ViT tracker (opencv_zoo object_tracking_vittrack, ~0.7 MB ONNX) follows any object frame to frame
# through scale and viewpoint changes, textured or not, at a few ms per frame. It is trusted above VIT_MIN; below
# that SIFT re-finds the object from the reference bank and restarts it (or, if that fails, a box down to VIT_KEEP
# is kept). While ViT tracks confidently it learns the object's current look every LEARN_S: a crop for the bank
# (the drawn crop always stays) and a fresh ViT template, so side views and other distances keep being recognized. On real footage (21 Nov clip): ViT kept the sticker in 122/149 frames at 4 ms, SIFT 94/149.

VIT_MODEL = Path(__file__).resolve().parent.parent / "director_data" / "models" / "vittrack.onnx"
VIT_MIN = 0.5              # tracking score below this: the tracker is not trusted (it drifts to wrong boxes)
VIT_KEEP = 0.3             # ...unless the SIFT re-find also fails: then a weaker box still beats losing the object
VIT_LEARN = 0.65           # confident enough to learn the object's current look (side views look different)
LEARN_S = 1.5              # at most this often
BANK_MAX = 5
TRACK_W = 960

_vit = None
_bank: list = []
_learned_at = 0.0


def _bgr(jpeg: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is not None and img.shape[1] > TRACK_W:
        img = cv2.resize(img, (TRACK_W, round(img.shape[0] * TRACK_W / img.shape[1])), interpolation=cv2.INTER_AREA)
    return img


def vit_available() -> bool:
    return VIT_MODEL.exists() and hasattr(cv2, "TrackerVit_create")


def _vit_start(img: np.ndarray, box: list):
    global _vit
    if not vit_available():
        _vit = None
        return
    h, w = img.shape[:2]
    rect = (int(box[0] * w), int(box[1] * h), max(8, int((box[2] - box[0]) * w)), max(8, int((box[3] - box[1]) * h)))
    p = cv2.TrackerVit_Params()
    p.net = str(VIT_MODEL)
    _vit = cv2.TrackerVit_create(p)
    _vit.init(img, rect)


def target_init(ref: Reference, jpeg: bytes, box: list) -> bool:
    """Start tracking a newly selected target: the reference bank starts with its crop, ViT with its box."""
    global _bank
    _bank = [ref]
    _vit_start(_bgr(jpeg), box)
    return _vit is not None


def _detect_bank(gray: np.ndarray, hint=None) -> dict | None:
    best = None
    for ref in _bank:
        hit = detect(ref, gray, hint)
        if hit and (best is None or hit["confidence"] > best["confidence"]):
            best = hit
    return best


def track(ref: Reference, jpeg: bytes, hint: list | None = None, redetect: bool = True) -> dict | None:
    """The target in this frame: ViT first; if it is not confident, re-find with SIFT over the bank (full
    resolution if needed) and restart ViT there. `redetect=False` (during moves) skips the slow re-find."""
    global _bank, _learned_at
    if not _bank or all(r.uid != ref.uid for r in _bank):
        _bank = [ref]
    img = _bgr(jpeg)
    weak = None
    if _vit is not None:
        ok, r = _vit.update(img)
        score = float(_vit.getTrackingScore())
        if ok and score >= VIT_KEEP and r[2] > 4 and r[3] > 4:
            h, w = img.shape[:2]
            box = [max(0.0, r[0] / w), max(0.0, r[1] / h), min(1.0, (r[0] + r[2]) / w), min(1.0, (r[1] + r[3]) / h)]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            hit = {"box": box, "quad": None, "confidence": round(score, 3), "inliers": None, "method": "vit",
                   "evidence": f"learned tracker, score {score:.2f}"}
            hit["frame_score"] = frame_score(hit, gray)
            if score < VIT_MIN:
                weak = hit                 # try the re-find first; fall back to this box
            else:
                if score >= VIT_LEARN and time.monotonic() - _learned_at >= LEARN_S:
                    _learned_at = time.monotonic()
                    try:                   # the object as it looks from here (a side view, another distance)
                        new = Reference.from_frame(gray, box, ref.label, ref.kind)
                        if len(new.pts) >= MIN_INLIERS:
                            _bank = [_bank[0]] + (_bank[1:] + [new])[-(BANK_MAX - 1):]   # the drawn crop stays
                    except ValueError:
                        pass
                    _vit_start(img, box)   # follow its current look, not only the drawn one
                return hit
    if not redetect:
        return weak
    gray = decode_gray(jpeg)
    hit = _detect_bank(gray, hint)
    if hit is None:
        gray = decode_gray(jpeg, full=True)
        hit = _detect_bank(gray, hint)
    if hit is not None:
        hit["frame_score"] = frame_score(hit, gray)
        _vit_start(img, hit["box"])            # pick the learned tracker up again from here
        return hit
    return weak
