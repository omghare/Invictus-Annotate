"""Tests for the 2.2 changes: security fixes, review, model assist, cloud bundles, exports, video tools."""
import io
import json
import os
import zipfile
from pathlib import Path

import pytest
import yaml
from PIL import Image

from conftest import hdr, login, make_images, make_project, make_user, tmp_dir
from test_api import BOX, import_folder, index, save


async def _project_with_images(client, admin, n=3, name="feat", size=(64, 48), classes=("car", "person")):
    pid = await make_project(client, admin, classes=classes)
    await import_folder(client, admin, pid, make_images(tmp_dir(name), [f"f_{i:03d}.png" for i in range(n)], size=size))
    return pid, [i["id"] for i in await index(client, admin, pid)]


# ── Classes ───────────────────────────────────────────────────────────────────

async def test_class_swap_does_not_merge(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=1, name="swap")
    await save(client, admin, ids[0], [{**BOX, "class_name": "car"}, {**BOX, "class_name": "person", "x": 30}])
    r = await client.patch(f"/api/projects/{pid}/classes",
                           json={"classes": ["person", "car"], "renames": {"car": "person", "person": "car"}},
                           headers=hdr(admin))
    assert r.status_code == 200
    anns = (await client.get(f"/api/annotations/{ids[0]}", headers=hdr(admin))).json()["annotations"]
    assert sorted((a["x"], a["class_name"]) for a in anns) == [(10, "person"), (30, "car")]


# ── Saving ────────────────────────────────────────────────────────────────────

async def test_stale_save_is_rejected_with_409(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=1, name="ver")
    v0 = (await client.get(f"/api/annotations/{ids[0]}", headers=hdr(admin))).json()["version"]
    first = await save(client, admin, ids[0], [BOX], base_version=v0)
    assert first.status_code == 200 and first.json()["version"] == v0 + 1
    stale = await save(client, admin, ids[0], [{**BOX, "x": 30}], base_version=v0)
    assert stale.status_code == 409
    ok = await save(client, admin, ids[0], [{**BOX, "x": 30}], base_version=v0 + 1)
    assert ok.status_code == 200


async def test_exported_frames_can_be_fixed(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=1, name="unlock")
    await save(client, admin, ids[0], [BOX])
    await client.post(f"/api/projects/{pid}/images/{ids[0]}/submit", headers=hdr(admin))
    await client.post(f"/api/projects/{pid}/export", json={"train_ratio": 1, "valid_ratio": 0, "test_ratio": 0},
                      headers=hdr(admin))
    assert (await index(client, admin, pid))[0]["state"] == "exported"
    r = await save(client, admin, ids[0], [{**BOX, "x": 5}])
    assert r.status_code == 200 and r.json()["state"] == "annotated"


async def test_viewing_does_not_claim_frames(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=1, name="noclaim")
    ann = await make_user(client, admin, "viewer")
    await client.post(f"/api/projects/{pid}/images/assign", json={"image_ids": ids, "user_id": ann["id"]}, headers=hdr(admin))
    await client.get(f"/api/annotations/{ids[0]}", headers=hdr(ann))
    assert (await index(client, admin, pid))[0]["state"] == "assigned"


# ── Review ────────────────────────────────────────────────────────────────────

async def test_review_reject_and_approve_flow(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=2, name="review")
    ann = await make_user(client, admin, "reviewee")
    await client.post(f"/api/projects/{pid}/images/assign", json={"image_ids": ids, "user_id": ann["id"]}, headers=hdr(admin))
    for iid in ids:
        await save(client, ann, iid, [BOX])
    await client.post(f"/api/projects/{pid}/images/submit-all-annotated", headers=hdr(ann))

    r = await client.post(f"/api/projects/{pid}/images/review",
                          json={"image_ids": [ids[0]], "action": "reject", "note": "box too loose"}, headers=hdr(admin))
    assert r.json()["count"] == 1
    body = (await client.get(f"/api/annotations/{ids[0]}", headers=hdr(ann))).json()
    assert body["state"] == "annotated" and body["review_status"] == "rejected" and body["review_note"] == "box too loose"
    fix = await client.get(f"/api/projects/{pid}/images?state=rejected&mine=true", headers=hdr(ann))
    assert [i["id"] for i in fix.json()["items"]] == [ids[0]]

    r = await client.post(f"/api/projects/{pid}/images/review",
                          json={"image_ids": [ids[1]], "action": "approve"}, headers=hdr(admin))
    assert r.json()["count"] == 1
    # Exporting only approved frames takes just the approved one
    ex = await client.post(f"/api/projects/{pid}/export",
                           json={"train_ratio": 1, "valid_ratio": 0, "test_ratio": 0, "only_approved": True},
                           headers=hdr(admin))
    assert ex.status_code == 200 and ex.json()["exported"] == 1
    workloads = (await client.get(f"/api/projects/{pid}/user-workloads", headers=hdr(admin))).json()
    assert workloads[ann["id"]]["approved"] == 1 and workloads[ann["id"]]["rejected"] == 1


