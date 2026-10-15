/**
 * dashboard.js — admin project dashboard (/projects/{id}#section).
 * Sections: overview, images, video, import, assign, review, assist, classes, export (+ cloud training).
 * Long work (model assist, cloud bundles) runs as server jobs; progress arrives as `job_update`.
 */
(async () => {
'use strict';

const PID = window.PROJECT_ID;
const user = await IA.shell({ adminOnly: true, projectId: PID });
if (!user) return;

let project = null;
let stats = {};
let users = [];
let workloads = {};
const names = new Map();          // image id → filename (for the activity feed)
const $ = (id) => document.getElementById(id);

// ── Project + header ──────────────────────────────────────────────────────────
try {
  project = await IA.get(`/api/projects/${PID}`);
} catch (e) {
  IA.toast(e.status === 404 ? 'This project no longer exists' : e.message, 'error');
  setTimeout(() => { location.href = '/home'; }, 1200);
  return;
}

function renderHeader() {
  document.title = `${project.name} · Invictus Annotate`;
  $('project-name').textContent = project.name;
  $('rail-project-name').textContent = project.name;
  $('project-meta').textContent = [
    IA.plural(project.classes.length, 'class', 'classes'),
    project.source_folder ? `Last import: ${project.source_folder}` : 'No images imported yet',
  ].join(' · ');
  $('hero-thumb').innerHTML = project.thumbnail_image_id
    ? `<img src="${IA.thumbUrl(project.thumbnail_image_id, 160)}" alt="" onerror="this.remove()"/>` : '';
}
renderHeader();

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats() {
  try { applyStats(await IA.get(`/api/projects/${PID}/stats`)); } catch { /* shown elsewhere */ }
}

function applyStats(s) {
  stats = IA.derive(s);
  const set = (k, v) => IA.countTo(document.querySelector(`#ov-stats [data-s="${k}"]`), v);
  ['total', 'unassigned', 'pending', 'done', 'ready_for_export', 'exported'].forEach((k) => set(k, stats[k]));
  document.querySelector('[data-s="pending-sub"]').textContent = stats.unannotated ? `${IA.fmtNum(stats.unannotated)} opened` : '';
  document.querySelector('[data-s="done-sub"]').textContent = stats.annotated ? `${IA.fmtNum(stats.annotated)} not yet added to dataset` : 'all added to dataset';
  $('ov-pct').textContent = `${stats.pct}%`;
  $('ov-progress-meta').textContent = `${IA.fmtNum(stats.done)} of ${IA.fmtNum(stats.total)} annotated`;
  if (!$('ov-bar').firstChild) $('ov-bar').innerHTML = IA.stateBar(stats, 'lg');
  else IA.updateStateBar($('ov-bar').firstChild, stats);
  $('ov-legend').innerHTML = Object.keys(IA.STATES).map((k) =>
    `<span><i style="background:${IA.stateColor(k)}"></i>${IA.stateLabel(k)} ${IA.fmtNum(stats[k] || 0)}</span>`).join('');
  IA.setNavCount('total', stats.total);
  IA.setNavCount('unassigned', stats.unassigned, true);
  IA.setNavCount('ready', stats.awaiting_review, true);
  const rvCounts = { ...stats, inds: (stats.ready_for_export || 0) + (stats.exported || 0) };
  $('rv-filter').querySelectorAll('button').forEach((b) => { b.querySelector('.count').textContent = IA.fmtNum(rvCounts[b.dataset.k] || 0); });
  $('as-unassigned').textContent = `${IA.fmtNum(stats.unassigned)} unassigned`;
  const sa = $('rv-submit-all');
  sa.disabled = !stats.annotated;
  sa.querySelector('span').textContent = stats.annotated
    ? `Add All Annotated Images to Dataset (${IA.fmtNum(stats.annotated)})` : 'Add All Annotated Images to Dataset';
  renderFilterCounts();
  renderPlan();
}

// ── People ────────────────────────────────────────────────────────────────────
async function loadPeople() {
  try {
    [users, workloads] = await Promise.all([IA.get('/api/users'), IA.get(`/api/projects/${PID}/user-workloads`)]);
  } catch (e) { IA.toast(e.message, 'error'); return; }
  const rows = users.map((u) => ({ u, w: workloads[u.id] })).filter((r) => r.w && r.w.total > 0)
    .sort((a, b) => b.w.total - a.w.total);
  $('ov-people').innerHTML = rows.length ? rows.map(({ u, w }) => {
    const done = w.annotated + w.ready + w.exported;
    const bar = IA.stateBar({ assigned: w.assigned, annotated: w.annotated, ready_for_export: w.ready, exported: w.exported });
    const reviewed = w.approved + w.rejected;
    const rv = reviewed ? `<div class="small muted" title="Frames reviewed: approved / sent back">${IA.fmtNum(w.approved)} approved · ${IA.fmtNum(w.rejected)} sent back (${Math.round((w.rejected / reviewed) * 100)}%)</div>` : '';
    return `<div class="person">${IA.avatar(u.username)}<div style="min-width:0"><div class="nm">${IA.esc(u.username)}</div>${bar}${rv}</div>
      <div class="n">${IA.fmtNum(done)} / ${IA.fmtNum(w.total)}</div></div>`;
  }).join('') : `<p class="small muted">Nobody has images yet. <a class="link" href="#assign">Assign some</a>.</p>`;
  const sel = $('img-assignee');
  const cur = sel.value;
  sel.innerHTML = '<option value="">Anyone</option>' + users.map((u) => `<option value="${u.id}">${IA.esc(u.username)}</option>`).join('');
  sel.value = cur;
  renderAssignUsers();
}

// ── Missing files warning (e.g. the image folder was moved or deleted) ────────
async function checkFiles() {
  const box = $('ov-files');
  try {
    const r = await IA.get(`/api/projects/${PID}/files/check`);
    box.classList.toggle('hidden', !r.missing);
    if (!r.missing) return;
    box.innerHTML = `<div class="callout warn">${IA.icon('image-off')}<div style="flex:1">
      <b>${IA.fmtNum(r.missing)} of ${IA.fmtNum(r.total)} image files can’t be found on the server.</b>
      Annotators will see an empty frame for these. Last known location: <code>${IA.esc(r.missing_folders[0] || '')}</code>${r.missing_folders.length > 1 ? ` and ${r.missing_folders.length - 1} more` : ''}.
      <div class="row" style="margin-top:8px"><a class="btn btn-sm" href="#import">${IA.icon('refresh-cw')}Relink to the new location</a></div></div></div>`;
  } catch { box.classList.add('hidden'); }
}

// ── Activity feed ─────────────────────────────────────────────────────────────
function feed(actor, html) {
  const list = $('feed');
  list.querySelector('.empty')?.remove();
  const item = IA.h(`<div class="feed-item">${actor ? IA.avatar(actor, 'sm') : `<span class="avatar sm">${IA.icon('activity', 'i-sm')}</span>`}
    <div>${html}</div><time>${IA.esc(IA.fmtTime(new Date().toISOString()))}</time></div>`);
  list.prepend(item);
  while (list.children.length > 40) list.lastElementChild.remove();
}
const fileLabel = (id) => (names.has(id) ? `<code>${IA.esc(names.get(id))}</code>` : 'an image');

// ── Generic paginated, selectable thumbnail grid ──────────────────────────────
function makeGrid({ el, pager, params, empty, onChange }) {
  const g = { page: 1, perPage: 50, items: [], total: 0, pages: 1, selected: new Set(), anchor: null, seq: 0 };
  g.load = async (resetPage = false) => {
    if (resetPage) g.page = 1;
    const seq = ++g.seq;
    if (!g.items.length) el.innerHTML = Array.from({ length: 8 }, () => '<div class="thumb"><div class="media skel"></div><div class="meta"><div class="skel" style="height:10px;width:70%"></div></div></div>').join('');
    const q = new URLSearchParams({ page: g.page, per_page: g.perPage, ...params() });
    let data;
    try { data = await IA.get(`/api/projects/${PID}/images?${q}`); }
    catch (e) { el.innerHTML = `<div class="empty"><h3>Couldn't load images</h3><p>${IA.esc(e.message)}</p></div>`; return; }
    if (seq !== g.seq) return;                       // a newer request superseded this one
    Object.assign(g, { items: data.items, total: data.total, pages: data.total_pages, page: data.page });
    data.items.forEach((i) => names.set(i.id, i.filename));
    g.render();
    onChange?.(g);
  };
  g.render = () => {
    if (!g.items.length) { el.innerHTML = empty(); pager.innerHTML = ''; return; }
    el.innerHTML = g.items.map((img, i) => `
      <a class="thumb rise ${g.selected.has(img.id) ? 'selected' : ''}" style="--i:${Math.min(i, 12)}" data-id="${img.id}" data-i="${i}"
         href="/projects/${PID}/annotate/${img.id}">
        <div class="media"><img src="${IA.thumbUrl(img.id)}" alt="" loading="lazy" onerror="this.remove()"/><div class="ph">${IA.icon('image')}</div></div>
        <div class="overlay-top"><span class="sel" role="checkbox" aria-checked="${g.selected.has(img.id)}">${IA.icon('check')}</span><span></span>${img.review_status === 'approved' ? `<span class="chip success">${IA.icon('check')}approved</span>` : img.review_status === 'rejected' ? `<span class="chip danger">${IA.icon('flag')}sent back</span>` : IA.stateChip(img.state)}</div>
        <div class="meta"><div class="name" title="${IA.esc(img.filename)}">${IA.esc(img.filename)}</div>
          <div class="sub"><span>${img.assigned_username ? IA.esc(img.assigned_username) : 'Unassigned'}</span>${img.box_count ? `<span>· ${IA.plural(img.box_count, 'box', 'boxes')}</span>` : ''}${img.is_background ? '<span>· background</span>' : ''}</div></div>
      </a>`).join('');
    IA.lazyFade(el);
    pager.innerHTML = g.pages > 1 || g.total > 50 ? `
      <span class="num">${IA.fmtNum((g.page - 1) * g.perPage + 1)}–${IA.fmtNum(Math.min(g.total, g.page * g.perPage))} of ${IA.fmtNum(g.total)}</span>
      <div class="pages"><button class="btn btn-sm btn-icon" data-pg="-1" ${g.page <= 1 ? 'disabled' : ''} aria-label="Previous page">${IA.icon('chevron-left')}</button>
        <span class="num">Page ${g.page} of ${g.pages}</span>
        <button class="btn btn-sm btn-icon" data-pg="1" ${g.page >= g.pages ? 'disabled' : ''} aria-label="Next page">${IA.icon('chevron-right')}</button></div>` : '';
  };
  g.toggle = (id, on) => {
    on = on ?? !g.selected.has(id);
    if (on) g.selected.add(id); else g.selected.delete(id);
    const card = el.querySelector(`.thumb[data-id="${id}"]`);
    card?.classList.toggle('selected', on);
    card?.querySelector('.sel')?.setAttribute('aria-checked', on);
  };
  g.clear = () => { g.selected.clear(); el.querySelectorAll('.thumb.selected').forEach((c) => c.classList.remove('selected')); bulk.refresh(); };
  el.addEventListener('click', (e) => {
    const card = e.target.closest('.thumb');
    if (!card) return;
    const selecting = e.target.closest('.sel') || e.shiftKey || g.selected.size > 0;
    if (!selecting || e.ctrlKey || e.metaKey) return;     // plain click opens the annotator
    e.preventDefault();
    const i = +card.dataset.i;
    if (e.shiftKey && g.anchor !== null) {
      const [a, b] = [Math.min(g.anchor, i), Math.max(g.anchor, i)];
      g.items.slice(a, b + 1).forEach((img) => g.toggle(img.id, true));
    } else {
      g.toggle(card.dataset.id);
      g.anchor = i;
    }
    bulk.refresh();
  });
  pager.addEventListener('click', (e) => {
    const b = e.target.closest('[data-pg]');
    if (!b) return;
    g.page += +b.dataset.pg;
    g.anchor = null;                                   // indices refer to the old page
    g.load();
    el.scrollIntoView({ block: 'start', behavior: 'smooth' });
  });
  return g;
}

// ── Bulk action bar (acts on the grid of the visible section) ─────────────────
const bulk = {
  grid: null, actions: [],
  set(grid, actions) { this.grid = grid; this.actions = actions; this.refresh(); },
  refresh() {
    const n = this.grid?.selected.size || 0;
    $('bulkbar').classList.toggle('open', n > 0);
    if (!n) return;
    $('bulk-count').textContent = `${IA.fmtNum(n)} selected`;
    const wrap = $('bulk-actions');
    wrap.innerHTML = '';
    this.actions.forEach((a) => {
      const b = IA.h(`<button class="btn btn-sm ${a.variant || ''}">${a.icon ? IA.icon(a.icon) : ''}${IA.esc(a.label)}</button>`);
      b.addEventListener('click', () => a.fn(b, [...this.grid.selected]));
      wrap.appendChild(b);
    });
  },
};
$('bulk-clear').addEventListener('click', () => bulk.grid?.clear());

function userMenu(anchor, onPick) {
  IA.menu(anchor, users.map((u) => ({ label: `${u.username}${u.id === user.id ? ' (you)' : ''}`, icon: 'user', fn: () => onPick(u) })));
}

async function assignTo(u, ids, grid) {
  try {
    const r = await IA.post(`/api/projects/${PID}/images/assign`, { image_ids: ids, user_id: u.id });
    IA.toast(r.detail, r.skipped ? 'warn' : 'success');
    grid.clear(); grid.load(); loadPeople();
  } catch (e) { IA.toast(e.message, 'error'); }
}

async function deleteImages(ids, grid) {
  const ok = await IA.confirm({
    title: `Delete ${IA.plural(ids.length, 'image')}?`,
    message: 'They are removed from the project together with their boxes. The files on disk are not deleted.',
    confirmLabel: 'Delete', danger: true,
  });
  if (!ok) return;
  try {
    const r = await IA.post(`/api/projects/${PID}/images/delete`, { image_ids: ids });
    IA.toast(r.deleted === ids.length ? `Deleted ${IA.plural(r.deleted, 'image')}` : `${r.deleted} of ${ids.length} deleted`, r.deleted === ids.length ? 'success' : 'warn');
  } catch (e) { IA.toast(e.message, 'error'); }
  grid.clear(); grid.load();
}

// ── Images section ────────────────────────────────────────────────────────────
const FILTERS = [
  ['', 'All', 'total'], ['unassigned', 'Unassigned', 'unassigned'], ['assigned,unannotated', 'To annotate', 'pending'],
  ['annotated', 'Annotated', 'annotated'], ['ready_for_export', 'In dataset', 'ready_for_export'], ['exported', 'Exported', 'exported'],
];
let imgFilter = IA.store.get('ia_dash_filter', '');
$('img-filter').innerHTML = FILTERS.map(([v, l, k]) => `<button data-v="${v}" data-k="${k}" class="${v === imgFilter ? 'active' : ''}">${l} <span class="count"></span></button>`).join('');
function renderFilterCounts() {
  $('img-filter').querySelectorAll('button').forEach((b) => { b.querySelector('.count').textContent = IA.fmtNum(stats[b.dataset.k] || 0); });
  IA.segSync($('img-filter'));
}
const imagesGrid = makeGrid({
  el: $('img-grid'), pager: $('img-pager'),
  params: () => ({ ...(imgFilter ? { state: imgFilter } : {}), ...($('img-assignee').value ? { assigned_to: $('img-assignee').value } : {}) }),
  empty: () => `<div class="empty"><div class="empty-art">${IA.icon('images')}</div><h3>No images here</h3><p>${stats.total ? 'Nothing matches this filter.' : 'Import a folder or extract frames from a video to get started.'}</p>${stats.total ? '' : '<a class="btn btn-primary" href="#import">Import images</a>'}</div>`,
});
$('img-filter').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  imgFilter = b.dataset.v;
  IA.store.set('ia_dash_filter', imgFilter);
  $('img-filter').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
  IA.segSync($('img-filter'));
  imagesGrid.clear();
  imagesGrid.load(true);
});
$('img-assignee').addEventListener('change', () => { imagesGrid.clear(); imagesGrid.load(true); });
$('img-per-page').addEventListener('change', (e) => { imagesGrid.perPage = +e.target.value; imagesGrid.load(true); });
const imageActions = [
  { label: 'Assign to…', icon: 'user-plus', fn: (b, ids) => userMenu(b, (u) => assignTo(u, ids, imagesGrid)) },
  { label: 'Unassign', icon: 'x', fn: async (_b, ids) => {
    try { const r = await IA.post(`/api/projects/${PID}/images/unassign`, { image_ids: ids }); IA.toast(r.detail, 'success'); imagesGrid.clear(); imagesGrid.load(); loadPeople(); }
    catch (e) { IA.toast(e.message, 'error'); }
  } },
  { label: 'Delete', icon: 'trash-2', variant: 'btn-danger', fn: (_b, ids) => deleteImages(ids, imagesGrid) },
];

