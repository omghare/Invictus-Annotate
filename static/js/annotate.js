/**
 * annotate.js — Full annotation canvas engine.
 *
 * Keyboard shortcuts:
 *   b         — draw mode
 *   Enter     — confirm/save box
 *   Delete/BS — delete selected box
 *   Escape    — cancel draw / deselect
 *   d         — select last box
 *   r         — copy boxes from previous image
 *   f / →     — next image
 *   s / ←     — previous image
 *   Ctrl+Z    — undo
 *   Ctrl+S    — force save
 *   1-9       — select class
 */

(() => {
'use strict';

// ── State ───────────────────────────────────────────────────────────

let token      = IA.getToken();
let user       = IA.getUser();
let project    = null;
let classes    = [];
let images     = [];          // user's assigned images
let currentIdx = -1;          // index in images[]
let currentImg = null;        // { id, filename, width, height, state }
let boxes      = [];          // confirmed boxes [{id,classId,className,x,y,w,h}]
let history    = [];          // undo stack (array of box snapshots)
let previousImageBoxes = []; // for 'r' key

// Draw / interaction state
let drawMode   = false;
let isDrawing  = false;
let drawStart  = null;        // {ix,iy} image coords
let pendingBox = null;        // box being drawn, not yet confirmed

let selectedIdx   = -1;       // index in boxes[]
let interactMode  = null;     // 'move' | 'resize' | null
let interactStart = null;     // {sx,sy,box snapshot}
let resizeHandle  = null;     // 'nw'|'n'|'ne'|'e'|'se'|'s'|'sw'|'w'

// Canvas transform
let scale   = 1.0;
let offsetX = 0;
let offsetY = 0;

// Pan
let isPanning    = false;
let panStart     = null;
let spacePressed = false;

// Save
let isDirty    = false;
let saveTimer  = null;
let isSaving   = false;

// Selected class
let selectedClassIdx = 0;

// Canvas elements
const canvas = document.getElementById('ann-canvas');
const ctx    = canvas.getContext('2d');
const canvasArea = document.getElementById('canvas-area');

let imgEl = new Image();    // offscreen image element

const HANDLE_R    = 5;       // resize handle radius px
const MIN_BOX_PX  = 5;       // minimum box size in image coords
const MAX_UNDO    = 50;

// ── Colour helpers ──────────────────────────────────────────────────

function classColor(classIdx, alpha = 1) {
  const hue = (classIdx * 137.508) % 360;  // golden angle distribution
  return `hsla(${hue.toFixed(0)}, 85%, 62%, ${alpha})`;
}

function classColorSolid(classIdx) { return classColor(classIdx, 1); }
function classColorFill(classIdx)  { return classColor(classIdx, 0.18); }

// ── Coordinate transforms ────────────────────────────────────────────

function screenToImage(sx, sy) {
  return { ix: (sx - offsetX) / scale, iy: (sy - offsetY) / scale };
}
function imageToScreen(ix, iy) {
  return { sx: ix * scale + offsetX, sy: iy * scale + offsetY };
}
function clampToImage(ix, iy) {
  if (!currentImg) return { ix, iy };
  return {
    ix: Math.max(0, Math.min(currentImg.width,  ix)),
    iy: Math.max(0, Math.min(currentImg.height, iy)),
  };
}

// ── Resize handle positions (image coords) ───────────────────────────

function getHandles(box) {
  const { x, y, w, h } = box;
  return [
    { id:'nw', ix: x,       iy: y       },
    { id:'n',  ix: x + w/2, iy: y       },
    { id:'ne', ix: x + w,   iy: y       },
    { id:'e',  ix: x + w,   iy: y + h/2 },
    { id:'se', ix: x + w,   iy: y + h   },
    { id:'s',  ix: x + w/2, iy: y + h   },
    { id:'sw', ix: x,       iy: y + h   },
    { id:'w',  ix: x,       iy: y + h/2 },
  ];
}

function hitHandle(box, ix, iy) {
  const threshold = HANDLE_R / scale + 2;
  for (const h of getHandles(box)) {
    const dx = ix - h.ix, dy = iy - h.iy;
    if (dx*dx + dy*dy <= threshold*threshold) return h.id;
  }
  return null;
}

function hitBox(box, ix, iy) {
  return ix >= box.x && ix <= box.x + box.w &&
         iy >= box.y && iy <= box.y + box.h;
}

function getCursorStyle(ix, iy) {
  if (spacePressed || isPanning) return 'grab';
  if (drawMode && !pendingBox) return 'crosshair';

  if (selectedIdx >= 0 && selectedIdx < boxes.length) {
    const box = boxes[selectedIdx];
    const h = hitHandle(box, ix, iy);
    if (h) {
      const cur = { nw:'nw-resize', n:'ns-resize', ne:'ne-resize',
                    e:'ew-resize',  se:'se-resize', s:'ns-resize',
                    sw:'sw-resize', w:'ew-resize' };
      return cur[h] || 'pointer';
    }
    if (hitBox(box, ix, iy)) return 'move';
  }

  for (let i = boxes.length - 1; i >= 0; i--) {
    if (hitBox(boxes[i], ix, iy)) return 'pointer';
  }
  return drawMode ? 'crosshair' : 'default';
}

// ── Canvas resize ────────────────────────────────────────────────────

function resizeCanvas() {
  const rect = canvasArea.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const sizeChanged = (canvas.width !== rect.width || canvas.height !== rect.height);
  canvas.width  = rect.width;
  canvas.height = rect.height;
  if (sizeChanged && currentImg && imgEl.complete && imgEl.naturalWidth > 0) {
    fitImage();
  }
  render();
}

// ── Fit image to canvas ──────────────────────────────────────────────

function fitImage() {
  if (!currentImg) return;
  const cw = canvas.width, ch = canvas.height;
  const scaleX = cw / currentImg.width;
  const scaleY = ch / currentImg.height;
  scale = Math.min(scaleX, scaleY) * 0.95;
  offsetX = (cw - currentImg.width  * scale) / 2;
  offsetY = (ch - currentImg.height * scale) / 2;
}

// ── Render ───────────────────────────────────────────────────────────

function render() {
  const cw = canvas.width, ch = canvas.height;
  ctx.clearRect(0, 0, cw, ch);

  // Draw image
  if (imgEl.complete && imgEl.naturalWidth > 0 && currentImg) {
    const sx = offsetX, sy = offsetY;
    const sw = currentImg.width * scale, sh = currentImg.height * scale;

    // Slight darken outside image area (cosmetic)
    ctx.fillStyle = '#050508';
    ctx.fillRect(0, 0, cw, ch);

    ctx.drawImage(imgEl, sx, sy, sw, sh);

    // Image border
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    ctx.strokeRect(sx, sy, sw, sh);
  } else {
    ctx.fillStyle = '#050508';
    ctx.fillRect(0, 0, cw, ch);
  }

  // Draw confirmed boxes
  boxes.forEach((box, i) => renderBox(box, i === selectedIdx));

  // Draw pending (in-progress draw)
  if (pendingBox) renderBox(pendingBox, true, true);
}

function renderBox(box, selected, pending = false) {
  const { sx, sy } = imageToScreen(box.x, box.y);
  const sw = box.w * scale, sh = box.h * scale;
  const color = classColorSolid(box.classIdx !== undefined ? box.classIdx : box.class_id);

  // Fill
  ctx.fillStyle = classColorFill(box.classIdx !== undefined ? box.classIdx : box.class_id);
  ctx.fillRect(sx, sy, sw, sh);

  // Border
  ctx.strokeStyle = color;
  ctx.lineWidth   = selected ? 2 : 1.5;
  if (pending) {
    ctx.setLineDash([6, 3]);
  } else {
    ctx.setLineDash([]);
  }
  ctx.strokeRect(sx, sy, sw, sh);
  ctx.setLineDash([]);

  // Class label badge
  const cls = classes[box.classIdx !== undefined ? box.classIdx : box.class_id] || box.className || '?';
  const labelText = `${(box.classIdx !== undefined ? box.classIdx : box.class_id)}: ${cls}`;
  ctx.font = `${Math.max(10, Math.min(13, 12 * scale))}px Inter, sans-serif`;
  const tw  = ctx.measureText(labelText).width;
  const bh  = 18, bp = 5;
  const lx  = sx, ly = sy - bh;

  if (ly >= 0) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.roundRect(lx, ly, tw + bp*2, bh, 3);
    ctx.fill();
    ctx.fillStyle = '#000';
    ctx.fillText(labelText, lx + bp, ly + bh - 4);
  }

  // Resize handles (if selected and not pending)
  if (selected && !pending) {
    ctx.setLineDash([]);
    for (const h of getHandles(box)) {
      const { sx: hx, sy: hy } = imageToScreen(h.ix, h.iy);
      ctx.beginPath();
      ctx.arc(hx, hy, HANDLE_R, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff';
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth   = 2;
      ctx.stroke();
    }
  }
}

// ── Undo ─────────────────────────────────────────────────────────────

function pushHistory() {
  history.push(JSON.parse(JSON.stringify(boxes)));
  if (history.length > MAX_UNDO) history.shift();
}

function undo() {
  if (history.length === 0) { IA.toast('Nothing to undo', 'info', 1500); return; }
  boxes = history.pop();
  selectedIdx = -1;
  pendingBox  = null;
  drawMode    = false;
  setDrawModeUI(false);
  markDirty();
  updateBoxList();
  render();
}

// ── Dirty / save ─────────────────────────────────────────────────────

function markDirty() {
  isDirty = true;
  setSaveStatus('unsaved');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveAnnotations, 1200);
}

function setSaveStatus(status) {
  const el = document.getElementById('save-status');
  if (!el) return;
  el.className = `save-status ${status}`;
  const labels = { saved: '● Saved', saving: '⟳ Saving…', unsaved: '● Unsaved' };
  el.textContent = labels[status] || status;
}

async function saveAnnotations() {
  if (!currentImg || !isDirty) return;

  const savingImgId = currentImg.id;
  const payload = boxes.map(b => ({
    class_id:   b.classIdx !== undefined ? b.classIdx : b.class_id,
    class_name: classes[b.classIdx !== undefined ? b.classIdx : b.class_id] || b.className,
    x: Math.round(b.x),
    y: Math.round(b.y),
    w: Math.round(b.w),
    h: Math.round(b.h),
  }));

  clearTimeout(saveTimer);
  isSaving = true;
  setSaveStatus('saving');

  try {
    const res = await IA.api(`/api/annotations/${savingImgId}`, {
      method: 'PUT',
      body: JSON.stringify({ annotations: payload }),
    });
    if (res && res.ok) {
      const data = await res.json();
      
      const targetImg = images.find(img => img.id === savingImgId);
      if (targetImg) {
        targetImg.state = data.state;
      }
      
      updateImageStateInList(savingImgId, data.state);

      if (currentImg && currentImg.id === savingImgId) {
        updateSubmitBtn();
        isDirty  = false;
        setSaveStatus('saved');
      }
      isSaving = false;
    } else {
      isSaving = false;
      if (currentImg && currentImg.id === savingImgId) {
        setSaveStatus('unsaved');
      }
      IA.toast('Save failed', 'error');
    }
  } catch(e) {
    isSaving = false;
    if (currentImg && currentImg.id === savingImgId) {
      setSaveStatus('unsaved');
    }
    IA.toast('Network error', 'error');
  }
}

// Immediate save (Enter key)
async function confirmSave() {
  clearTimeout(saveTimer);
  isDirty = true;
  await saveAnnotations();
}

// ── Navigate images ───────────────────────────────────────────────────

async function navigateImage(delta) {
  if (images.length === 0) return;
  // Auto-save if dirty before navigating
  if (isDirty) { await saveAnnotations(); }
  // Store previous boxes for 'r' key
  previousImageBoxes = JSON.parse(JSON.stringify(boxes));

  let nextIdx = currentIdx + delta;
  if (nextIdx < 0) nextIdx = images.length - 1;
  if (nextIdx >= images.length) nextIdx = 0;
  await loadImage(nextIdx);
}

// ── Load image ────────────────────────────────────────────────────────

async function loadImage(idx) {
  if (idx < 0 || idx >= images.length) return;
  currentIdx = idx;
  currentImg = images[idx];

  // Highlight in list
  document.querySelectorAll('.img-list-item').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
    if (i === idx) el.scrollIntoView({ block: 'nearest' });
  });

  // Update topbar
  const fnEl = document.getElementById('topbar-filename');
  if (fnEl) fnEl.textContent = currentImg.filename;

  // Reset canvas state
  selectedIdx  = -1;
  pendingBox   = null;
  isDrawing    = false;
  drawMode     = false;
  setDrawModeUI(false);
  isDirty      = false;
  clearTimeout(saveTimer);
  history      = [];
  boxes        = [];

  setSaveStatus('saved');
  updateBoxList();
  updateSubmitBtn();
  updateSubmitAllBtn();
  render();

  // Load image file
  imgEl = new Image();
  imgEl.onload = () => { fitImage(); render(); };
  imgEl.src = IA.imgUrl(currentImg.id);

  // Fetch annotations
  const loadingImgId = currentImg.id;
  try {
    const res = await IA.api(`/api/annotations/${loadingImgId}`);
    if (res && res.ok) {
      const data = await res.json();
      
      const targetImg = images.find(img => img.id === loadingImgId);
      if (targetImg) {
        targetImg.width  = data.width;
        targetImg.height = data.height;
        updateImageStateInList(loadingImgId, data.state);
      }

      if (currentImg && currentImg.id === loadingImgId) {
        boxes = data.annotations.map(a => ({
          id:        a.id,
          classIdx:  a.class_id,
          className: a.class_name,
          x: a.x, y: a.y, w: a.w, h: a.h,
        }));
        updateBoxList();
        updateSubmitBtn();
        render();
      }
    }
  } catch(e) {
    IA.toast('Failed to load annotations', 'error');
  }
}

