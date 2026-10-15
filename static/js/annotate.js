/**
 * annotate.js — bounding-box annotation engine.
 *
 * Frame state machine
 *   LOADING  → annotations are being fetched; edits are ignored
 *   CLEAN    → matches what the server has
 *   DIRTY    → local edits not yet saved (debounced auto-save)
 *   CARRIED  → boxes copied from the previous frame and not yet accepted; nothing is written
 *              until you edit, press Enter, or move on with Next (accept-on-next)
 *   READONLY → nothing to annotate (no frames assigned)
 *
 * Saving is serialised (one request in flight, a follow-up queued) and versioned twice over:
 * editVersion catches edits made while a save is in flight, and the frame's server version
 * (base_version) makes the server refuse a save over someone else's newer change (409).
 * Navigation waits for the save to finish and stays on the frame if it fails, offering to
 * discard the edits instead.
 *
 * Model suggestions (source "model", from Model assist) are drawn dashed; Enter accepts them,
 * and any edit on the frame counts as reviewing it.
 */
(async () => {
'use strict';

const PID = window.ANNOTATE_PROJECT_ID;
const user = IA.requireAuth();
if (!user) return;
await IA.ensureSession();

const $ = (id) => document.getElementById(id);
const canvas = $('canvas');
const ctx = canvas.getContext('2d');
const stage = $('stage');

const MIN_BOX = 4;               // px in image space
const MAX_HISTORY = 100;
const ROW_H = 56;                // filmstrip row height (must match CSS)
const AUTOSAVE_MS = 900;
const TODO = ['unassigned', 'assigned', 'unannotated'];
const DONE = ['annotated', 'ready_for_export', 'exported'];

// ── State ─────────────────────────────────────────────────────────────────────
let project = null;
let classes = [];
let allImages = [];              // full index (ordered by seq)
let byId = new Map();            // image id → entry of allImages
let view = [];                   // filtered list shown in the filmstrip
let idx = -1;                    // index into view
let cur = null;                  // current image meta (object from allImages)

let boxes = [];                  // {id?, classIdx, className, x, y, w, h, source, trackId, conf?, carried?}
let isBackground = false;
let history = [];
let future = [];
let selected = -1;
let selClass = 0;

let mode = 'LOADING';
let serverSnap = '';
let editVersion = 0;
let savePromise = null;
let saveAgain = false;
let saveTimer = null;
let forceSave = false;           // save even if nothing changed (accepting model suggestions)
let loadToken = 0;
let navQueue = Promise.resolve();
let navPending = 0;
let review = null;               // {status, note, by} of the current frame

let carryOn = IA.store.get(`ia_carry_${PID}`, false);
let carrySrc = null;             // {imageId, seqGroup, w, h, boxes}
let carryDir = 1;
let prevFrame = null;            // {imageId, boxes, w, h} of the frame we just left (for R)
const BATCH_KEY = `ia_last_batch_${PID}_${user.id}`;
let lastBatch = IA.store.get(BATCH_KEY, null);
let range = null;                // filmstrip shift-selection {a, b} (indices into view)
let anchor = null;

// canvas transform / interaction
let scale = 1, ox = 0, oy = 0, dpr = window.devicePixelRatio || 1;
let userZoomed = false;          // the user zoomed/panned since the last fit
let lockZoom = IA.store.get('ia_lock_zoom', false);
let lastDims = null;
let stageSize = null;
let img = new Image();
let drawMode = false;
let hideBoxes = false;
let hDownAt = 0;
let drag = null;                 // {kind: 'draw'|'move'|'resize'|'pan', ...}
let hover = null;                // {ix, iy} image-space cursor
let spaceDown = false;
let antsOffset = 0;
let adj = IA.store.get(`ia_adjust_${PID}`, { bright: 100, contrast: 100, invert: false });
let qaOn = IA.store.get('ia_qa_overlay', new URLSearchParams(location.search).has('qa'));
let qa = null;                   // {boxes, issues} model view for the current frame

// ── Helpers ───────────────────────────────────────────────────────────────────
const round = (v) => Math.round(v);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const color = (b, a = 1) => IA.classColor(b.classIdx, a);
// The class name is the source of truth (the index follows reorders / renames)
const clsName = (b) => b.className || (b.classIdx >= 0 ? classes[b.classIdx] : '') || '?';
const snapshot = () => JSON.parse(JSON.stringify(boxes));
const isAdmin = !!user.is_admin;
const newTrackId = () => Math.random().toString(36).slice(2, 10);

function resolveClassIdx(name, fallbackId) {
  const i = classes.indexOf(name);
  if (i >= 0) return i;
  if (!name && fallbackId >= 0 && fallbackId < classes.length) return fallbackId;
  return -1;                       // orphan class — kept, drawn grey
}

function canonical() {
  return JSON.stringify({
    bg: isBackground && !boxes.length,
    b: boxes.map((b) => [clsName(b), round(b.x), round(b.y), round(b.w), round(b.h), b.source || 'manual', b.trackId || null]),
  });
}

function payload() {
  return boxes.map((b) => {
    const x1 = round(b.x), y1 = round(b.y), x2 = round(b.x + b.w), y2 = round(b.y + b.h);
    return { class_name: clsName(b), class_id: b.classIdx, x: x1, y: y1, w: Math.max(1, x2 - x1), h: Math.max(1, y2 - y1),
             source: b.source || 'manual', track_id: b.trackId || null, confidence: b.source === 'model' ? (b.conf ?? null) : null };
  });
}

function fromServer(a) {
  const className = a.class_name || classes[a.class_id] || '';
  return { id: a.id, classIdx: resolveClassIdx(className, a.class_id), className,
           x: a.x, y: a.y, w: a.w, h: a.h, source: a.source || 'manual', trackId: a.track_id || null, conf: a.confidence ?? null };
}

/** Move a box between frames of different sizes (normalised), clipped; null if too small. */
function fitBox(b, sw, sh, dw, dh) {
  let { x, y, w, h } = b;
  if (sw && sh && (sw !== dw || sh !== dh)) { x *= dw / sw; w *= dw / sw; y *= dh / sh; h *= dh / sh; }
  const x1 = clamp(x, 0, dw), y1 = clamp(y, 0, dh), x2 = clamp(x + w, 0, dw), y2 = clamp(y + h, 0, dh);
  if (x2 - x1 < MIN_BOX || y2 - y1 < MIN_BOX) return null;
  return { ...b, x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

const hasModelSuggestions = () => !!cur && TODO.includes(cur.state) && boxes.some((b) => b.source === 'model');

// ── Save pipeline ─────────────────────────────────────────────────────────────
function setSave(state, text) {
  const pill = $('save-pill');
  pill.dataset.state = state;
  pill.querySelector('.txt').textContent = text || { saved: 'Saved', saving: 'Saving…', unsaved: 'Unsaved', error: 'Not saved', carried: 'Carried · not saved' }[state];
  if (state === 'saved') { pill.classList.add('just-saved'); setTimeout(() => pill.classList.remove('just-saved'), 250); }
}

/** Call after every edit. Accepts a carried frame (the edit means "yes, keep these"). */
function markDirty() {
  if (mode === 'READONLY' || mode === 'LOADING') return;
  if (mode === 'CARRIED') acceptCarried(false);
  editVersion++;
  mode = 'DIRTY';
  setSave('unsaved');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, AUTOSAVE_MS);
}

function markClean() {
  if (mode === 'DIRTY') mode = 'CLEAN';
  setSave('saved');
}

async function save() {
  clearTimeout(saveTimer);
  if (savePromise) { saveAgain = true; return savePromise; }
  if (!cur || mode !== 'DIRTY') return true;
  const target = cur;
  const snap = canonical();
  if (snap === serverSnap && !forceSave) { markClean(); return true; }
  const v = editVersion;
  const body = { annotations: payload(), is_background: isBackground && !boxes.length, base_version: target.version ?? null };
  setSave('saving');
  savePromise = (async () => {
    try {
      const data = await IA.put(`/api/annotations/${target.id}`, body);
      target.version = data.version;
      forceSave = false;
      applyImageMeta(target, { state: data.state, box_count: data.annotations.length, model_boxes: 0,
                               is_background: data.is_background, review_status: data.review_status,
                               is_keyframe: data.annotations.some((a) => a.source === 'manual') });
      if (cur === target) {
        hideConflict();
        if (editVersion === v) {
          boxes = data.annotations.map(fromServer);          // adopt server ids / clipping
          if (selected >= boxes.length) selected = boxes.length - 1;
          serverSnap = canonical();
          markClean();
          renderBoxList(); updateBanners(); render();
        } else {
          serverSnap = snap;
        }
        if (data.dropped) IA.toast(`${IA.plural(data.dropped, 'box', 'boxes')} outside the image were removed`, 'warn');
      }
      return true;
    } catch (e) {
      if (cur !== target) return false;
      setSave('error');
      if (e.status === 409) showConflict();
      else if (e.status === 0) {                                     // offline: try again shortly
        IA.toast('Could not reach the server — retrying…', 'warn', 2500);
        setTimeout(() => { if (cur === target && mode === 'DIRTY') save(); }, 4000);
      } else if (e.status === 403 || e.status === 404) {
        IA.toast(`Could not save: ${e.message}`, 'error', { action: { label: 'Discard & move on', fn: () => leaveCurrent() } });
      } else IA.toast(`Could not save: ${e.message}`, 'error');
      return false;
    } finally {
      savePromise = null;
      if (saveAgain) { saveAgain = false; if (mode === 'DIRTY' && cur === target) setTimeout(save, 0); }
    }
  })();
  return savePromise;
}

/** Save everything pending; resolves false if the frame could not be saved. */
async function flushSave() {
  clearTimeout(saveTimer);
  for (let i = 0; i < 4; i++) {
    if (savePromise) { const ok = await savePromise; if (!ok) return false; }
    if (mode !== 'DIRTY') return true;
    if (!(await save())) return false;
  }
  return mode !== 'DIRTY';
}

/** Throw away unsaved edits on this frame (after a failed save). */
function discardLocal() {
  clearTimeout(saveTimer);
  forceSave = false;
  if (mode === 'DIRTY') mode = 'CLEAN';
  setSave('saved');
}

function showConflict() {
  $('conflict-banner').classList.remove('hidden');
}
function hideConflict() { $('conflict-banner').classList.add('hidden'); }
$('conflict-reload').addEventListener('click', () => { hideConflict(); reloadCurrent(true); });

// ── History ───────────────────────────────────────────────────────────────────
function pushHistory() {
  history.push({ boxes: snapshot(), bg: isBackground });
  if (history.length > MAX_HISTORY) history.shift();
  future = [];
}
function undo() {
  if (mode === 'CARRIED') { discardCarried(); return; }
  if (!history.length) { IA.toast('Nothing to undo', 'info', 1200); return; }
  future.push({ boxes: snapshot(), bg: isBackground });
  const s = history.pop();
  boxes = s.boxes; isBackground = s.bg;
  selected = -1;
  afterEdit();
}
function redo() {
  if (!future.length) return;
  history.push({ boxes: snapshot(), bg: isBackground });
  const s = future.pop();
  boxes = s.boxes; isBackground = s.bg;
  selected = -1;
  afterEdit();
}
function afterEdit() {
  markDirty();
  renderBoxList();
  updateBanners();
  render();
}

// ── Carry-over / model suggestions ────────────────────────────────────────────
function acceptCarried(persist = true) {
  if (mode !== 'CARRIED') return;
  boxes.forEach((b) => { delete b.carried; });
  mode = 'CLEAN';
  updateBanners();
  if (persist) markDirty();
  renderBoxList();
}
function discardCarried() {
  if (mode !== 'CARRIED') return;
  boxes = [];
  selected = -1;
  mode = 'CLEAN';
  setSave('saved');
  updateBanners(); renderBoxList(); render();
}
/** Keep the model's boxes as they are: saving marks the frame annotated. */
function acceptModel() {
  if (!hasModelSuggestions()) return;
  forceSave = true;
  markDirty();
  save();
  IA.toast(`Accepted ${IA.plural(boxes.length, 'box', 'boxes')}`, 'success', 900);
}

function canCarryInto(target, step, repeat) {
  if (!carryOn || !carrySrc || !carrySrc.boxes.length || repeat) return false;
  if (Math.abs(step) !== 1) return false;
  if (carrySrc.seqGroup !== target.seq_group) return false;
  return true;
}

// ── Loading frames ────────────────────────────────────────────────────────────
function applyImageMeta(imgMeta, patch) {
  Object.assign(imgMeta, patch);
  renderStripRows();
  updateCounts();
  if (imgMeta === cur) updateSubmit();
}

async function loadFrame(newIdx, { step = 0, repeat = false } = {}) {
  if (newIdx < 0 || newIdx >= view.length) return;
  idx = newIdx;
  cur = view[idx];
  const token = ++loadToken;
  mode = 'LOADING';
  boxes = []; history = []; future = []; selected = -1; isBackground = false; review = null; qa = null;
  drag = null; forceSave = false;
  hideConflict();
  setSave('saved');
  $('t-file').textContent = cur.filename;
  $('t-frame').textContent = `${idx + 1} / ${view.length}`;
  $('sb-size').textContent = `${cur.width} × ${cur.height}`;
  const sameDims = lastDims && lastDims[0] === cur.width && lastDims[1] === cur.height;
  if (!(lockZoom && sameDims && userZoomed)) fit();
  lastDims = [cur.width, cur.height];
  history_replace();
  scrollStripTo(idx);
  renderStripRows();
  renderBoxList(); updateBanners(); render();

  // Image (with a small spinner if it takes a moment)
  showMissing(null);
  let imgFailed = false, meta = null;
  const loading = setTimeout(() => $('stage-loading').classList.remove('hidden'), 150);
  const el = new Image();
  el.decoding = 'async';
  el.onload = () => {
    clearTimeout(loading);
    if (token !== loadToken) return;
    $('stage-loading').classList.add('hidden');
    img = el;
    render();
  };
  el.onerror = () => {
    clearTimeout(loading);
    if (token !== loadToken) return;
    $('stage-loading').classList.add('hidden');
    imgFailed = true;
    // The annotations response says whether the file is missing; until it arrives assume so
    if (meta) showMissing(meta.file_missing ? 'missing' : 'unreadable', meta.filepath);
  };
  el.src = IA.imgUrl(cur.id);
  prefetch(idx);

  let data;
  try { data = await IA.get(`/api/annotations/${cur.id}`); }
  catch (e) {
    if (token !== loadToken) return;
    $('box-list').innerHTML = `<div class="box-err">Could not load this frame: ${IA.esc(e.message)}<button class="btn btn-sm" id="retry-load">${IA.icon('refresh-cw')}Retry</button></div>`;
    $('retry-load').addEventListener('click', () => reloadCurrent(true));
    return;
  }
  if (token !== loadToken) return;
  meta = data;
  if (data.file_missing) showMissing('missing', data.filepath);
  else if (imgFailed) showMissing('unreadable', data.filepath);

  cur.width = data.width; cur.height = data.height; cur.version = data.version;
  boxes = data.annotations.map(fromServer);
  isBackground = !!data.is_background;
  review = data.review_status ? { status: data.review_status, note: data.review_note, by: data.reviewed_by } : null;
  serverSnap = canonical();
  applyImageMeta(cur, { state: data.state, box_count: boxes.length, is_background: isBackground, review_status: data.review_status,
                        model_boxes: boxes.filter((b) => b.source === 'model').length });

  if (!boxes.length && !isBackground && !DONE.includes(data.state) && canCarryInto(cur, step, repeat)) {
    boxes = carrySrc.boxes
      .map((b) => fitBox(b, carrySrc.w, carrySrc.h, cur.width, cur.height))
      .filter(Boolean)
      .map((b) => ({ ...b, id: undefined, carried: true, source: 'carried' }));
    mode = boxes.length ? 'CARRIED' : 'CLEAN';
    carryDir = Math.sign(step);
    if (mode === 'CARRIED') setSave('carried');
    const dropped = carrySrc.boxes.length - boxes.length;
    if (dropped) IA.toast(`${IA.plural(dropped, 'box', 'boxes')} did not fit this frame and were left out`, 'warn');
  } else {
    mode = 'CLEAN';
  }
  renderBoxList(); updateBanners(); updateSubmit(); render();
  if (qaOn) loadQa(token);
}

/** In-canvas explanation when a frame's image cannot be shown (kind: 'missing' | 'unreadable' | null). */
function showMissing(kind, path) {
  const panel = $('missing-panel');
  panel.classList.toggle('hidden', !kind);
  if (!kind) return;
  const fixable = isAdmin && kind === 'missing';
  $('missing-text').textContent = kind === 'unreadable'
    ? 'The file is on the server but could not be opened — it may be damaged or in an unsupported format.'
    : isAdmin
      ? 'Nothing is at the path stored for this image. Its folder was probably moved, renamed or deleted. If the images still exist somewhere, relink the project to their new location.'
      : 'This image is no longer on the server. Ask an admin to relink the project’s images.';
  $('missing-path').textContent = path || '';
  $('missing-path').classList.toggle('hidden', !path);
  const fix = $('missing-fix');
  fix.classList.toggle('hidden', !fixable);
  fix.href = `/projects/${PID}#import`;
}

function prefetch(i) {
  [i + 1, i + 2, i - 1].forEach((j) => { if (view[j]) { const p = new Image(); p.src = IA.imgUrl(view[j].id); } });
}

function history_replace() {
  const url = `/projects/${PID}/annotate/${cur.id}${location.search}`;
  if (location.pathname + location.search !== url) window.history.replaceState(null, '', url);
}

/** Queue navigation so fast key presses never race each other. */
function navigate(delta, opts = {}) {
  navPending++;
  navQueue = navQueue.then(() => doNavigate(idx + delta, { ...opts, step: delta })).catch(() => {}).finally(() => { navPending--; });
  return navQueue;
}
function jumpTo(i) {
  navPending++;
  navQueue = navQueue.then(() => doNavigate(i, { step: i - idx })).catch(() => {}).finally(() => { navPending--; });
  return navQueue;
}

async function doNavigate(target, { step = 0, repeat = false } = {}) {
  if (target === idx && cur) return;
  if (target < 0 || target >= view.length) {
    IA.toast(target < 0 ? 'This is the first frame' : 'This is the last frame', 'info', 1200);
    return;
  }
  if (mode === 'CARRIED') {
    // Moving on in the same direction means "these boxes are right"
    if (Math.sign(step) === carryDir && Math.abs(step) === 1 && !repeat) acceptCarried(true);
    else discardCarried();
  }
  if (cur && !(await flushSave())) {
    IA.toast('Could not save this frame — staying here so nothing is lost', 'error', {
      action: { label: 'Discard changes', fn: () => { discardLocal(); jumpTo(target); } },
    });
    return;
  }
  const leaving = cur;
  if (leaving) {
    const kept = boxes.filter((b) => !b.carried).map((b) => ({ ...b }));
    prevFrame = kept.length ? { imageId: leaving.id, boxes: kept, w: leaving.width, h: leaving.height } : prevFrame;
    carrySrc = kept.length ? { imageId: leaving.id, seqGroup: leaving.seq_group, w: leaving.width, h: leaving.height, boxes: kept } : null;
  }
  await loadFrame(target, { step, repeat });
}

/** The current frame left this user's scope (deleted, reassigned): drop it and go to the nearest one. */
function leaveCurrent(message) {
  clearTimeout(saveTimer);
  const pos = Math.max(0, view.indexOf(cur));
  mode = 'LOADING';
  boxes = []; selected = -1; forceSave = false;
  cur = null;
  if (message) IA.toast(message, 'warn');
  applyFilter();
  updateCounts();
  if (view.length) { idx = -1; loadFrame(Math.min(pos, view.length - 1)); }
  else showNoFrames();
}

function showNoFrames() {
  mode = 'READONLY';
  cur = null;
  $('t-file').textContent = 'No images assigned to you yet';
  $('t-frame').textContent = '';
  $('box-list').innerHTML = '<div class="box-empty">Ask an admin to assign images to you.</div>';
  updateBanners(); updateSubmit(); render();
}

// ── Canvas geometry & rendering ───────────────────────────────────────────────
function resize() {
  const r = stage.getBoundingClientRect();
  dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(r.width * dpr);
  canvas.height = Math.round(r.height * dpr);
  // Keep the view centred on the same spot instead of re-fitting (and losing the zoom)
  if (stageSize && cur) {
    if (userZoomed) { ox += (r.width - stageSize[0]) / 2; oy += (r.height - stageSize[1]) / 2; }
    else fit();
  }
  stageSize = [r.width, r.height];
  render();
}
function viewH() { return stage.clientHeight - 34; }   // leave room for the status bar
function fit() {
  if (!cur) return;
  const w = stage.clientWidth, h = viewH();
  scale = Math.min(w / cur.width, h / cur.height) * 0.94;
  ox = (w - cur.width * scale) / 2;
  oy = (h - cur.height * scale) / 2;
  userZoomed = false;
  updateZoomLabel();
}
function zoomAt(sx, sy, factor) {
  const ns = clamp(scale * factor, 0.05, 32);
  ox = sx - (sx - ox) * (ns / scale);
  oy = sy - (sy - oy) * (ns / scale);
  scale = ns;
  userZoomed = true;
  updateZoomLabel();
  render();
}
function updateZoomLabel() { $('zoom-val').textContent = `${Math.round(scale * 100)}%`; }
const toImage = (sx, sy) => ({ ix: (sx - ox) / scale, iy: (sy - oy) / scale });
const toScreen = (ix, iy) => ({ sx: ix * scale + ox, sy: iy * scale + oy });

function handlesOf(b) {
  const { x, y, w, h } = b;
  return [['nw', x, y], ['n', x + w / 2, y], ['ne', x + w, y], ['e', x + w, y + h / 2],
          ['se', x + w, y + h], ['s', x + w / 2, y + h], ['sw', x, y + h], ['w', x, y + h / 2]];
}
function hitHandle(b, ix, iy) {
  const t = 7 / scale;
  for (const [id, hx, hy] of handlesOf(b)) if (Math.abs(ix - hx) <= t && Math.abs(iy - hy) <= t) return id;
  return null;
}
const inBox = (b, ix, iy) => ix >= b.x && ix <= b.x + b.w && iy >= b.y && iy <= b.y + b.h;
function topBoxAt(ix, iy) {
  // Smallest box under the cursor wins, so nested boxes stay reachable
  let best = -1, bestArea = Infinity;
  boxes.forEach((b, i) => { if (inBox(b, ix, iy) && b.w * b.h < bestArea) { best = i; bestArea = b.w * b.h; } });
  return best;
}

let rafPending = false;
function render() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; draw(); });
}

