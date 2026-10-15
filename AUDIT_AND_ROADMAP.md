# Invictus Annotate: Audit & Roadmap

*Prepared 2026-10-02.*

## Implementation status (version 2.2)

| Area | Done | Not done (on purpose) |
|---|---|---|
| Bugs | B1–B26, nearly all of the low-severity list | Multi-select of boxes; full incremental dashboard grid updates (P5, partially done: bulk delete, maps by id) |
| Security | S1–S7, S9 (headers, docs off, socket same-origin), S10 (requirements) | S8 path allow-list (admin-only; kept simple) · PyJWT swap (python-jose ≥ 3.4 is patched) |
| Limitations | P1 hard-linked exports, P2 weights linked once, P4, P5 (part), P6 job system, P7 exported frames editable, P9 review, P10, P11 run history, P12 install profiles | P8 COCO/VOC import/export (F4) · P13 polygons (F16) |
| Housekeeping | H2 (−138.6 MB freed), H4, H5 daily backups, H6 log file + token masking, H7 tests (26 → 49) | **H1 git cleanup and H3 deleting old runs/exports — waiting for your OK (they delete data)** |
| Features | F1 pre-labelling, F2 label-error finder, F3 review (lean), F7 smart extraction, F9 speed pack (subset), F10 jobs, F11-lite (picker timeouts), F12-lite (run history, resume, fine-tune, per-class view), F13 Kaggle/Colab bundle + result import, F15-lite (System page), G0 compute/provider layer | F4, F5 (versions), F6, F8, F14, F16, G1–G7 — the base for G is in place (`app/compute.py`, `app/infer_worker.py`) |

Verified: 49 automated tests pass. In a sandbox copy of the app (fresh database, real model, real images):
- **Pre-labelling:** 99 frames in ~35 s on this CPU; accepting with Enter works.
- **Label check:** found the deliberately wrong labels.
- **Review:** approve and send-back work, and the reason shows on the frame.
- **Live edits:** a class rename and a frame deletion reach an open annotator tab correctly.
- **Cloud bundle:** built, then its generated `train.py` was run end to end, and the result zip was imported.
- **Training:** a local run produced hard-linked weights.
- **Frame extraction:** a real run with the fps cap and duplicate skipping.
- **Migration:** run on a copy of the real database (integrity OK, 12 existing runs imported, existing logins kept).

---

How to use this: every item has an ID (B = bug, S = security, P = limitation/performance, H = housekeeping, F = feature for now, G = dormant GPU feature). Tell me which IDs to build. Effort: **S** = under half a day, **M** = 1–2 days, **L** = 3–5 days, **XL** = 1–2 weeks.

---

## 0. What I looked at

| | |
|---|---|
| Code | 14,174 lines: Python backend (FastAPI + Socket.IO + SQLite), vanilla JS frontend, Jinja templates. I read every backend file in full. Two helper agents read all the JS and templates, and I re-checked every high-severity claim against the code. |
| Tests | 26/26 pass (26.5 s). Nothing in `trainer.py` / `train_worker.py`, the JS, or about half the API routes is tested. |
| Current data | 5 projects, 363 images (640×640), 238 boxes, 4 users, 12 training runs |
| This laptop | HP 15s, Ryzen 3 3250U (2 cores/4 threads), 13.9 GB RAM, AMD Radeon Vega iGPU (no CUDA), NVMe SSD. **D: has only 11.3 GB free**; C: has 62 GB. |
| Measured here | YOLO11n inference **260 ms/image** at 640 px on CPU; YOLO11s **700 ms/image** |
| Installed stack | torch 2.13.0+**cpu**, ultralytics 8.4.30, OpenCV 5.0, python-socketio 5.16 ↔ socket.io client 4.7.5 (compatible) |

**Useful discovery:** the installed Ultralytics already includes SAM, SAM 2.1, **SAM 3**, MobileSAM, FastSAM, YOLO-World, YOLOE and RT-DETR. OpenCV 5 ships the `TrackerVit`/`TrackerNano`/`TrackerMIL` trackers. So most AI features below need **no new Python packages**, only model weights (downloaded when a feature is turned on) and, on the GPU machine, the CUDA build of PyTorch.

**What's already good:** SQL is parameterised everywhere, user strings are consistently escaped (no XSS found apart from one self-XSS), and the sequence tools are well thought out (carry, propagate, interpolate, plus undo batches with conflict detection). Exports use sequence-aware splits that prevent train/val leakage (many tools skip this). The filmstrip is virtualised, and training runs in a subprocess so a crash never takes the web server down.

---

## 1. Bugs

### 1a. High: data corruption or a stuck UI