// ── Image list helpers ────────────────────────────────────────────────

function updateImageStateInList(imageId, newState) {
  const img = images.find(i => i.id === imageId);
  if (img) img.state = newState;
  // Update dot in list
  const dot = document.querySelector(`.img-list-item[data-id="${imageId}"] .il-state-dot`);
  if (dot) dot.style.background = IA.stateColor(newState);
  updateSubmitAllBtn();
}

function updateSubmitBtn() {
  const btn = document.getElementById('submit-btn');
  if (!btn || !currentImg) return;
  const state = currentImg.state;
  if (state === 'annotated') {
    btn.disabled = false;
    btn.textContent = '✓ Add to Dataset';
    btn.style.background = '';
    btn.style.color = '';
  } else if (state === 'ready_for_export') {
    btn.disabled = false;
    btn.textContent = '✓ Added to Dataset';
    btn.style.background = 'linear-gradient(135deg, #9d92ff, #7c6dff)';
    btn.style.color = '#fff';
  } else {
    btn.disabled = true;
    btn.textContent = '✓ Add to Dataset';
    btn.style.background = '';
    btn.style.color = '';
  }
}

function updateSubmitAllBtn() {
  const btn = document.getElementById('submit-all-btn');
  if (!btn) return;
  const count = images.filter(i => i.state === 'annotated').length;
  if (count > 0) {
    btn.disabled = false;
    btn.textContent = `✓ Add All Annotated (${count})`;
  } else {
    btn.disabled = true;
    btn.textContent = '✓ Add All Annotated';
  }

  // Update header progress counter
  const countEl = document.getElementById('img-count');
  if (countEl) {
    const annotated = images.filter(i => i.state === 'annotated').length;
    const ready = images.filter(i => i.state === 'ready_for_export' || i.state === 'exported').length;
    countEl.textContent = `${annotated} ann / ${ready} ready / ${images.length} total`;
  }
}