# ── Access control / auth ─────────────────────────────────────────────────────

async def test_annotators_only_see_their_projects_and_images(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=2, name="acl")
    other_pid, other_ids = await _project_with_images(client, admin, n=1, name="acl2")
    ann = await make_user(client, admin, "scoped")
    await client.post(f"/api/projects/{pid}/images/assign", json={"image_ids": ids[:1], "user_id": ann["id"]}, headers=hdr(admin))

    assert (await client.get(f"/api/projects/{pid}", headers=hdr(ann))).status_code == 200
    assert (await client.get(f"/api/projects/{other_pid}", headers=hdr(ann))).status_code == 404
    assert (await client.get(f"/api/images/{other_ids[0]}/thumb", headers=hdr(ann))).status_code == 404
    assert (await client.get(f"/api/images/{ids[0]}/thumb", headers=hdr(ann))).status_code == 200
    assert (await client.get(f"/api/projects/{pid}/user-workloads", headers=hdr(ann))).status_code == 403


async def test_media_cookie_replaces_token_in_urls(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=1, name="cookie")
    client.cookies.clear()
    assert (await client.get(f"/api/images/{ids[0]}/file")).status_code == 401
    r = await client.post("/api/auth/session", headers=hdr(admin))
    assert r.status_code == 200 and "ia_media" in r.cookies
    client.cookies.set("ia_media", r.cookies["ia_media"])
    assert (await client.get(f"/api/images/{ids[0]}/file")).status_code == 200
    client.cookies.clear()


async def test_password_reset_revokes_sessions(client):
    admin = await login(client)
    ann = await make_user(client, admin, "revoked", "first-pass")
    assert (await client.get("/api/auth/me", headers=hdr(ann))).status_code == 200
    r = await client.patch(f"/api/users/{ann['id']}/password", json={"password": "temp-pass-1"}, headers=hdr(admin))
    assert r.status_code == 200
    assert (await client.get("/api/auth/me", headers=hdr(ann))).status_code == 401
    again = await login(client, "revoked", "temp-pass-1")
    assert again["must_change_password"] is True


async def test_login_is_throttled_and_passwords_validated(client):
    admin = await login(client)
    for _ in range(5):
        r = await client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
        assert r.status_code == 401
    r = await client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
    assert r.status_code == 429
    too_long = await client.post("/api/users", json={"username": "longpw", "password": "x" * 80}, headers=hdr(admin))
    assert too_long.status_code == 422
    too_short = await client.post("/api/users", json={"username": "shortpw", "password": "abc"}, headers=hdr(admin))
    assert too_short.status_code == 422


async def test_socket_refuses_revoked_tokens(client, monkeypatch):
    import app.socket_manager as sm

    async def no_room(sid, room):
        pass
    monkeypatch.setattr(sm.sio, "enter_room", no_room)
    admin = await login(client)
    ann = await make_user(client, admin, "sockuser")
    await sm.connect("sid-ok", {}, {"token": ann["_token"]})
    assert sm._connected.pop("sid-ok")["username"] == "sockuser"
    await client.delete(f"/api/users/{ann['id']}", headers=hdr(admin))
    with pytest.raises(ConnectionRefusedError):
        await sm.connect("sid-revoked", {}, {"token": ann["_token"]})


# ── Removing a user keeps the images ──────────────────────────────────────────

async def test_remove_user_discards_boxes_but_keeps_images(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=2, name="remove")
    ann = await make_user(client, admin, "leaver")
    await client.post(f"/api/projects/{pid}/images/assign", json={"image_ids": ids, "user_id": ann["id"]}, headers=hdr(admin))
    await save(client, ann, ids[0], [BOX])
    r = await client.post(f"/api/projects/{pid}/users/{ann['id']}/remove", json={"keep_annotated": False}, headers=hdr(admin))
    assert r.status_code == 200
    items = await index(client, admin, pid)
    assert len(items) == 2 and all(i["state"] == "unassigned" and i["box_count"] == 0 for i in items)


