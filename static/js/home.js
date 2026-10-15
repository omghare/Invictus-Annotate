/**
 * home.js — project list, new project, history, delete. Live stats via socket.
 */
(async () => {
'use strict';

const user = await IA.shell({ section: 'projects' });
if (!user) return;
IA.setCrumbs([{ label: 'Projects' }]);

let projects = [];
const grid = document.getElementById('project-grid');

document.getElementById('home-eyebrow').textContent = user.is_admin ? 'Workspace' : `Signed in as ${user.username}`;

// ── Load / render ─────────────────────────────────────────────────────────────
async function load() {
  try {
    projects = await IA.get('/api/projects');
  } catch (e) {
    grid.innerHTML = `<div class="empty"><div class="empty-art">${IA.icon('circle-alert')}</div><h3>Couldn't load projects</h3><p>${IA.esc(e.message)}</p></div>`;
    return;
  }
  render();
}

const statsOf = (p) => ({ ...p.stats, total: p.image_count });

function render() {
  const lead = document.getElementById('home-lead');
  if (user.is_admin) {
    const images = projects.reduce((s, p) => s + p.image_count, 0);
    lead.textContent = projects.length ? `${IA.plural(projects.length, 'project')} · ${IA.plural(images, 'image')}` : 'Create a project to start labelling.';
    renderTotals();
  }

  if (!projects.length) {
    grid.innerHTML = user.is_admin
      ? `<div class="empty"><div class="empty-art">${IA.icon('folder')}</div><h3>No projects yet</h3>
           <p>A project holds a set of images, the classes you label them with, and the people doing the work.</p>
           <button class="btn btn-primary" data-act="new">${IA.icon('plus')}New project</button></div>`
      : `<div class="empty"><div class="empty-art">${IA.icon('image-off')}</div><h3>Nothing assigned yet</h3>
           <p>When an admin assigns images to you, the project shows up here.</p></div>`;
    return;
  }
  grid.innerHTML = projects.map(card).join('');
  IA.lazyFade(grid);
}

function card(p, i) {
  const s = statsOf(p);
  const pct = p.image_count ? Math.round((p.annotated_count / p.image_count) * 100) : 0;
  const dest = user.is_admin ? `/projects/${p.id}` : `/projects/${p.id}/view`;
  const media = p.thumbnail_image_id
    ? `<img src="${IA.thumbUrl(p.thumbnail_image_id, 640)}" alt="" loading="lazy" onerror="this.remove()"/>`
    : '';
  return `
    <a class="project-card rise" style="--i:${i}" href="${dest}" data-id="${p.id}">
      <div class="pc-media">${media}<div class="ph">${IA.icon('images', 'i-xl')}</div></div>
      <div class="pc-body">
        <div class="pc-title-row">
          <h3 class="pc-title" title="${IA.esc(p.name)}">${IA.esc(p.name)}</h3>
          ${user.is_admin ? `<button class="btn btn-ghost btn-icon btn-sm" data-act="menu" aria-label="Project actions">${IA.icon('ellipsis')}</button>` : ''}
        </div>
        <div class="pc-progress">
          ${IA.stateBar(s)}
          <div class="pc-progress-meta"><span><b data-pct>${pct}%</b> annotated</span><span data-counts>${IA.fmtNum(p.annotated_count)} / ${IA.fmtNum(p.image_count)}</span></div>
        </div>
        <div class="pc-foot">
          <span>${IA.plural(p.classes.length, 'class', 'classes')}</span>
          <span class="sep">·</span>
          ${p.ready_count ? `<span class="pc-ready">${IA.fmtNum(p.ready_count)} in dataset</span><span class="sep">·</span>` : ''}
          <span>${IA.esc(IA.fmtDate(p.created_at))}</span>
        </div>
      </div>
    </a>`;
}

function renderTotals() {
  const sum = (k) => projects.reduce((s, p) => s + (p[k] || 0), 0);
  const images = sum('image_count'), done = sum('annotated_count');
  IA.countTo(IA.$('[data-stat="projects"]'), projects.length, { delta: false });
  IA.countTo(IA.$('[data-stat="images"]'), images);
  IA.countTo(IA.$('[data-stat="done"]'), done);
  IA.countTo(IA.$('[data-stat="ready"]'), sum('ready_count'));
  IA.countTo(IA.$('[data-stat="exported"]'), sum('exported_count'));
  IA.$('[data-stat="done-pct"]').textContent = images ? `${Math.round((done / images) * 100)}% of all images` : '';
}

/** Live update from the throttled global event (no refetch). */
function applyStats(projectId, st) {
  const p = projects.find((x) => x.id === projectId);
  if (!p) return;
  const d = IA.derive(st);
  Object.assign(p, {
    image_count: d.total, annotated_count: d.done, ready_count: d.ready_for_export,
    exported_count: d.exported, unassigned_count: d.unassigned, stats: st,
  });
  const el = grid.querySelector(`.project-card[data-id="${projectId}"]`);
  if (el) {
    IA.updateStateBar(el.querySelector('.statebar'), { ...st, total: d.total });
    el.querySelector('[data-pct]').textContent = `${d.pct}%`;
    el.querySelector('[data-counts]').textContent = `${IA.fmtNum(d.done)} / ${IA.fmtNum(d.total)}`;
  }
  if (user.is_admin) renderTotals();
}

// ── Interactions ──────────────────────────────────────────────────────────────
IA.on(grid, 'click', '.project-card', (e, el) => {
  const act = e.target.closest('[data-act]');
  if (act?.dataset.act === 'menu') {
    e.preventDefault();
    const p = projects.find((x) => x.id === el.dataset.id);
    IA.menu(act, [
      { label: 'Open dashboard', icon: 'layout-dashboard', fn: () => { location.href = `/projects/${p.id}`; } },
      { label: 'Annotate my images', icon: 'pencil', fn: () => { location.href = `/projects/${p.id}/view`; } },
      'sep',
      { label: 'Delete project…', icon: 'trash-2', danger: true, fn: () => deleteProject(p) },
    ]);
    return;
  }
  IA.heroTransition(el.querySelector('.pc-media'));
});
IA.on(grid, 'click', '[data-act="new"]', () => newProject());
document.getElementById('new-project-btn')?.addEventListener('click', () => newProject());
document.getElementById('history-btn')?.addEventListener('click', () => showHistory());

function newProject() {
  const m = IA.modal({
    title: 'New project',
    sub: 'You can import images and change classes later.',
    body: `
      <div class="field"><label for="np-name">Name</label><input class="input" id="np-name" placeholder="e.g. Site safety — helmets" autofocus/></div>
      <div class="field"><label for="np-classes">Classes</label><input class="input" id="np-classes" placeholder="helmet, vest, person"/>
        <span class="hint">Comma-separated. Order sets the YOLO class ids.</span></div>
      <div class="error" id="np-error"></div>`,
    actions: [
      { label: 'Cancel', variant: 'btn-ghost' },
      { id: 'create', label: 'Create project', variant: 'btn-primary', fn: async () => {
        const name = m.body.querySelector('#np-name').value.trim();
        const classes = m.body.querySelector('#np-classes').value.split(',').map((c) => c.trim()).filter(Boolean);
        const errEl = m.body.querySelector('#np-error');
        if (name.length < 2) { errEl.textContent = 'Give the project a name (at least 2 characters).'; return false; }
        if (!classes.length) { errEl.textContent = 'Add at least one class.'; return false; }
        try {
          const p = await IA.post('/api/projects', { name, classes });
          location.href = `/projects/${p.id}#import`;
        } catch (e) { errEl.textContent = e.message; return false; }
        return true;
      } },
    ],
  });
  m.body.querySelectorAll('input').forEach((inp) => inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') m.actions.create.click(); }));
}

async function deleteProject(p) {
  const ok = await IA.confirm({
    title: `Delete “${p.name}”?`,
    html: `<div class="callout danger">${IA.icon('circle-alert')}<div>Removes all ${IA.plural(p.image_count, 'image')} from the project, with their annotations and assignments, plus any frames this app extracted from videos. Original image folders and exported datasets are not touched.</div></div>`,
    confirmLabel: 'Delete project', danger: true, requireText: p.name,
  });
  if (!ok) return;
  try {
    await IA.del(`/api/projects/${p.id}`);
    IA.toast(`Deleted “${p.name}”`, 'success');
    await load();
  } catch (e) { IA.toast(e.message, 'error'); }
}

async function showHistory() {
  const m = IA.modal({ title: 'Project history', size: 'wide', body: '<div class="row"><span class="spinner"></span><span class="muted">Loading…</span></div>' });
  try {
    const rows = await IA.get('/api/projects/history');
    m.body.innerHTML = rows.length ? `
      <div class="table-wrap"><table class="table">
        <thead><tr><th>Project</th><th>Status</th><th>Created</th><th class="num">Images</th><th>Deleted</th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td class="strong">${IA.esc(r.name)}</td>
          <td>${r.date_deleted ? '<span class="chip danger">Deleted</span>' : '<span class="chip success">Active</span>'}</td>
          <td>${IA.esc(IA.fmtDate(r.date_created))}</td>
          <td class="num">${IA.fmtNum(r.dataset_size)}</td>
          <td class="muted">${r.date_deleted ? IA.esc(IA.fmtDate(r.date_deleted)) : '—'}</td></tr>`).join('')}</tbody>
      </table></div>` : '<p class="muted">No projects yet.</p>';
  } catch (e) { m.body.innerHTML = `<div class="callout danger">${IA.esc(e.message)}</div>`; }
}

// ── Live updates ──────────────────────────────────────────────────────────────
const socket = IA.connectSocket();
socket?.on('projects_changed', IA.debounce(load, 300));
socket?.on('project_stats_changed', ({ project_id, stats }) => applyStats(project_id, stats));

await load();
})();
