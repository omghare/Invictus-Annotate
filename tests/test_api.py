import base64
import json
from pathlib import Path

import pytest
import yaml

from conftest import force_password_change, hdr, login, make_images, make_project, make_user, tmp_dir


async def import_folder(client, admin, pid, folder: Path) -> dict:
    r = await client.post(f"/api/projects/{pid}/import", json={"folder_path": str(folder)}, headers=hdr(admin))
    assert r.status_code == 200, r.text
    return r.json()


async def index(client, auth, pid) -> list[dict]:
    r = await client.get(f"/api/projects/{pid}/images/index", headers=hdr(auth))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def save(client, auth, iid, boxes, **extra):
    return await client.put(f"/api/annotations/{iid}", json={"annotations": boxes, **extra}, headers=hdr(auth))


BOX = {"class_name": "car", "class_id": 0, "x": 10, "y": 10, "w": 20, "h": 15}


# ── Auth ──────────────────────────────────────────────────────────────────────

async def test_default_admin_must_change_password(client):
    await force_password_change()
    admin = await login(client)
    assert admin["must_change_password"] is True
    # Enforced by the API, not just the browser
    blocked = await client.get("/api/projects", headers=hdr(admin))
    assert blocked.status_code == 403
    r = await client.patch("/api/auth/me/password",
                           json={"current_password": "admin123", "new_password": "better-pass"}, headers=hdr(admin))
    assert r.status_code == 200
    # The old token is revoked; the response carries a fresh one
    assert (await client.get("/api/projects", headers=hdr(admin))).status_code == 401
    fresh = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.get("/api/projects", headers=fresh)).status_code == 200
    again = await login(client, "admin", "better-pass")
    assert again["must_change_password"] is False


async def test_jwt_payload_is_base64url(client):
    """The browser must decode with base64url rules; make sure the payload fields exist."""
    admin = await login(client)
    part = admin["_token"].split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    assert payload["username"] == "admin" and "exp" in payload


# ── Security ──────────────────────────────────────────────────────────────────

async def test_video_routes_require_admin(client):
    for method, url, body in [
        ("get", "/api/video/stream?path=C:/Windows/win.ini", None),
        ("post", "/api/video/stats", {"filepath": "x.mp4"}),
        ("post", "/api/video/cut", {"filepath": "a.mp4", "save_path": "b.mp4", "start_sec": 0, "end_sec": 1}),
        ("get", "/api/video/pick_import", None),
        ("post", "/api/video/extract_frames", {"filepath": "a.mp4", "fps": 1, "project_id": "x"}),
    ]:
        r = await getattr(client, method)(url, **({"json": body} if body else {}))
        assert r.status_code == 401, (url, r.status_code)


