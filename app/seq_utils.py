"""
Pure helpers for frame sequences and box geometry.
No DB / IO here so everything is easy to unit-test.

Boxes are dicts in image-pixel space: {class_name, x, y, w, h, track_id?, ...}.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable, Optional

_NUM_RE = re.compile(r"(\d+)")


def natural_key(name: str) -> list:
    """Sort key so that img2 < img10 and frame_000009 < frame_000010."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in _NUM_RE.split(name)]


def seq_group_for(folder: Path) -> str:
    """Stable identifier for the folder (video / capture) a frame came from."""
    resolved = str(folder.resolve()) if folder else ""
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:6]
    return f"{folder.name}:{digest}"


# ── Geometry ──────────────────────────────────────────────────────────────────

def clip_box(x: float, y: float, w: float, h: float, img_w: int, img_h: int) -> Optional[tuple[int, int, int, int]]:
    """Clip a box to the image in corner space and round. Returns None if degenerate."""
    x1 = max(0.0, min(float(img_w), x))
    y1 = max(0.0, min(float(img_h), y))
    x2 = max(0.0, min(float(img_w), x + w))
    y2 = max(0.0, min(float(img_h), y + h))
    rx1, ry1, rx2, ry2 = round(x1), round(y1), round(x2), round(y2)
    if rx2 - rx1 < 1 or ry2 - ry1 < 1:
        return None
    return rx1, ry1, rx2 - rx1, ry2 - ry1


def fit_box(box: dict, src_w: int, src_h: int, dst_w: int, dst_h: int, min_px: int = 1) -> Optional[dict]:
    """
    Move a box from one image to another. If the sizes differ the box is scaled in
    normalised coordinates; the result is clipped to the target image.
    Returns None when the box ends up smaller than min_px.
    """
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    if src_w and src_h and (src_w != dst_w or src_h != dst_h):
        sx, sy = dst_w / src_w, dst_h / src_h
        x, y, w, h = x * sx, y * sy, w * sx, h * sy
    clipped = clip_box(x, y, w, h, dst_w, dst_h)
    if clipped is None or clipped[2] < min_px or clipped[3] < min_px:
        return None
    out = dict(box)
    out["x"], out["y"], out["w"], out["h"] = clipped
    return out


def iou(a: dict, b: dict) -> float:
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    iw = max(0.0, min(ax2, bx2) - max(a["x"], b["x"]))
    ih = max(0.0, min(ay2, by2) - max(a["y"], b["y"]))
    inter = iw * ih
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def lerp_box(a: dict, b: dict, alpha: float) -> dict:
    """Linear interpolation of x, y, w, h between two matched boxes (alpha in [0, 1])."""
    out = dict(a)
    for k in ("x", "y", "w", "h"):
        out[k] = a[k] + (b[k] - a[k]) * alpha
    return out


def match_boxes(A: list[dict], B: list[dict], img_w: int, img_h: int) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """
    Pair boxes between two keyframes. Returns (pairs, unmatched_a_idx, unmatched_b_idx).
    Order of preference:
      1. same track_id and class
      2. per class, exactly one box left on each side
      3. greedy lowest cost = (1 - IoU) + centre distance / diagonal,
         gated by (IoU > 0 or dist/diag < 0.25) and area ratio within [0.25, 4]
    """
    pairs: list[tuple[int, int]] = []
    used_a: set[int] = set()
    used_b: set[int] = set()

    # 1. explicit track ids
    for i, a in enumerate(A):
        tid = a.get("track_id")
        if not tid:
            continue
        for j, b in enumerate(B):
            if j in used_b or b.get("track_id") != tid or b["class_name"] != a["class_name"]:
                continue
            pairs.append((i, j)); used_a.add(i); used_b.add(j)
            break

    # 2. unique leftover per class
    classes = {a["class_name"] for a in A} | {b["class_name"] for b in B}
    for cls in classes:
        la = [i for i, a in enumerate(A) if i not in used_a and a["class_name"] == cls]
        lb = [j for j, b in enumerate(B) if j not in used_b and b["class_name"] == cls]
        if len(la) == 1 and len(lb) == 1:
            pairs.append((la[0], lb[0])); used_a.add(la[0]); used_b.add(lb[0])

    # 3. greedy by cost
    diag = max(1.0, (img_w ** 2 + img_h ** 2) ** 0.5)
    candidates = []
    for i, a in enumerate(A):
        if i in used_a:
            continue
        for j, b in enumerate(B):
            if j in used_b or a["class_name"] != b["class_name"]:
                continue
            ov = iou(a, b)
            acx, acy = a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
            bcx, bcy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
            dist = ((acx - bcx) ** 2 + (acy - bcy) ** 2) ** 0.5 / diag
            area_a, area_b = a["w"] * a["h"], b["w"] * b["h"]
            ratio = area_a / area_b if area_b > 0 else 0
            if not (ov > 0 or dist < 0.25) or not (0.25 <= ratio <= 4):
                continue
            candidates.append(((1 - ov) + dist, i, j))
    for _, i, j in sorted(candidates):
        if i in used_a or j in used_b:
            continue
        pairs.append((i, j)); used_a.add(i); used_b.add(j)

    unmatched_a = [i for i in range(len(A)) if i not in used_a]
    unmatched_b = [j for j in range(len(B)) if j not in used_b]
    return pairs, unmatched_a, unmatched_b


def ann_hash(anns: Iterable[dict]) -> str:
    """Order-independent fingerprint of a frame's boxes (used for undo conflict checks)."""
    canon = sorted(
        (a["class_name"], int(a["x"]), int(a["y"]), int(a["w"]), int(a["h"]))
        for a in anns
    )
    return hashlib.sha1(json.dumps(canon).encode("utf-8")).hexdigest()


def split_counts(n: int, ratios: list[float]) -> list[int]:
    """
    Largest-remainder rounding of n items into len(ratios) buckets.
    Bucket 0 (train) always gets at least one item when n > 0.
    """
    total = sum(ratios) or 1.0
    raw = [n * r / total for r in ratios]
    counts = [int(v) for v in raw]
    remainder = n - sum(counts)
    order = sorted(range(len(ratios)), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in order[:remainder]:
        counts[i] += 1
    if n > 0 and counts[0] == 0:
        donor = max(range(1, len(counts)), key=lambda i: counts[i])
        counts[donor] -= 1
        counts[0] += 1
    return counts