async def test_bulk_delete(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=4, name="bulkdel")
    await save(client, admin, ids[0], [BOX])
    r = await client.post(f"/api/projects/{pid}/images/delete", json={"image_ids": ids[:3]}, headers=hdr(admin))
    assert r.json()["deleted"] == 3
    assert [i["id"] for i in await index(client, admin, pid)] == ids[3:]


# ── Exports ───────────────────────────────────────────────────────────────────

async def test_export_hard_links_and_portable_yaml(client):
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=2, name="links")
    for iid in ids:
        await save(client, admin, iid, [BOX])
        await client.post(f"/api/projects/{pid}/images/{iid}/submit", headers=hdr(admin))
    r = (await client.post(f"/api/projects/{pid}/export",
                           json={"train_ratio": 0.5, "valid_ratio": 0.5, "test_ratio": 0}, headers=hdr(admin))).json()
    out = Path(r["output_path"])
    data = yaml.safe_load((out / "data.yaml").read_text(encoding="utf-8"))
    assert "path" not in data and data["train"] == "train/images"
    exported = next((out / "train" / "images").iterdir())
    assert r["linked"] == 2 and os.stat(exported).st_nlink >= 2


# ── Training guards ───────────────────────────────────────────────────────────

def test_dataset_yaml_resolution_matches_how_people_lay_out_folders():
    from app.trainer import resolve_data_yaml, _validate_dataset_sync
    root = tmp_dir("rf_dataset")
    for split in ("train", "valid"):
        make_images(root / split / "images", ["a.png"])
    # Roboflow style: no path, "../train/images"
    (root / "data.yaml").write_text("train: ../train/images\nval: ../valid/images\nnc: 1\nnames: [a]\ndownload: evil()\n")
    data = resolve_data_yaml(root / "data.yaml")
    assert Path(data["train"]) == (root / "train" / "images").resolve() and "download" not in data
    assert _validate_dataset_sync(root)["is_valid"]
    # "path: ." means the yaml's folder, not the server's working directory
    (root / "data.yaml").write_text("path: .\ntrain: train/images\nval: valid/images\nnames: [a]\n")
    assert Path(resolve_data_yaml(root / "data.yaml")["val"]) == (root / "valid" / "images").resolve()


def test_training_refuses_unsafe_model_paths():
    from fastapi import HTTPException
    from app.config import MODELS_DIR
    from app.trainer import resolve_model
    assert resolve_model("yolo11n.pt") == "yolo11n.pt"
    for bad in ("https://evil.example/x.pt", r"\\server\share\x.pt", "C:/Windows/x.pt", "../../x.pt"):
        with pytest.raises(HTTPException):
            resolve_model(bad)
    (MODELS_DIR / "run1").mkdir(parents=True, exist_ok=True)
    (MODELS_DIR / "run1" / "best.pt").write_bytes(b"x")
    assert resolve_model("run1/best.pt").endswith("best.pt")


async def test_training_params_are_bounded(client):
    admin = await login(client)
    r = await client.post("/api/train/start", json={"yaml_path": "x.yaml", "model": "yolo11n.pt", "dataset_name": "d",
                                                    "epochs": 0}, headers=hdr(admin))
    assert r.status_code == 422


# ── Model assist ──────────────────────────────────────────────────────────────

def test_label_check_finds_missed_wrong_class_and_loose_boxes():
    from app.qa import compare
    labels = [{"class_name": "car", "x": 0, "y": 0, "w": 10, "h": 10},          # good
              {"class_name": "car", "x": 50, "y": 0, "w": 10, "h": 10},         # model says person
              {"class_name": "car", "x": 0, "y": 50, "w": 20, "h": 20}]         # loose
    preds = [{"class_name": "car", "conf": 0.9, "x": 0, "y": 0, "w": 10, "h": 10},
             {"class_name": "person", "conf": 0.8, "x": 50, "y": 0, "w": 10, "h": 10},
             {"class_name": "car", "conf": 0.7, "x": 5, "y": 55, "w": 20, "h": 20},       # IoU ≈ 0.39
             {"class_name": "car", "conf": 0.95, "x": 80, "y": 80, "w": 10, "h": 10}]   # unlabelled
    kinds = sorted(i["type"] for i in compare(labels, preds))
    assert kinds == ["class", "loose", "missed"]