async def test_video_stream_rejects_non_video_and_serves_ranges(client):
    admin = await login(client)
    r = await client.get("/api/video/stream", params={"path": __file__, "token": admin["_token"]})
    assert r.status_code == 400
    video = tmp_dir("vid") / "clip.mp4"
    payload = bytes(range(256)) * 400          # 102 400 bytes, contains many b"\n"
    video.write_bytes(payload)
    full = await client.get("/api/video/stream", params={"path": str(video), "token": admin["_token"]})
    assert full.status_code == 200 and full.content == payload
    part = await client.get("/api/video/stream", params={"path": str(video), "token": admin["_token"]},
                            headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == payload[100:200]


async def test_annotator_cannot_join_training_room(client):
    import app.socket_manager as sm
    sm._connected["sid-x"] = {"user_id": "u", "username": "u", "is_admin": False, "project_id": None}
    entered = []

    async def fake_enter(sid, room):
        entered.append(room)
    orig = sm.sio.enter_room
    sm.sio.enter_room = fake_enter
    try:
        await sm.join_training_room("sid-x")
    finally:
        sm.sio.enter_room = orig
        sm._connected.pop("sid-x", None)
    assert entered == []


# ── Import ────────────────────────────────────────────────────────────────────

async def test_import_same_filenames_from_two_videos(client):
    admin = await login(client)
    pid = await make_project(client, admin)
    a = make_images(tmp_dir("videoA"), ["frame_000001.png", "frame_000002.png"])
    b = make_images(tmp_dir("videoB"), ["frame_000001.png", "frame_000002.png"])
    assert (await import_folder(client, admin, pid, a))["imported"] == 2
    assert (await import_folder(client, admin, pid, b))["imported"] == 2
    assert (await import_folder(client, admin, pid, b))["skipped"] == 2
    items = await index(client, admin, pid)
    assert [i["seq"] for i in items] == [1, 2, 3, 4]
    assert len({i["seq_group"] for i in items}) == 2


async def test_import_natural_order(client):
    admin = await login(client)
    pid = await make_project(client, admin)
    folder = make_images(tmp_dir("natural"), ["img10.png", "img2.png", "img1.png"])
    await import_folder(client, admin, pid, folder)
    assert [i["filename"] for i in await index(client, admin, pid)] == ["img1.png", "img2.png", "img10.png"]


async def test_index_returns_more_than_200(client):
    admin = await login(client)
    pid = await make_project(client, admin)
    folder = make_images(tmp_dir("many"), [f"f_{i:04d}.png" for i in range(230)], size=(8, 8))
    await import_folder(client, admin, pid, folder)
    assert len(await index(client, admin, pid)) == 230


# ── Users / states ────────────────────────────────────────────────────────────

async def test_delete_user_keeps_their_annotated_work(client):
    admin = await login(client)
    pid = await make_project(client, admin)
    await import_folder(client, admin, pid, make_images(tmp_dir("du"), ["a.png", "b.png"]))
    ann = await make_user(client, admin, "worker")
    ids = [i["id"] for i in await index(client, admin, pid)]
    await client.post(f"/api/projects/{pid}/images/assign", json={"image_ids": ids, "user_id": ann["id"]}, headers=hdr(admin))
    assert (await save(client, ann, ids[0], [BOX])).status_code == 200

    r = await client.delete(f"/api/users/{ann['id']}", headers=hdr(admin))
    assert r.status_code == 200, r.text
    states = {i["id"]: i["state"] for i in await index(client, admin, pid)}
    assert states[ids[0]] == "annotated" and states[ids[1]] == "unassigned"
    # username is free again, old login no longer works
    await make_user(client, admin, "worker")


async def test_admin_viewing_unassigned_image_does_not_claim_it(client):
    admin = await login(client)
    pid = await make_project(client, admin)
    await import_folder(client, admin, pid, make_images(tmp_dir("view"), ["a.png"]))
    iid = (await index(client, admin, pid))[0]["id"]
    r = await client.get(f"/api/annotations/{iid}", headers=hdr(admin))
    assert r.json()["state"] == "unassigned"
    item = (await index(client, admin, pid))[0]
    assert item["state"] == "unassigned" and item["assigned_to"] is None


async def test_class_removal_keeps_labels_by_name(client):
    admin = await login(client)
    pid = await make_project(client, admin, classes=("helmet", "vest", "person"))
    await import_folder(client, admin, pid, make_images(tmp_dir("cls"), ["a.png"]))
    iid = (await index(client, admin, pid))[0]["id"]
    await save(client, admin, iid, [{**BOX, "class_name": "vest", "class_id": 1},
                                    {**BOX, "class_name": "helmet", "class_id": 0, "x": 30}])
    await client.patch(f"/api/projects/{pid}/classes", json={"classes": ["vest", "person"]}, headers=hdr(admin))
    anns = (await client.get(f"/api/annotations/{iid}", headers=hdr(admin))).json()["annotations"]
    by_name = {a["class_name"]: a["class_id"] for a in anns}
    assert by_name == {"vest": 0, "helmet": -1}      # vest re-indexed, helmet kept as orphan
    # A stale client sending the old index still resolves by name
    r = await save(client, admin, iid, [{**BOX, "class_name": "vest", "class_id": 1}])
    assert r.json()["annotations"][0]["class_id"] == 0


async def test_boxes_are_clipped_to_image(client):
    admin = await login(client)
    pid = await make_project(client, admin)
    await import_folder(client, admin, pid, make_images(tmp_dir("clip"), ["a.png"]))   # 64x48
    iid = (await index(client, admin, pid))[0]["id"]
    r = await save(client, admin, iid, [{**BOX, "x": 50.4, "y": -5, "w": 30, "h": 20},
                                        {**BOX, "x": 100, "y": 100, "w": 5, "h": 5}])
    body = r.json()
    assert body["dropped"] == 1
    a = body["annotations"][0]
    assert (a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]) == (50, 0, 64, 15)


