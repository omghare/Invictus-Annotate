/**
 * project_view.js — Annotator project view.
 * v2.0.0 — Multi-project, filter toggles, paginated image loading.
 */

'use strict';

let _socket     = null;
let _user       = null;
let _projectId  = null;
let _project    = null;

// Pagination / filter state
let _currentFilter  = 'all';      // all | unannotated | annotated | ready_for_export
let _currentPage    = 1;
let _perPage        = parseInt(localStorage.getItem('ia_per_page') || '50', 10);
let _totalPages     = 1;
let _totalImages    = 0;

// Filter → state param mapping (null means no state filter)
const FILTER_STATE = {
  all:              null,
  unannotated:      'unannotated',
  annotated:        'annotated',
  ready_for_export: 'ready_for_export',
};

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  _user = IA.requireAuth(false);
  if (!_user) return;

  // Extract project ID from URL: /projects/{id}/view
  const parts = window.location.pathname.split('/');
  _projectId = parts[2] || null;

  if (!_projectId) {
    IA.toast('No project selected', 'error');
    setTimeout(() => { window.location.href = '/home'; }, 1500);
    return;
  }

  // Validate per_page
  if (![50, 100, 200].includes(_perPage)) _perPage = 50;

  document.getElementById('username-display').textContent = _user.username;

  // Highlight active per_page button
  syncPerPageButtons();

  // Load project info + images
  await loadProject();
  await loadStats();
  await loadImages();

  // Connect socket with project_id for real-time updates
  _socket = IA.connectSocket(_projectId, (sock) => {
    sock.on('stats_update',        onStatsUpdate);
    sock.on('image_state_change',  onStateChange);
    sock.on('image_deleted',       onImageDeleted);
    sock.on('project_updated',     onProjectUpdated);
    // If project is deleted while we're on it, redirect home
    sock.on('projects_changed',    checkProjectStillExists);
  });
});

// ── Project loading ───────────────────────────────────────────────────────────

async function loadProject() {
  const res = await IA.api(`/api/projects/${_projectId}`);
  if (!res) return;
  if (!res.ok) {
    IA.toast('Project not found — returning to Home', 'error');
    setTimeout(() => { window.location.href = '/home'; }, 1500);
    return;
  }
  _project = await res.json();

  document.title = `${_project.name} — Invictus Annotate`;
  document.getElementById('project-name-display').textContent = _project.name;
  document.getElementById('bc-project-name').textContent = _project.name;
  document.getElementById('project-subtitle').textContent =
    `${(_project.classes || []).length} classes · Your assigned images are shown below.`;

  // Populate left sidebar navigation
  const sideLinks = document.getElementById('pv-sidebar-links');
  if (sideLinks) {
    if (_user && _user.is_admin) {
      sideLinks.innerHTML = `
        <div class="sidebar-section-label">Project</div>
        <a class="nav-item" href="/projects/${_projectId}#overview">
          <span class="nav-icon">📊</span> Overview
        </a>
        <a class="nav-item" href="/projects/${_projectId}#import">
          <span class="nav-icon">📁</span> Import Images
        </a>
        <a class="nav-item" href="/projects/${_projectId}#assign">
          <span class="nav-icon">👤</span> Assign
        </a>
        <a class="nav-item" href="/projects/${_projectId}#review">
          <span class="nav-icon">✅</span> Review Queue
        </a>
        <a class="nav-item" href="/projects/${_projectId}#all-images">
          <span class="nav-icon">🖼️</span> All Images
        </a>
        <a class="nav-item active" href="/projects/${_projectId}/view">
          <span class="nav-icon">✏️</span> My Annotations
        </a>

        <div class="sidebar-section-label">Admin</div>
        <a class="nav-item" href="/projects/${_projectId}#users">
          <span class="nav-icon">👥</span> Users
        </a>
        <a class="nav-item" href="/projects/${_projectId}#classes">
          <span class="nav-icon">🏷</span> Classes
        </a>
        <a class="nav-item" href="/projects/${_projectId}#dataset">
          <span class="nav-icon">📦</span> Dataset
        </a>
        <a class="nav-item" href="/train" target="_blank" style="background:rgba(34,211,160,0.06);border:1px dashed rgba(34,211,160,0.3);">
          <span class="nav-icon">🧠</span> Train Model
        </a>
      `;
    } else {
      sideLinks.innerHTML = `
        <div class="sidebar-section-label">Project</div>
        <a class="nav-item active" href="/projects/${_projectId}/view">
          <span class="nav-icon">✏️</span> My Annotations
        </a>
      `;
    }
  }

  const sideUser = document.getElementById('pv-sidebar-user');
  if (sideUser) sideUser.textContent = _user ? _user.username : '';

  const sideProj = document.getElementById('pv-sidebar-project');
  if (sideProj) sideProj.textContent = _project.name;

  // Render backlink to Admin Dashboard if admin
  if (_user && _user.is_admin) {
    const breadcrumb = document.querySelector('.breadcrumb');
    if (breadcrumb && !document.getElementById('bc-admin-link')) {
      const sep = document.createElement('span');
      sep.className = 'bc-sep';
      sep.textContent = '/';
      const link = document.createElement('a');
      link.href = `/projects/${_projectId}`;
      link.id = 'bc-admin-link';
      link.textContent = '⚙️ Admin Dashboard';
      breadcrumb.appendChild(sep);
      breadcrumb.appendChild(link);
    }
  }
}