function draw() {
  const W = canvas.width / dpr, H = canvas.height / dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#08090a';
  ctx.fillRect(0, 0, W, H);
  if (!cur) return;

  if (img.complete && img.naturalWidth && img.src.includes(cur.id)) {
    ctx.imageSmoothingEnabled = scale < 3;
    ctx.imageSmoothingQuality = 'high';
    const filt = `${adj.bright !== 100 ? `brightness(${adj.bright}%) ` : ''}${adj.contrast !== 100 ? `contrast(${adj.contrast}%) ` : ''}${adj.invert ? 'invert(1)' : ''}`.trim();
    if (filt) ctx.filter = filt;
    ctx.drawImage(img, ox, oy, cur.width * scale, cur.height * scale);
    ctx.filter = 'none';
  }
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.lineWidth = 1;
  ctx.strokeRect(ox - 0.5, oy - 0.5, cur.width * scale + 1, cur.height * scale + 1);

  if (qaOn && qa) drawQa();
  if (!hideBoxes) boxes.forEach((b, i) => { if (i !== selected) drawBox(b, false); });
  if (selected >= 0 && boxes[selected]) drawBox(boxes[selected], true);
  if (drag?.kind === 'draw' && drag.box) drawBox(drag.box, false, true);

  // Crosshair guides while drawing
  if (drawMode && hover && !drag?.kind?.match(/move|resize|pan/)) {
    const { sx, sy } = toScreen(hover.ix, hover.iy);
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.35)';
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, Math.round(sy) + 0.5); ctx.lineTo(W, Math.round(sy) + 0.5);
    ctx.moveTo(Math.round(sx) + 0.5, 0); ctx.lineTo(Math.round(sx) + 0.5, H);
    ctx.stroke();
    ctx.restore();
  }
}