// ── Video → frames ────────────────────────────────────────────────────────────
let videoInfo = null;
let vidFormat = 'jpg';
IA.segSync($('vid-format'));
$('vid-format').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  vidFormat = b.dataset.v;
  $('vid-format').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
  IA.segSync($('vid-format'));
  estimate();
});
async function probeVideo() {
  const path = $('vid-path').value.trim();
  videoInfo = null;
  if (!path) { $('vid-info').textContent = 'MP4, MKV, AVI, MOV or WEBM.'; estimate(); return; }
  $('vid-info').textContent = 'Reading video…';
  try {
    videoInfo = await IA.post('/api/video/stats', { filepath: path });
    const mins = Math.floor(videoInfo.duration / 60), secs = Math.round(videoInfo.duration % 60);
    $('vid-info').textContent = `${videoInfo.resolution} · ${videoInfo.fps} fps · ${mins}:${String(secs).padStart(2, '0')} · ${videoInfo.codec}`;
  } catch (e) { $('vid-info').textContent = e.message; }
  estimate();
}
function estimate() {
  const fps = parseFloat($('vid-fps').value);
  const frames = videoInfo && fps > 0 ? Math.max(1, Math.round(videoInfo.duration * Math.min(fps, videoInfo.fps || fps))) : 0;
  $('vid-estimate').textContent = frames ? `≈ ${IA.fmtNum(frames)} frames before skipping duplicates` : 'Lower = fewer, more varied frames.';
  const disk = $('vid-disk');
  if (!frames || !videoInfo.bytes_per_pixel) { disk.textContent = ''; return; }
  const [w, h] = String(videoInfo.resolution).split('x').map(Number);
  const bytes = frames * (w || 1920) * (h || 1080) * videoInfo.bytes_per_pixel[vidFormat];
  const free = videoInfo.free_bytes - 1024 ** 3;
  disk.innerHTML = bytes > free
    ? `<span style="color:var(--danger)">Needs about ${IA.fmtBytes(bytes)} but only ${IA.fmtBytes(Math.max(0, free))} is free — lower the fps or use JPG.</span>`
    : `Up to about ${IA.fmtBytes(bytes)} on disk (${IA.fmtBytes(videoInfo.free_bytes)} free).`;
}
$('vid-path').addEventListener('change', probeVideo);
$('vid-fps').addEventListener('input', estimate);
$('vid-browse').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  IA.toast('The file dialog opens on the server screen', 'info', 2500);
  try { $('vid-path').value = (await IA.get('/api/video/pick_import')).filepath; await probeVideo(); }
  catch (err) { if (err.status !== 400) IA.toast(err.message, 'error'); }
}));
$('vid-go').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  const filepath = $('vid-path').value.trim();
  const fps = parseFloat($('vid-fps').value);
  const res = $('vid-result');
  res.innerHTML = '';
  if (!filepath) { IA.toast('Choose a video first', 'warn'); return; }
  if (!(fps > 0)) { IA.toast('Frames per second must be greater than 0', 'warn'); return; }
  $('vid-progress-wrap').classList.remove('hidden');
  setVideoProgress(0, 'Extracting frames…');
  try {
    const ex = await IA.post('/api/video/extract_frames', { filepath, fps, project_id: PID, image_format: vidFormat, dedupe: +$('vid-dedupe').value });
    setVideoProgress(100, `Importing ${IA.fmtNum(ex.frames)} frames…`);
    $('vid-bar').parentElement.classList.add('indeterminate');
    const imp = await IA.post(`/api/projects/${PID}/import`, { folder_path: ex.folder_path });
    const dup = ex.removed_duplicates ? ` ${IA.fmtNum(ex.removed_duplicates)} near-identical frames were skipped.` : '';
    res.innerHTML = `<div class="callout success">${IA.icon('circle-check')}<div>Imported <b>${IA.fmtNum(imp.imported)}</b> frames in order.${dup} <a class="link" href="#assign">Assign them</a> or <a class="link" href="/projects/${PID}/view">start annotating</a>.</div></div>`;
    await refreshProject();
  } catch (err) {
    res.innerHTML = `<div class="callout danger">${IA.icon('circle-alert')}<div>${IA.esc(err.message)}</div></div>`;
  } finally {
    $('vid-bar').parentElement.classList.remove('indeterminate');
    $('vid-progress-wrap').classList.add('hidden');
  }
}));
function setVideoProgress(pct, stage) {
  $('vid-bar').style.width = `${pct}%`;
  $('vid-pct').textContent = `${Math.round(pct)}%`;
  if (stage) $('vid-stage').textContent = stage;
}

