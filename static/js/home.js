/**
 * home.js — Home page logic: project card grid, new project modal,
 *            global stats, real-time updates via Socket.IO.
 * v2.0.0
 */

'use strict';

let _socket = null;
let _user   = null;
let _projects = [];

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  _user = IA.requireAuth(false);   // any authenticated user
  if (!_user) return;

  // Set greeting
  document.getElementById('greeting-name').textContent = _user.username;
  document.getElementById('sidebar-username').textContent = _user.username;

  // Show admin-only UI
  if (_user.is_admin) {
    document.getElementById('global-stats-bar').style.display = 'flex';
    document.getElementById('new-project-btn').style.display  = 'inline-flex';
    const historyBtn = document.getElementById('project-history-btn');
    if (historyBtn) historyBtn.style.display = 'inline-flex';
    document.getElementById('projects-section-heading').textContent = 'All Projects';
    // Set role badge
    const badge = document.getElementById('topbar-role-badge');
    if (badge) { badge.textContent = 'ADMIN'; badge.style.display = 'inline'; }
  } else {
    document.getElementById('home-subtitle').textContent =
      'Projects with images assigned to you appear here';
    document.getElementById('projects-section-heading').textContent = 'Your Projects';
  }

  await loadProjects();

  // Connect socket — no project_id (home page = global room only)
  _socket = IA.connectSocket(null, (sock) => {
    // Re-fetch project list whenever anything changes globally
    sock.on('projects_changed', () => loadProjects());
    // Stats changes may affect card progress bars
    sock.on('stats_update', (data) => {
      if (data._project_id) updateCardStats(data._project_id, data);
    });
  });
});

// ── Load and render projects ───────────────────────────────────────────────

async function loadProjects() {
  const res = await IA.api('/api/projects');
  if (!res) return;

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    IA.toast(IA.formatError(err), 'error');
    return;
  }

  _projects = await res.json();
  renderProjects(_projects);

  if (_user.is_admin) {
    renderGlobalStats(_projects);
  }
}