// ── Box list (right panel) ───────────────────────────────────────────

function updateBoxList() {
  const list = document.getElementById('box-list');
  if (!list) return;
  if (boxes.length === 0) {
    list.innerHTML = '<div class="box-empty">No boxes yet.<br>Press <b>B</b> to draw.</div>';
    return;
  }
  list.innerHTML = boxes.map((b, i) => {
    const cidx  = b.classIdx !== undefined ? b.classIdx : b.class_id;
    const cname = classes[cidx] || b.className || '?';
    const color = classColorSolid(cidx);
    return `<div class="box-item${i === selectedIdx ? ' active' : ''}"
                 data-idx="${i}"
                 style="--cls-color: ${color}"
                 onclick="IA._selectBox(${i})">
      <span class="box-dot"></span>
      <span class="box-label">${cidx}: ${cname}</span>
      <span class="box-del" onclick="event.stopPropagation(); IA._deleteBox(${i})">×</span>
    </div>`;
  }).join('');
}

// Expose for onclick handlers
IA._selectBox = (idx) => {
  selectedIdx = idx;
  drawMode    = false;
  setDrawModeUI(false);
  updateBoxList();
  render();
};
IA._deleteBox = (idx) => {
  pushHistory();
  boxes.splice(idx, 1);
  if (selectedIdx >= boxes.length) selectedIdx = boxes.length - 1;
  markDirty();
  updateBoxList();
  render();
};

