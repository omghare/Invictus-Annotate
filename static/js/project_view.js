/**
 * project_view.js — an annotator's images in one project (/projects/{id}/view).
 * Admins land here from "My images" and see only images assigned to them.
 */
(async () => {
'use strict';

const PID = window.PROJECT_ID;
const user = await IA.shell({ projectId: PID, section: 'mine' });
if (!user) return;
const $ = (id) => document.getElementById(id);

let project;
try { project = await IA.get(`/api/projects/${PID}`); }
catch (e) {
  IA.toast(e.status === 404 ? 'This project no longer exists' : e.message, 'error');
  setTimeout(() => { location.href = '/home'; }, 1200);
  return;
}
document.title = `${project.name} · My images`;
$('project-name').textContent = project.name;
$('rail-project-name').textContent = project.name;
$('project-meta').textContent = `${IA.plural(project.classes.length, 'class', 'classes')}: ${project.classes.join(', ')}`;
$('hero-thumb').innerHTML = project.thumbnail_image_id ? `<img src="${IA.thumbUrl(project.thumbnail_image_id, 160)}" alt="" onerror="this.remove()"/>` : '';
IA.setCrumbs([{ label: 'Projects', href: '/home' }, { label: project.name, href: user.is_admin ? `/projects/${PID}` : undefined }, { label: 'My images' }]);

// ── Stats ─────────────────────────────────────────────────────────────────────
let stats = {};
async function loadStats() {
  try { applyStats(await IA.get(`/api/projects/${PID}/images/stats?mine=true`)); } catch { /* ignore */ }
}
function applyStats(s) {
  stats = IA.derive(s);
  ['total', 'pending', 'done', 'ready_for_export', 'exported'].forEach((k) => IA.countTo(document.querySelector(`#my-stats [data-s="${k}"]`), stats[k]));
  document.querySelector('[data-s="project"]').textContent = `of ${IA.fmtNum(stats.project_total)} in the project`;
  document.querySelector('[data-s="done-sub"]').textContent = stats.annotated ? `${IA.fmtNum(stats.annotated)} waiting to be added` : '';
  $('my-pct').textContent = `${stats.pct}% · ${IA.fmtNum(stats.done)} of ${IA.fmtNum(stats.total)}`;
  if (!$('my-bar').firstChild) $('my-bar').innerHTML = IA.stateBar(stats, 'lg'); else IA.updateStateBar($('my-bar').firstChild, stats);
  IA.setNavCount('mine', stats.pending, true);
  const btn = $('submit-all-btn');
  btn.disabled = !stats.annotated;
  btn.querySelector('span').textContent = stats.annotated ? `Add All Annotated Images to Dataset (${IA.fmtNum(stats.annotated)})` : 'Add All Annotated Images to Dataset';
  renderFilterCounts();
}

// ── Grid ──────────────────────────────────────────────────────────────────────
const FILTERS = [['', 'All', 'total'], ['assigned,unannotated', 'To annotate', 'pending'], ['rejected', 'Sent back', 'rejected'], ['annotated', 'Annotated', 'annotated'], ['ready_for_export', 'In dataset', 'ready_for_export'], ['exported', 'Exported', 'exported']];
let filter = IA.store.get(`ia_view_filter_${PID}`, 'assigned,unannotated');
let page = 1, perPage = 50, seq = 0;
$('filter').innerHTML = FILTERS.map(([v, l, k]) => `<button data-v="${v}" data-k="${k}" class="${v === filter ? 'active' : ''}">${l} <span class="count"></span></button>`).join('');
function renderFilterCounts() {
  $('filter').querySelectorAll('button').forEach((b) => { b.querySelector('.count').textContent = IA.fmtNum(stats[b.dataset.k] || 0); });
  IA.segSync($('filter'));
}

async function loadImages() {
  const mySeq = ++seq;
  const q = new URLSearchParams({ page, per_page: perPage, mine: 'true', ...(filter ? { state: filter } : {}) });
  let data;
  try { data = await IA.get(`/api/projects/${PID}/images?${q}`); }
  catch (e) { $('grid').innerHTML = `<div class="empty"><h3>Couldn't load images</h3><p>${IA.esc(e.message)}</p></div>`; return; }
  if (mySeq !== seq) return;                         // a newer filter/page request won
  page = data.page;
  if (!data.items.length) {
    const msg = filter === 'assigned,unannotated' && stats.total
      ? ['All done here', 'You have annotated every image assigned to you.', 'check-check']
      : stats.total ? ['Nothing in this view', 'Try another filter.', 'filter'] : ['Nothing assigned yet', 'When an admin assigns images to you, they appear here.', 'image-off'];
    $('grid').innerHTML = `<div class="empty"><div class="empty-art">${IA.icon(msg[2])}</div><h3>${msg[0]}</h3><p>${msg[1]}</p></div>`;
    $('pager').innerHTML = '';
    return;
  }
  $('grid').innerHTML = data.items.map((img, i) => `
    <a class="thumb rise" style="--i:${Math.min(i, 12)}" href="/projects/${PID}/annotate/${img.id}">
      <div class="media"><img src="${IA.thumbUrl(img.id)}" alt="" loading="lazy" onerror="this.remove()"/><div class="ph">${IA.icon('image')}</div></div>
      <div class="overlay-top"><span></span>${img.review_status === 'rejected' ? `<span class="chip danger">${IA.icon('flag')}sent back</span>` : IA.stateChip(img.state)}</div>
      <div class="meta"><div class="name" title="${IA.esc(img.filename)}">${IA.esc(img.filename)}</div>
        <div class="sub">${img.box_count ? IA.plural(img.box_count, 'box', 'boxes') : img.is_background ? 'No objects' : 'No boxes yet'}</div></div>
    </a>`).join('');
  IA.lazyFade($('grid'));
  $('pager').innerHTML = data.total_pages > 1 ? `
    <span class="num">${IA.fmtNum((page - 1) * perPage + 1)}–${IA.fmtNum(Math.min(data.total, page * perPage))} of ${IA.fmtNum(data.total)}</span>
    <div class="pages"><button class="btn btn-sm btn-icon" data-pg="-1" ${page <= 1 ? 'disabled' : ''}>${IA.icon('chevron-left')}</button>
      <span class="num">Page ${page} of ${data.total_pages}</span>
      <button class="btn btn-sm btn-icon" data-pg="1" ${page >= data.total_pages ? 'disabled' : ''}>${IA.icon('chevron-right')}</button></div>` : '';
}
$('pager').addEventListener('click', (e) => { const b = e.target.closest('[data-pg]'); if (!b) return; page += +b.dataset.pg; loadImages(); window.scrollTo({ top: 0, behavior: 'smooth' }); });
$('filter').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  filter = b.dataset.v; page = 1;
  IA.store.set(`ia_view_filter_${PID}`, filter);
  $('filter').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
  IA.segSync($('filter'));
  loadImages();
});
$('per-page').addEventListener('change', (e) => { perPage = +e.target.value; page = 1; loadImages(); });