function renderProjects(projects) {
  const grid = document.getElementById('projects-grid');

  if (projects.length === 0) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1">
        <div class="empty-state-icon">${_user.is_admin ? '📁' : '📋'}</div>
        <div class="empty-state-title">${_user.is_admin ? 'No projects yet' : 'No projects assigned'}</div>
        <div class="empty-state-subtitle">
          ${_user.is_admin
            ? 'Click "New Project" to create your first annotation project.'
            : 'Ask your admin to assign images to you.'}
        </div>
        ${_user.is_admin
          ? `<button class="btn btn-primary" style="margin-top:20px" onclick="openNewProjectModal()">+ New Project</button>`
          : ''}
      </div>
    `;
    return;
  }

  grid.innerHTML = projects.map(p => buildProjectCard(p)).join('');
}

function buildProjectCard(p) {
  const total      = p.image_count || 0;
  const annotated  = p.annotated_count || 0;
  const pct        = total > 0 ? Math.round((annotated / total) * 100) : 0;
  const classCount = (p.classes || []).length;

  // Build the thumbnail or placeholder
  const thumbHtml = p.thumbnail_image_id
    ? `<img src="${IA.imgUrl(p.thumbnail_image_id)}" alt="Project thumbnail" loading="lazy" onerror="this.parentElement.innerHTML=placeholderHtml()">`
    : `<div class="card-thumb-placeholder">
         <div class="ph-icon">🗂️</div>
         <div class="ph-text">No images yet</div>
       </div>`;

  // Destination URL — admin goes to dashboard, annotator goes to project view
  const dest = _user.is_admin
    ? `/projects/${p.id}`
    : `/projects/${p.id}/view`;

  // Chip: annotated count
  const annotatedChip = `
    <span class="card-chip" style="border-color:rgba(255,153,64,0.3);color:var(--orange)">
      <span class="chip-dot" style="background:var(--orange)"></span>
      ${annotated} annotated
    </span>`;

  // Chip: ready count
  const readyChip = p.ready_count > 0 ? `
    <span class="card-chip" style="border-color:rgba(34,211,160,0.3);color:var(--green)">
      <span class="chip-dot" style="background:var(--green)"></span>
      ${p.ready_count} ready
    </span>` : '';

  // Chip: class count
  const classChip = `
    <span class="card-chip">
      🏷 ${classCount} class${classCount !== 1 ? 'es' : ''}
    </span>`;

  return `
    <div class="project-card" id="card-${p.id}" onclick="openProject('${p.id}', '${dest}')">
      <div class="card-thumb">
        ${thumbHtml}
        <div class="card-cta-overlay">
          <span class="card-cta-text">Open Project →</span>
        </div>
      </div>

      <div class="card-body">
        <div class="card-title" title="${escHtml(p.name)}">${escHtml(p.name)}</div>

        <div class="card-progress">
          <div class="card-progress-header">
            <span class="card-progress-label">${total} images</span>
            <span class="card-progress-pct" id="pct-${p.id}">${pct}%</span>
          </div>
          <div class="progress-bar">
            <div class="progress-fill" id="prog-${p.id}" style="width:${pct}%"></div>
          </div>
        </div>

        <div class="card-chips">
          ${annotatedChip}
          ${readyChip}
          ${classChip}
        </div>
      </div>

      <div class="card-footer">
        <span class="card-created">Created ${IA.fmtDate(p.created_at)}</span>
        <div style="display:flex; gap:6px; align-items:center;">
          ${_user.is_admin ? `
            <button class="card-delete-btn" title="Delete Project" onclick="event.stopPropagation(); confirmDeleteProject('${p.id}', '${escHtml(p.name)}')">
              🗑️
            </button>
          ` : ''}
          <button class="card-open-btn" onclick="event.stopPropagation(); openProject('${p.id}', '${dest}')">
            Open →
          </button>
        </div>
      </div>
    </div>
  `;
}

function openProject(projectId, dest) {
  window.location.href = dest;
}

function updateCardStats(projectId, stats) {
  // Called when we get a stats_update socket event for a specific project
  const total     = stats.total || 0;
  const annotated = (stats.annotated || 0) + (stats.ready_for_export || 0) + (stats.exported || 0);
  const pct       = total > 0 ? Math.round((annotated / total) * 100) : 0;

  const pctEl  = document.getElementById(`pct-${projectId}`);
  const progEl = document.getElementById(`prog-${projectId}`);
  if (pctEl)  pctEl.textContent = `${pct}%`;
  if (progEl) progEl.style.width = `${pct}%`;
}

function renderGlobalStats(projects) {
  const totalProjects  = projects.length;
  const totalImages    = projects.reduce((s, p) => s + (p.image_count || 0), 0);
  const totalAnnotated = projects.reduce((s, p) => s + (p.annotated_count || 0), 0);
  const totalExported  = projects.reduce((s, p) => s + (p.exported_count || 0), 0);

  document.getElementById('qs-projects').textContent  = totalProjects;
  document.getElementById('qs-images').textContent    = totalImages;
  document.getElementById('qs-annotated').textContent = totalAnnotated;
  document.getElementById('qs-exported').textContent  = totalExported;
}

// ── New Project Modal ─────────────────────────────────────────────────────────

function openNewProjectModal() {
  IA.openModal(
    'Create New Project',
    `
    <div class="new-project-modal-body">
      <div class="form-group">
        <label class="form-label" for="np-name">Project name</label>
        <input class="form-input" id="np-name" placeholder="e.g. Safety Equipment Detection" autofocus/>
      </div>
      <div class="form-group">
        <label class="form-label" for="np-classes">Classes <span style="color:var(--text-muted)">(comma-separated)</span></label>
        <input class="form-input" id="np-classes" placeholder="helmet, vest, person"/>
      </div>
      <div id="np-error" style="display:none;color:var(--red);font-size:12px;margin-top:-4px"></div>
    </div>
    `,
    [
      { label: 'Cancel',         cls: 'btn-ghost',   fn: IA.closeModal },
      { label: 'Create Project', cls: 'btn-primary',  fn: submitNewProject },
    ]
  );

  // Allow Enter key in modal inputs
  document.getElementById('np-name').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitNewProject();
  });
  document.getElementById('np-classes').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitNewProject();
  });
}

async function submitNewProject() {
  const name    = (document.getElementById('np-name').value || '').trim();
  const rawCls  = (document.getElementById('np-classes').value || '').trim();
  const errEl   = document.getElementById('np-error');

  errEl.style.display = 'none';

  if (!name) {
    errEl.textContent = 'Project name is required.';
    errEl.style.display = 'block';
    document.getElementById('np-name').focus();
    return;
  }

  const classes = rawCls.split(',').map(c => c.trim()).filter(Boolean);
  if (classes.length === 0) {
    errEl.textContent = 'At least one class is required.';
    errEl.style.display = 'block';
    document.getElementById('np-classes').focus();
    return;
  }

  // Disable button to prevent double-submit
  const modalActions = document.getElementById('modal-actions');
  const createBtn = modalActions ? modalActions.lastElementChild : null;
  if (createBtn) { createBtn.disabled = true; createBtn.textContent = 'Creating…'; }

  const res = await IA.api('/api/projects', {
    method: 'POST',
    body: JSON.stringify({ name, classes }),
  });

  if (!res) return;

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    errEl.textContent = IA.formatError(err);
    errEl.style.display = 'block';
    if (createBtn) { createBtn.disabled = false; createBtn.textContent = 'Create Project'; }
    return;
  }

  const project = await res.json();
  IA.closeModal();
  IA.toast(`Project "${project.name}" created!`, 'success');

  // Admins go directly to the new project's dashboard
  setTimeout(() => {
    window.location.href = `/projects/${project.id}`;
  }, 600);
}

// ── Utility ───────────────────────────────────────────────────────────────────

function placeholderHtml() {
  return `<div class="card-thumb-placeholder">
    <div class="ph-icon">🖼️</div>
    <div class="ph-text">Image unavailable</div>
  </div>`;
}

function escHtml(str) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(str || ''));
  return div.innerHTML;
}

// ── Project History & Deletion Modals ────────────────────────────────────────

async function openProjectHistoryModal() {
  const modalBox = document.getElementById('modal-box');
  if (modalBox) modalBox.classList.add('modal-wide');

  IA.openModal(
    'Project History Log',
    `<div class="text-muted text-sm" style="text-align:center; padding: 20px;">Loading history log…</div>`,
    [
      { label: 'Close', cls: 'btn-ghost', fn: closeProjectHistoryModal }
    ]
  );

  const res = await IA.api('/api/projects/history');
  if (!res) return;

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    document.getElementById('modal-body').innerHTML = `
      <div style="color:var(--red); font-size:13px; text-align:center; padding:20px;">
        Failed to load history: ${IA.formatError(err)}
      </div>
    `;
    return;
  }

  const history = await res.json();
  if (history.length === 0) {
    document.getElementById('modal-body').innerHTML = `
      <div class="text-muted text-sm" style="text-align:center; padding: 20px;">
        No project history found.
      </div>
    `;
    return;
  }

  const tableRows = history.map(item => {
    const isDeleted = !!item.date_deleted;
    const statusBadge = isDeleted 
      ? `<span class="badge" style="background:var(--red-d); color:var(--red);">Deleted</span>` 
      : `<span class="badge" style="background:var(--green-d); color:var(--green);">Active</span>`;
    
    const createdStr = IA.fmtDate(item.date_created);
    const deletedStr = isDeleted ? IA.fmtDate(item.date_deleted) : '<span style="color:var(--text-muted); font-style:italic;">—</span>';
    
    return `
      <tr class="${isDeleted ? 'history-deleted-row' : ''}">
        <td>${statusBadge}</td>
        <td style="font-weight:600; color:var(--text-primary);" title="${escHtml(item.name)}">${escHtml(item.name)}</td>
        <td>${createdStr}</td>
        <td style="font-variant-numeric: tabular-nums;">${item.dataset_size} frame${item.dataset_size !== 1 ? 's' : ''}</td>
        <td>${deletedStr}</td>
      </tr>
    `;
  }).join('');

  document.getElementById('modal-body').innerHTML = `
    <div class="history-table-container">
      <table class="history-table">
        <thead>
          <tr>
            <th>Status</th>
            <th>Project Name</th>
            <th>Created At</th>
            <th>Size</th>
            <th>Deleted At</th>
          </tr>
        </thead>
        <tbody>
          ${tableRows}
        </tbody>
      </table>
    </div>
  `;
}

function closeProjectHistoryModal() {
  const modalBox = document.getElementById('modal-box');
  if (modalBox) modalBox.classList.remove('modal-wide');
  IA.closeModal();
}

async function confirmDeleteProject(projectId, projectName) {
  IA.openModal(
    'Delete Project',
    `
    <div style="display:flex; flex-direction:column; gap:12px; min-width:320px;">
      <p style="color:var(--text-secondary); font-size:13px; line-height:1.5;">
        Are you sure you want to delete the project <strong>"${escHtml(projectName)}"</strong>?
      </p>
      <div style="background:rgba(255, 95, 126, 0.08); border-left: 3px solid var(--red); padding: 10px 12px; border-radius: var(--radius-sm); font-size: 12.5px; color: var(--red); line-height:1.5;">
        ⚠️ This will cascade delete all images, annotations, assignments, and extracted video frames.
        <br/>
        <strong>Exported YOLO datasets will NOT be deleted.</strong>
      </div>
      <div class="form-group" style="margin-top:8px;">
        <label class="form-label" style="font-size:11px; text-transform:none;">
          To confirm, type the project name below:
        </label>
        <input class="form-input" id="confirm-delete-name" placeholder="${escHtml(projectName)}" autocomplete="off" style="margin-top:4px;"/>
      </div>
      <div id="delete-error" style="display:none; color:var(--red); font-size:12px; margin-top:4px;"></div>
    </div>
    `,
    [
      { label: 'Cancel', cls: 'btn-ghost', fn: IA.closeModal },
      { label: 'Delete Project', cls: 'btn-danger', fn: () => submitDeleteProject(projectId, projectName) }
    ]
  );

  setTimeout(() => {
    const input = document.getElementById('confirm-delete-name');
    if (input) {
      input.focus();
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') submitDeleteProject(projectId, projectName);
      });
    }
  }, 100);
}

async function submitDeleteProject(projectId, projectName) {
  const inputVal = (document.getElementById('confirm-delete-name').value || '').trim();
  const errEl = document.getElementById('delete-error');
  if (!errEl) return;
  errEl.style.display = 'none';

  if (inputVal !== projectName) {
    errEl.textContent = 'Project name does not match.';
    errEl.style.display = 'block';
    return;
  }

  // Disable submit button
  const modalActions = document.getElementById('modal-actions');
  const deleteBtn = modalActions ? modalActions.lastElementChild : null;
  if (deleteBtn) {
    deleteBtn.disabled = true;
    deleteBtn.textContent = 'Deleting…';
  }

  const res = await IA.api(`/api/projects/${projectId}`, {
    method: 'DELETE'
  });

  if (!res) return;

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    errEl.textContent = IA.formatError(err);
    errEl.style.display = 'block';
    if (deleteBtn) {
      deleteBtn.disabled = false;
      deleteBtn.textContent = 'Delete Project';
    }
    return;
  }

  IA.closeModal();
  IA.toast(`Project "${projectName}" successfully deleted.`, 'success');
  await loadProjects();
}