async def test_prelabel_job_fills_empty_frames_and_can_be_undone(client):
    from app import jobs
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=3, name="prelabel", size=(100, 100))
    await save(client, admin, ids[0], [BOX])                       # already labelled → left alone
    fixture = {"names": ["Car", "dog"], "fixture_boxes": [
        {"class_name": "Car", "conf": 0.9, "x": 10, "y": 10, "w": 30, "h": 30},
        {"class_name": "dog", "conf": 0.9, "x": 50, "y": 50, "w": 20, "h": 20},          # not a project class
        {"class_name": "Car", "conf": 0.1, "x": 60, "y": 10, "w": 10, "h": 10}]}         # below threshold
    r = await client.post(f"/api/projects/{pid}/assist/predict",
                          json={"model": "fixture.pt", "mode": "prelabel", "scope": "all", "conf": 0.25,
                                "fixture": fixture}, headers=hdr(admin))
    assert r.status_code == 200, r.text
    job = await jobs.wait(r.json()["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["frames"] == 2 and res["boxes"] == 2 and res["skipped"] == 1 and res["unknown_classes"] == {"dog": 2}

    items = {i["id"]: i for i in await index(client, admin, pid)}
    assert items[ids[1]]["state"] == "unassigned" and items[ids[1]]["box_count"] == 1    # suggestion, not "done"
    anns = (await client.get(f"/api/annotations/{ids[1]}", headers=hdr(admin))).json()["annotations"]
    assert anns[0]["source"] == "model" and anns[0]["class_name"] == "car" and anns[0]["confidence"] == 0.9

    undo = await client.post(f"/api/projects/{pid}/annotations/batches/{res['batch_id']}/undo", headers=hdr(admin))
    assert len(undo.json()["restored"]) == 2
    assert (await client.get(f"/api/annotations/{ids[1]}", headers=hdr(admin))).json()["annotations"] == []


async def test_check_job_reports_findings(client):
    from app import jobs
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=2, name="check", size=(100, 100))
    await save(client, admin, ids[0], [{**BOX, "x": 10, "y": 10, "w": 30, "h": 30}])
    await save(client, admin, ids[1], [{**BOX, "x": 60, "y": 60, "w": 30, "h": 30}])
    fixture = {"names": ["car"], "fixture_boxes": [{"class_name": "car", "conf": 0.9, "x": 10, "y": 10, "w": 30, "h": 30}]}
    r = await client.post(f"/api/projects/{pid}/assist/predict",
                          json={"model": "fixture.pt", "mode": "check", "scope": "labeled", "fixture": fixture},
                          headers=hdr(admin))
    job = await jobs.wait(r.json()["id"])
    assert job["status"] == "done" and job["result"]["with_issues"] == 1
    f = (await client.get(f"/api/projects/{pid}/assist/findings", headers=hdr(admin))).json()
    assert [x["image_id"] for x in f["findings"]] == [ids[1]]
    assert sorted(i["type"] for i in f["findings"][0]["issues"]) == ["extra", "missed"]
    pred = (await client.get(f"/api/annotations/{ids[1]}/predictions", headers=hdr(admin))).json()
    assert len(pred["boxes"]) == 1


async def test_predict_refuses_models_outside_models_folder(client):
    admin = await login(client)
    pid, _ = await _project_with_images(client, admin, n=1, name="badmodel")
    r = await client.post(f"/api/projects/{pid}/assist/predict", json={"model": "../../evil.pt"}, headers=hdr(admin))
    assert r.status_code == 400


# ── Cloud bundle round trip ───────────────────────────────────────────────────

async def test_cloud_bundle_and_result_import(client, monkeypatch):
    from app import jobs
    admin = await login(client)
    pid, ids = await _project_with_images(client, admin, n=4, name="cloud")
    for iid in ids:
        await save(client, admin, iid, [BOX])
    await client.post(f"/api/projects/{pid}/images/submit-all-annotated", headers=hdr(admin))
    r = await client.post(f"/api/projects/{pid}/cloud-bundle",
                          json={"train_ratio": 0.5, "valid_ratio": 0.5, "test_ratio": 0, "include_test": False,
                                "train": {"model": "yolo11s.pt", "epochs": 30}}, headers=hdr(admin))
    assert r.status_code == 200, r.text
    job = await jobs.wait(r.json()["id"])
    assert job["status"] == "done", job
    name = job["result"]["file"]
    dl = await client.get(f"/api/cloud/bundles/{name}", headers=hdr(admin))
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        names = set(zf.namelist())
        assert {"train.py", "train.ipynb", "README.md", "invictus_bundle.json", "dataset/data.yaml"} <= names
        assert sum(n.startswith("dataset/train/images/") and not n.endswith("/") for n in names) == 2
        bundle = json.loads(zf.read("invictus_bundle.json"))
        script = zf.read("train.py").decode()
        nb = json.loads(zf.read("train.ipynb"))
    ns = {"__name__": "invictus_train"}                       # run the top level (not main) like Python would
    exec(compile(script, "train.py", "exec"), ns)
    assert ns["BUNDLE"]["id"] == bundle["id"] and ns["TRAIN"]["epochs"] == 30 and nb["nbformat"] == 4
    # In a notebook the code cell only defines things (main() runs in its own cell)
    import sys, types
    monkeypatch.setitem(sys.modules, "ipykernel", types.ModuleType("ipykernel"))
    nb_ns = {"__name__": "__main__"}
    exec(compile("".join(nb["cells"][2]["source"]), "train.ipynb", "exec"), nb_ns)
    assert callable(nb_ns["main"]) and "".join(nb["cells"][4]["source"]).strip() == "main()"
    assert {i["state"] for i in await index(client, admin, pid)} == {"exported"}

    # The cloud run sends back a result zip — import it
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as zf:
        zf.writestr("invictus_result.json", json.dumps({
            "bundle_id": bundle["id"], "project": "cloud", "run_name": "cloud_run_x", "model": "yolo11s.pt",
            "metrics": {"metrics/mAP50(B)": 0.81}, "per_class": {"car": {"mAP50": 0.81}}, "epochs_done": 30,
            "train_config": {"epochs": 30}}))
        zf.writestr("weights/best.pt", b"weights")
        zf.writestr("plots/results.png", b"png")
        zf.writestr("../escape.txt", b"nope")
    r = await client.post("/api/train/import-result", files={"file": ("invictus_result_x.zip", result.getvalue())},
                          headers=hdr(admin))
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "cloud_run_x/best.pt" and "../escape.txt" not in r.json()["files"]
    runs = (await client.get("/api/train/runs", headers=hdr(admin))).json()["runs"]
    run = next(x for x in runs if x["name"] == "cloud_run_x")
    assert run["source"] == "cloud" and run["metrics"]["metrics/mAP50(B)"] == 0.81 and run["best_pt"]
    models = (await client.get("/api/assist/models", headers=hdr(admin))).json()["models"]
    assert any(m["name"] == "cloud_run_x/best.pt" for m in models)


# ── Video tools ───────────────────────────────────────────────────────────────

def test_near_duplicate_frames_are_dropped_but_small_motion_is_kept():
    from app.video_processor import DEDUPE_LEVELS, drop_near_duplicates, estimate_extraction
    folder = tmp_dir("dedupe")
    frames = []
    # 4 identical-but-noisy frames, then a small object (6 % of the frame) moving across a still scene
    for i in range(8):
        img = Image.new("L", (200, 150), 90 + (i % 2))                   # ±1 grey level of "noise"
        if i >= 4:
            x = 20 + (i - 4) * 40
            img.paste(230, (x, 60, x + 30, 90))
        f = folder / f"f{i}.png"
        img.save(f)
        frames.append(f)
    removed = drop_near_duplicates(frames, DEDUPE_LEVELS[1])
    kept = [f.name for f in frames if f.exists()]
    assert kept == ["f0.png", "f4.png", "f5.png", "f6.png", "f7.png"] and removed == 3
    est = estimate_extraction({"duration": 600, "fps": 30, "resolution": "1920x1080"}, 60, "png")
    assert est["fps"] == 30 and est["frames"] == 18000 and est["bytes"] > 40 * 1024 ** 3


def test_daily_backup_is_written_once():
    from app.config import BACKUPS_DIR
    from app.database import backup_db
    first = backup_db(keep=3)
    assert first is None or first.exists()
    assert backup_db(keep=3) is None                      # one per day
    assert list(BACKUPS_DIR.glob("invictus_*.db"))