// ── Import ────────────────────────────────────────────────────────────────────
async function pickFolder(input) {
  IA.toast('The folder dialog opens on the server screen', 'info', 2500);
  try { input.value = (await IA.get('/api/train/pick-dataset')).path; }
  catch (err) { if (err.status !== 400) IA.toast(err.message, 'error'); }
}
$('imp-browse').addEventListener('click', (e) => IA.busy(e.currentTarget, () => pickFolder($('imp-path'))));
$('rel-browse').addEventListener('click', (e) => IA.busy(e.currentTarget, () => pickFolder($('rel-path'))));
$('imp-go').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  const folder_path = $('imp-path').value.trim();
  if (!folder_path) { IA.toast('Enter a folder path or use Browse…', 'warn'); return; }
  const out = $('imp-result');
  try {
    const r = await IA.post(`/api/projects/${PID}/import`, { folder_path, include_subfolders: $('imp-recursive').checked });
    out.innerHTML = `<div class="callout success">${IA.icon('circle-check')}<div>Imported <b>${IA.fmtNum(r.imported)}</b> images${r.skipped ? ` · ${IA.fmtNum(r.skipped)} skipped (already in the project or unreadable)` : ''}. <a class="link" href="#assign">Assign them →</a></div></div>`;
    await refreshProject();
  } catch (err) { out.innerHTML = `<div class="callout danger">${IA.icon('circle-alert')}<div>${IA.esc(err.message)}</div></div>`; }
}));
$('chk-go').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  try {
    const r = await IA.get(`/api/projects/${PID}/files/check`);
    $('chk-result').innerHTML = r.missing
      ? `<span style="color:var(--warning)">${IA.fmtNum(r.missing)} of ${IA.fmtNum(r.total)} files missing</span> — last seen in <code>${IA.esc(r.missing_folders[0] || '')}</code>`
      : `All ${IA.fmtNum(r.total)} files found.`;
  } catch (err) { IA.toast(err.message, 'error'); }
}));
$('rel-go').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  const folder_path = $('rel-path').value.trim();
  if (!folder_path) { IA.toast('Enter the new folder', 'warn'); return; }
  try {
    const r = await IA.post(`/api/projects/${PID}/files/relink`, { folder_path });
    IA.toast(`Relinked ${IA.plural(r.relinked, 'file')}${r.unresolved ? ` · ${r.unresolved} still not found` : ''}`, r.unresolved ? 'warn' : 'success');
    $('chk-result').textContent = '';
    await refreshProject();
  } catch (err) { IA.toast(err.message, 'error'); }
}));