function drawLabel(text, sx, sy, fill, ink = '#0b0c0e') {
  ctx.font = '500 11px Geist, "Segoe UI", sans-serif';
  const tw = ctx.measureText(text).width;
  const lh = 17, lw = tw + 10;
  const inside = sy - lh < 0;
  const lx = Math.round(sx), ly = Math.round(inside ? sy : sy - lh);
  ctx.fillStyle = fill;
  ctx.beginPath();
  ctx.roundRect(lx, ly, lw, lh, inside ? [0, 0, 3, 0] : [3, 3, 0, 0]);
  ctx.fill();
  ctx.fillStyle = ink;
  ctx.fillText(text, lx + 5, ly + 12);
}

function drawBox(b, isSel, pending = false) {
  const { sx, sy } = toScreen(b.x, b.y);
  const sw = b.w * scale, sh = b.h * scale;
  const c = color(b);
  const model = b.source === 'model' && hasModelSuggestions();
  ctx.save();
  ctx.fillStyle = color(b, isSel ? 0.16 : model ? 0.05 : 0.1);
  ctx.fillRect(sx, sy, sw, sh);
  ctx.lineWidth = isSel ? 2 : 1.5;
  ctx.strokeStyle = c;
  if (b.carried || pending) {
    ctx.setLineDash([6, 4]);
    ctx.lineDashOffset = -antsOffset;
  } else if (model) {
    ctx.setLineDash([3, 3]);
  }
  ctx.strokeRect(Math.round(sx) + 0.5, Math.round(sy) + 0.5, Math.round(sw), Math.round(sh));
  ctx.setLineDash([]);

  if (!pending) {
    const conf = b.source === 'model' && b.conf != null ? ` ${Math.round(b.conf * 100)}%` : '';
    drawLabel(clsName(b) + conf, sx, sy, c);
  }

  if (isSel && mode !== 'READONLY') {
    for (const [, hx, hy] of handlesOf(b)) {
      const p = toScreen(hx, hy);
      ctx.fillStyle = '#fff';
      ctx.strokeStyle = c;
      ctx.lineWidth = 1.5;
      ctx.fillRect(Math.round(p.sx) - 3.5, Math.round(p.sy) - 3.5, 7, 7);
      ctx.strokeRect(Math.round(p.sx) - 3.5, Math.round(p.sy) - 3.5, 7, 7);
    }
  }
  ctx.restore();
}

/** Model view: the model's own boxes (white) and what the label check thinks is wrong (red). */
function drawQa() {
  const flagged = new Set(qa.issues.filter((i) => i.type === 'missed' || i.type === 'class').map((i) => JSON.stringify(i.box)));
  ctx.save();
  for (const p of qa.boxes) {
    const { sx, sy } = toScreen(p.x, p.y);
    const bad = flagged.has(JSON.stringify({ x: +p.x.toFixed(1), y: +p.y.toFixed(1), w: +p.w.toFixed(1), h: +p.h.toFixed(1) }));
    ctx.strokeStyle = bad ? 'rgba(242,85,90,0.95)' : 'rgba(255,255,255,0.75)';
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 3]);
    ctx.strokeRect(Math.round(sx) + 0.5, Math.round(sy) + 0.5, Math.round(p.w * scale), Math.round(p.h * scale));
    ctx.setLineDash([]);
    drawLabel(`model: ${p.class_name} ${Math.round(p.conf * 100)}%`, sx, sy + p.h * scale + 17, bad ? 'rgba(242,85,90,0.95)' : 'rgba(255,255,255,0.8)');
  }
  ctx.restore();
}

async function loadQa(token = loadToken) {
  if (!cur) return;
  try {
    const r = await IA.get(`/api/annotations/${cur.id}/predictions`);
    if (token !== loadToken) return;
    qa = r.job_id ? r : null;
    if (!qa && qaOn) $('qa-toggle').title = 'No model results for this frame — run Model assist from the dashboard';
    render();
  } catch { qa = null; }
}

// Marching ants for carried boxes
(function ants() {
  if (boxes.some((b) => b.carried)) { antsOffset = (antsOffset + 0.4) % 20; render(); }
  requestAnimationFrame(ants);
})();