// ── Stats loading ─────────────────────────────────────────────────────────────

async function loadStats() {
  const res = await IA.api(`/api/projects/${_projectId}/images/stats?user_id=${_user.id}`);
  if (!res || !res.ok) return;
  const stats = await res.json();
  applyStats(stats);
}

function applyStats(stats) {
  const safe = (v) => (v === undefined || v === null ? '—' : v);

  document.getElementById('stat-total').textContent      = safe(stats.total);
  document.getElementById('stat-unannotated').textContent= safe(stats.unannotated);
  document.getElementById('stat-annotated').textContent  = safe(stats.annotated);
  document.getElementById('stat-ready').textContent      = safe(stats.ready_for_export);

  // "Assigned to Me" = sum of assigned + unannotated + annotated + ready (user's workload)
  const myTotal = (stats.assigned || 0)
                + (stats.unannotated || 0)
                + (stats.annotated || 0)
                + (stats.ready_for_export || 0);
  document.getElementById('stat-assigned').textContent = myTotal;

  // Update filter count badges
  document.getElementById('fc-unannotated').textContent = stats.unannotated || 0;
  document.getElementById('fc-annotated').textContent   = stats.annotated || 0;
  document.getElementById('fc-ready').textContent       = stats.ready_for_export || 0;

  // Show/hide submit-all button
  const submitAllBtn = document.getElementById('project-submit-all-btn');
  if (submitAllBtn) {
    submitAllBtn.style.display = (stats.annotated > 0) ? 'inline-flex' : 'none';
  }
}

// ── Image loading (paginated + filtered) ──────────────────────────────────────

async function loadImages() {
  const grid = document.getElementById('img-grid');
  const paginationBar = document.getElementById('pagination-bar');

  // Show spinner
  grid.innerHTML = `
    <div style="color:var(--text-muted);font-size:13px;grid-column:1/-1;padding:40px 0;text-align:center">
      <div class="spinner" style="margin:0 auto 12px"></div>
      Loading images…
    </div>`;

  const stateParam = FILTER_STATE[_currentFilter];
  const params = new URLSearchParams({
    page:     _currentPage,
    per_page: _perPage,
    assigned_to: _user.id,
  });
  if (stateParam) params.set('state', stateParam);

  const res = await IA.api(`/api/projects/${_projectId}/images?${params}`);
  if (!res) return;

  if (!res.ok) {
    grid.innerHTML = `<div class="text-muted" style="grid-column:1/-1;padding:40px;text-align:center">Failed to load images.</div>`;
    return;
  }

  const data = await res.json();   // PaginatedImages
  const { items, total, page, per_page, total_pages } = data;

  _totalImages = total;
  _totalPages  = total_pages;
  _currentPage = page;

  // Update count label
  const start = total === 0 ? 0 : (page - 1) * per_page + 1;
  const end   = Math.min(page * per_page, total);
  const countLabel = document.getElementById('img-count-label');
  if (countLabel) {
    countLabel.textContent = total === 0 ? '' : `Showing ${start}–${end} of ${total}`;
  }

  // Update fc-all
  const fcAll = document.getElementById('fc-all');
  if (fcAll) fcAll.textContent = total;

  // Render cards
  if (items.length === 0) {
    const emptyMessages = {
      all:              { icon: '📂', msg: 'No images assigned to you', sub: 'Ask your admin to assign some images.' },
      unannotated:      { icon: '🟡', msg: 'No unannotated images',     sub: 'All your images have been annotated!' },
      annotated:        { icon: '🟠', msg: 'No annotated images',        sub: 'Start annotating to see them here.' },
      ready_for_export: { icon: '🟢', msg: 'No images in ready queue',   sub: 'Submit annotated images to add them.' },
    };
    const em = emptyMessages[_currentFilter] || emptyMessages.all;
    grid.innerHTML = `
      <div class="filter-empty">
        <div class="fe-icon">${em.icon}</div>
        <div class="fe-msg">${em.msg}</div>
        <div class="fe-sub">${em.sub}</div>
      </div>`;
    paginationBar.style.display = 'none';
    return;
  }

  grid.innerHTML = items.map(img => buildImageCard(img)).join('');

  // Pagination controls
  paginationBar.style.display = 'flex';
  document.getElementById('page-current').textContent = _currentPage;
  document.getElementById('page-total').textContent   = _totalPages;
  document.getElementById('total-count').textContent  = _totalImages;
  document.getElementById('btn-prev').disabled = _currentPage <= 1;
  document.getElementById('btn-next').disabled = _currentPage >= _totalPages;
}