// ── Assign ────────────────────────────────────────────────────────────────────
let strategy = 'contiguous';
const storedUsers = IA.store.get(`ia_assign_users_${PID}`, null);
const checkedUsers = new Set(storedUsers || []);
let assignDefaulted = storedUsers !== null;
function renderAssignUsers() {
  if (!users.length) { $('as-users').innerHTML = '<p class="small muted">No users yet. <a class="link" href="/users">Create one</a>.</p>'; return; }
  // First visit only: preselect the annotators. After that an empty selection stays empty.
  if (!assignDefaulted) { users.filter((u) => !u.is_admin).forEach((u) => checkedUsers.add(u.id)); assignDefaulted = true; }
  $('as-users').innerHTML = users.map((u) => `
    <label><span class="check"><input type="checkbox" data-uid="${u.id}" ${checkedUsers.has(u.id) ? 'checked' : ''}/></span>
      ${IA.avatar(u.username)}<span class="nm">${IA.esc(u.username)}${u.is_admin ? ' <span class="muted small">admin</span>' : ''}</span>
      <span class="n">${IA.fmtNum(workloads[u.id]?.total || 0)} images</span></label>`).join('');
  renderPlan();
}
$('as-users').addEventListener('change', (e) => {
  const cb = e.target.closest('[data-uid]'); if (!cb) return;
  if (cb.checked) checkedUsers.add(cb.dataset.uid); else checkedUsers.delete(cb.dataset.uid);
  IA.store.set(`ia_assign_users_${PID}`, [...checkedUsers]);
  renderPlan();
});
$('as-all').addEventListener('click', () => { users.forEach((u) => checkedUsers.add(u.id)); IA.store.set(`ia_assign_users_${PID}`, [...checkedUsers]); renderAssignUsers(); });
$('as-none').addEventListener('click', () => { checkedUsers.clear(); IA.store.set(`ia_assign_users_${PID}`, []); renderAssignUsers(); });
IA.segSync($('as-strategy'));
$('as-strategy').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  strategy = b.dataset.v;
  $('as-strategy').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
  IA.segSync($('as-strategy'));
  $('as-strategy-hint').textContent = strategy === 'contiguous'
    ? 'Each person gets one continuous run of frames — best for video, and needed for carry / propagate.'
    : 'Images are dealt out one by one. Use for unrelated photos when you want everyone to see a mix.';
  renderPlan();
});
function renderPlan() {
  const k = [...checkedUsers].filter((id) => users.some((u) => u.id === id)).length;
  const sel = assignGrid?.selected.size || 0;
  const n = sel || stats.unassigned || 0;
  const btn = $('as-go');
  if (!k || !n) {
    $('as-plan').innerHTML = !k ? 'Pick at least one person.' : 'There are no unassigned images.';
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  const each = Math.floor(n / k), extra = n % k;
  $('as-plan').innerHTML = `<b>${IA.fmtNum(n)}</b> ${sel ? 'selected' : 'unassigned'} images → <b>${k}</b> ${k === 1 ? 'person' : 'people'}: ` +
    (each ? `${IA.fmtNum(each)} each${extra ? `, ${extra} get one more` : ''}` : `${extra} get one each`) +
    (strategy === 'contiguous' ? ', as consecutive ranges.' : ', dealt in turn.');
}
$('as-go').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  const body = { user_ids: [...checkedUsers].filter((id) => users.some((u) => u.id === id)), strategy };
  if (assignGrid.selected.size) body.image_ids = [...assignGrid.selected];
  try {
    const r = await IA.post(`/api/projects/${PID}/images/auto-assign`, body);
    IA.toast(r.detail, 'success');
    assignGrid.clear(); assignGrid.load(true); loadPeople();
  } catch (err) { IA.toast(err.message, 'error'); }
}));
const assignGrid = makeGrid({
  el: $('as-grid'), pager: $('as-pager'),
  params: () => ({ state: 'unassigned' }),
  empty: () => `<div class="empty"><div class="empty-art">${IA.icon('check')}</div><h3>Everything is assigned</h3><p>New imports will show up here.</p></div>`,
  onChange: (g) => { $('as-count').textContent = IA.fmtNum(g.total); renderPlan(); },
});
$('as-page-all').addEventListener('click', () => { assignGrid.items.forEach((i) => assignGrid.toggle(i.id, true)); bulk.refresh(); renderPlan(); });
const assignActions = [
  { label: 'Assign to…', icon: 'user-plus', fn: (b, ids) => userMenu(b, (u) => assignTo(u, ids, assignGrid)) },
  { label: 'Assign to me', icon: 'user', fn: (_b, ids) => assignTo(user, ids, assignGrid) },
];
$('as-grid').addEventListener('click', () => setTimeout(renderPlan, 0));