// ── Pointer interactions ──────────────────────────────────────────────────────
function pos(e) {
  const r = canvas.getBoundingClientRect();
  return { sx: e.clientX - r.left, sy: e.clientY - r.top };
}
function setCursor(ix, iy) {
  if (spaceDown || drag?.kind === 'pan') { canvas.style.cursor = drag ? 'grabbing' : 'grab'; return; }
  if (mode === 'READONLY') { canvas.style.cursor = 'default'; return; }
  const sb = boxes[selected];
  if (sb) {
    const h = hitHandle(sb, ix, iy);
    if (h) { canvas.style.cursor = { nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize' }[h]; return; }
    if (inBox(sb, ix, iy)) { canvas.style.cursor = 'move'; return; }
  }
  if (drawMode) { canvas.style.cursor = 'crosshair'; return; }
  canvas.style.cursor = topBoxAt(ix, iy) >= 0 ? 'pointer' : 'default';
}

canvas.addEventListener('pointerdown', (e) => {
  if (!cur) return;
  canvas.setPointerCapture(e.pointerId);
  const { sx, sy } = pos(e);
  const { ix, iy } = toImage(sx, sy);

  if (e.button === 1 || (e.button === 0 && spaceDown)) {
    drag = { kind: 'pan', sx, sy, ox, oy };
    setCursor(ix, iy);
    return;
  }
  if (e.button !== 0 || mode === 'LOADING' || mode === 'READONLY') return;

  const sb = boxes[selected];
  if (sb) {
    const h = hitHandle(sb, ix, iy);
    if (h) { drag = { kind: 'resize', handle: h, index: selected, start: { ...sb }, ix, iy, before: snapshot(), bg: isBackground }; return; }
    if (inBox(sb, ix, iy)) { drag = { kind: 'move', index: selected, start: { ...sb }, ix, iy, before: snapshot(), bg: isBackground }; return; }
  }
  if (drawMode) {
    const p = { ix: clamp(ix, 0, cur.width), iy: clamp(iy, 0, cur.height) };
    drag = { kind: 'draw', p, box: null };
    return;
  }
  const hit = topBoxAt(ix, iy);
  selected = hit;
  if (hit >= 0) drag = { kind: 'move', index: hit, start: { ...boxes[hit] }, ix, iy, before: snapshot(), bg: isBackground };
  renderBoxList();
  render();
});

canvas.addEventListener('pointermove', (e) => {
  if (!cur) return;
  const { sx, sy } = pos(e);
  const { ix, iy } = toImage(sx, sy);
  hover = { ix, iy };
  $('sb-cursor').textContent = ix >= 0 && iy >= 0 && ix <= cur.width && iy <= cur.height ? `${round(ix)}, ${round(iy)}` : '—';

  if (!drag) { setCursor(ix, iy); if (drawMode) render(); return; }

  if (drag.kind === 'pan') {
    ox = drag.ox + (sx - drag.sx); oy = drag.oy + (sy - drag.sy);
    userZoomed = true;
  } else if (drag.kind === 'draw') {
    const q = { ix: clamp(ix, 0, cur.width), iy: clamp(iy, 0, cur.height) };
    drag.box = { classIdx: selClass, className: classes[selClass], x: Math.min(drag.p.ix, q.ix), y: Math.min(drag.p.iy, q.iy),
                 w: Math.abs(q.ix - drag.p.ix), h: Math.abs(q.iy - drag.p.iy) };
  } else if (drag.kind === 'move' && boxes[drag.index]) {
    const s = drag.start;
    const nx = clamp(s.x + (ix - drag.ix), 0, cur.width - s.w);
    const ny = clamp(s.y + (iy - drag.iy), 0, cur.height - s.h);
    boxes[drag.index] = { ...boxes[drag.index], x: nx, y: ny };
    drag.moved = true;
  } else if (drag.kind === 'resize' && boxes[drag.index]) {
    // Work in corner space so dragging past the opposite edge flips instead of sliding
    const s = drag.start;
    let x1 = s.x, y1 = s.y, x2 = s.x + s.w, y2 = s.y + s.h;
    const px = clamp(ix, 0, cur.width), py = clamp(iy, 0, cur.height);
    if (drag.handle.includes('w')) x1 = px;
    if (drag.handle.includes('e')) x2 = px;
    if (drag.handle.includes('n')) y1 = py;
    if (drag.handle.includes('s')) y2 = py;
    const nx = Math.min(x1, x2), ny = Math.min(y1, y2);
    boxes[drag.index] = { ...boxes[drag.index], x: nx, y: ny, w: Math.max(MIN_BOX, Math.abs(x2 - x1)), h: Math.max(MIN_BOX, Math.abs(y2 - y1)) };
    drag.moved = true;
  }
  render();
});

function endDrag() {
  if (!drag) return;
  const d = drag;
  drag = null;
  if (d.kind === 'draw') {
    if (d.box && d.box.w >= MIN_BOX && d.box.h >= MIN_BOX) {
      pushHistory();                              // before adding, so one undo removes it
      if (isBackground) isBackground = false;
      // track id links this object's copies across frames (carry / propagate → interpolate)
      boxes.push({ ...d.box, source: 'manual', trackId: newTrackId() });
      selected = boxes.length - 1;
      afterEdit();
    } else render();
  } else if ((d.kind === 'move' || d.kind === 'resize') && d.moved && boxes[d.index]) {
    const b = boxes[d.index];
    if (b.x !== d.start.x || b.y !== d.start.y || b.w !== d.start.w || b.h !== d.start.h) {
      history.push({ boxes: d.before, bg: d.bg });
      if (history.length > MAX_HISTORY) history.shift();
      future = [];
      b.source = 'manual';                         // hand-adjusted → this frame becomes a keyframe
      afterEdit();
    }
  } else render();
}
/** Escape while dragging: put the box back exactly where it was. */
function cancelDrag() {
  if (!drag) return;
  if ((drag.kind === 'move' || drag.kind === 'resize') && drag.moved) { boxes = drag.before; isBackground = drag.bg; }
  drag = null;
  renderBoxList();
  render();
}
canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', endDrag);
canvas.addEventListener('pointerleave', () => { hover = null; $('sb-cursor').textContent = '—'; if (drawMode) render(); });
canvas.addEventListener('contextmenu', (e) => e.preventDefault());
canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const { sx, sy } = pos(e);
  const lines = e.deltaMode === 1 ? 33 : 1;                    // Firefox reports lines
  if (e.ctrlKey) { zoomAt(sx, sy, Math.exp(-e.deltaY * lines * 0.01)); return; }   // trackpad pinch
  if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {               // two-finger sideways swipe pans
    ox -= e.deltaX * lines; userZoomed = true; render(); return;
  }
  // Mouse wheel notch (~100) ≈ ×1.16; small trackpad deltas zoom smoothly
  zoomAt(sx, sy, Math.exp(-clamp(e.deltaY * lines, -200, 200) * 0.0015));
}, { passive: false });

// ── Actions ───────────────────────────────────────────────────────────────────
const editable = () => mode !== 'READONLY' && mode !== 'LOADING';

function setDraw(on) {
  drawMode = on ?? !drawMode;
  $('tool-draw').classList.toggle('active', drawMode);
  if (drawMode) selected = -1;
  renderBoxList();
  render();
}
function deleteSelected() {
  if (!editable() || selected < 0) return;
  pushHistory();
  boxes.splice(selected, 1);
  selected = Math.min(selected, boxes.length - 1);
  afterEdit();
}
function setClass(i) {
  if (i < 0 || i >= classes.length) return;
  selClass = i;
  if (selected >= 0 && editable() && clsName(boxes[selected]) !== classes[i]) {
    pushHistory();
    boxes[selected] = { ...boxes[selected], classIdx: i, className: classes[i], source: 'manual' };
    afterEdit();
  }
  renderClasses();
}

/** The frame just before this one in the same video/folder (not just the last one visited). */
function previousInSequence() {
  const at = allImages.indexOf(cur);
  for (let i = at - 1; i >= 0; i--) if (allImages[i].seq_group === cur.seq_group) return allImages[i];
  return null;
}
async function copyPrevious() {
  if (!editable()) return;
  if (mode === 'CARRIED') { IA.toast('Boxes are already carried over — press Enter to keep them', 'info', 1800); return; }
  const prev = previousInSequence();
  let src = prevFrame && prev && prevFrame.imageId === prev.id ? prevFrame : null;
  if (!src && prev) {
    try {
      const d = await IA.get(`/api/annotations/${prev.id}`);
      src = { imageId: prev.id, boxes: d.annotations.map(fromServer), w: d.width, h: d.height };
    } catch { src = null; }
  }
  src = src || prevFrame;
  if (!src?.boxes.length) { IA.toast('No boxes on the previous frame to copy', 'info', 1800); return; }
  const copies = src.boxes.map((b) => fitBox(b, src.w, src.h, cur.width, cur.height)).filter(Boolean)
    .map((b) => ({ ...b, id: undefined, carried: false, source: 'carried' }));
  pushHistory();
  isBackground = false;
  boxes = boxes.concat(copies);
  selected = boxes.length - 1;
  afterEdit();
  IA.toast(`Copied ${IA.plural(copies.length, 'box', 'boxes')} from ${src.imageId === prev?.id ? 'the previous frame' : 'the last frame you visited'}`, 'info', 1600);
}

// ── Clipboard (Ctrl+C / Ctrl+V), works across frames ──────────────────────────
function copyBoxes() {
  if (!cur || !boxes.length) return;
  const pick = selected >= 0 ? [boxes[selected]] : boxes;
  IA.store.set('ia_clipboard', { from: cur.id, w: cur.width, h: cur.height, boxes: pick.map((b) => ({ ...b, id: undefined, carried: undefined })) });
  IA.toast(`Copied ${IA.plural(pick.length, 'box', 'boxes')} — Ctrl+V on any frame`, 'info', 1400);
}
function pasteBoxes() {
  const clip = IA.store.get('ia_clipboard');
  if (!editable() || !clip?.boxes?.length) return;
  const same = clip.from === cur.id;
  const pasted = clip.boxes.map((b) => fitBox(same ? { ...b, x: b.x + 8, y: b.y + 8 } : b, clip.w, clip.h, cur.width, cur.height))
    .filter(Boolean)
    .map((b) => ({ ...b, classIdx: resolveClassIdx(b.className), source: 'manual', trackId: same ? newTrackId() : b.trackId }));
  if (!pasted.length) return;
  pushHistory();
  isBackground = false;
  boxes = boxes.concat(pasted);
  selected = boxes.length - 1;
  afterEdit();
}

function toggleBackground() {
  if (!editable()) return;
  if (!isBackground && boxes.length) {
    IA.confirm({ title: 'Mark as “no objects”?', message: `This removes the ${IA.plural(boxes.length, 'box', 'boxes')} on this frame. Background frames teach the model what is not an object.`, confirmLabel: 'Remove boxes & mark' })
      .then((ok) => { if (ok) { pushHistory(); boxes = []; isBackground = true; selected = -1; afterEdit(); } });
    return;
  }
  pushHistory();
  isBackground = !isBackground;
  afterEdit();
}
function nudge(dx, dy) {
  const b = boxes[selected];
  if (!b || !editable()) return;
  pushHistory();
  boxes[selected] = { ...b, x: clamp(b.x + dx, 0, cur.width - b.w), y: clamp(b.y + dy, 0, cur.height - b.h), source: 'manual' };
  afterEdit();
}

// ── View adjustments ──────────────────────────────────────────────────────────
function syncAdjust() {
  $('adj-bright').value = adj.bright;
  $('adj-contrast').value = adj.contrast;
  $('adj-invert').checked = adj.invert;
  IA.store.set(`ia_adjust_${PID}`, adj);
  render();
}
$('adj-bright').addEventListener('input', (e) => { adj.bright = +e.target.value; syncAdjust(); });
$('adj-contrast').addEventListener('input', (e) => { adj.contrast = +e.target.value; syncAdjust(); });
$('adj-invert').addEventListener('change', (e) => { adj.invert = e.target.checked; syncAdjust(); });
$('adj-reset').addEventListener('click', () => { adj = { bright: 100, contrast: 100, invert: false }; syncAdjust(); });
function setLockZoom(on) {
  lockZoom = on;
  IA.store.set('ia_lock_zoom', on);
  $('lock-zoom').classList.toggle('active', on);
}
function setQa(on) {
  qaOn = on;
  IA.store.set('ia_qa_overlay', on);
  $('qa-toggle').classList.toggle('active', on);
  if (on) loadQa(); else { qa = null; render(); }
}