| ID | Where | What happens | Fix | Effort |
|---|---|---|---|---|
| **B1** | `app/projects.py:202-209` | **Swapping class names merges them.** Renaming cat→dog and dog→cat runs two UPDATEs in sequence: first every cat becomes dog, then every dog becomes cat. Every box ends up "cat". Chains (A→B, B→C) break the same way. | Rename through temporary names in one transaction; reject cycles that can't be resolved. | S |
| **B2** | `static/js/annotate.js:79, 1220-1227` | **An admin changing classes silently corrupts open annotator tabs.** `project_updated` carries no rename map, and `clsName()` prefers the class *index*. After a rename or reorder, the next save, Ctrl+Z or R writes the old or wrong class back. That frame then becomes an orphan class, which is skipped at export. | Send the renames in the event; remap boxes, undo history, `prevFrame` and `carrySrc` by name; make the name the source of truth. | M |
| **B3** | `annotate.js:1210-1218, 146, 379-386` | **The annotator freezes when its current image is deleted, reassigned or no longer saveable.** `applyFilter()` sets `idx=-1`, so `loadFrame(-1)` is a no-op; `cur=null` then makes save a no-op. Navigation either says "Could not save… staying here" forever or throws a TypeError that is silently swallowed. The same trap happens after any 403/400/404 from a save. | Reset frame state and jump to the nearest frame; on a 4xx save error offer "Discard & continue". | M |
| **B4** | `app/annotations.py:164`, `annotate.js` (no `annotation_saved` handler) | **Last write wins, with no warning.** An admin fixes a frame in Review while the annotator has it open; the annotator nudges a box and silently overwrites the fix (and vice versa). | Add a version column and send it with the PUT (409 on mismatch), plus a live "someone else changed this frame" banner. | M |
| **B5** | `annotate.js:1179-1189` | **Keepalive save on tab-hide can mark later edits as saved.** Its `.then` marks the frame clean without checking whether you edited again in the meantime. Those edits are lost, and the "unsaved changes" warning no longer fires. | Mark clean only if the sent version still matches; route through the normal save queue. | S |
| **B6** | `annotate.js:1133` | **Escape during a move/resize doesn't undo it.** The box looks moved, isn't saved, still gets carried/copied to the next frame, and is saved later by an unrelated edit. | Restore `drag.before` on Escape. | S |
| **B7** | `templates/training.html:274`, `static/js/training.js:131-137` | **On the GPU machine, training will run on the CPU by default.** The device field is hard-coded to `cpu`; the server's `default_device` is ignored, and the only warning covers the opposite mismatch. This will bite the day you move to the RTX laptop. | Default to the detected GPU; warn when CUDA is available but the device is `cpu`. | S |
| **B8** | `app/trainer.py:810-830` | **The dataset validator disagrees with how Ultralytics resolves paths.** A Roboflow-style yaml (`train: ../train/images`, no `path`) is rejected, so Launch stays disabled. Conversely, `path: .` passes validation but fails in training (the worker's working directory is the server root). Moved app exports also fail (the absolute path is baked into `export.py:92`). | Mirror Ultralytics' rules, or write a normalised temp yaml with absolute paths and set the worker's working directory. | M |
| **B9** | `training.js:1045-1141` | **A second run is drawn on top of the first.** `beginTraining` never resets charts, log, results panel or stats, so "Best" and "Δ last 5" span both runs. | `resetRunUI()` on start and whenever the `session_id` changes. | S |

### 1b. Medium: wrong results or wasted time

| ID | Where | What happens | Fix | Effort |
|---|---|---|---|---|
| **B10** | `training.js:1250-1279` | A NaN loss (common with a high LR or AMP) becomes `null` and `.toFixed` throws. Live charts stop, and restoring after a reload breaks for that session. | Guard with `Number.isFinite`. | S |
| **B11** | `training.js:89-92, 1355` | Epochs missed during the 2nd+ reconnect (laptop sleep, Wi-Fi drop) are never recovered, leaving gaps in charts and log. | Track the last rendered epoch; replay the rest on every reconnect. | S |
| **B12** | `trainer.py:969-976`, `training.js:1330-1346` | No status event when a run starts: other tabs see nothing until epoch 1 (minutes on CPU) and can hit a 409. Elapsed time counts from the *previous* run. | Emit status in `/start`; reset when `session_id` changes. | S |
| **B13** | `training.js:106-118, 607` | If you validate a dataset before system info arrives (the first probe imports torch and takes a while), the recommendations assume CPU and never refresh. | Await a system-info promise. | S |
| **B14** | `trainer.py:996-1018` | Stop blocks the request for up to about 40 s; the banner Stop button stays visible and a second click returns a 400. | Return 202 at once and finish in the background; disable both Stop buttons. | S |
| **B15** | `trainer.py:64-84`, `training.js:1353` | Stopped/failed runs aren't restored after a page reload. All run state lives in memory and disappears when the server restarts, so you can't see why a run failed. | Persist runs in the DB (see F12). | M |
| **B16** | `annotate.js:1128-1131` | Enter (accept carried boxes) also "clicks" whichever toolbar button has focus: duplicates boxes, toggles draw mode, reopens dialogs. | `preventDefault`; blur buttons after a mouse click. | S |
| **B17** | `annotate.js:719` | Ctrl+Enter (submit) is silently ignored on a fresh frame until autosave finishes. | `flushSave()` first, then submit. | S |
| **B18** | `annotate.js:359-362`, `annotations.py:133-141` | Holding F or an arrow builds an unbounded navigation queue that overshoots. Merely *viewing* a frame flips it from assigned to "in progress" and broadcasts stats. | Drop key-repeat while navigating; claim a frame on first edit, not on view. | S |
| **B19** | `annotate.js:1251, 270, 286` | Zoom/pan resets on every frame change and every window resize, which hurts when labelling small objects in video. | Keep the transform; add "keep zoom across this sequence". | S |
| **B20** | `static/js/video_processor.js:47-58, 100` | Files the browser can't play (MKV/HEVC) can't be cut (the range stays 0). Opening a second video reuses the first video's in/out range. | Reset the range in `open()` from the probe duration; allow typed times. | S |
| **B21** | `app/video_processor.py:520-536` | Frame extraction has **no fps cap and no disk-space check.** A 10-min 1080p video at 30 fps as PNG is about 18k frames × ~3 MB ≈ 54 GB, nearly five times what D: has free. | Estimate the output size first; refuse or warn; cap fps at the source fps. | S |
| **B22** | `app/auth.py:24-25`, `app/models.py` | **Verified:** bcrypt 5 raises on passwords over 72 bytes, so creating a user or changing a password with a long passphrase returns HTTP 500. | Validate length (≤72 bytes) in the model. | S |
| **B23** | `projects.py:261`, `images.py:570-578` | EXIF orientation is ignored: phone/camera photos stored rotated look **stretched** in the annotator (the browser rotates, the DB stores raw width/height) and rotated differently in thumbnails. Exported labels stay correct by luck (normalised coordinates). High confidence; I haven't reproduced it with a real phone photo. | `ImageOps.exif_transpose` when reading sizes and making thumbnails. | S |
| **B24** | `static/css/app.css:458 vs 463` | Below 860 px wide (small laptop window, tablet) the side menu can't be opened: a later `display:none !important` overrides the media-query rule. | Reorder or scope the rule. | S |
| **B25** | `static/js/dashboard.js:373, 387` | In the Distribute dialog, "None" re-checks every annotator, so work can be distributed to everyone by mistake. | Apply the default only when nothing is stored yet. | S |
| **B26** | `app/projects.py:329-333`, `trainer.py:770` | Folder/file pickers (tkinter) open **on the server's screen**. From another LAN PC the request hangs: forever for import (no timeout), 300 s for training. | See F11 (web folder browser) plus a timeout. | S |

### 1c. Low: smaller defects, grouped by file

- **annotate.js:**
  - Shift+Tab with nothing selected picks the wrong box (1126); the loading spinner can stick after fast navigation (282).
  - Wheel zoom uses a fixed 15 % step: trackpads zoom far too fast, and a two-finger horizontal scroll zooms out (643).
  - Interpolate looks for keyframes only among *filtered* frames, so it fails under the "To do" filter (999); the propagate preview ignores the server's cross-video warning (884); the preview can fire after its dialog closes (985).
  - "Undo propagate" is stored per project rather than per user, so on a shared PC it returns 403 (61).
  - A failed load/save is never retried (300, 172); R copies from the last *visited* frame, not the previous frame in the sequence (385); track IDs assigned by the server aren't synced back to the open frame (902).
  - For admins, "Add All Annotated Images to Dataset" submits *every annotator's* frames (728); keyboard edits during a drag write to the wrong box or throw (597, 609, 629); the H tooltip says "hold", but H only toggles (`annotate.html:83`).
- **training.js:**
  - Clearing the custom-model box leaves a stale model selected (356); "params tuned for this model" isn't true, since the backend ignores the model (347; `trainer.py:743`).
  - A 409 resets the button to idle during an active run (1076); the completion handler fires 3× when `best_pt` is null (1290); every visit re-announces the last completed run (1354).
  - Re-validating doesn't clear the previous dataset's panels (553); "Best" covers only the last 200 points (1454); the YAML picker hides the real error (600); the ETA row is stale after a run (1129); the session tag is never filled (`training.html:504`).
  - The default **75 % VRAM cap** can cause OOM on a 6 GB GPU where an uncapped run would fit (`train_worker.py:108-118`).
- **trainer.py:**
  - No server-side bounds on epochs/imgsz/workers (98-110); a failed version probe is cached until restart (439); both checkpoints are copied *every epoch* even when best didn't change, possibly catching a half-written file (609-610).
- **dashboard.js:**
  - The export split handle can stay "stuck" after the mouse is released outside the bar, and validation can be dragged to 0 % (540-560); the shift-click range anchor carries over across pages (164-170).
- **core.js:**
  - Nothing refreshes after a socket reconnect (423); logging in as another user in a 2nd tab makes the 1st tab save under the new identity (46/84); a modal action that throws leaves the dialog open (327).
- **Backend:**
  - Undo restores the old assignee even if the frame was reassigned since (`sequence.py:497`); two simultaneous exports can pick the same output folder (`export.py:104`); deleting an image/project leaves orphan undo rows (`annotation_batch_items`).
  - A password reset by the admin sets `must_change_password=0`; it should force a change (`projects.py:519`).
  - Thumbnail generation shares one `.tmp` name, so concurrent requests can occasionally 500 on Windows (`images.py:576`; plausible, not reproduced).
- **Tests:**
  - `test_api.py:343` asserts something that is always true, so the sequence-grouping split is effectively untested.
  - The module-wide asyncio mark causes 4 warnings.
  - `conftest.py` doesn't isolate `models/` and `training_runs/` (any future trainer test would write into the real folders), and its temp folder is never deleted.

---

## 2. Security

Context: this is a trusted-LAN tool. None of these matter much on a home network with two trusted users. They matter on an office/college LAN, or if it is ever exposed to the internet (don't port-forward it).

| ID | Issue | Fix | Effort |
|---|---|---|---|
| **S1** | **An admin token gives code execution on the server.** The training `model` field accepts URLs/UNC paths, which Ultralytics downloads and loads with pickle (`torch.load`, `weights_only=False`). A dataset yaml with a `download:` key gets executed by Ultralytics (`trainer.py:911-929` → `train_worker.py:200`). Combined with S4, that is a real risk on a shared LAN. | Model limited to known names or `.pt` files under `models/`; reject URL/UNC; strip `download:` by passing a sanitised yaml copy; restrict dataset paths to allowed roots. | S |
| **S2** | **Login tokens in URLs.** Every `<img>`/video/download URL carries `?token=` (`core.js:132-133`). Uvicorn's access log prints them; they end up in browser history; a copied image link is a 24-hour full-account key. Logout and password changes don't revoke anything. | Short-lived per-image signed URLs (or an HttpOnly media cookie); a token version per user (bumped on logout/password change/deactivate); redact `token=` in logs. | M |
| **S3** | **"Must change password" is enforced only in the browser** (`core.js:85`). On a fresh install the API works with `admin/admin123`, and the server binds to `0.0.0.0` (`run.py:30`), so anyone on the LAN can reach it. | Enforce it server-side (403 on everything except the password change); optional `HOST` setting. | S |
| **S4** | No login rate limit or lockout; minimum password length is 4 for users and 6 for your own (`models.py:37, 47, 67`). | Per-IP/user backoff; minimum 8. | S |
| **S5** | **Missing per-project access checks:** any logged-in user can fetch any image/thumbnail by ID (`images.py:558-597`), any project's details (`projects.py:135`), workloads and class usage, and can join **any project's live socket room** (`socket_manager.py:308-326`). | Scope checks: admin, or has images in the project. | S |
| **S6** | The socket server trusts the token's contents only: a deactivated or demoted admin keeps live access (including training logs and server paths) for up to 24 h (`socket_manager.py:264-297`). | Look up the user in the DB on connect and on room joins. | S |
| **S7** | **Open redirect on login:** `/login?next=/%5Cevil.com` passes the check and sends a signed-in user to another site automatically (`login.html:61`). | Accept `next` only when `new URL(next, origin).origin === origin`. | S |
| **S8** | Admin path inputs (import, relink, validate-dataset, video stream) can probe the whole server filesystem, and `validate-dataset` can crawl up to 200k files. | Allowed roots in settings. | S |
| **S9** | Socket CORS `*`; no security headers/CSP; public `/api/docs`; a self-XSS in training warnings (`training.js:972`). | Tighten. | S |
| **S10** | Dependencies: `python-jose` (works; PyJWT is leaner and better maintained); `passlib` is listed but unused (and incompatible with bcrypt 5); `pynvml` duplicates `nvidia-ml-py`. | Clean up `requirements.txt`. | S |

---

## 3. Limitations that slow you down

| ID | Limitation | Why it matters | Fix | Effort |
|---|---|---|---|---|
| **P1** | **Every export copies every image** (`export.py:201`). `dataset/` already holds 8 near-identical exports. | At 10k images of 400 KB, each export costs another 4 GB. D: has 11 GB. | Hard-link images (same NTFS volume, near-zero space) with a copy fallback; or versioned exports (F5). | S |
| **P2** | **Weights are stored twice.** Each epoch copies best+last from `training_runs/<run>/weights` into `models/<run>/` (`trainer.py:609-610`). | `models/` (149 MB) is entirely duplicates; every future run doubles. | Hard-link, or copy once at the end and only when best changed. | S |
| **P3** | `.git` is 201 MB, but the commit only needs a few hundred KB. **Verified:** about 200 MB of model weights from your first commit, before the amend, are still in the local pack as unreachable objects. GitHub is fine (`origin/main` = the amended commit). | Wasted space on D:. | `git reflog expire --expire=now --all && git gc --prune=now` (I'll ask before running). | S |
| **P4** | Box counts are computed by scanning the **whole** annotations table on every list/index/sequence call (`images.py:30, 148`; `sequence.py:69`). | Instant now (238 boxes); noticeably slow at 100k+. | A `box_count` column on images, or a filtered correlated subquery. | S |
| **P5** | Bulk socket events are handled O(m·n) in the annotator (`annotate.js:1192-1213`); the dashboard deletes images **one HTTP request each** (`dashboard.js:223`); the grid fully redraws on every change (634-654); the file check runs on every visit (91-102). | Assigning/exporting 20k frames freezes open tabs; deleting 2k images means 2k requests. | Map by id, bulk-delete endpoint, incremental grid updates. | M |
| **P6** | Long jobs (import, export, frame extraction, Stop) run inside a single HTTP request: no queue, no cancel, nothing survives a reload. | Big imports/exports look hung; a closed tab means an unknown state. | Job system (F10). | M |
| **P7** | **Exported images are locked forever** (`annotations.py:182`). | The usual moment you find a wrong label is *after* training, and right now it can't be fixed. | Dataset versions (F5): versions are frozen, images stay editable. | M |
| **P8** | **No import of existing labels** (YOLO/COCO/VOC), and export is YOLO-detect only. | Roboflow/public datasets (like your `Test_Dataset`) come in with no labels. | F4. | M |
| **P9** | No review step: annotators submit straight to "ready for export"; the admin's only lever is Delete. Saving re-creates every box with new IDs and overwrites `created_by` (`annotations.py:65-75`). | No quality control, no per-person accuracy, no history of who changed what. | F3. | M |
| **P10** | Removing a user from a project with "don't keep annotated" **hard-deletes their annotated images** (`images.py:345-352`). | One wrong click destroys finished work. | Soft-delete / move to unassigned with a confirm count. | S |
| **P11** | Training state only in memory; no run history or comparison. Missing base weights are auto-downloaded into the **repo root** (`train_worker.py:200`). | Can't compare experiments; clutter (`yolo11n.pt` etc. in root). | F12; a weights cache folder. | M |
| **P12** | Install instructions point at the **CUDA nightly** wheel (`requirements.txt` comment, `torch_version.txt`), but this laptop has no NVIDIA GPU (the venv correctly has `+cpu`). | Following the docs on this laptop downloads about 3 GB of unusable CUDA libraries. | `requirements-cpu.txt` / `requirements-gpu.txt` profiles plus a setup script that detects the GPU. | S |
| **P13** | Bounding boxes only. | Segmentation needs polygons/masks (F16 first, then G1). | — | — |

---

## 4. Housekeeping (recommended first, mostly frees space)

| ID | Task | Space effect | Effort |
|---|---|---|---|
| **H1** | P3 git cleanup | **−200 MB** | S |
| **H2** | P2 weight dedupe and re-link the existing duplicates | **−149 MB** now; roughly half of each future run | S |
| **H3** | Optional: delete the 8 old test exports / 12 test runs (with a listing first) | up to **−230 MB** | S |
| **H4** | Install profiles (P12), `.env.example`, one-click `start.bat` | 0 | S |
| **H5** | Nightly SQLite backup (online backup API) keeping the last 14 | +~8 MB at today's size | S |
| **H6** | Rotating log file with tokens redacted | +≤20 MB (capped) | S |
| **H7** | Test fixes (B-tests above); make `MODELS_DIR`/`TRAINING_RUNS_DIR` overridable; trainer and auth tests | +~60 KB | M |

---

## 5. Features that work on this laptop now

Ordered by value for the effort.

| ID | Feature | Why it's worth it | CPU feasibility (measured/estimated) | Effort |
|---|---|---|---|---|
| **F1** | **Model-assisted pre-labelling.** Pick any trained `.pt` in `models/`, run it over unannotated frames as a background job, and insert boxes as `source="model"` (already in the schema), shown dashed until accepted. Confidence threshold, class mapping by name, "accept all / accept this frame / reject". | The biggest speed-up available: correcting boxes is about 3–5× faster than drawing them. It also closes the train → label loop. | YOLO11n: **~4.5 min per 1,000 images** (measured 260 ms/img). | M |
| **F2** | **Label-error finder.** Run the model over *labelled* images and rank frames by disagreement: missed objects, wrong class, sloppy boxes (low IoU). One click jumps into the annotator. | Finds the labelling mistakes that cap your mAP. Cheap once F1 exists. | Same as F1. | S (after F1) |
| **F3** | **Review workflow.** New states: submitted → approved / rejected (comment, sent back to the annotator). Reviewer role, per-annotator throughput and rejection rate, a change-history table (who changed which box and when), stable box IDs. | Quality control is what separates a dataset you trust from one you don't. | DB only. | L |
| **F4** | **Import/export formats.** Import YOLO txt, COCO JSON and Pascal VOC *with labels* (class mapping dialog); export COCO, VOC, YOLO (and YOLO-seg once F16 exists). | Bring in Roboflow/public datasets; hand data to other tools. | — | M |
| **F5** | **Dataset versions instead of copies.** Each export becomes an immutable version (manifest, hard-linked images, labels); images stay editable afterwards. Diff between versions; "what changed since v3". | Fixes P1 and P7 together; reproducible experiments. | — | M |
| **F6** | **Dataset health page.** Class balance, boxes per image, box-size histogram against training imgsz (flags boxes too small to learn), aspect ratios, empty frames, exact and near-duplicate images (perceptual hash in pure Pillow, no new dependency), missing files. | Catches the problems that cost mAP before you spend hours training. | Seconds for thousands of images. | M |
| **F7** | **Smart frame extraction.** Scene-change/motion sampling (ffmpeg `select=gt(scene,…)` / `mpdecimate`) plus near-duplicate removal, a disk-space estimate and guard (fixes B21), and an auto-import into the project. | Stops you labelling 30 nearly identical frames per second of video. Probably the biggest *time* saver for video work. | ffmpeg is already bundled. | S–M |
| **F8** | **Tracker between keyframes.** Replace straight-line interpolation with OpenCV `TrackerVit`/`TrackerNano` (already in your OpenCV 5; ~1–3 MB of model files), writing `source="tracked"` (already reserved). | Linear interpolation fails as soon as an object changes speed or turns. | ~20–60 ms/frame per object on CPU (estimate). | M |
| **F9** | **Annotator speed pack.** Keep zoom across frames (B19); copy/paste and multi-select boxes; bulk class change; class hotkeys beyond 9 plus next/prev class; "submit & next"; hold-to-hide; trackpad pan/pinch; **brightness/contrast/gamma/invert view controls** (very useful for X-rays/dental images); crosshair guides; "discard changes". | Every second per frame × thousands of frames. | — | M |
| **F10** | **Background job system.** One jobs table and worker for import, export, extraction, pre-labelling and health scans; live progress, cancel, survives reload; a small "Jobs" panel. | The base for F1/F2/F6/F7; fixes P6. | — | M |
| **F11** | **Web folder browser** (limited to allowed roots), replacing tkinter dialogs for remote admins. | Makes the app usable from any LAN PC (fixes B26). | — | S |
| **F12** | **Training upgrades:** resume from `last.pt`; persistent run history with config and final mAP; side-by-side comparison (from Ultralytics' `results.csv`); confusion matrix, PR curves and validation-set predictions in the UI; pick a project dataset version from a list; fine-tune from a library model; saved presets; **export to ONNX/OpenVINO** (OpenVINO often speeds up CPU inference noticeably). | You stop losing experiments and can deploy models. | — | L |
| **F13** | **"Train in the cloud" bundle.** One click zips a dataset version with a ready-made Colab/Kaggle notebook; drag the resulting `best.pt` back in. | Kaggle's free tier gives roughly 30 GPU-hours/week. That's real GPU training **today**, without waiting for the new laptop. | Zip time only. | S |
| **F14** | **Active-learning queue.** Rank unlabelled frames by model uncertainty and diversity so annotators do the most informative frames first. | Reaches the same mAP with fewer labels. | Uses F1 outputs. | S (after F1) |
| **F15** | **Settings & storage page.** Feature flags, a compute-profile readout, model-cache location (point it at C: to spare D:), a disk-usage breakdown with cleanup buttons for old runs/exports. | You're short on disk, and this makes that visible and manageable. | — | S–M |
| **F16** | **Manual polygon annotation.** Add a polygon tool to the canvas (vertex edit, holes optional), store polygons alongside boxes, export YOLO-seg/COCO-seg. | Needed before any segmentation assist (G1) is useful, and it works fine on CPU. | — | L |

---

## 6. Dormant GPU features (built now, switched on later)

### G0. The base everything else plugs into (build this now, M–L)

1. **Capability detection** at startup: CUDA present? GPU name, VRAM, torch build (cpu/cuda), free disk. Shown on the Settings page.
2. **Feature registry:** each AI feature declares its needs (`min_vram_gb`, `needs_cuda`, weights file, size). The UI shows each one as *Ready / Available on CPU (slow) / Needs GPU (≥6 GB VRAM) / Weights not downloaded (324 MB)*. Nothing turns on automatically.
3. **Lazy model download** into a configurable cache folder (`INVICTUS_MODEL_CACHE`, defaulting to somewhere on C:). **Nothing is downloaded until you click Enable.**
4. **Separate inference worker process** (the same pattern as `train_worker.py`), so heavy models never block the web server and crashes are isolated.
5. **Provider interface:** `detect(image, prompt)`, `segment(image, points|box)`, `track(frames, boxes)`, `embed(images)`. Providers register themselves; the UI just calls "assist".
6. **Proven on this laptop now** with tiny models (YOLO nano for detection, MobileSAM 40 MB for click-to-box), so the plumbing is tested and the GPU machine only needs the CUDA torch build and an "Enable" click.

### Features that plug into G0

| ID | Feature | Model choice | Here (CPU) | RTX 4050 laptop (6 GB) | Effort |
|---|---|---|---|---|---|
| **G1** | **Click-to-segment / click-to-box.** Click an object → mask → polygon, or a tight box. Box → mask. | SAM 2.1 (t/s/b+/l) via Ultralytics; MobileSAM as the CPU fallback | MobileSAM: ~1–3 s per image to prepare, then fast clicks (estimate). Usable for box projects. | SAM 2.1 base+/large comfortably | M (after G0, F16) |
| **G2** | **Video mask/box propagation.** Annotate one frame; SAM 2 tracks the object through the whole clip. | SAM 2.1 video predictor | Too slow | Fits well with tiny/small/base+; large on long clips may need frame offloading | L |
| **G3** | **Text-prompt auto-labelling.** Type "helmet, person" and get boxes on every frame with no training. | CPU now: YOLO-World v2-s / YOLOE-s. GPU: Grounding DINO (tiny/base), Florence-2, or **SAM 3** (text → masks for all instances) | YOLO-World-s: roughly 0.5–1 s/img (estimate). Works, slowly. | Grounding DINO / Florence-2 comfortably; SAM 3 (~848M params) should fit in fp16, to be verified on the machine | M–L |
| **G4** | **GPU training profile.** Auto device, AMP, AutoBatch, bigger models (YOLO11s/m at 640), segmentation training (`yolo11-seg`), RT-DETR, hyper-parameter tuning; preset "RTX 4050 Laptop (6 GB)"; no default VRAM cap. | — | n/a | YOLO11m at 640, batch ≈8–16 with AMP | S–M |
| **G5** | **Embedding index:** "find more frames like this", near-duplicate removal, diversity sampling for active learning, outlier detection, text search over images ("person at night"). | DINOv2-S (88 MB) or CLIP ViT-B/32 | DINOv2-S works for a few thousand images (minutes) | Fast for 100k+ | M |
| **G6** | **Teacher → student.** Train a large teacher (YOLO11l / RT-DETR-l) on GPU, pre-label everything with it (F1), distil to nano for deployment. | — | n/a | Yes | S (after F1, G4) |
| **G7** | **Detect + track whole videos** with consistent track IDs (ByteTrack/BoT-SORT from Ultralytics). | your detector + ByteTrack | Slow but works | Real-time | M |

Notes: the laptop's AMD Vega iGPU can't help. There's no CUDA, and on Windows the only option is DirectML/ONNX, which on a Vega 3 is unlikely to beat the CPU. Not worth the effort.

---

## 7. Suggested order

| Phase | Contents | Why first |
|---|---|---|
| 0 | H1–H5, P12 | Frees ~350 MB on D:, safe install, backups before anything else |
| 1 | B1–B9, S1, S3, S5, S7, B21, B22 | Stops data corruption, lost work and the RCE path |
| 2 | F9, B10–B20, F3 | Annotators get faster; quality control |
| 3 | F10, F1, F2, F14, F13, F12 | The model loop on CPU, plus real GPU training via Kaggle |
| 4 | F4, F5, F6, F7, F8, F11, F15, P1–P5 | Data management at scale |
| 5 | G0 (+ MobileSAM CPU proof), F16 | Dormant AI base is ready |
| 6 (new laptop) | Install the CUDA torch build → enable G1, G2, G3, G4, G5, G6, G7 | Flip switches |

---

## 8. Storage estimates

"Code" is source added to the repo. "Deps" are new Python packages. "Weights" are model files (downloaded only when a feature is enabled). "Data" is growth that depends on how much you use the feature.

### Fixes, security, performance, housekeeping

| Items | Code | Deps / weights | Disk effect |
|---|---|---|---|
| All bugs B1–B26 + low-severity list | ~60 KB | — | ~0 |
| All security S1–S10 | ~25 KB | −1 MB (remove passlib/pynvml), +~0.1 MB if PyJWT replaces jose | ~0 |
| P1–P13 | ~40 KB | — | saves ~4 GB **per future export** at 10k-image scale (P1) |
| H1 git cleanup | — | — | **−200 MB** |
| H2 weight dedupe | — | — | **−149 MB** now |
| H3 old test runs/exports (optional) | — | — | **up to −230 MB** |
| H5 backups | — | — | +~8 MB now (grows with DB; ~14× DB size) |
| H6 logs | — | — | +≤20 MB (capped) |
| H7 tests | ~60 KB | — | — |

### Features for now (F)

| ID | Code | Deps / weights | Data growth |
|---|---|---|---|
| F1 pre-labelling | ~40 KB | 0 (torch + Ultralytics already installed); optional ONNX Runtime ≈ +40 MB | negligible (box rows) |
| F2 label-error finder | ~20 KB | 0 | ~0 |
| F3 review + history | ~60 KB | 0 | ~200 B per box change, about 20 MB per 100k edits |
| F4 formats | ~45 KB | 0 | — |
| F5 versions (hard links) | ~35 KB | 0 | labels only, ~1 MB per 10k images per version |
| F6 health | ~35 KB | 0 | 8 bytes per image (hash) |
| F7 smart extraction | ~20 KB | 0 | *saves* frames |
| F8 tracker | ~30 KB | OpenCV tracker models ~1–3 MB | — |
| F9 annotator pack | ~50 KB | 0 | — |
| F10 jobs | ~30 KB | 0 | ~0 |
| F11 folder browser | ~15 KB | 0 | — |
| F12 training upgrades | ~60 KB | ONNX export: `onnx` + `onnxslim` ≈ +45 MB; OpenVINO export ≈ +150 MB (both optional) | ONNX file ≈ size of the `.pt` per exported model |
| F13 cloud bundle | ~15 KB | 0 | temp zip ≈ dataset size (deleted after download) |
| F14 active learning | ~15 KB | 0 | ~0 |
| F15 settings/storage | ~25 KB | 0 | — |
| F16 polygons | ~60 KB | 0 | polygons ≈ 0.5–2 KB per object |
| **F total** | **≈ 0.55 MB** | **0 required**; ≈ +0.24 GB if all optional exports are added | usage-dependent, small |

### Dormant features (G): on this laptop vs. when activated

**On this laptop:** G0 plus all G-feature code is ≈ **0.15 MB**. Weights downloaded: **none** (optionally MobileSAM 40 MB to prove G1 works here).

**On the GPU machine, when you enable them:**

| Item | Approx. size |
|---|---|
| PyTorch **CUDA** build (replaces the 0.5 GB CPU build) | ~3 GB download, **~4.5 GB installed (net ≈ +4 GB)**. Use `pip --no-cache-dir`, or the pip cache doubles it temporarily. |
| G1/G2 SAM 2.1 | tiny 156 MB · small 184 MB · **base+ 324 MB** (recommended) · large 898 MB |
| G1 MobileSAM (CPU fallback) | 40 MB |
| G3 YOLO-World v2-s + CLIP text encoder | ~26 MB + ~340 MB ≈ 0.37 GB |
| G3 YOLOE-s + MobileCLIP text encoder | ~0.3–0.6 GB (verify at install) |
| G3 Grounding DINO tiny + `transformers` stack | ~690 MB + ~150 MB ≈ 0.85 GB |
| G3 Florence-2-large (alternative) | ~1.5 GB (+ shared `transformers`) |
| G3 SAM 3 (optional, gated download) | ≈ 3.4 GB |
| G4 bigger base weights (YOLO11m/l/x, seg variants, RT-DETR-l) | ~40–110 MB each; ~0.3 GB for a sensible set |
| G4 GPU training runs (after the H2 dedupe) | ~90 MB per YOLO11m run, ~120 MB per YOLO11l run |
| G5 DINOv2-S (or CLIP B/32) + index | 88 MB (or ~340 MB) + 1.5 KB per image (150 MB per 100k images) |
| G6 teacher weights + runs | ~50–130 MB weights + ~0.1–0.2 GB per run |
| G7 tracker | ~1 MB (`lap`) |

### Totals

| Scenario | Net disk change |
|---|---|
| **All fixes + housekeeping on this laptop** | **≈ −350 MB** (−580 MB if H3 is included) |
| + all "now" features (required parts) | still ≈ **−350 MB** (code is ~1 MB) |
| + every optional CPU extra (ONNX Runtime, ONNX, OpenVINO, MobileSAM, tracker models) | ≈ **−70 MB** overall, still a net saving |
| **GPU machine, recommended set** (CUDA torch + SAM 2.1 base+ + Grounding DINO tiny + YOLO-World + DINOv2-S + bigger YOLO weights) | ≈ **+6 GB** |
| **GPU machine, everything** (all four SAM 2.1 sizes, SAM 3, Florence-2-L, YOLOE, CLIP, all weights) | ≈ **+11–12 GB** |
| Working data on either machine | Depends on use: ~1 MB of labels per 10k images per dataset version (with hard links), plus your training runs |

---

## 9. Reality check

**Where it stands.** This is a capable in-house labelling tool for a small team. Some parts are better than common open-source tools: the video-sequence workflow (carry, propagate, interpolate with undo), contiguous frame assignment, leakage-aware splits, and one-click training with live charts. The code is mostly careful: parameterised SQL everywhere, consistent escaping, tests that pass. It is not a toy.

**But be honest about the competition.**
- **CVAT** (free, open source) already has polygons, masks, tracking, SAM/auto-annotation, review, and dozens of formats.
- **X-AnyLabeling** ships SAM/Grounding DINO in a desktop app.
- **Label Studio** is flexible and free.
- **Roboflow** does hosted auto-labelling and training.

Feature for feature, everything in section 6 already exists elsewhere. If the goal is "a general labelling tool that beats those", it will lose. That race is decided by team size, not cleverness.

**Where it can win.** Not as a canvas, but as an **opinionated, offline, zero-Docker loop**:

> video → smart frames → label → train → pre-label with your own model → fix the flagged errors → retrain

This loop runs on a Windows laptop with 14 GB RAM, installs with pip, and can be learned in ten minutes. CVAT wants Docker and a beefy machine; Roboflow wants your data in the cloud. For a 2–5 person team doing repeated, domain-specific video datasets (dental, a factory line, a specific camera setup) that's a real advantage. Lean into it: F1, F2, F7, F13, F3 and F5 make *the loop* excellent. Most of G is a nice bonus, not the core.

**Constructive criticism:**
1. **Data is your bottleneck, not features.** 363 images and 238 boxes is a demo-sized dataset. No feature on this list turns that into a strong model. The best next step is a few thousand well-reviewed labels, and F1 + F2 + F7 exist to get you there faster.
2. **It hasn't met real scale yet.** Several limits (P4, P5, P6, B21) only show at 10k+ images or several simultaneous annotators. Run a realistic load test (10k frames, 3 annotators) before trusting it in production.
3. **Someone must own the code.** It's 14k lines, and the notes in `torch_version.txt` read like AI prompt logs. That's fine, but make sure you can explain every module. Raise test coverage on the risky parts (trainer, auth, sequence tools); currently ~0 % on the trainer and no JS tests.
4. **Licensing, if this ever goes commercial.** Ultralytics YOLO is **AGPL-3.0**. Internal use is fine. Selling the tool, hosting it for clients, or shipping trained models in a product needs an Ultralytics Enterprise licence (per their terms) or a switch to permissively licensed detectors (e.g. RF-DETR, D-FINE, YOLOX). Decide this *before* you build a business on it.
5. **The security model is "trusted LAN".** Fine at home or in a lab. Never expose it to the internet, even after S1–S10.
6. **Beware roadmap creep.** This document lists ~45 features. Doing 8 of them very well beats doing 30 halfway. My pick for the highest return: **Phase 0–1, then F1, F2, F3, F7, F9, F13.**
7. **Training on this laptop is a stopgap.** A 2-core Ryzen 3 is fine for labelling and nano-model inference, but CPU training is painfully slow. F13 (Kaggle/Colab bundle) gets you GPU training *now*, for free, and builds exactly the habit you'll use on the RTX 4050.

**Is it actually useful?** Yes, for a small team that labels video-derived data repeatedly, works offline or on a LAN, and wants one place from raw video to a trained model. As a product competing broadly with CVAT or Roboflow, no. As a focused, fast, self-hosted loop for a niche, it can be genuinely better than both. The roadmap above is ordered to push it in that direction.