// ── Review ────────────────────────────────────────────────────────────────────
let rvFilter = IA.store.get('ia_review_filter', 'awaiting_review');
const reviewGrid = makeGrid({
  el: $('rv-grid'), pager: $('rv-pager'),
  params: () => ({ state: rvFilter }),
  empty: () => `<div class="empty"><div class="empty-art">${IA.icon('check-check')}</div><h3>${rvFilter === 'awaiting_review' ? 'Nothing waiting' : 'Nothing here'}</h3><p>${rvFilter === 'awaiting_review' ? 'Frames appear here once annotators add them to the dataset.' : 'Try another filter.'}</p></div>`,
});
$('rv-filter').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.v === rvFilter));
$('rv-filter').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  rvFilter = b.dataset.v;
  IA.store.set('ia_review_filter', rvFilter);
  $('rv-filter').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
  IA.segSync($('rv-filter'));
  reviewGrid.clear(); reviewGrid.load(true);
});
async function reviewAction(ids, action) {
  let note = null;
  if (action === 'reject') {
    note = await IA.prompt({ title: `Send back ${IA.plural(ids.length, 'frame')}?`, label: 'What should the annotator fix? (they see this on the frame)',
      placeholder: 'e.g. boxes too loose; the second car is missing', confirmLabel: 'Send back', validate: (v) => (v.trim() ? '' : 'Write a short note') });
    if (note === null) return;
  }
  try {
    const r = await IA.post(`/api/projects/${PID}/images/review`, { image_ids: ids, action, note });
    IA.toast(r.detail + (r.skipped ? ` · ${r.skipped} skipped` : ''), r.skipped ? 'warn' : 'success');
    reviewGrid.clear(); reviewGrid.load(); loadStats(); loadPeople();
  } catch (e) { IA.toast(e.message, 'error'); }
}
// Admin: submit every annotated image in the project (all annotators), not just your own
$('rv-submit-all').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  try {
    const r = await IA.post(`/api/projects/${PID}/images/submit-all-annotated`);
    IA.toast(r.detail, r.skipped_empty ? 'warn' : 'success', r.skipped_empty ? 7000 : 3500);
    reviewGrid.load();
    loadStats();
  } catch (err) { IA.toast(err.message, 'error'); }
}));
const reviewActions = [
  { label: 'Approve', icon: 'check', variant: 'btn-success', fn: (_b, ids) => reviewAction(ids, 'approve') },
  { label: 'Send back…', icon: 'flag', fn: (_b, ids) => reviewAction(ids, 'reject') },
  { label: 'Delete', icon: 'trash-2', variant: 'btn-danger', fn: (_b, ids) => deleteImages(ids, reviewGrid) },
];

// ── Classes ───────────────────────────────────────────────────────────────────
let clsRows = [];
let usage = {};
async function loadClasses() {
  clsRows = project.classes.map((c) => ({ orig: c, name: c }));
  try { usage = await IA.get(`/api/projects/${PID}/classes/usage`); } catch { usage = {}; }
  renderClasses();
}
function clsDirty() {
  const names = clsRows.map((r) => r.name.trim());
  return JSON.stringify(names) !== JSON.stringify(project.classes);
}
function renderClasses() {
  const list = $('cls-list');
  list.innerHTML = clsRows.map((r, i) => `
    <div class="class-row" draggable="true" data-i="${i}">
      <span class="grip">${IA.icon('grid-2x2', 'i-sm')}</span>
      <span class="idx">${i}</span>
      <div class="row" style="gap:8px;flex-wrap:nowrap;min-width:0"><span class="sw" style="--c:${IA.classColor(i)}"></span><input value="${IA.esc(r.name)}" data-i="${i}" aria-label="Class name"/></div>
      <span class="cnt">${r.orig && usage[r.orig] ? IA.plural(usage[r.orig].boxes, 'box', 'boxes') : ''}</span>
      <button class="btn btn-ghost btn-icon btn-sm" data-del="${i}" aria-label="Remove class">${IA.icon('trash-2')}</button>
    </div>`).join('');
  const orphans = Object.keys(usage).filter((n) => !project.classes.includes(n));
  if (orphans.length) {
    list.insertAdjacentHTML('beforeend', `<div class="callout warn">${IA.icon('circle-alert')}<div>Boxes use classes that are not in the list: ${orphans.map((o) => `<b>${IA.esc(o)}</b> (${usage[o].boxes})`).join(', ')}. Add the class back to include them in exports.</div></div>`);
  }
  $('cls-dirty').textContent = clsDirty() ? 'Unsaved changes' : '';
}
$('cls-list').addEventListener('input', (e) => {
  const inp = e.target.closest('input[data-i]'); if (!inp) return;
  clsRows[+inp.dataset.i].name = inp.value;
  $('cls-dirty').textContent = clsDirty() ? 'Unsaved changes' : '';
});
$('cls-list').addEventListener('click', async (e) => {
  const del = e.target.closest('[data-del]'); if (!del) return;
  const r = clsRows[+del.dataset.del];
  const used = r.orig && usage[r.orig]?.boxes;
  if (used && !(await IA.confirm({ title: `Remove “${r.orig}”?`, message: `${IA.plural(used, 'box', 'boxes')} use this class. They are kept but excluded from exports until the class is added back.`, confirmLabel: 'Remove', danger: true }))) return;
  clsRows.splice(+del.dataset.del, 1);
  renderClasses();
});
let dragFrom = null;
$('cls-list').addEventListener('dragstart', (e) => { const row = e.target.closest('.class-row'); if (!row) return; dragFrom = +row.dataset.i; row.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; });
$('cls-list').addEventListener('dragover', (e) => { const row = e.target.closest('.class-row'); if (!row) return; e.preventDefault(); $('cls-list').querySelectorAll('.over').forEach((x) => x.classList.remove('over')); row.classList.add('over'); });
$('cls-list').addEventListener('drop', (e) => {
  const row = e.target.closest('.class-row'); if (!row || dragFrom === null) return;
  e.preventDefault();
  const [moved] = clsRows.splice(dragFrom, 1);
  clsRows.splice(+row.dataset.i, 0, moved);
  dragFrom = null;
  renderClasses();
});
$('cls-list').addEventListener('dragend', () => { dragFrom = null; $('cls-list').querySelectorAll('.dragging,.over').forEach((x) => x.classList.remove('dragging', 'over')); });
function addClass() {
  const v = $('cls-new').value.trim();
  if (!v) return;
  if (clsRows.some((r) => r.name.trim() === v)) { IA.toast('That class already exists', 'warn'); return; }
  clsRows.push({ orig: project.classes.includes(v) ? v : null, name: v });
  $('cls-new').value = '';
  renderClasses();
}
$('cls-add').addEventListener('click', addClass);
$('cls-new').addEventListener('keydown', (e) => { if (e.key === 'Enter') addClass(); });
$('cls-reset').addEventListener('click', loadClasses);
$('cls-save').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  const classes = clsRows.map((r) => r.name.trim());
  if (classes.some((c) => !c)) { IA.toast('Class names cannot be empty', 'warn'); return; }
  if (new Set(classes).size !== classes.length) { IA.toast('Class names must be unique', 'warn'); return; }
  const renames = {};
  clsRows.forEach((r) => { if (r.orig && r.orig !== r.name.trim()) renames[r.orig] = r.name.trim(); });
  try {
    project = await IA.patch(`/api/projects/${PID}/classes`, { classes, renames });
    IA.toast('Classes saved', 'success');
    renderHeader();
    await loadClasses();
  } catch (err) { IA.toast(err.message, 'error'); }
}));