// ── Submit ────────────────────────────────────────────────────────────────────
function updateSubmit() {
  const b = $('submit-btn');
  if (!cur) { b.disabled = true; return; }
  const st = cur.state;
  b.disabled = st !== 'annotated' || mode === 'CARRIED';
  b.innerHTML = st === 'ready_for_export' || st === 'exported'
    ? `${IA.icon('check-check')}In dataset` : `${IA.icon('check')}Add to dataset`;
  b.classList.toggle('btn-success', st === 'annotated');
}
/** Save, add to the dataset, and (Ctrl+Enter) go straight to the next frame. */
async function submitCurrent({ advance = false } = {}) {
  if (!cur) return;
  if (mode === 'CARRIED') acceptCarried(true);
  if (hasModelSuggestions()) acceptModel();
  if (!(await flushSave())) return;
  if (cur.state === 'annotated') {
    try {
      await IA.post(`/api/projects/${PID}/images/${cur.id}/submit`);
      applyImageMeta(cur, { state: 'ready_for_export', review_status: null });
      review = null; updateBanners();
      IA.toast('Added to the dataset', 'success', 1200);
    } catch (e) { IA.toast(e.message, 'error'); return; }
  } else if (!advance) {
    IA.toast(cur.state === 'ready_for_export' || cur.state === 'exported' ? 'Already in the dataset' : 'Draw a box or mark the frame as “no objects” first', 'info', 1500);
    return;
  }
  if (advance) navigate(1);
}
$('submit-btn').addEventListener('click', () => submitCurrent());
$('submit-all-btn').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  if (!(await flushSave())) return;
  if (isAdmin) {
    const n = allImages.filter((i) => i.state === 'annotated').length;
    const ok = await IA.confirm({ title: 'Add everyone’s annotated frames?', message: `As an admin this adds all ${IA.plural(n, 'annotated frame')} in the project to the dataset — not just yours.`, confirmLabel: 'Add all' });
    if (!ok) return;
  }
  try {
    const r = await IA.post(`/api/projects/${PID}/images/submit-all-annotated`);
    IA.toast(r.detail, r.skipped_empty ? 'warn' : 'success', r.skipped_empty ? 7000 : 3000);
    await refreshIndex();
  } catch (err) { IA.toast(err.message, 'error'); }
}));

// ── Filmstrip (virtualised) ───────────────────────────────────────────────────
const strip = $('strip');
let stripFilter = IA.store.get(`ia_strip_filter_${PID}`, 'all');
const FILTER_FN = {
  all: () => true,
  todo: (i) => TODO.includes(i.state),
  done: (i) => DONE.includes(i.state),
  fix: (i) => i.review_status === 'rejected',
};

function applyFilter() {
  const f = FILTER_FN[stripFilter] || FILTER_FN.all;
  // The frame being worked on stays in the list even once it no longer matches the filter
  view = allImages.filter((i) => i === cur || f(i));
  if (!view.length && allImages.length && stripFilter !== 'all') { stripFilter = 'all'; return applyFilter(); }
  $('strip-spacer').style.height = `${view.length * ROW_H}px`;
  $('strip-count').textContent = IA.fmtNum(view.length);
  $('strip-filter').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.v === stripFilter));
  IA.segSync($('strip-filter'));
  idx = cur ? view.indexOf(cur) : -1;
  range = null; updateRange();
  renderStripRows();
}

function renderStripRows() {
  const top = strip.scrollTop, h = strip.clientHeight;
  const first = Math.max(0, Math.floor(top / ROW_H) - 6), last = Math.min(view.length, Math.ceil((top + h) / ROW_H) + 6);
  const want = new Set();
  for (let i = first; i < last; i++) want.add(i);
  strip.querySelectorAll('.frame').forEach((el) => { if (!want.has(+el.dataset.i)) el.remove(); });
  for (let i = first; i < last; i++) {
    const it = view[i];
    let el = strip.querySelector(`.frame[data-i="${i}"]`);
    const suggest = it.model_boxes > 0 && TODO.includes(it.state);
    const key = `${it.id}|${it.state}|${it.box_count}|${it.is_keyframe}|${it.is_background}|${it.review_status}|${suggest}`;
    if (!el || el.dataset.key !== key) {
      const html = `<div class="frame" data-i="${i}" data-key="${IA.esc(key)}" style="top:${i * ROW_H}px" title="${IA.esc(it.filename)}">
        <div class="ft"><img src="${IA.thumbUrl(it.id, 160)}" alt="" loading="lazy" onload="this.classList.add('loaded')" onerror="this.remove()"/><span class="pip state-pip" style="--c:${IA.stateColor(it.state)}"></span></div>
        <div style="min-width:0"><div class="fn">${IA.esc(it.filename)}</div>
          <div class="fm"><span>#${it.seq ?? i + 1}</span>${it.box_count ? `<span>${IA.icon('box')}${it.box_count}</span>` : ''}${it.is_keyframe ? `<span class="kf" title="Keyframe — has hand-drawn boxes">${IA.icon('milestone')}key</span>` : ''}${it.is_background ? `<span>${IA.icon('ban')}bg</span>` : ''}${suggest ? `<span class="ai" title="Model suggestions to review">${IA.icon('sparkle')}AI</span>` : ''}${it.review_status === 'rejected' ? `<span class="fix" title="Sent back by a reviewer">${IA.icon('flag')}fix</span>` : ''}</div></div></div>`;
      const nel = IA.h(html);
      if (el) el.replaceWith(nel); else strip.appendChild(nel);
      el = nel;
    }
    el.classList.toggle('active', i === idx);
    el.classList.toggle('in-sel', !!range && i >= Math.min(range.a, range.b) && i <= Math.max(range.a, range.b));
    el.classList.toggle('group-start', i > 0 && view[i - 1].seq_group !== it.seq_group);
  }
}
strip.addEventListener('scroll', () => requestAnimationFrame(renderStripRows), { passive: true });
function scrollStripTo(i) {
  const y = i * ROW_H;
  if (y < strip.scrollTop + ROW_H || y > strip.scrollTop + strip.clientHeight - ROW_H * 2) {
    strip.scrollTo({ top: Math.max(0, y - strip.clientHeight / 2 + ROW_H / 2), behavior: Math.abs(strip.scrollTop - y) < 2000 ? 'smooth' : 'auto' });
  }
}
strip.addEventListener('click', (e) => {
  const el = e.target.closest('.frame');
  if (!el) return;
  const i = +el.dataset.i;
  if (e.shiftKey) {
    range = { a: anchor ?? idx, b: i };
    updateRange();
    renderStripRows();
    return;
  }
  anchor = i;
  if (range) { range = null; updateRange(); }
  jumpTo(i);
});
function updateRange() {
  const box = $('strip-sel');
  if (!range) { box.classList.add('hidden'); renderStripRows(); return; }
  const n = Math.abs(range.b - range.a) + 1;
  $('strip-sel-txt').textContent = `${n} frames selected — P / I act on these`;
  box.classList.remove('hidden');
}
$('strip-sel-clear').addEventListener('click', () => { range = null; updateRange(); });
$('strip-filter').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  stripFilter = b.dataset.v;
  IA.store.set(`ia_strip_filter_${PID}`, stripFilter);
  applyFilter();
});

function updateCounts() {
  const mine = allImages;
  const done = mine.filter((i) => DONE.includes(i.state)).length;
  const pending = mine.filter((i) => i.state === 'annotated').length;
  $('t-progress').textContent = `${IA.fmtNum(done)} / ${IA.fmtNum(mine.length)} annotated`;
  $('strip-count').textContent = IA.fmtNum(view.length);
  const btn = $('submit-all-btn');
  btn.disabled = pending === 0;
  btn.querySelector('span').textContent = pending ? `Add All Annotated Images to Dataset (${IA.fmtNum(pending)})` : 'Add All Annotated Images to Dataset';
}

function setIndex(items) {
  allImages = items;
  byId = new Map(allImages.map((i) => [i.id, i]));
}

async function refreshIndex() {
  try {
    const data = await IA.get(`/api/projects/${PID}/images/index`);
    setIndex(data.items.map((it) => (byId.has(it.id) ? Object.assign(byId.get(it.id), it) : it)));
    if (cur && !byId.has(cur.id)) {
      // Unassigned or deleted while open: keep nothing that can't be saved any more
      leaveCurrent(isAdmin ? 'This frame was removed from the project' : 'This frame is no longer assigned to you');
      return;
    }
    if (cur) cur = byId.get(cur.id);
    applyFilter();
    updateCounts();
    if (cur) updateSubmit();
  } catch (e) { IA.toast(e.message, 'error'); }
}
const refreshIndexSoon = IA.debounce(refreshIndex, 500);

// ── Inspector rendering ───────────────────────────────────────────────────────
function renderClasses() {
  const q = ($('cls-search').value || '').toLowerCase();
  $('cls-search').classList.toggle('hidden', classes.length <= 9);
  const key = (i) => (i < 9 ? `${i + 1}` : i < 18 ? `⇧${i - 8}` : '');
  $('cls-list').innerHTML = classes.map((c, i) => (q && !c.toLowerCase().includes(q)) ? '' : `
    <button class="cls ${i === selClass ? 'active' : ''}" data-i="${i}" style="--c:${IA.classColor(i)}">
      <span class="sw"></span><span>${IA.esc(c)}</span>${key(i) ? `<kbd>${key(i)}</kbd>` : ''}</button>`).join('');
}
$('cls-list').addEventListener('click', (e) => { const b = e.target.closest('.cls'); if (b) setClass(+b.dataset.i); });
$('cls-search').addEventListener('input', renderClasses);