// ── Class UI ─────────────────────────────────────────────────────────

function buildClassUI() {
  const list = document.getElementById('class-list');
  if (!list) return;
  list.innerHTML = classes.map((cls, i) => {
    const color = classColorSolid(i);
    return `<button class="class-btn${i === selectedClassIdx ? ' selected' : ''}"
                    data-idx="${i}"
                    style="--cls-color: ${color}"
                    onclick="IA._selectClass(${i})">
      <span class="cls-dot"></span>
      <span style="flex:1">${cls}</span>
      ${i < 9 ? `<span class="cls-kb">${i+1}</span>` : ''}
    </button>`;
  }).join('');
}

IA._selectClass = (idx) => {
  selectedClassIdx = idx;
  buildClassUI();
  // If a box is selected, change its class
  if (selectedIdx >= 0 && selectedIdx < boxes.length) {
    pushHistory();
    boxes[selectedIdx].classIdx  = idx;
    boxes[selectedIdx].className = classes[idx];
    markDirty();
    updateBoxList();
    render();
  }
};

// ── Draw mode toggle ──────────────────────────────────────────────────

function setDrawModeUI(active) {
  const btn = document.getElementById('shortcut-draw');
  if (btn) btn.classList.toggle('active', active);
  canvas.style.cursor = active ? 'crosshair' : 'default';
}