// ── Export ────────────────────────────────────────────────────────────────────
const split = IA.store.get(`ia_split_${PID}`, { train: 80, valid: 10, test: 10 });
function renderSplit() {
  const incTest = $('ex-test').checked;
  const parts = incTest ? [['train', 'Train'], ['valid', 'Valid'], ['test', 'Test']] : [['train', 'Train'], ['valid', 'Valid']];
  if (!incTest) { split.train = 100 - split.valid; }
  $('ex-split').innerHTML = parts.map(([k, l]) => `<div class="seg-part split-${k}" data-k="${k}" style="flex-basis:${split[k]}%" title="${l} ${split[k]}%">${split[k] >= 18 ? `${l} ` : ''}${split[k] >= 7 ? `${split[k]}%` : ''}</div>`).join('')
    + `<span class="handle" data-h="0" style="left:${split.train}%"></span>`
    + (incTest ? `<span class="handle" data-h="1" style="left:${split.train + split.valid}%"></span>` : '');
  let legend = $('ex-legend');
  if (!legend) { legend = IA.h('<div class="legend" id="ex-legend"></div>'); $('ex-split').after(legend); }
  legend.innerHTML = parts.map(([k, l]) => `<span><i class="split-${k}"></i>${l} ${split[k]}% <b class="num" data-n="${k}"></b></span>`).join('');
}
// Dragging is tracked on window: re-rendering the bar replaces the handle under the pointer
let dragH = null;
const MIN_VALID = 5;                 // YOLO needs validation images to report accuracy
$('ex-split').addEventListener('pointerdown', (e) => { const h = e.target.closest('.handle'); if (!h) return; e.preventDefault(); dragH = +h.dataset.h; });
window.addEventListener('pointermove', (e) => {
  if (dragH === null) return;
  const r = $('ex-split').getBoundingClientRect();
  const pct = Math.round(Math.max(0, Math.min(100, ((e.clientX - r.left) / r.width) * 100)));
  // Two boundaries: b1 = end of train, b2 = end of valid (100 when there is no test split)
  const hasTest = $('ex-test').checked;
  let b1 = split.train, b2 = hasTest ? split.train + split.valid : 100;
  if (dragH === 0) b1 = Math.max(5, Math.min(pct, b2 - MIN_VALID));
  else b2 = Math.max(b1 + MIN_VALID, Math.min(100, pct));
  Object.assign(split, { train: b1, valid: b2 - b1, test: hasTest ? 100 - b2 : 0 });
  renderSplit();
});
window.addEventListener('pointerup', () => { if (dragH === null) return; dragH = null; IA.store.set(`ia_split_${PID}`, split); previewExport(); });
$('ex-test').addEventListener('change', () => {
  if ($('ex-test').checked && !split.test) { split.test = 10; split.train = Math.max(5, 100 - split.valid - split.test); }
  if (!$('ex-test').checked) split.test = 0;
  renderSplit(); previewExport();
});
$('ex-group').addEventListener('change', previewExport);
$('ex-approved').addEventListener('change', previewExport);
const ratios = () => ({ train_ratio: split.train / 100, valid_ratio: split.valid / 100, test_ratio: ($('ex-test').checked ? split.test : 0) / 100,
  include_test: $('ex-test').checked, group_sequences: $('ex-group').checked, only_approved: $('ex-approved').checked });
async function previewExport() {
  try {
    const q = new URLSearchParams(ratios());
    const [p, y] = await Promise.all([
      IA.get(`/api/projects/${PID}/export/preview?${q}`),
      IA.get(`/api/projects/${PID}/export/yaml-preview?include_test=${$('ex-test').checked}`),
    ]);
    $('ex-total').textContent = `${IA.plural(p.total_ready, 'image')}${$('ex-approved').checked ? ' approved' : ' in the dataset'}`;
    [['train', p.train_count], ['valid', p.valid_count], ['test', p.test_count]].forEach(([k, n]) => {
      const el = document.querySelector(`#ex-legend [data-n="${k}"]`); if (el) el.textContent = `· ${IA.plural(n, 'image')}`;
    });
    $('ex-yaml').textContent = y.yaml;
    $('ex-out').textContent = y.output_path.split(/[\\/]/).pop();
    $('ex-go').disabled = !p.total_ready;
    $('cl-go').disabled = !p.total_ready;
  } catch (err) { $('ex-yaml').textContent = err.message; }
}
$('ex-go').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  $('ex-result').innerHTML = '';
  $('ex-progress-wrap').classList.remove('hidden');
  $('ex-bar').style.width = '0%';
  try {
    const r = await IA.post(`/api/projects/${PID}/export`, ratios());
    const warn = r.orphan_warnings.length ? `<details style="margin-top:8px"><summary class="small">${IA.plural(r.orphan_warnings.length, 'warning')}</summary><ul class="small" style="margin:6px 0 0 18px">${r.orphan_warnings.slice(0, 50).map((w) => `<li>${IA.esc(w)}</li>`).join('')}</ul></details>` : '';
    const linked = r.linked ? `<br/><span class="small">${IA.fmtNum(r.linked)} images hard-linked — no extra disk space used.</span>` : '';
    $('ex-result').innerHTML = `<div class="callout success">${IA.icon('circle-check')}<div>Exported <b>${IA.fmtNum(r.exported)}</b> images — train ${IA.fmtNum(r.train)} · valid ${IA.fmtNum(r.valid)} · test ${IA.fmtNum(r.test)}<br/><code>${IA.esc(r.output_path)}</code>${linked}${warn}
      <div class="row" style="margin-top:10px"><a class="btn btn-sm" href="/train">${IA.icon('cpu')}Train a model on it</a></div></div></div>`;
    await previewExport();
  } catch (err) { $('ex-result').innerHTML = `<div class="callout danger">${IA.icon('circle-alert')}<div>${IA.esc(err.message)}</div></div>`; }
  finally { $('ex-progress-wrap').classList.add('hidden'); }
}));

// ── Background jobs (model assist, cloud bundles) ─────────────────────────────
const jobPanels = {};               // panel key → {el, id, onDone}
const panelKey = (job) => (job.kind === 'predict' ? `predict_${job.params?.mode}` : job.kind);
function renderJob(el, job) {
  const pct = Math.round(job.progress || 0);
  const active = ['queued', 'running'].includes(job.status);
  const label = { queued: 'Waiting…', running: job.message || 'Working…', done: job.message || 'Done', failed: job.message || 'Failed',
                  cancelled: 'Cancelled', interrupted: 'Stopped when the server restarted' }[job.status] || job.status;
  el.innerHTML = `<div class="job-box ${job.status}"><div class="row"><span>${IA.esc(label)}</span>${active ? `<button class="btn btn-ghost btn-sm" data-cancel="${job.id}">Cancel</button>` : `<span class="muted small">${IA.esc(IA.fmtRel(job.finished_at || job.created_at))}</span>`}</div>
    ${active ? `<div class="progress"><span style="width:${pct}%"></span></div>` : ''}</div>`;
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-cancel]'); if (!b) return;
  try { await IA.post(`/api/jobs/${b.dataset.cancel}/cancel`); } catch (err) { IA.toast(err.message, 'error'); }
});
function trackJob(key, job) {
  const panel = jobPanels[key];
  if (!panel) return;
  panel.id = job.id;
  renderJob(panel.el, job);
  if (job.status === 'done') panel.onDone?.(job);
}
function onJobUpdate(job) {
  if (job.project_id !== PID) return;
  const key = panelKey(job);
  if (jobPanels[key] && (jobPanels[key].id === job.id || !jobPanels[key].id)) trackJob(key, job);
}
async function restoreJobs() {
  try {
    const { jobs } = await IA.get(`/api/jobs?project_id=${PID}&limit=20`);
    const seen = new Set();
    for (const j of jobs) {                       // newest first: show the latest job of each kind
      const key = panelKey(j);
      if (seen.has(key)) continue;
      seen.add(key);
      if (jobPanels[key]) { jobPanels[key].id = j.id; renderJob(jobPanels[key].el, j); jobPanels[key].onRestore?.(j); }
    }
  } catch { /* ignore */ }
}