function buildImageCard(img) {
  const dest = `/projects/${_projectId}/annotate/${img.id}`;
  return `
    <a class="pv-img-card" href="${dest}">
      <div class="card-thumb">
        <img src="${IA.imgUrl(img.id)}" alt="${escHtml(img.filename)}" loading="lazy"/>
        <div class="state-badge-overlay">
          ${IA.stateBadge(img.state)}
        </div>
      </div>
      <div class="card-footer">
        <div class="card-name">${escHtml(img.filename)}</div>
      </div>
    </a>
  `;
}

// ── Filter controls ───────────────────────────────────────────────────────────

function setFilter(filter) {
  if (_currentFilter === filter) return;
  _currentFilter = filter;
  _currentPage   = 1;

  // Update active button
  document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
  const btnEl = document.getElementById(`fb-${filter}`);
  if (btnEl) btnEl.classList.add('active');

  loadImages();
}

// ── Pagination controls ───────────────────────────────────────────────────────

function goPage(delta) {
  const newPage = _currentPage + delta;
  if (newPage < 1 || newPage > _totalPages) return;
  _currentPage = newPage;
  loadImages();
  // Scroll to top of grid
  document.getElementById('img-grid').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function setPerPage(n) {
  if (_perPage === n) return;
  _perPage     = n;
  _currentPage = 1;
  localStorage.setItem('ia_per_page', n);
  syncPerPageButtons();
  loadImages();
}

function syncPerPageButtons() {
  [50, 100, 200].forEach(n => {
    const btn = document.getElementById(`pp-${n}`);
    if (btn) btn.classList.toggle('active', _perPage === n);
  });
}

// ── Submit all annotated ──────────────────────────────────────────────────────

async function submitAllAnnotated() {
  const btn = document.getElementById('project-submit-all-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }

  const res = await IA.api(`/api/projects/${_projectId}/images/submit-all-annotated`, {
    method: 'POST',
  });

  if (btn) { btn.disabled = false; btn.textContent = '✓ Add All Annotated to Dataset'; }

  if (!res || !res.ok) {
    const err = await res?.json().catch(() => ({}));
    IA.toast(IA.formatError(err), 'error');
    return;
  }

  const result = await res.json();
  IA.toast(result.detail, 'success');
  await loadStats();
  await loadImages();
}

// ── Socket.IO event handlers ──────────────────────────────────────────────────

function onStatsUpdate(stats) {
  loadStats();
}

function onStateChange(data) {
  // Update the badge on the card if it's currently visible
  const card = document.querySelector(`a[href="/projects/${_projectId}/annotate/${data.image_id}"]`);
  if (card) {
    const badgeEl = card.querySelector('.state-badge-overlay');
    if (badgeEl) badgeEl.innerHTML = IA.stateBadge(data.new_state);
  }

  // If filtering by a specific state, reload to keep the list accurate
  if (_currentFilter !== 'all') {
    loadImages();
  }
}

function onImageDeleted(data) {
  // Remove the card if visible
  const card = document.querySelector(`a[href="/projects/${_projectId}/annotate/${data.image_id}"]`);
  if (card) {
    card.style.transition = 'opacity 300ms, transform 300ms';
    card.style.opacity    = '0';
    card.style.transform  = 'scale(0.9)';
    setTimeout(() => { card.remove(); }, 300);
  }
}

function onProjectUpdated(data) {
  if (_project && data.classes) {
    _project.classes = data.classes;
    IA.toast('Project classes updated by admin', 'info');
  }
}

async function checkProjectStillExists() {
  // Called when projects_changed fires — verify this project still exists
  const res = await fetch(`/api/projects/${_projectId}`, {
    headers: { 'Authorization': `Bearer ${IA.getToken()}` }
  });
  if (res.status === 404) {
    IA.toast('This project was deleted by an admin', 'error');
    setTimeout(() => { window.location.href = '/home'; }, 2000);
  }
}

// ── Utility ───────────────────────────────────────────────────────────────────

function escHtml(str) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(str || ''));
  return div.innerHTML;
}