function renderBoxList() {
  $('box-count').textContent = boxes.length || '';
  $('sb-boxes').textContent = IA.plural(boxes.length, 'box', 'boxes');
  if (!boxes.length) {
    $('box-list').innerHTML = `<div class="box-empty">${isBackground ? 'Background frame — no objects.' : 'No boxes yet.'}<br/>Press <kbd>B</kbd> to draw${isBackground ? '' : ', <kbd>N</kbd> if there is nothing to label'}.</div>`;
    return;
  }
  $('box-list').innerHTML = boxes.map((b, i) => {
    const src = b.carried ? '<span class="src carried">carried</span>'
      : b.source === 'model' ? `<span class="src model">model${b.conf != null ? ` ${Math.round(b.conf * 100)}%` : ''}</span>`
      : b.source === 'propagated' ? '<span class="src">prop</span>' : b.source === 'interpolated' ? '<span class="src">interp</span>' : '';
    return `<div class="box-row ${i === selected ? 'active' : ''}" data-i="${i}" style="--c:${color(b)}">
      <span class="sw"></span><span class="nm ${b.classIdx < 0 ? 'orphan' : ''}" title="${b.classIdx < 0 ? 'Class no longer in the project' : ''}">${IA.esc(clsName(b))}</span>${src}
      <button class="btn btn-ghost btn-icon btn-sm del" data-del="${i}" aria-label="Delete box">${IA.icon('x')}</button></div>`;
  }).join('');
}
$('box-list').addEventListener('click', (e) => {
  const del = e.target.closest('[data-del]');
  if (del) { selected = +del.dataset.del; deleteSelected(); return; }
  const row = e.target.closest('.box-row');
  if (row) { selected = +row.dataset.i; setDraw(false); }
});

function updateBanners() {
  $('carry-banner').classList.toggle('hidden', mode !== 'CARRIED');
  if (mode === 'CARRIED') $('carry-text').textContent = `${IA.plural(boxes.length, 'box', 'boxes')} carried from the previous frame`;
  const model = hasModelSuggestions();
  $('model-banner').classList.toggle('hidden', !model);
  if (model) $('model-text').textContent = `${IA.plural(boxes.filter((b) => b.source === 'model').length, 'model suggestion')}`;
  const rejected = review?.status === 'rejected';
  $('review-banner').classList.toggle('hidden', !rejected);
  if (rejected) $('review-text').textContent = `Sent back${review.by ? ` by ${review.by}` : ''}: ${review.note || 'please check this frame again'}`;
  $('readonly-banner').classList.toggle('hidden', cur?.state !== 'exported');
  $('bg-banner').classList.toggle('hidden', !(isBackground && !boxes.length));
  $('tool-bg').classList.toggle('active', isBackground && !boxes.length);
  ['tool-draw', 'tool-copy', 'tool-bg', 'prop-btn', 'interp-btn'].forEach((id) => { $(id).disabled = mode === 'READONLY'; });
}

// ── Propagate / interpolate / undo batch ──────────────────────────────────────
function previewRows(targets, max = 200) {
  const reason = { not_empty: 'already has boxes', forbidden: 'not yours', exported: 'exported', submitted: 'in dataset', manual: 'hand-drawn', not_found: 'missing' };
  return `<div class="pv-list"><table class="table"><thead><tr><th>Frame</th><th>Result</th></tr></thead><tbody>${
    targets.slice(0, max).map((t) => `<tr><td>${IA.esc(t.filename || t.image_id)}${t.warning === 'other_group' ? ' <span class="chip warn" title="From a different video or folder than the source frame">other video</span>' : ''}</td><td>${t.action === 'write'
      ? `<span class="chip success">write ${t.box_count_new}</span>` : `<span class="chip">skip · ${IA.esc(reason[t.reason] || t.reason)}</span>`}</td></tr>`).join('')
  }${targets.length > max ? `<tr><td colspan="2" class="muted">…and ${targets.length - max} more</td></tr>` : ''}</tbody></table></div>`;
}
function highlight(targets) {
  const w = new Set(targets.filter((t) => t.action === 'write').map((t) => t.image_id));
  const s = new Set(targets.filter((t) => t.action !== 'write').map((t) => t.image_id));
  strip.querySelectorAll('.frame').forEach((el) => {
    const it = view[+el.dataset.i];
    el.classList.toggle('pv-write', !!it && w.has(it.id));
    el.classList.toggle('pv-skip', !!it && s.has(it.id));
  });
}
const clearHighlight = () => strip.querySelectorAll('.pv-write,.pv-skip').forEach((el) => el.classList.remove('pv-write', 'pv-skip'));

function applyBulkResult(res, kind) {
  (res.applied || res.restored || []).forEach((a) => {
    const it = byId.get(a.image_id);
    if (it) Object.assign(it, { state: a.state, box_count: a.box_count, is_keyframe: kind === 'undo' ? it.is_keyframe : false });
  });
  renderStripRows(); updateCounts();
  if (res.batch_id) {
    lastBatch = { id: res.batch_id, kind, n: (res.applied || []).length };
    IA.store.set(BATCH_KEY, lastBatch);
  }
  renderUndoBatch();
  // The source frame's boxes may have received track ids too, so always refresh the open frame
  reloadCurrent();
}
function renderUndoBatch() {
  const b = $('undo-batch-btn');
  b.classList.toggle('hidden', !lastBatch);
  if (lastBatch) b.querySelector('span').textContent = `Undo ${lastBatch.kind} (${lastBatch.n} frames)`;
}
async function undoBatch() {
  if (!lastBatch) return;
  try {
    const r = await IA.post(`/api/projects/${PID}/annotations/batches/${lastBatch.id}/undo`);
    const kind = lastBatch.kind;
    lastBatch = null; IA.store.del(BATCH_KEY);
    applyBulkResult(r, 'undo');
    await refreshIndex();
    IA.toast(`Undid ${kind} on ${IA.plural(r.restored.length, 'frame')}${r.conflicts.length ? ` · ${r.conflicts.length} changed since and kept` : ''}`, r.conflicts.length ? 'warn' : 'success');
  } catch (e) {
    if ([403, 404, 409].includes(e.status)) { lastBatch = null; IA.store.del(BATCH_KEY); renderUndoBatch(); }
    IA.toast(e.message, 'error');
  }
}
$('undo-batch-btn').addEventListener('click', undoBatch);
const undoAction = { label: 'Undo', fn: undoBatch };

async function openPropagate() {
  if (!editable() || !cur) return;
  if (mode === 'CARRIED') acceptCarried(true);
  if (!(await flushSave())) return;
  if (!boxes.length) { IA.toast('Draw the boxes on this frame first — they are what gets copied', 'info', 2500); return; }
  const inRange = range ? view.slice(Math.min(range.a, range.b), Math.max(range.a, range.b) + 1).filter((i) => i.id !== cur.id) : null;
  const saved = IA.store.get('ia_prop_opts', { count: 10, mode: 'only_empty', until: true });
  let closed = false, previewSeq = 0;
  const m = IA.modal({
    title: 'Propagate boxes',
    sub: `Copy the ${IA.plural(boxes.length, 'box', 'boxes')} on this frame to other frames — for objects that stay in place.`,
    size: 'wide',
    body: `
      <div class="grid-2">
        <div class="field">${inRange ? `<label>Target frames</label><div class="input" style="display:flex;align-items:center">${IA.plural(inRange.length, 'selected frame')}</div>`
          : `<label for="pp-count">Next frames</label><input class="input" id="pp-count" type="number" min="1" max="5000" value="${saved.count}"/>`}</div>
        <div class="field"><label for="pp-mode">If a frame already has boxes</label>
          <select class="select" id="pp-mode"><option value="only_empty">Skip it (safe)</option><option value="append">Add these boxes too</option><option value="replace">Replace its boxes</option></select></div>
      </div>
      ${inRange ? '' : `<label class="switch"><input type="checkbox" id="pp-until" ${saved.until ? 'checked' : ''}/>Stop at the next frame that already has boxes</label>`}
      <div class="pv-summary" id="pp-summary"><span class="muted">Checking frames…</span></div>
      <div id="pp-list"></div>`,
    onClose: () => { closed = true; clearHighlight(); },
    actions: [
      { label: 'Cancel', variant: 'btn-ghost' },
      { id: 'go', label: 'Propagate', variant: 'btn-primary', fn: async () => {
        const body = params(false);
        try {
          const r = await IA.post(`/api/projects/${PID}/annotations/propagate`, body);
          applyBulkResult(r, 'propagate');
          IA.toast(`Propagated to ${IA.plural(r.applied.length, 'frame')}${r.summary.skip ? ` · ${r.summary.skip} skipped` : ''}`, 'success', { action: undoAction });
        } catch (e) { IA.toast(e.message, 'error'); return false; }
        return true;
      } },
    ],
  });
  m.body.querySelector('#pp-mode').value = saved.mode;
  function params(dry) {
    const o = { source_image_id: cur.id, mode: m.body.querySelector('#pp-mode').value, dry_run: dry };
    if (inRange) o.target_image_ids = inRange.map((i) => i.id);
    else {
      o.count = Math.max(1, parseInt(m.body.querySelector('#pp-count').value, 10) || 1);
      o.until_next_annotated = m.body.querySelector('#pp-until').checked;
    }
    IA.store.set('ia_prop_opts', { count: o.count ?? saved.count, mode: o.mode, until: o.until_next_annotated ?? saved.until });
    return o;
  }
  const preview = IA.debounce(async () => {
    if (closed) return;
    const seq = ++previewSeq;
    try {
      const r = await IA.post(`/api/projects/${PID}/annotations/propagate`, params(true));
      if (closed || seq !== previewSeq) return;                // a newer preview (or closing) wins
      const other = r.targets.filter((t) => t.warning === 'other_group').length;
      m.body.querySelector('#pp-summary').innerHTML = `<span><b>${r.summary.write}</b> frames will get boxes</span><span class="muted"><b>${r.summary.skip}</b> skipped</span>${other ? `<span style="color:var(--warning)">${other} from another video</span>` : ''}`;
      m.body.querySelector('#pp-list').innerHTML = r.targets.length ? previewRows(r.targets) : '<p class="small muted">No frames in range.</p>';
      m.actions.go.disabled = !r.summary.write;
      highlight(r.targets);
    } catch (e) { if (!closed) { m.body.querySelector('#pp-summary').innerHTML = `<span style="color:var(--danger)">${IA.esc(e.message)}</span>`; m.actions.go.disabled = true; } }
  }, 250);
  m.body.addEventListener('input', preview);
  m.body.addEventListener('change', preview);
  preview();
}

/** Keyframes around the current frame, searched in the full sequence (not just the filtered list). */
function nearestKeyframes() {
  const at = allImages.indexOf(cur);
  const isKf = (it) => it && (it.is_keyframe || (it === cur && boxes.some((b) => b.source === 'manual' && !b.carried)));
  if (range) return [view[Math.min(range.a, range.b)], view[Math.max(range.a, range.b)]];
  let before = null, after = null;
  for (let i = at - 1; i >= 0; i--) {
    if (allImages[i].seq_group !== cur.seq_group) break;
    if (isKf(allImages[i])) { before = allImages[i]; break; }
  }
  if (isKf(cur)) return before ? [before, cur] : [cur, null];
  for (let i = at + 1; i < allImages.length; i++) {
    if (allImages[i].seq_group !== cur.seq_group) break;
    if (isKf(allImages[i])) { after = allImages[i]; break; }
  }
  return [before, after];
}

