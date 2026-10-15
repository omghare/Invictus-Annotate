"""
Label checks — compare a frame's boxes with a model's predictions and describe likely
labelling mistakes. Pure functions (no DB / IO) so they are easy to test.

Issue types (each with a 0–1 weight that ranks frames worst-first):
  missed  the model is confident about an object nothing was drawn on
  class   a box sits where the model sees a different class
  loose   the box is roughly on the object but fits it poorly
  extra   nothing in the model's output (above its confidence threshold) supports this box
"""
from app.seq_utils import iou

MISSED_CONF = 0.5     # a prediction this confident with no box nearby → probably a missed label
CLASS_CONF = 0.5
MATCH_IOU = 0.5       # same object, good fit
NEAR_IOU = 0.3        # same object, poor fit


def compare(labels: list[dict], preds: list[dict]) -> list[dict]:
    """labels: [{class_name, x, y, w, h}]; preds: same + conf. Returns a list of issues."""
    issues: list[dict] = []
    used_preds: set[int] = set()

    for li, lab in enumerate(labels):
        best_same, best_same_iou = -1, 0.0
        best_any, best_any_iou = -1, 0.0
        for pi, p in enumerate(preds):
            ov = iou(lab, p)
            if p["class_name"] == lab["class_name"] and ov > best_same_iou:
                best_same, best_same_iou = pi, ov
            if ov > best_any_iou:
                best_any, best_any_iou = pi, ov
        if best_same >= 0 and best_same_iou >= MATCH_IOU:
            used_preds.add(best_same)
            continue
        if best_same >= 0 and best_same_iou >= NEAR_IOU:
            used_preds.add(best_same)
            issues.append({"type": "loose", "label": li, "class_name": lab["class_name"],
                           "iou": round(best_same_iou, 2), "conf": preds[best_same]["conf"],
                           "box": _box(preds[best_same]), "weight": 0.4})
            continue
        if best_any >= 0 and best_any_iou >= MATCH_IOU and preds[best_any]["conf"] >= CLASS_CONF:
            used_preds.add(best_any)
            issues.append({"type": "class", "label": li, "class_name": lab["class_name"],
                           "pred_class": preds[best_any]["class_name"], "conf": preds[best_any]["conf"],
                           "box": _box(preds[best_any]), "weight": 0.8})
            continue
        issues.append({"type": "extra", "label": li, "class_name": lab["class_name"],
                       "box": _box(lab), "weight": 0.3})

    for pi, p in enumerate(preds):
        if pi in used_preds or p["conf"] < MISSED_CONF:
            continue
        if any(iou(lab, p) >= NEAR_IOU for lab in labels):
            continue
        issues.append({"type": "missed", "class_name": p["class_name"], "conf": p["conf"],
                       "box": _box(p), "weight": 0.5 + 0.5 * p["conf"]})
    return issues


def score(issues: list[dict]) -> float:
    return round(sum(i["weight"] for i in issues), 3)


def _box(b: dict) -> dict:
    return {k: round(float(b[k]), 1) for k in ("x", "y", "w", "h")}