function toggleDrawMode() {
  drawMode = !drawMode;
  if (!drawMode) { pendingBox = null; isDrawing = false; }
  selectedIdx = -1;
  setDrawModeUI(drawMode);
  updateBoxList();
  render();
}

// ── Mouse events ──────────────────────────────────────────────────────

canvas.addEventListener('mousedown', onMouseDown);
canvas.addEventListener('mousemove', onMouseMove);
canvas.addEventListener('mouseup',   onMouseUp);
canvas.addEventListener('mouseleave',() => { isPanning = false; render(); });
canvas.addEventListener('wheel',     onWheel, { passive: false });
canvas.addEventListener('contextmenu', e => e.preventDefault());

function getCanvasPos(e) {
  const rect = canvas.getBoundingClientRect();
  return { sx: e.clientX - rect.left, sy: e.clientY - rect.top };
}

function onMouseDown(e) {
  if (!currentImg) return;
  e.preventDefault();
  const { sx, sy } = getCanvasPos(e);
  const { ix, iy } = screenToImage(sx, sy);

  // Middle mouse or Space+left → pan
  if (e.button === 1 || (e.button === 0 && spacePressed)) {
    isPanning = true;
    panStart  = { sx, sy, ox: offsetX, oy: offsetY };
    return;
  }

  if (e.button !== 0) return;

  // Check if clicking on a resize handle of selected box
  if (selectedIdx >= 0 && selectedIdx < boxes.length) {
    const h = hitHandle(boxes[selectedIdx], ix, iy);
    if (h) {
      interactMode  = 'resize';
      resizeHandle  = h;
      interactStart = { sx, sy, ix, iy, box: JSON.parse(JSON.stringify(boxes[selectedIdx])) };
      pushHistory();
      return;
    }
    // Check if clicking inside selected box → move
    if (hitBox(boxes[selectedIdx], ix, iy)) {
      interactMode  = 'move';
      interactStart = { sx, sy, ix, iy, box: JSON.parse(JSON.stringify(boxes[selectedIdx])) };
      pushHistory();
      return;
    }
  }

  // Draw mode
  if (drawMode) {
    const clamped = clampToImage(ix, iy);
    isDrawing = true;
    drawStart = clamped;
    pendingBox = { classIdx: selectedClassIdx, x: clamped.ix, y: clamped.iy, w: 0, h: 0 };
    render();
    return;
  }

  // Click on any other box → select it
  let found = false;
  for (let i = boxes.length - 1; i >= 0; i--) {
    if (hitBox(boxes[i], ix, iy)) {
      selectedIdx = i;
      found = true;
      break;
    }
  }
  if (!found) selectedIdx = -1;
  updateBoxList();
  render();
}

function onMouseMove(e) {
  if (!currentImg) return;
  const { sx, sy } = getCanvasPos(e);
  const { ix, iy } = screenToImage(sx, sy);

  // Pan
  if (isPanning && panStart) {
    offsetX = panStart.ox + (sx - panStart.sx);
    offsetY = panStart.oy + (sy - panStart.sy);
    render();
    return;
  }

  // Resize
  if (interactMode === 'resize' && interactStart) {
    const orig = interactStart.box;
    const di   = ix - interactStart.ix;
    const dj   = iy - interactStart.iy;
    let { x, y, w, h } = orig;

    switch (resizeHandle) {
      case 'nw': x += di; y += dj; w -= di; h -= dj; break;
      case 'n':              y += dj;         h -= dj; break;
      case 'ne':             y += dj; w += di; h -= dj; break;
      case 'e':                       w += di;          break;
      case 'se':                      w += di; h += dj; break;
      case 's':                                h += dj; break;
      case 'sw': x += di; w -= di; h += dj; break;
      case 'w':  x += di; w -= di;           break;
    }

    // Clamp
    x = Math.max(0, x); y = Math.max(0, y);
    w = Math.max(MIN_BOX_PX, Math.min(currentImg.width  - x, w));
    h = Math.max(MIN_BOX_PX, Math.min(currentImg.height - y, h));

    // Handle negative width/height (drag past opposite edge)
    if (w < 0) { x += w; w = Math.abs(w); }
    if (h < 0) { y += h; h = Math.abs(h); }

    boxes[selectedIdx] = { ...boxes[selectedIdx], x, y, w, h };
    markDirty();
    updateBoxList();
    render();
    return;
  }

  // Move
  if (interactMode === 'move' && interactStart) {
    const di = ix - interactStart.ix;
    const dj = iy - interactStart.iy;
    const orig = interactStart.box;
    let nx = orig.x + di, ny = orig.y + dj;
    nx = Math.max(0, Math.min(currentImg.width  - orig.w, nx));
    ny = Math.max(0, Math.min(currentImg.height - orig.h, ny));
    boxes[selectedIdx] = { ...boxes[selectedIdx], x: nx, y: ny };
    markDirty();
    updateBoxList();
    render();
    return;
  }

  // Drawing
  if (isDrawing && drawStart) {
    const clamped = clampToImage(ix, iy);
    const bx = Math.min(drawStart.ix, clamped.ix);
    const by = Math.min(drawStart.iy, clamped.iy);
    const bw = Math.abs(clamped.ix - drawStart.ix);
    const bh = Math.abs(clamped.iy - drawStart.iy);
    pendingBox = { classIdx: selectedClassIdx, x: bx, y: by, w: bw, h: bh };
    render();
    return;
  }

  // Update cursor
  canvas.style.cursor = getCursorStyle(ix, iy);
}