async def test_submit_all_mine_and_background_frames(client):
    admin = await login(client)
    pid = await make_project(client, admin)
    await import_folder(client, admin, pid, make_images(tmp_dir("sub"), ["a.png", "b.png", "c.png", "d.png"]))
    ids = [i["id"] for i in await index(client, admin, pid)]
    ann = await make_user(client, admin, "sub_ann")
    await client.post(f"/api/projects/{pid}/images/assign", json={"image_ids": ids[2:], "user_id": ann["id"]}, headers=hdr(admin))
    await save(client, admin, ids[0], [BOX])
    await save(client, admin, ids[1], [], is_background=True)
    await save(client, ann, ids[2], [BOX])
    await save(client, ann, ids[3], [])                      # empty, not marked → stays

    r = await client.post(f"/api/projects/{pid}/images/submit-all-annotated?mine=true", headers=hdr(admin))
    assert r.json()["count"] == 2                            # admin's own two, background included
    r = await client.post(f"/api/projects/{pid}/images/submit-all-annotated", headers=hdr(ann))
    assert r.json()["count"] == 1 and r.json()["skipped_empty"] == 1
    stats = (await client.get(f"/api/projects/{pid}/stats", headers=hdr(admin))).json()
    assert stats["ready_for_export"] == 3 and stats["done"] == 4


async def test_contiguous_auto_assign(client):
    admin = await login(client)
    pid = await make_project(client, admin)
    await import_folder(client, admin, pid, make_images(tmp_dir("aa"), [f"f{i}.png" for i in range(7)]))
    u1 = await make_user(client, admin, "c1")
    u2 = await make_user(client, admin, "c2")
    r = await client.post(f"/api/projects/{pid}/images/auto-assign", json={"user_ids": [u1["id"], u2["id"]]}, headers=hdr(admin))
    assert r.status_code == 200
    owners = [i["assigned_to"] for i in await index(client, admin, pid)]
    assert owners == [u1["id"]] * 4 + [u2["id"]] * 3


# ── Sequence tools ────────────────────────────────────────────────────────────

async def _sequence_project(client, admin, n=8, name="seq"):
    pid = await make_project(client, admin)
    await import_folder(client, admin, pid, make_images(tmp_dir(name), [f"frame_{i:03d}.png" for i in range(n)], size=(200, 100)))
    return pid, [i["id"] for i in await index(client, admin, pid)]


async def test_propagate_only_empty_and_undo(client, no_socket_emits):
    admin = await login(client)
    pid, ids = await _sequence_project(client, admin, name="prop")
    await save(client, admin, ids[0], [BOX])
    await save(client, admin, ids[3], [{**BOX, "x": 50}])          # already annotated → skipped

    dry = await client.post(f"/api/projects/{pid}/annotations/propagate",
                            json={"source_image_id": ids[0], "count": 5, "dry_run": True}, headers=hdr(admin))
    assert dry.json()["summary"] == {"write": 4, "skip": 1}
    no_socket_emits.clear()
    r = await client.post(f"/api/projects/{pid}/annotations/propagate",
                          json={"source_image_id": ids[0], "count": 5}, headers=hdr(admin))
    body = r.json()
    assert len(body["applied"]) == 4
    assert [e for e, *_ in no_socket_emits].count("stats_update") == 1   # one stats emit per bulk op
    anns = (await client.get(f"/api/annotations/{ids[1]}", headers=hdr(admin))).json()["annotations"]
    assert anns[0]["source"] == "propagated" and anns[0]["track_id"]

    undo = await client.post(f"/api/projects/{pid}/annotations/batches/{body['batch_id']}/undo", headers=hdr(admin))
    assert len(undo.json()["restored"]) == 4
    assert (await client.get(f"/api/annotations/{ids[1]}", headers=hdr(admin))).json()["annotations"] == []
    again = await client.post(f"/api/projects/{pid}/annotations/batches/{body['batch_id']}/undo", headers=hdr(admin))
    assert again.status_code == 409


async def test_undo_reports_edited_frames_as_conflicts(client):
    admin = await login(client)
    pid, ids = await _sequence_project(client, admin, name="conflict")
    await save(client, admin, ids[0], [BOX])
    body = (await client.post(f"/api/projects/{pid}/annotations/propagate",
                              json={"source_image_id": ids[0], "count": 2}, headers=hdr(admin))).json()
    await save(client, admin, ids[1], [{**BOX, "x": 99}])
    undo = (await client.post(f"/api/projects/{pid}/annotations/batches/{body['batch_id']}/undo", headers=hdr(admin))).json()
    assert [c["image_id"] for c in undo["conflicts"]] == [ids[1]]
    assert [r["image_id"] for r in undo["restored"]] == [ids[2]]