async function openInterpolate() {
  if (!editable() || !cur) return;
  if (mode === 'CARRIED') acceptCarried(true);
  if (!(await flushSave())) return;
  const [a, b] = nearestKeyframes();
  if (!a || !b) {
    IA.modal({ title: 'Interpolate between keyframes', body: `
      <p>Interpolation fills the frames <i>between</i> two keyframes by moving each box in a straight line.</p>
      <div class="drop-steps small">
        <div>Draw the boxes on a first frame (e.g. frame 10).</div>
        <div>Jump ahead to where the object has moved (e.g. frame 60) and draw or adjust the same boxes.</div>
        <div>Press <kbd>I</kbd>. Frames 11–59 are filled in. If the path bends, fix a middle frame by hand and press <kbd>I</kbd> again.</div>
      </div>
      <p class="small muted">Tip: shift-click two frames in the filmstrip to choose the range yourself.</p>`,
      actions: [{ label: 'Got it', variant: 'btn-primary' }] });
    return;
  }
  let closed = false;
  const m = IA.modal({
    title: 'Interpolate between keyframes',
    sub: `${a.filename} → ${b.filename}`,
    size: 'wide',
    body: `
      <div class="field"><label for="ip-over">Frames in between that already have generated boxes</label>
        <select class="select" id="ip-over"><option value="empty_only">Leave them (only fill empty frames)</option><option value="generated">Recompute them (hand-drawn frames are never touched)</option></select></div>
      <div class="pv-summary" id="ip-summary"><span class="muted">Matching boxes…</span></div>
      <div id="ip-warn"></div><div id="ip-list"></div>`,
    onClose: () => { closed = true; clearHighlight(); },
    actions: [
      { label: 'Cancel', variant: 'btn-ghost' },
      { id: 'go', label: 'Interpolate', variant: 'btn-primary', fn: async () => {
        try {
          const r = await IA.post(`/api/projects/${PID}/annotations/interpolate`, params(false));
          applyBulkResult(r, 'interpolate');
          IA.toast(`Interpolated ${IA.plural(r.applied.length, 'frame')}`, 'success', { action: undoAction });
        } catch (e) { IA.toast(e.message, 'error'); return false; }
        return true;
      } },
    ],
  });
  const params = (dry) => ({ start_image_id: a.id, end_image_id: b.id, overwrite: m.body.querySelector('#ip-over').value, dry_run: dry });
  const preview = async () => {
    try {
      const r = await IA.post(`/api/projects/${PID}/annotations/interpolate`, params(true));
      if (closed) return;
      const matched = r.segments.reduce((s, x) => s + x.matched, 0);
      m.body.querySelector('#ip-summary').innerHTML = `<span><b>${r.summary.write}</b> frames to fill</span><span class="muted"><b>${r.summary.keyframes}</b> keyframes · <b>${matched}</b> matched boxes · <b>${r.summary.skip}</b> skipped</span>`;
      const un = r.segments.flatMap((s) => [...s.unmatched_from, ...s.unmatched_to]);
      m.body.querySelector('#ip-warn').innerHTML = un.length ? `<div class="callout warn">${IA.icon('circle-alert')}<div>${IA.plural(un.length, 'box', 'boxes')} (${IA.esc([...new Set(un)].join(', '))}) have no partner on the other keyframe and won't be interpolated. Draw the same object on both keyframes.</div></div>` : '';
      m.body.querySelector('#ip-list').innerHTML = r.targets.length ? previewRows(r.targets) : '<p class="small muted">These keyframes are next to each other — nothing in between.</p>';
      m.actions.go.disabled = !r.summary.write;
      highlight(r.targets);
    } catch (e) {
      if (closed) return;
      m.body.querySelector('#ip-summary').innerHTML = `<span style="color:var(--danger)">${IA.esc(e.message)}</span>`;
      m.actions.go.disabled = true;
    }
  };
  m.body.querySelector('#ip-over').addEventListener('change', preview);
  preview();
}

/** Re-read the open frame from the server. force = drop unsaved local edits. */
async function reloadCurrent(force = false) {
  if (!cur) return;
  if (!force && (mode === 'DIRTY' || savePromise)) { showConflict(); return; }
  if (force) discardLocal();
  const keepIdx = idx;
  idx = -1;
  await loadFrame(keepIdx);
}

// ── Keyboard ──────────────────────────────────────────────────────────────────
function showShortcuts() {
  const k = (keys, label) => `<div><span>${label}</span><span>${keys.map((x) => `<kbd>${x}</kbd>`).join('')}</span></div>`;
  IA.modal({ title: 'Keyboard shortcuts', size: 'wide', body: `<div class="keys">
    <h4>Frames</h4>${k(['F'], 'Next frame')}${k(['S'], 'Previous frame')}${k(['→'], 'Next (← previous) when no box is selected')}${k(['Ctrl', 'Enter'], 'Add to dataset and go to the next frame')}
    <h4>Boxes</h4>${k(['B'], 'Draw mode on / off')}${k(['1–9'], 'Pick class (changes the selected box)')}${k(['Shift', '1–9'], 'Classes 10–18')}${k(['[', ']'], 'Previous / next class')}${k(['Del'], 'Delete selected box')}${k(['Tab'], 'Select next box')}
    ${k(['D'], 'Select last box')}${k(['Arrows'], 'Nudge selected box (Shift = 10 px)')}${k(['Ctrl', 'C'], 'Copy selected box (or all)')}${k(['Ctrl', 'V'], 'Paste boxes — also on another frame')}${k(['R'], 'Copy boxes from the previous frame')}${k(['N'], 'Mark frame as “no objects”')}
    <h4>Sequences &amp; suggestions</h4>${k(['C'], 'Carry boxes forward on / off')}${k(['Enter'], 'Keep carried boxes / accept model suggestions')}${k(['Esc'], 'Discard carried boxes · cancel a drag')}${k(['P'], 'Propagate to next frames')}${k(['I'], 'Interpolate between keyframes')}${k(['Shift', 'click'], 'Select a range in the filmstrip')}
    <h4>View &amp; edit</h4>${k(['Ctrl', 'Z'], 'Undo')}${k(['Ctrl', 'Shift', 'Z'], 'Redo')}${k(['Ctrl', 'S'], 'Save now')}${k(['H'], 'Tap: hide / show boxes · hold: peek')}${k(['M'], 'Model view (compare with the model)')}${k(['L'], 'Lock zoom between frames')}${k(['Space'], 'Hold + drag to pan')}${k(['0'], 'Fit image')}${k(['Wheel'], 'Zoom at cursor (pinch on a trackpad)')}
  </div>`, actions: [{ label: 'Close', variant: 'btn-primary' }] });
}

function setHidden(on) {
  hideBoxes = on;
  $('tool-hide').classList.toggle('active', hideBoxes);
  render();
}