function onMouseUp(e) {
  if (!currentImg) return;

  if (isPanning) { isPanning = false; return; }

  if (interactMode) {
    interactMode  = null;
    interactStart = null;
    resizeHandle  = null;
    return;
  }

  if (isDrawing && pendingBox) {
    isDrawing = false;
    if (pendingBox.w >= MIN_BOX_PX && pendingBox.h >= MIN_BOX_PX) {
      // Box is large enough — add as selected (highlighted) pending confirmation
      const newBox = {
        classIdx:  selectedClassIdx,
        className: classes[selectedClassIdx],
        x: pendingBox.x, y: pendingBox.y,
        w: pendingBox.w, h: pendingBox.h,
      };
      boxes.push(newBox);
      selectedIdx = boxes.length - 1;
      // Auto-save immediately when a box is drawn
      pushHistory();
      markDirty();
      updateBoxList();
    }
    pendingBox = null;
    render();
  }
}

// ── Scroll to zoom ────────────────────────────────────────────────────

function onWheel(e) {
  e.preventDefault();
  const { sx, sy } = getCanvasPos(e);
  const factor = e.deltaY < 0 ? 1.1 : (1/1.1);
  const newScale = Math.max(0.05, Math.min(20, scale * factor));
  // Zoom toward mouse position
  offsetX = sx - (sx - offsetX) * (newScale / scale);
  offsetY = sy - (sy - offsetY) * (newScale / scale);
  scale   = newScale;
  document.getElementById('zoom-level').textContent = `${Math.round(scale * 100)}%`;
  render();
}

// ── Keyboard ──────────────────────────────────────────────────────────

document.addEventListener('keydown', onKeyDown);
document.addEventListener('keyup',   onKeyUp);

function onKeyDown(e) {
  // Ignore when typing in an input
  if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) return;

  if (e.ctrlKey || e.metaKey) {
    if (e.key === 'z' || e.key === 'Z') { e.preventDefault(); undo(); return; }
    if (e.key === 's' || e.key === 'S') { e.preventDefault(); confirmSave(); return; }
  }

  if (e.code === 'Space') { spacePressed = true; canvas.style.cursor = 'grab'; return; }

  switch (e.key) {
    case 'b': case 'B':
      toggleDrawMode(); break;

    case 'Enter':
      // Confirm: if box is selected (just drawn or re-selected), save
      if (selectedIdx >= 0) {
        confirmSave();
        selectedIdx = -1;
        updateBoxList();
        render();
      } else if (pendingBox) {
        // Finish pending draw
        pendingBox = null; isDrawing = false; render();
      }
      break;

    case 'Delete': case 'Backspace':
      if (selectedIdx >= 0 && selectedIdx < boxes.length) {
        e.preventDefault();
        pushHistory();
        boxes.splice(selectedIdx, 1);
        selectedIdx = Math.min(selectedIdx, boxes.length - 1);
        markDirty();
        updateBoxList();
        render();
      }
      break;

    case 'Escape':
      drawMode    = false;
      isDrawing   = false;
      pendingBox  = null;
      selectedIdx = -1;
      setDrawModeUI(false);
      updateBoxList();
      render();
      break;

    case 'd': case 'D':
      if (boxes.length > 0) {
        selectedIdx = boxes.length - 1;
        updateBoxList();
        render();
      }
      break;

    case 'r': case 'R':
      if (previousImageBoxes.length === 0) {
        IA.toast('No boxes to copy from previous image', 'info', 2000);
      } else {
        pushHistory();
        const copies = previousImageBoxes.map(b => ({
          classIdx:  b.classIdx !== undefined ? b.classIdx : b.class_id,
          className: b.className || classes[b.class_id] || '?',
          x: b.x, y: b.y, w: b.w, h: b.h,
        }));
        // Clamp to current image dimensions (may differ from previous)
        copies.forEach(b => {
          if (currentImg) {
            b.x = Math.min(b.x, currentImg.width  - b.w);
            b.y = Math.min(b.y, currentImg.height - b.h);
            b.w = Math.min(b.w, currentImg.width);
            b.h = Math.min(b.h, currentImg.height);
          }
        });
        boxes = copies;
        selectedIdx = boxes.length - 1;
        drawMode    = false;
        setDrawModeUI(false);
        markDirty();
        updateBoxList();
        render();
        IA.toast(`Copied ${copies.length} box(es) from previous image`, 'info', 2000);
      }
      break;

    case 'f': case 'F': case 'ArrowRight':
      e.preventDefault();
      navigateImage(1);
      break;

    case 's': case 'S':
      // 's' for previous (not Ctrl+S which is handled above)
      if (!e.ctrlKey && !e.metaKey) { e.preventDefault(); navigateImage(-1); }
      break;

    case 'ArrowLeft':
      e.preventDefault();
      navigateImage(-1);
      break;

    default:
      // Number keys 1-9 for class selection
      if (e.key >= '1' && e.key <= '9') {
        const idx = parseInt(e.key) - 1;
        if (idx < classes.length) IA._selectClass(idx);
      }
  }
}