async def test_propagate_respects_ownership(client):
    admin = await login(client)
    pid, ids = await _sequence_project(client, admin, name="own")
    a1 = await make_user(client, admin, "own1")
    a2 = await make_user(client, admin, "own2")
    await client.post(f"/api/projects/{pid}/images/assign", json={"image_ids": ids[:4], "user_id": a1["id"]}, headers=hdr(admin))
    await client.post(f"/api/projects/{pid}/images/assign", json={"image_ids": ids[4:], "user_id": a2["id"]}, headers=hdr(admin))
    await save(client, a1, ids[0], [BOX])
    r = await client.post(f"/api/projects/{pid}/annotations/propagate",
                          json={"source_image_id": ids[0], "target_image_ids": ids[1:6]}, headers=hdr(a1))
    reasons = {t["image_id"]: t["reason"] for t in r.json()["targets"]}
    assert reasons[ids[4]] == "forbidden" and reasons[ids[5]] == "forbidden"
    assert len(r.json()["applied"]) == 3
    denied = await client.post(f"/api/projects/{pid}/annotations/propagate",
                               json={"source_image_id": ids[0], "count": 3}, headers=hdr(a2))
    assert denied.status_code == 403


async def test_interpolate_midpoint_and_generated_rerun(client):
    admin = await login(client)
    pid, ids = await _sequence_project(client, admin, n=5, name="interp")
    await save(client, admin, ids[0], [{**BOX, "x": 0, "y": 0, "w": 20, "h": 20}])
    await save(client, admin, ids[4], [{**BOX, "x": 80, "y": 40, "w": 40, "h": 20}])
    r = await client.post(f"/api/projects/{pid}/annotations/interpolate",
                          json={"start_image_id": ids[0], "end_image_id": ids[4]}, headers=hdr(admin))
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["write"] == 3
    mid = (await client.get(f"/api/annotations/{ids[2]}", headers=hdr(admin))).json()["annotations"][0]
    assert (mid["x"], mid["y"], mid["w"], mid["h"]) == (40, 20, 30, 20)
    assert mid["source"] == "interpolated"

    # Fix frame 2 by hand → it becomes a keyframe; rerun with overwrite=generated bends motion around it
    await save(client, admin, ids[2], [{**BOX, "x": 60, "y": 20, "w": 30, "h": 20}])
    r = await client.post(f"/api/projects/{pid}/annotations/interpolate",
                          json={"start_image_id": ids[0], "end_image_id": ids[4], "overwrite": "generated"}, headers=hdr(admin))
    assert r.json()["summary"] == {"write": 2, "skip": 0, "keyframes": 3}
    f1 = (await client.get(f"/api/annotations/{ids[1]}", headers=hdr(admin))).json()["annotations"][0]
    assert f1["x"] == 30


async def test_interpolate_requires_keyframes(client):
    admin = await login(client)
    pid, ids = await _sequence_project(client, admin, n=4, name="nokf")
    await save(client, admin, ids[0], [BOX])
    r = await client.post(f"/api/projects/{pid}/annotations/interpolate",
                          json={"start_image_id": ids[0], "end_image_id": ids[3]}, headers=hdr(admin))
    assert r.status_code == 400


# ── Export ────────────────────────────────────────────────────────────────────

async def test_export_single_image_goes_to_train_and_yaml_is_valid(client):
    admin = await login(client)
    pid = await make_project(client, admin, classes=("bob's car", "a: b"))
    await import_folder(client, admin, pid, make_images(tmp_dir("exp1"), ["only.png"]))
    iid = (await index(client, admin, pid))[0]["id"]
    await save(client, admin, iid, [{**BOX, "class_name": "a: b", "x": 0, "y": 0, "w": 32, "h": 24}])
    await client.post(f"/api/projects/{pid}/images/{iid}/submit", headers=hdr(admin))
    r = await client.post(f"/api/projects/{pid}/export",
                          json={"train_ratio": 0.8, "valid_ratio": 0.1, "test_ratio": 0.1}, headers=hdr(admin))
    assert r.status_code == 200, r.text
    res = r.json()
    assert (res["train"], res["valid"], res["test"]) == (1, 0, 0)
    out = Path(res["output_path"])
    data = yaml.safe_load((out / "data.yaml").read_text(encoding="utf-8"))
    assert data["names"] == ["bob's car", "a: b"] and data["nc"] == 2
    label = (out / "train" / "labels" / "only.txt").read_text()
    assert label == "1 0.250000 0.250000 0.500000 0.500000"