// "Continue annotating" opens the first image that still needs work
$('continue-btn').addEventListener('click', async (e) => {
  e.preventDefault();
  try {
    const d = await IA.get(`/api/projects/${PID}/images?mine=true&state=assigned,unannotated&per_page=50`);
    const any = d.items[0] || (await IA.get(`/api/projects/${PID}/images?mine=true&per_page=50`)).items[0];
    if (!any) { IA.toast('No images are assigned to you yet', 'info'); return; }
    location.href = `/projects/${PID}/annotate/${any.id}`;
  } catch (err) { IA.toast(err.message, 'error'); }
});

$('submit-all-btn').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  try {
    const r = await IA.post(`/api/projects/${PID}/images/submit-all-annotated?mine=true`);
    IA.toast(r.detail, r.skipped_empty ? 'warn' : 'success', r.skipped_empty ? 7000 : 3500);
    await loadStats();
    loadImages();
  } catch (err) { IA.toast(err.message, 'error'); }
}));

// ── Live updates (only my images matter) ──────────────────────────────────────
const refresh = IA.debounce(() => { loadStats(); loadImages(); }, 400);
const socket = IA.connectSocket(PID);
const mine = (ch) => ch.assigned_to === user.id;
socket?.on('image_state_change', (ch) => { if (mine(ch)) refresh(); });
socket?.on('image_state_change_bulk', ({ changes }) => { if (changes.some(mine)) refresh(); });
socket?.on('annotations_bulk_saved', refresh);
socket?.on('image_deleted', refresh);
socket?.on('images_deleted', refresh);
IA.onReconnect(refresh);

await loadStats();
await loadImages();
})();