function onKeyUp(e) {
  if (e.code === 'Space') { spacePressed = false; render(); }
}

// ── Zoom control buttons ─────────────────────────────────────────────

document.getElementById('zoom-in') ?.addEventListener('click', () => {
  scale = Math.min(20, scale * 1.25);
  document.getElementById('zoom-level').textContent = `${Math.round(scale * 100)}%`;
  render();
});
document.getElementById('zoom-out')?.addEventListener('click', () => {
  scale = Math.max(0.05, scale / 1.25);
  document.getElementById('zoom-level').textContent = `${Math.round(scale * 100)}%`;
  render();
});
document.getElementById('zoom-fit') ?.addEventListener('click', () => {
  fitImage();
  document.getElementById('zoom-level').textContent = `${Math.round(scale * 100)}%`;
  render();
});

// ── Shortcut panel buttons ────────────────────────────────────────────

document.getElementById('shortcut-draw') ?.addEventListener('click', toggleDrawMode);
document.getElementById('shortcut-last') ?.addEventListener('click', () => onKeyDown({ key: 'd' }));
document.getElementById('shortcut-copy') ?.addEventListener('click', () => onKeyDown({ key: 'r' }));
document.getElementById('shortcut-del')  ?.addEventListener('click', () => onKeyDown({ key: 'Delete' }));
document.getElementById('shortcut-undo') ?.addEventListener('click', undo);
document.getElementById('shortcut-prev') ?.addEventListener('click', () => navigateImage(-1));
document.getElementById('shortcut-next') ?.addEventListener('click', () => navigateImage(1));

// ── Submit button ─────────────────────────────────────────────────────

document.getElementById('submit-btn')?.addEventListener('click', async () => {
  if (!currentImg) return;
  const projectId = window.ANNOTATE_PROJECT_ID;
  // Save first
  if (isDirty) await saveAnnotations();
  try {
    const res = await IA.api(`/api/projects/${projectId}/images/${currentImg.id}/submit`, { method: 'POST' });
    if (res && res.ok) {
      currentImg.state = 'ready_for_export';
      updateSubmitBtn();
      updateImageStateInList(currentImg.id, 'ready_for_export');
      IA.toast('Image added to dataset queue ✓', 'success');
    } else {
      const err = await res?.json();
      IA.toast(err?.detail || 'Submit failed', 'error');
    }
  } catch { IA.toast('Network error', 'error'); }
});

// ── Submit all button ──────────────────────────────────────────────────

document.getElementById('submit-all-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('submit-all-btn');
  if (!btn || btn.disabled) return;
  const projectId = window.ANNOTATE_PROJECT_ID;

  // Auto-save if dirty before bulk submitting
  if (isDirty) {
    await saveAnnotations();
  }

  btn.disabled = true;
  btn.textContent = '⟳ Submitting…';

  try {
    const res = await IA.api(`/api/projects/${projectId}/images/submit-all-annotated`, { method: 'POST' });
    if (res && res.ok) {
      const data = await res.json();
      const count = data.count || 0;

      // Update all local images that were 'annotated' to 'ready_for_export'
      images.forEach(img => {
        if (img.state === 'annotated') {
          img.state = 'ready_for_export';
          const dot = document.querySelector(`.img-list-item[data-id="${img.id}"] .il-state-dot`);
          if (dot) dot.style.background = IA.stateColor('ready_for_export');
        }
      });

      if (currentImg && currentImg.state === 'annotated') {
        currentImg.state = 'ready_for_export';
      }

      updateSubmitBtn();
      updateSubmitAllBtn();

      IA.toast(`✓ Added ${count} images to dataset`, 'success');
    } else {
      const err = await res?.json();
      IA.toast(IA.formatError(err) || 'Bulk submit failed', 'error');
      updateSubmitAllBtn();
    }
  } catch(e) {
    IA.toast('Network error', 'error');
    updateSubmitAllBtn();
  }
});