// Model assist
let assistModels = [];
async function loadAssist() {
  // The device note needs the one-off torch probe (slow on first use) — never make the model list wait for it
  IA.get('/api/system').then((sys) => {
    const c = sys.compute;
    $('as-device').textContent = c.cuda_available
      ? `Runs on the GPU (${c.gpu_name || 'CUDA'}).`
      : `Runs on the CPU (${c.cpu_cores} cores) — about 0.3 s per frame with a nano model. On a machine with an NVIDIA GPU this switches to the GPU automatically.`;
  }).catch(() => {});
  try {
    const { models } = await IA.get('/api/assist/models');
    assistModels = models.filter((m) => !m.is_last);
    const sel = $('as-model');
    const keep = sel.value || IA.store.get(`ia_assist_model_${PID}`, '');
    sel.innerHTML = assistModels.length
      ? assistModels.map((m) => `<option value="${IA.esc(m.name)}">${IA.esc(m.name)} · ${m.size_mb} MB · ${IA.esc(IA.fmtDate(m.modified))}</option>`).join('')
      : '<option value="">No trained models yet</option>';
    if (keep && assistModels.some((m) => m.name === keep)) sel.value = keep;
    $('pl-go').disabled = $('qa-go').disabled = !assistModels.length;
    $('as-model-hint').innerHTML = assistModels.length ? 'Any model trained here or imported from cloud training.'
      : 'Train one under <a class="link" href="/train">Train model</a>, or use cloud training (Export section).';
  } catch (e) { IA.toast(e.message, 'error'); }
  await restoreJobs();
  loadFindings();
}
$('as-model').addEventListener('change', (e) => IA.store.set(`ia_assist_model_${PID}`, e.target.value));
$('pl-conf').addEventListener('input', (e) => { $('pl-conf-v').textContent = (+e.target.value).toFixed(2); });
async function startPredict(mode, btn) {
  const model = $('as-model').value;
  if (!model) { IA.toast('Pick a model first', 'warn'); return; }
  await IA.busy(btn, async () => {
    try {
      const body = mode === 'prelabel'
        ? { model, mode, scope: 'todo', conf: +$('pl-conf').value }
        : { model, mode, scope: 'labeled', conf: 0.1 };          // low threshold: the check needs the model's doubts too
      trackJob(`predict_${mode}`, await IA.post(`/api/projects/${PID}/assist/predict`, body));
    } catch (e) { IA.toast(e.message, 'error'); }
  });
}
$('pl-go').addEventListener('click', (e) => startPredict('prelabel', e.currentTarget));
$('qa-go').addEventListener('click', (e) => startPredict('check', e.currentTarget));
let prelabelBatch = null;
function showPrelabelResult(job) {
  const r = job.result || {};
  prelabelBatch = r.batch_id || null;
  $('pl-undo').classList.toggle('hidden', !prelabelBatch);
  const unknown = Object.keys(r.unknown_classes || {});
  if (unknown.length) {
    $('pl-job').insertAdjacentHTML('beforeend', `<div class="callout warn" style="margin-top:8px">${IA.icon('circle-alert')}<div>The model also found classes this project doesn't have: ${unknown.map((u) => `<b>${IA.esc(u)}</b>`).join(', ')}. Those boxes were left out — add the class (same name) to use them.</div></div>`);
  }
}
jobPanels.predict_prelabel = { el: $('pl-job'), onDone: (j) => { showPrelabelResult(j); loadStats(); }, onRestore: (j) => { if (j.status === 'done') showPrelabelResult(j); } };
jobPanels.predict_check = { el: $('qa-job'), onDone: () => loadFindings() };
$('pl-undo').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  if (!prelabelBatch) return;
  try {
    const r = await IA.post(`/api/projects/${PID}/annotations/batches/${prelabelBatch}/undo`);
    IA.toast(`Removed the suggestions from ${IA.plural(r.restored.length, 'frame')}${r.conflicts.length ? ` · ${r.conflicts.length} already edited and kept` : ''}`, 'success');
    prelabelBatch = null; $('pl-undo').classList.add('hidden');
  } catch (err) { IA.toast(err.message, 'error'); }
}));
const ISSUE = {
  missed: (i) => `<span class="chip danger" title="The model sees an object nobody labelled">missed ${IA.esc(i.class_name)} ${Math.round(i.conf * 100)}%</span>`,
  class: (i) => `<span class="chip warn" title="The model thinks this is a different class">${IA.esc(i.class_name)} → ${IA.esc(i.pred_class)}?</span>`,
  loose: (i) => `<span class="chip" title="The box overlaps the model's box only partly (IoU ${i.iou})">loose ${IA.esc(i.class_name)} box</span>`,
  extra: (i) => `<span class="chip" title="The model found nothing here — a false label or a hard case">unconfirmed ${IA.esc(i.class_name)}</span>`,
};
async function loadFindings() {
  try {
    const r = await IA.get(`/api/projects/${PID}/assist/findings`);
    const box = $('qa-findings');
    if (!r.job_id) { box.innerHTML = '<p class="small muted">Run “Find label mistakes” to fill this list.</p>'; $('qa-meta').textContent = ''; return; }
    const c = r.counts || {};
    $('qa-meta').textContent = r.findings.length ? `${IA.plural(r.findings.length, 'frame')} · ${c.missed || 0} missed · ${c.class || 0} class · ${c.loose || 0} loose · ${c.extra || 0} unconfirmed` : '';
    box.innerHTML = r.findings.length ? `<div class="findings">${r.findings.slice(0, 300).map((f) => `
      <a class="finding" href="/projects/${PID}/annotate/${f.image_id}?qa=1">
        <img src="${IA.thumbUrl(f.image_id, 160)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'"/>
        <div style="min-width:0"><div class="nm">${IA.esc(f.filename)} <span class="muted small">#${f.seq ?? ''}${f.assignee ? ` · ${IA.esc(f.assignee)}` : ''}</span></div>
          <div class="issues">${f.issues.map((i) => ISSUE[i.type]?.(i) || '').join('')}</div></div>
        ${IA.stateChip(f.state)}</a>`).join('')}</div>${r.findings.length > 300 ? `<p class="small muted">…and ${r.findings.length - 300} more</p>` : ''}`
      : '<div class="callout success">' + IA.icon('circle-check') + '<div>The model agrees with the labels on every checked frame.</div></div>';
  } catch (e) { $('qa-findings').innerHTML = `<p class="small muted">${IA.esc(e.message)}</p>`; }
}