def test_group_aware_split_keeps_runs_together():
    from app.export import SEQUENCE_CHUNK, plan_split
    imgs = [{"id": f"{g}{i}", "filename": f"{g}{i:03d}.png", "seq": i, "seq_group": g}
            for g in ("A", "B", "C", "D") for i in range(50)]
    train, valid, test = plan_split(imgs, [0.8, 0.1, 0.1], True, seed=1)
    assert len(train) + len(valid) + len(test) == 200
    assert 150 <= len(train) <= 170
    split_of = {x["id"]: n for n, part in enumerate((train, valid, test)) for x in part}
    chunk = max(1, min(SEQUENCE_CHUNK, len(imgs) // 20))
    for g in ("A", "B", "C", "D"):
        # consecutive frames of one video are cut into blocks; a block never straddles two splits
        for start in range(0, 50, chunk):
            block = {split_of[f"{g}{i}"] for i in range(start, min(start + chunk, 50))}
            assert len(block) == 1, (g, start, block)


def test_split_counts_largest_remainder():
    from app.seq_utils import split_counts
    assert split_counts(1, [0.8, 0.1, 0.1]) == [1, 0, 0]
    assert split_counts(10, [0.7, 0.2, 0.1]) == [7, 2, 1]
    assert sum(split_counts(7, [0.34, 0.33, 0.33])) == 7


def test_match_boxes_prefers_track_ids():
    from app.seq_utils import match_boxes
    A = [{"class_name": "car", "x": 0, "y": 0, "w": 10, "h": 10, "track_id": "t1"},
         {"class_name": "car", "x": 50, "y": 0, "w": 10, "h": 10, "track_id": "t2"}]
    # objects crossed over: geometry would pair them wrongly, track ids must win
    B = [{"class_name": "car", "x": 0, "y": 0, "w": 10, "h": 10, "track_id": "t2"},
         {"class_name": "car", "x": 50, "y": 0, "w": 10, "h": 10, "track_id": "t1"}]
    pairs, ua, ub = match_boxes(A, B, 100, 100)
    assert sorted(pairs) == [(0, 1), (1, 0)] and not ua and not ub


async def test_relink_after_folder_move(client):
    import shutil
    admin = await login(client)
    pid = await make_project(client, admin)
    old = make_images(tmp_dir("old_place"), ["a.png", "b.png"])
    await import_folder(client, admin, pid, old)
    new = tmp_dir("new_place") / "moved"
    shutil.move(str(old), str(new))
    chk = (await client.get(f"/api/projects/{pid}/files/check", headers=hdr(admin))).json()
    assert chk["missing"] == 2
    r = await client.post(f"/api/projects/{pid}/files/relink", json={"folder_path": str(new.parent)}, headers=hdr(admin))
    assert r.json()["relinked"] == 2
    chk = (await client.get(f"/api/projects/{pid}/files/check", headers=hdr(admin))).json()
    assert chk["missing"] == 0


def test_small_single_video_still_gets_validation_split():
    from app.export import plan_split
    imgs = [{"id": f"i{i}", "filename": f"f{i:03d}.png", "seq": i, "seq_group": "cam"} for i in range(20)]
    train, valid, test = plan_split(imgs, [0.8, 0.2, 0.0], True, seed=3)
    assert len(valid) >= 3 and len(train) >= 12 and not test
    two = plan_split(imgs[:2], [0.8, 0.2, 0.0], True, seed=3)
    assert len(two[0]) == 1 and len(two[1]) == 1


async def test_missing_file_is_reported_and_relink_accepts_identical_copies(client):
    import shutil
    admin = await login(client)
    pid = await make_project(client, admin)
    src = make_images(tmp_dir("gone_src"), ["x.png", "y.png"])
    await import_folder(client, admin, pid, src)
    iid = (await index(client, admin, pid))[0]["id"]
    # two identical "exports" containing the same files, then the original disappears
    for n in ("exp_a", "exp_b"):
        shutil.copytree(src, tmp_dir("exports") / n / "train" / "images")
    shutil.rmtree(src)
    body = (await client.get(f"/api/annotations/{iid}", headers=hdr(admin))).json()
    assert body["file_missing"] is True and body["filepath"]
    r = await client.post(f"/api/projects/{pid}/files/relink", json={"folder_path": str(tmp_dir("exports"))}, headers=hdr(admin))
    assert r.json() == {"relinked": 2, "unresolved": 0, "dry_run": False}
    assert (await client.get(f"/api/annotations/{iid}", headers=hdr(admin))).json()["file_missing"] is False