// ── Image list build ──────────────────────────────────────────────────

function buildImageList() {
  const list = document.getElementById('img-list');
  if (!list) return;
  if (images.length === 0) {
    list.innerHTML = '<div class="box-empty" style="padding:20px">No images assigned</div>';
    return;
  }
  list.innerHTML = images.map((img, i) => {
    const color = IA.stateColor(img.state);
    return `<div class="img-list-item${i === currentIdx ? ' active' : ''}"
                 data-id="${img.id}" data-idx="${i}"
                 onclick="IA._loadImageIdx(${i})">
      <span class="il-state-dot" style="background:${color}"></span>
      <span class="il-name" title="${img.filename}">${img.filename}</span>
    </div>`;
  }).join('');
  updateSubmitAllBtn();
}

IA._loadImageIdx = async (idx) => {
  if (idx === currentIdx) return;
  if (isDirty) await saveAnnotations();
  previousImageBoxes = JSON.parse(JSON.stringify(boxes));
  await loadImage(idx);
};

// ── Socket events ─────────────────────────────────────────────────────

function initSocket() {
  const projectId = window.ANNOTATE_PROJECT_ID;
  const socket = IA.connectSocket(projectId, () => {});

  socket?.on('image_deleted', ({ image_id }) => {
    if (currentImg?.id === image_id) {
      IA.toast('This image was deleted by admin', 'error', 5000);
      // Navigate to first available image
      images = images.filter(i => i.id !== image_id);
      buildImageList();
      if (images.length > 0) loadImage(0);
      else { currentImg = null; currentIdx = -1; render(); }
    } else {
      images = images.filter(i => i.id !== image_id);
      buildImageList();
    }
  });

  socket?.on('image_state_change', ({ image_id, new_state }) => {
    const img = images.find(i => i.id === image_id);
    if (img) img.state = new_state;
    if (currentImg && currentImg.id === image_id) {
      currentImg.state = new_state;
      updateSubmitBtn();
    }
    updateImageStateInList(image_id, new_state);
  });

  socket?.on('project_updated', ({ classes: newClasses }) => {
    classes = newClasses;
    buildClassUI();
    render();
    IA.toast('Class list updated by admin', 'info', 2000);
  });
}

// ── Initialise ────────────────────────────────────────────────────────

async function init() {
  user = IA.requireAuth();
  if (!user) return;

  const projectId = window.ANNOTATE_PROJECT_ID;
  if (!projectId) {
    IA.toast('Missing project context', 'error');
    setTimeout(() => { window.location.href = '/home'; }, 1500);
    return;
  }

  // Set back button href
  const backBtn = document.getElementById('back-btn');
  if (backBtn) {
    if (user.is_admin) {
      backBtn.href = `/projects/${projectId}`;
    } else {
      backBtn.href = `/projects/${projectId}/view`;
    }
  }

  // Fetch project
  const pRes = await IA.api(`/api/projects/${projectId}`);
  if (!pRes || !pRes.ok) { IA.toast('Project not found', 'error'); setTimeout(() => { window.location.href = '/home'; }, 1500); return; }
  project = await pRes.json();
  classes = project.classes;

  // Fetch assigned images for this project (annotator sees their own; admin sees all)
  const iRes = await IA.api(`/api/projects/${projectId}/images?per_page=200`);
  if (!iRes || !iRes.ok) { IA.toast('Failed to load images', 'error'); return; }
  const iData = await iRes.json();
  images = iData.items;

  buildClassUI();
  buildImageList();
  initSocket();

  // Determine starting image
  const startIdFromURL = window.ANNOTATE_IMAGE_ID;
  let startIdx = 0;
  if (startIdFromURL) {
    const found = images.findIndex(i => i.id === startIdFromURL);
    if (found >= 0) startIdx = found;
  }

  // Canvas resize observer — initialize bounds BEFORE loading first image
  const ro = new ResizeObserver(resizeCanvas);
  ro.observe(canvasArea);
  resizeCanvas();

  if (images.length > 0) {
    await loadImage(startIdx);
  } else {
    document.getElementById('topbar-filename').textContent = 'No images assigned';
  }
}

init();

})(); // end IIFE