// Cloud training bundles
async function loadBundles() {
  try {
    const { bundles } = await IA.get('/api/cloud/bundles');
    $('cl-list').innerHTML = bundles.length ? bundles.map((b) => `
      <div class="bundle"><div style="min-width:0"><div class="nm" title="${IA.esc(b.name)}">${IA.esc(b.name)}</div><div class="small muted">${IA.fmtBytes(b.bytes)} · ${IA.esc(IA.fmtRel(b.created))}</div></div>
        <div class="row" style="gap:4px"><a class="btn btn-sm" href="/api/cloud/bundles/${encodeURIComponent(b.name)}" download aria-label="Download">${IA.icon('download')}</a>
          <button class="btn btn-ghost btn-sm btn-icon" data-del-bundle="${IA.esc(b.name)}" aria-label="Delete">${IA.icon('trash-2')}</button></div></div>`).join('')
      : '<p class="small muted">None yet.</p>';
  } catch { /* ignore */ }
}
$('cl-list').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-del-bundle]'); if (!b) return;
  if (!(await IA.confirm({ title: 'Delete this bundle?', message: 'Only the .zip is removed — your images and labels are not affected.', confirmLabel: 'Delete', danger: true }))) return;
  try { await IA.del(`/api/cloud/bundles/${encodeURIComponent(b.dataset.delBundle)}`); loadBundles(); } catch (err) { IA.toast(err.message, 'error'); }
});
jobPanels.cloud_bundle = { el: $('cl-job'), onDone: (j) => {
  loadBundles();
  const r = j.result || {};
  $('cl-job').insertAdjacentHTML('beforeend', `<div class="callout success" style="margin-top:8px">${IA.icon('circle-check')}<div>
    <b>${IA.esc(r.file || 'Bundle')}</b> is ready (${IA.fmtBytes(r.bytes)}). <a class="link" href="/api/cloud/bundles/${encodeURIComponent(r.file || '')}" download>Download it</a>, then follow README.md inside:
    Kaggle → upload as a Dataset, import <code>train.ipynb</code>, turn on GPU + Internet, Run all. Colab → upload <code>train.ipynb</code> and the zip, Run all.</div></div>`);
} };
$('cl-go').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  try {
    const job = await IA.post(`/api/projects/${PID}/cloud-bundle`, {
      ...ratios(), train: { model: $('cl-model').value, epochs: Math.max(1, parseInt($('cl-epochs').value, 10) || 100), imgsz: +$('cl-imgsz').value },
    });
    trackJob('cloud_bundle', job);
  } catch (err) { IA.toast(err.message, 'error'); }
}));

// ── Routing ───────────────────────────────────────────────────────────────────
const SECTIONS = {
  overview: { title: 'Overview', nav: 'overview', load: () => { loadPeople(); checkFiles(); } },
  images:   { title: 'All images', nav: 'images', load: () => imagesGrid.load(), bulk: [imagesGrid, imageActions] },
  video:    { title: 'Video → frames', nav: 'video-frames', load: () => {} },
  import:   { title: 'Import images', nav: 'import', load: () => {} },
  assign:   { title: 'Assign', nav: 'assign', load: () => { assignGrid.load(); loadPeople(); }, bulk: [assignGrid, assignActions] },
  review:   { title: 'Review', nav: 'review', load: () => reviewGrid.load(), bulk: [reviewGrid, reviewActions] },
  assist:   { title: 'Model assist', nav: 'assist', load: () => loadAssist() },
  classes:  { title: 'Classes', nav: 'classes', load: () => loadClasses() },
  export:   { title: 'Export & cloud training', nav: 'export', load: () => { renderSplit(); previewExport(); loadBundles(); restoreJobs(); } },
};
let current = null;
function route() {
  const key = SECTIONS[location.hash.slice(1)] ? location.hash.slice(1) : 'overview';
  if (current === 'classes' && key !== 'classes' && clsDirty()) IA.toast('Unsaved class changes were kept — come back to save them', 'warn');
  current = key;
  const s = SECTIONS[key];
  document.querySelectorAll('.dash-section').forEach((el) => el.classList.toggle('active', el.dataset.section === key));
  IA.setActiveNav(s.nav);
  IA.setCrumbs([{ label: 'Projects', href: '/home' }, { label: project.name, href: '#overview' }, { label: s.title }]);
  $('section-eyebrow').textContent = s.title;
  bulk.grid?.selected.size && bulk.grid.clear();
  bulk.set(s.bulk?.[0] || null, s.bulk?.[1] || []);
  if (key === 'classes' && clsRows.length && clsDirty()) renderClasses(); else s.load();
  requestAnimationFrame(() => document.querySelectorAll('.dash-section.active .seg').forEach(IA.segSync));
}
window.addEventListener('hashchange', route);

async function refreshProject() {
  try { project = await IA.get(`/api/projects/${PID}`); renderHeader(); } catch { /* ignore */ }
  loadStats();
  checkFiles();
}

// ── Live updates ──────────────────────────────────────────────────────────────
const socket = IA.connectSocket(PID);
const reloadVisible = IA.debounce(() => {
  if (current === 'images') imagesGrid.load();
  if (current === 'assign') assignGrid.load();
  if (current === 'review') reviewGrid.load();
  if (current === 'overview') loadPeople();
}, 600);
socket?.on('stats_update', (s) => { if (!s.project_id || s.project_id === PID) applyStats(s); });
socket?.on('annotation_saved', ({ image_id, box_count, saved_by }) => {
  feed(saved_by, `<b>${IA.esc(saved_by)}</b> saved ${IA.plural(box_count, 'box', 'boxes')} on ${fileLabel(image_id)}`);
});
socket?.on('image_state_change', ({ image_id, new_state }) => {
  if (new_state === 'ready_for_export') feed(null, `${fileLabel(image_id)} added to the dataset`);
  reloadVisible();
});
socket?.on('image_state_change_bulk', ({ summary }) => { if (summary) feed(null, IA.esc(summary)); reloadVisible(); });
socket?.on('annotations_bulk_saved', ({ saved_by, kind, image_ids }) => {
  const verb = { propagate: 'propagated boxes to', interpolate: 'interpolated boxes on', undo: 'undid changes on', bulk: 'updated' }[kind] || 'updated';
  feed(saved_by, `<b>${IA.esc(saved_by)}</b> ${verb} ${IA.plural(image_ids.length, 'frame')}`);
  reloadVisible();
});
socket?.on('image_deleted', reloadVisible);
socket?.on('export_progress', ({ percent }) => { $('ex-bar').style.width = `${percent}%`; $('ex-pct').textContent = `${percent}%`; });
socket?.on('video_progress', ({ percent, stage }) => {
  if (stage === 'extracting') setVideoProgress(percent, 'Extracting frames…');
  if (stage === 'deduplicating') setVideoProgress(percent, 'Skipping near-identical frames…');
});
socket?.on('project_updated', ({ classes }) => { project.classes = classes; renderHeader(); });
socket?.on('job_update', onJobUpdate);
socket?.on('images_deleted', reloadVisible);
IA.onReconnect(() => { loadStats(); reloadVisible(); if (current === 'assist' || current === 'export') restoreJobs(); });
socket?.on('projects_changed', async () => {
  try { await IA.get(`/api/projects/${PID}`); }
  catch (e) { if (e.status === 404) { IA.toast('This project was deleted', 'error'); setTimeout(() => { location.href = '/home'; }, 1500); } }
});

await loadStats();
await loadPeople();
route();
})();