document.addEventListener('keydown', (e) => {
  if (document.querySelector('.overlay')) return;                // a dialog owns the keyboard
  const t = e.target;
  if (t.matches('input, textarea, select') && !t.matches('input[type="checkbox"], input[type="range"]')) return;
  const ctrl = e.ctrlKey || e.metaKey;
  const k = e.key;

  // While dragging, only Escape (cancel) and Space (pan) do anything — no edits under the mouse
  if (drag && k !== 'Escape' && e.code !== 'Space') { e.preventDefault(); return; }

  if (ctrl && (k === 'z' || k === 'Z')) { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
  if (ctrl && (k === 'y' || k === 'Y')) { e.preventDefault(); redo(); return; }
  if (ctrl && (k === 's' || k === 'S')) { e.preventDefault(); if (mode === 'CARRIED') acceptCarried(true); else if (hasModelSuggestions()) acceptModel(); else if (mode === 'DIRTY') save(); else IA.toast('Already saved', 'info', 900); return; }
  if (ctrl && k === 'Enter') { e.preventDefault(); submitCurrent({ advance: true }); return; }
  if (ctrl && (k === 'c' || k === 'C') && !window.getSelection()?.toString()) { e.preventDefault(); copyBoxes(); return; }
  if (ctrl && (k === 'v' || k === 'V')) { e.preventDefault(); pasteBoxes(); return; }
  if (ctrl || e.altKey) return;

  if (e.code === 'Space') { e.preventDefault(); if (!spaceDown) { spaceDown = true; canvas.style.cursor = 'grab'; } return; }

  const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  if (arrows[k]) {
    e.preventDefault();
    if (selected >= 0 && editable()) { const [dx, dy] = arrows[k]; const s = e.shiftKey ? 10 : 1; nudge(dx * s, dy * s); }
    else if (e.repeat && navPending) return;                     // holding a key: don't pile up frames
    else if (k === 'ArrowRight') navigate(1, { repeat: e.repeat });
    else if (k === 'ArrowLeft') navigate(-1, { repeat: e.repeat });
    return;
  }
  if (e.shiftKey && /^Digit[1-9]$/.test(e.code)) { setClass(8 + +e.code.slice(5)); return; }

  switch (k.toLowerCase()) {
    case 'f': e.preventDefault(); if (!(e.repeat && navPending)) navigate(1, { repeat: e.repeat }); return;
    case 's': e.preventDefault(); if (!(e.repeat && navPending)) navigate(-1, { repeat: e.repeat }); return;
    case 'b': setDraw(); return;
    case 'v': setDraw(false); return;
    case 'r': copyPrevious(); return;
    case 'n': toggleBackground(); return;
    case 'c': setCarry(!carryOn); return;
    case 'p': openPropagate(); return;
    case 'i': openInterpolate(); return;
    case 'm': setQa(!qaOn); return;
    case 'l': setLockZoom(!lockZoom); IA.toast(lockZoom ? 'Zoom stays locked between frames' : 'Each frame opens fitted', 'info', 1300); return;
    case 'h': if (!e.repeat) { hDownAt = performance.now(); setHidden(!hideBoxes); } return;
    case '[': setClass((selClass - 1 + classes.length) % classes.length); return;
    case ']': setClass((selClass + 1) % classes.length); return;
    case 'd': if (boxes.length) { selected = boxes.length - 1; renderBoxList(); render(); } return;
    case '0': fit(); render(); return;
    case '?': showShortcuts(); return;
    case 'tab': {
      e.preventDefault();
      const n = boxes.length;
      if (n) { selected = selected < 0 ? (e.shiftKey ? n - 1 : 0) : (selected + (e.shiftKey ? n - 1 : 1)) % n; renderBoxList(); render(); }
      return;
    }
    case 'delete': case 'backspace': e.preventDefault(); deleteSelected(); return;
    case 'enter':
      e.preventDefault();                                          // never also "click" a focused button
      if (mode === 'CARRIED') { acceptCarried(true); IA.toast('Kept the carried boxes', 'success', 900); }
      else if (hasModelSuggestions()) acceptModel();
      else { selected = -1; if (mode === 'DIRTY') save(); renderBoxList(); render(); }
      return;
    case 'escape':
      if (drag) { cancelDrag(); return; }
      if (mode === 'CARRIED' && selected < 0) { discardCarried(); return; }
      if (range) { range = null; updateRange(); return; }
      selected = -1; setDraw(false);
      return;
    default:
      if (/^[1-9]$/.test(k)) setClass(+k - 1);
  }
});
document.addEventListener('keyup', (e) => {
  if (e.code === 'Space') { spaceDown = false; canvas.style.cursor = drawMode ? 'crosshair' : 'default'; }
  // Held H (rather than tapped) only peeks: release brings the boxes back
  if (e.key.toLowerCase() === 'h' && hDownAt && performance.now() - hDownAt > 350) setHidden(!hideBoxes);
  if (e.key.toLowerCase() === 'h') hDownAt = 0;
});
window.addEventListener('blur', () => { spaceDown = false; });
// A mouse click leaves focus on the button; Enter / Space must not trigger it again later
document.addEventListener('click', (e) => { if (e.detail > 0) e.target.closest('button')?.blur(); });

// ── Toolbar wiring ────────────────────────────────────────────────────────────
function setCarry(on) {
  carryOn = on;
  IA.store.set(`ia_carry_${PID}`, on);
  $('carry-toggle').checked = on;
  if (!on && mode === 'CARRIED') discardCarried();
  IA.toast(on ? 'Carry boxes forward: on — each next frame starts with these boxes' : 'Carry boxes forward: off', 'info', 1800);
}
$('carry-toggle').checked = carryOn;
$('carry-toggle').addEventListener('change', (e) => setCarry(e.target.checked));
$('tool-draw').addEventListener('click', () => setDraw());
$('tool-copy').addEventListener('click', copyPrevious);
$('tool-undo').addEventListener('click', undo);
$('tool-hide').addEventListener('click', () => setHidden(!hideBoxes));
$('tool-bg').addEventListener('click', toggleBackground);
$('tool-save').addEventListener('click', () => { if (mode === 'CARRIED') acceptCarried(true); else if (hasModelSuggestions()) acceptModel(); else if (mode === 'DIRTY') save(); else IA.toast('Already saved', 'info', 900); });
$('prop-btn').addEventListener('click', openPropagate);
$('interp-btn').addEventListener('click', openInterpolate);
$('zoom-in').addEventListener('click', () => zoomAt(stage.clientWidth / 2, viewH() / 2, 1.25));
$('zoom-out').addEventListener('click', () => zoomAt(stage.clientWidth / 2, viewH() / 2, 1 / 1.25));
$('zoom-val').addEventListener('click', () => { fit(); render(); });
$('shortcuts-btn').addEventListener('click', showShortcuts);
$('lock-zoom').addEventListener('click', () => setLockZoom(!lockZoom));
$('qa-toggle').addEventListener('click', () => setQa(!qaOn));

// Leaving the page: save first
$('back-btn').addEventListener('click', async (e) => {
  e.preventDefault();
  const href = e.currentTarget.href;
  if (mode === 'CARRIED') discardCarried();
  if (await flushSave()) location.href = href;
  else if (await IA.confirm({ title: 'Leave without saving?', message: 'The last changes on this frame could not be saved.', confirmLabel: 'Leave anyway', danger: true })) location.href = href;
});
window.addEventListener('beforeunload', (e) => {
  if (mode === 'DIRTY' || savePromise) { e.preventDefault(); e.returnValue = ''; }
});
document.addEventListener('visibilitychange', () => {
  // Tab hidden / closing: send pending edits with keepalive so they survive the unload
  if (document.visibilityState === 'hidden' && mode === 'DIRTY' && !savePromise && cur) {
    clearTimeout(saveTimer);
    const target = cur, v = editVersion;
    fetch(`/api/annotations/${target.id}`, {
      method: 'PUT', keepalive: true,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${IA.getToken()}` },
      body: JSON.stringify({ annotations: payload(), is_background: isBackground && !boxes.length, base_version: target.version ?? null }),
    }).then(async (r) => {
      if (!r.ok) return;
      const data = await r.json().catch(() => null);
      if (data) target.version = data.version;
      // Only "saved" if nothing was edited after this request was sent
      if (cur === target && editVersion === v) { serverSnap = canonical(); markClean(); }
      else if (cur === target && mode === 'DIRTY') save();
    }).catch(() => {});
  }
});

// ── Live updates ──────────────────────────────────────────────────────────────
function onState(ch) {
  const it = byId.get(ch.image_id);
  if (!it) {
    if (isAdmin || ch.assigned_to === user.id) refreshIndexSoon();
    return;
  }
  if (!isAdmin && ch.assigned_to && ch.assigned_to !== user.id) { refreshIndexSoon(); return; }
  const was = it.state;
  it.state = ch.new_state;
  if (it !== cur) return;
  // Submitted → annotated by someone else = a reviewer sent it back: show their note
  if (ch.new_state === 'annotated' && ['ready_for_export', 'exported'].includes(was) && mode === 'CLEAN' && !savePromise) reloadCurrent();
  else { updateSubmit(); updateBanners(); }
}
function dropImages(ids) {
  const gone = new Set(ids);
  const wasCur = cur && gone.has(cur.id);
  setIndex(allImages.filter((i) => !gone.has(i.id)));
  if (wasCur) { leaveCurrent('This image was deleted by an admin'); return; }
  applyFilter(); updateCounts();
}
function remapClasses(next, renames = {}) {
  classes = next;
  const fix = (b) => { if (renames[b.className]) b.className = renames[b.className]; b.classIdx = resolveClassIdx(b.className); };
  boxes.forEach(fix);
  history.forEach((h) => h.boxes.forEach(fix));
  future.forEach((h) => h.boxes.forEach(fix));
  prevFrame?.boxes.forEach(fix);
  carrySrc?.boxes.forEach(fix);
  const clip = IA.store.get('ia_clipboard');
  if (clip?.boxes) { clip.boxes.forEach(fix); IA.store.set('ia_clipboard', clip); }
  selClass = Math.min(selClass, classes.length - 1);
}
function startSocket() {
  const socket = IA.connectSocket(PID);
  socket?.on('image_state_change', (ch) => { onState(ch); renderStripRows(); updateCounts(); });
  socket?.on('image_state_change_bulk', ({ changes }) => { changes.forEach(onState); renderStripRows(); updateCounts(); });
  socket?.on('annotations_bulk_saved', ({ image_ids, saved_by }) => {
    if (saved_by !== user.username && cur && image_ids.includes(cur.id)) reloadCurrent();
    if (saved_by !== user.username) refreshIndexSoon();
  });
  socket?.on('annotation_saved', ({ image_id, saved_by }) => {
    // Someone else saved the frame that is open here
    if (saved_by === user.username || !cur || image_id !== cur.id) return;
    if (mode === 'CLEAN' && !savePromise) { reloadCurrent(); IA.toast(`${saved_by} just updated this frame`, 'info', 2500); }
    else { $('conflict-text').textContent = `${saved_by} changed this frame — saving yours would overwrite it`; showConflict(); }
  });
  socket?.on('image_deleted', ({ image_id }) => dropImages([image_id]));
  socket?.on('images_deleted', ({ image_ids }) => dropImages(image_ids));
  socket?.on('project_updated', ({ classes: next, renames }) => {
    remapClasses(next, renames || {});
    renderClasses(); renderBoxList(); render();
    if (mode === 'DIRTY') save();                                 // write the renamed classes right away
    IA.toast('The class list was updated by an admin', 'info', 2500);
  });
  IA.onReconnect(() => { refreshIndex(); if (mode === 'CLEAN') reloadCurrent(); });
}

// ── Init ──────────────────────────────────────────────────────────────────────
try {
  project = await IA.get(`/api/projects/${PID}`);
} catch (e) {
  IA.toast(e.status === 404 ? 'Project not found' : e.message, 'error');
  setTimeout(() => { location.href = '/home'; }, 1200);
  return;
}
classes = project.classes;
document.title = `${project.name} · Annotate`;
$('t-project').textContent = project.name;
$('back-btn').href = isAdmin ? `/projects/${PID}#images` : `/projects/${PID}/view`;
$('back-label').textContent = isAdmin ? 'Dashboard' : 'My images';

try {
  const data = await IA.get(`/api/projects/${PID}/images/index`);
  setIndex(data.items);
} catch (e) { IA.toast(e.message, 'error'); }

renderClasses();
renderUndoBatch();
syncAdjust();
setLockZoom(lockZoom);
$('qa-toggle').classList.toggle('active', qaOn);
new ResizeObserver(() => { resize(); renderStripRows(); }).observe(stage);
resize();
startSocket();

// Start on the image from the URL even if the filmstrip filter would hide it
let startIdx = allImages.findIndex((i) => i.id === window.ANNOTATE_IMAGE_ID);
if (startIdx >= 0 && stripFilter !== 'all' && !(FILTER_FN[stripFilter] || FILTER_FN.all)(allImages[startIdx])) stripFilter = 'all';
applyFilter();
updateCounts();
if (!view.length) {
  showNoFrames();
} else {
  startIdx = startIdx >= 0 ? view.indexOf(allImages[startIdx]) : 0;
  if (startIdx < 0) {
    // Jump to the first frame that still needs work
    startIdx = Math.max(0, view.findIndex((i) => ['assigned', 'unannotated'].includes(i.state)));
  }
  await loadFrame(startIdx);
}
})();
