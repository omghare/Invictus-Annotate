/**
 * dashboard.js — Admin dashboard logic (multi-project aware).
 * v2.0.0 — All API calls scoped to /api/projects/{project_id}/...
 * Project ID is extracted from the URL: /projects/{id}
 */
(() => {
'use strict';

let user       = null;
let project    = null;
let projectId  = null;
let allImages  = [];
let users      = [];
let classes    = [];
let selectedImageIds = new Set();
let unassignedCount = 0;

// ── Init ──────────────────────────────────────────────────────────────

async function init() {
  user = IA.requireAuth(true);
  if (!user) return;

  // Extract project ID from URL: /projects/{id}
  const parts = window.location.pathname.split('/');
  projectId = parts[2] || null;

  if (!projectId) {
    IA.toast('No project selected — returning to Home', 'error');
    setTimeout(() => { window.location.href = '/home'; }, 1500);
    return;
  }

  await loadProject();
  await loadStats();
  await loadUsers();
  initSocket();

  const hashSection = window.location.hash.replace('#', '');
  const validSections = ['overview', 'import', 'assign', 'review', 'all-images', 'users', 'classes', 'dataset'];
  const startSection = validSections.includes(hashSection) ? hashSection : 'overview';
  showSection(startSection);

  window.addEventListener('hashchange', () => {
    const s = window.location.hash.replace('#', '');
    if (validSections.includes(s)) {
      showSection(s);
    }
  });
}

// ── Section routing ───────────────────────────────────────────────────

window.showSection = (name) => {
  document.querySelectorAll('.dash-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(`section-${name}`)?.classList.add('active');
  document.getElementById(`nav-${name}`)?.classList.add('active');

  // Lazy load section data
  if (name === 'assign')  loadAssignSection();
  if (name === 'import')  loadImportSection();
  if (name === 'review')  loadReviewSection();
  if (name === 'users')   renderUsersTable();
  if (name === 'classes') renderClassesEditor();
  if (name === 'dataset') { loadExportPreview(); buildYamlPreview(); loadExportedImages(); }
  if (name === 'all-images') loadAllImagesSection();
};

// ── Project ───────────────────────────────────────────────────────────

async function loadProject() {
  const res = await IA.api(`/api/projects/${projectId}`);
  if (res && res.ok) {
    project = await res.json();
    classes = project.classes;

    // Update breadcrumbs in all sections
    document.querySelectorAll('[id^="bc-project-name"]').forEach(el => {
      el.textContent = project.name;
    });

    document.getElementById('project-name-side').textContent = project.name;
    document.getElementById('source-folder-side').textContent = project.source_folder || 'No folder imported yet';
    document.getElementById('project-label').textContent = project.name;
    document.title = `${project.name} — Admin`;
  } else {
    // Project not found — go home
    IA.toast('Project not found — returning to Home', 'error');
    setTimeout(() => { window.location.href = '/home'; }, 1500);
  }
}

// ── Stats ─────────────────────────────────────────────────────────────

async function loadStats() {
  const res = await IA.api(`/api/projects/${projectId}/images/stats`);
  if (!res || !res.ok) return;
  const d = await res.json();
  updateStats(d);
}

function updateStats(d) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set('s-total',      d.total);
  set('s-unassigned', d.unassigned);
  unassignedCount = d.unassigned;
  if (typeof updateDistributionPreview === 'function') updateDistributionPreview();
  set('s-assigned',   d.assigned);
  set('s-unannotated',d.unannotated);
  set('s-annotated',  d.annotated);
  set('s-ready',      d.ready_for_export);
  set('s-exported',   d.exported);

  // Sidebar badges
  set('nav-unassigned-cnt', d.unassigned);
  set('nav-ready-cnt',      d.ready_for_export);
  set('ready-badge',        `${d.ready_for_export} ready`);
}

// ── Live feed ─────────────────────────────────────────────────────────

function addFeedItem(msg) {
  const list = document.getElementById('feed-list');
  if (!list) return;
  const placeholder = list.querySelector('.text-muted');
  if (placeholder) placeholder.remove();

  const item = document.createElement('div');
  item.className = 'feed-item';
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  item.innerHTML = `<div class="feed-dot"></div><div class="feed-time">${time}</div><div class="feed-msg">${msg}</div>`;
  list.insertBefore(item, list.firstChild);

  while (list.children.length > 50) list.lastChild.remove();
}

// ── Import ────────────────────────────────────────────────────────────

async function loadImportSection() {
  const res = await IA.api(`/api/projects/${projectId}/images?per_page=200`);
  if (!res || !res.ok) return;
  const data = await res.json();
  allImages = data.items;
  renderImportGrid(allImages);
  document.getElementById('imported-count').textContent = `${data.total} images`;
}

function renderImportGrid(imgs) {
  const grid = document.getElementById('import-image-grid');
  if (!grid) return;
  if (imgs.length === 0) {
    grid.innerHTML = '<div class="text-muted text-sm" style="padding:20px;grid-column:1/-1">No images yet. Import a folder above.</div>';
    return;
  }
  grid.innerHTML = imgs.map(img => `
    <div class="img-thumb" title="${img.filename}" style="cursor:default">
      <img src="${IA.imgUrl(img.id)}" alt="${img.filename}" loading="lazy" onerror="this.style.display='none'"/>
      <div class="img-state-dot" style="background:${IA.stateColor(img.state)}"></div>
    </div>
  `).join('');
}

window.importImages = async () => {
  if (!project) { IA.toast('Project not loaded', 'error'); return; }
  const pathInput = document.getElementById('import-folder-path');
  const folder_path = pathInput ? pathInput.value.trim() : '';
  const btn = document.getElementById('import-btn');
  btn.disabled = true;

  btn.textContent = folder_path ? '⏳ Importing images…' : '⏳ Opening folder dialog…';

  try {
    const res = await IA.api(`/api/projects/${projectId}/import`, {
      method: 'POST',
      body: JSON.stringify({ folder_path })
    });
    if (res && res.ok) {
      const data = await res.json();
      const resultEl = document.getElementById('import-result');
      resultEl.classList.remove('hidden');
      resultEl.innerHTML = `<div style="background:var(--green-d);border:1px solid rgba(34,211,160,0.3);border-radius:var(--radius);padding:12px 16px;font-size:13px;color:var(--green)">
        ✓ Imported <strong>${data.imported}</strong> images from <code>${data.folder}</code>.
        ${data.skipped > 0 ? `<br>${data.skipped} skipped (already imported).` : ''}
      </div>`;
      if (pathInput) pathInput.value = '';
      await loadStats();
      await loadImportSection();
    } else {
      const err = await res?.json();
      IA.toast(IA.formatError(err), 'error');
    }
  } catch(e) {
    IA.toast('Network error', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '📁 Import Images';
  }
};

// ── Video Import ────────────────────────────────────────────────────────

window.openVideoImportModal = () => {
  const body = `
    <div style="display:flex; flex-direction:column; gap:16px;">
      <p style="color:var(--text-secondary);font-size:13px;margin:0">
        Select a video file to losslessly extract frames from. The frames will be extracted as PNGs to preserve maximum quality, then automatically imported into your dataset in chronological order.
      </p>
      <div style="display:flex;gap:10px;align-items:center">
        <input class="form-input" id="vi-path" placeholder="Click 'Select Video'..." readonly style="flex:1"/>
        <button class="btn btn-secondary" onclick="pickVideoFile()" style="flex-shrink:0">📁 Select Video</button>
      </div>
      <div>
        <label class="form-label" style="display:inline-block; margin-right:10px;">Extraction FPS:</label>
        <input type="number" id="vi-fps" class="form-input" value="5" min="0.1" step="0.1" style="width:80px; display:inline-block;"/>
      </div>
      <div id="vi-status" class="hidden" style="font-size:13px; padding:10px; border-radius:4px;"></div>
    </div>
  `;

  IA.openModal('Import Video to Dataset', body, [
    { label: 'Cancel', cls: 'btn-ghost', fn: IA.closeModal },
    { label: 'Extract & Import', cls: 'btn-primary', fn: processAndImportVideo }
  ]);
};

window.pickVideoFile = async () => {
  const pathInput = document.getElementById('vi-path');
  const btn = document.querySelector('button[onclick="pickVideoFile()"]');
  const oldText = btn.textContent;
  btn.textContent = '⏳ ...';
  btn.disabled = true;

  try {
    const res = await IA.api('/api/video/pick_import');
    if (res && res.ok) {
      const data = await res.json();
      pathInput.value = data.filepath;
    } else {
      const err = await res?.json();
      IA.toast(IA.formatError(err) || 'No file selected', 'error');
    }
  } catch (e) {
    IA.toast('Error opening file dialog', 'error');
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
};

window.processAndImportVideo = async () => {
  const filepath = document.getElementById('vi-path').value.trim();
  const fps = parseFloat(document.getElementById('vi-fps').value);
  const statusEl = document.getElementById('vi-status');

  if (!filepath) {
    IA.toast('Please select a video file first', 'error');
    return;
  }
  if (isNaN(fps) || fps <= 0) {
    IA.toast('Please enter a valid FPS greater than 0', 'error');
    return;
  }

  // Disable actions
  const actions = document.getElementById('modal-actions').querySelectorAll('button');
  actions.forEach(b => b.disabled = true);

  statusEl.classList.remove('hidden');
  statusEl.style.background = 'var(--blue-d)';
  statusEl.style.color = 'var(--blue)';
  statusEl.style.border = '1px solid rgba(0, 122, 204, 0.3)';
  statusEl.innerHTML = '⏳ Extracting frames losslessly... This may take a while depending on video length and FPS.';

  try {
    // 1. Extract Frames
    const extractRes = await IA.api('/api/video/extract_frames', {
      method: 'POST',
      body: JSON.stringify({ filepath, fps, project_id: projectId })
    });
    
    if (!extractRes || !extractRes.ok) {
      const err = await extractRes?.json();
      throw new Error(IA.formatError(err) || 'Extraction failed');
    }
    const extractData = await extractRes.json();
    const folder_path = extractData.folder_path;

    statusEl.innerHTML = '⏳ Extraction complete! Importing frames into dataset...';

    // 2. Import Images from the new folder
    const importRes = await IA.api(`/api/projects/${projectId}/import`, {
      method: 'POST',
      body: JSON.stringify({ folder_path })
    });

    if (importRes && importRes.ok) {
      const importData = await importRes.json();
      statusEl.style.background = 'var(--green-d)';
      statusEl.style.color = 'var(--green)';
      statusEl.style.border = '1px solid rgba(34, 211, 160, 0.3)';
      statusEl.innerHTML = `✓ Success! Imported <strong>${importData.imported}</strong> frames into the dataset.`;
      
      // Update UI
      await loadStats();
      await loadImportSection();
      
      // Close modal after 2 seconds
      setTimeout(() => IA.closeModal(), 2000);
    } else {
      const err = await importRes?.json();
      throw new Error(IA.formatError(err) || 'Import failed');
    }
  } catch (e) {
    statusEl.style.background = 'var(--red-d)';
    statusEl.style.color = 'var(--red)';
    statusEl.style.border = '1px solid rgba(239, 68, 68, 0.3)';
    statusEl.innerHTML = `❌ Error: ${e.message}`;
  } finally {
    // Re-enable close button only
    actions[0].disabled = false;
  }
};

// ── Assign ────────────────────────────────────────────────────────────

async function loadAssignSection() {
  const res = await IA.api(`/api/projects/${projectId}/images?state=unassigned&per_page=200`);
  if (!res || !res.ok) return;
  const data = await res.json();

  // Fetch workloads
  const wlRes = await IA.api(`/api/projects/${projectId}/user-workloads`);
  const workloads = (wlRes && wlRes.ok) ? await wlRes.json() : {};

  renderAssignGrid(data.items);
  populateUserSelect();
  renderAssignUsers(workloads);
  updateDistributionPreview();
}

function renderAssignGrid(imgs) {
  const grid = document.getElementById('assign-image-grid');
  selectedImageIds.clear();
  updateSelectedCount();

  if (imgs.length === 0) {
    grid.innerHTML = '<div class="text-muted text-sm" style="padding:20px;grid-column:1/-1;text-align:center">No unassigned images.</div>';
    return;
  }
  grid.innerHTML = imgs.map(img => `
    <div class="img-thumb" data-id="${img.id}" onclick="toggleSelect('${img.id}', this)">
      <input type="checkbox" class="img-check" onclick="event.stopPropagation()"/>
      <img src="${IA.imgUrl(img.id)}" alt="${img.filename}" loading="lazy" onerror="this.style.display='none'"/>
    </div>
  `).join('');
}

window.toggleSelect = (id, el) => {
  if (selectedImageIds.has(id)) {
    selectedImageIds.delete(id);
    el.classList.remove('selected');
    el.querySelector('.img-check').checked = false;
  } else {
    selectedImageIds.add(id);
    el.classList.add('selected');
    el.querySelector('.img-check').checked = true;
  }
  updateSelectedCount();
};
window.selectAll  = () => { document.querySelectorAll('#assign-image-grid .img-thumb').forEach(el => { const id = el.dataset.id; selectedImageIds.add(id); el.classList.add('selected'); el.querySelector('.img-check').checked = true; }); updateSelectedCount(); };
window.selectNone = () => { document.querySelectorAll('#assign-image-grid .img-thumb').forEach(el => { selectedImageIds.delete(el.dataset.id); el.classList.remove('selected'); el.querySelector('.img-check').checked = false; }); selectedImageIds.clear(); updateSelectedCount(); };

function updateSelectedCount() {
  const el = document.getElementById('selected-count');
  if (el) el.textContent = `${selectedImageIds.size} selected`;
  updateDistributionPreview();
}

function populateUserSelect() {
  const sel = document.getElementById('assign-user-select');
  if (!sel) return;
  const currentUserId = user ? user.id : null;
  sel.innerHTML = '<option value="">Select user…</option>' +
    users.map(u => {
      // Auto-select if single user or matching current user
      const isSelected = (users.length === 1 || u.id === currentUserId);
      return `<option value="${u.id}" ${isSelected ? 'selected' : ''}>${u.username}${u.is_admin ? ' (Admin)' : ''}</option>`;
    }).join('');
}

window.assignSelected = async () => {
  let uid = document.getElementById('assign-user-select').value;
  // If no user selected, fallback to current logged in admin user
  if (!uid && user && user.id) uid = user.id;

  if (!uid)                     { IA.toast('Select a user first', 'error'); return; }
  if (selectedImageIds.size===0){ IA.toast('Select images first', 'error'); return; }

  const res = await IA.api(`/api/projects/${projectId}/images/assign`, {
    method: 'POST',
    body: JSON.stringify({ image_ids: [...selectedImageIds], user_id: uid }),
  });
  if (res && res.ok) {
    IA.toast(`${selectedImageIds.size} images assigned ✓`, 'success');
    await loadAssignSection();
    await loadStats();
  } else {
    const e = await res?.json();
    IA.toast(IA.formatError(e), 'error');
  }
};

window.assignToMe = async () => {
  if (!user || !user.id) { IA.toast('User context missing', 'error'); return; }
  const uid = user.id;

  // Use selected images if any, otherwise assign all unassigned images
  let targetImageIds = [...selectedImageIds];
  if (targetImageIds.length === 0) {
    const resUnassigned = await IA.api(`/api/projects/${projectId}/images?state=unassigned&per_page=200`);
    if (resUnassigned && resUnassigned.ok) {
      const data = await resUnassigned.json();
      targetImageIds = (data.items || []).map(img => img.id);
    }
  }

  if (targetImageIds.length === 0) {
    IA.toast('No unassigned images to assign', 'error');
    return;
  }

  const res = await IA.api(`/api/projects/${projectId}/images/assign`, {
    method: 'POST',
    body: JSON.stringify({ image_ids: targetImageIds, user_id: uid }),
  });
  if (res && res.ok) {
    IA.toast(`${targetImageIds.length} images assigned to ${user.username} ✓`, 'success');
    selectedImageIds.clear();
    await loadAssignSection();
    await loadStats();
  } else {
    const e = await res?.json();
    IA.toast(IA.formatError(e), 'error');
  }
};

function renderAssignUsers(workloads) {
  const container = document.getElementById('assign-users-list');
  if (!container) return;

  if (users.length === 0) {
    container.innerHTML = '<div class="text-muted text-sm" style="padding:10px;text-align:center">No users available.</div>';
    return;
  }

  container.innerHTML = users.map(u => {
    const wl = workloads[u.id] || { assigned: 0, annotated: 0, ready: 0, total: 0 };
    const initials = u.username.slice(0, 2).toUpperCase();
    
    // Check all users by default so single admin or selected users are ready for distribution
    const checkedAttr = 'checked';
    
    return `
      <label class="assign-user-item" style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);cursor:pointer;user-select:none;transition:border-color var(--t-fast);">
        <div style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" class="user-check" data-id="${u.id}" onchange="updateDistributionPreview()" ${checkedAttr} style="accent-color:var(--accent)"/>
          <span class="user-avatar" style="width:24px;height:24px;font-size:10px;display:inline-flex;align-items:center;justify-content:center;background:var(--accent-dim);color:var(--accent-light);border-radius:50%;font-weight:700;">${initials}</span>
          <span style="font-weight:500;">${u.username}</span>
          ${u.is_admin ? '<span class="badge badge-ready" style="font-size:9px;padding:1px 4px;">Admin</span>' : '<span class="badge badge-assigned" style="font-size:9px;padding:1px 4px;">Annotator</span>'}
        </div>
        <div style="font-size:11px;color:var(--text-secondary)">
          <span>${wl.total}</span> assigned
        </div>
      </label>
    `;
  }).join('');
}

window.updateDistributionPreview = () => {
  const distUnassigned = document.getElementById('dist-unassigned-cnt');
  const distSelected = document.getElementById('dist-selected-cnt');
  const distUsers = document.getElementById('dist-users-cnt');
  const distPreviewMsg = document.getElementById('dist-preview-msg');
  const btnAutoDistribute = document.getElementById('btn-auto-distribute');
  
  if (!distUnassigned || !distSelected || !distUsers || !distPreviewMsg || !btnAutoDistribute) return;
  
  distUnassigned.textContent = unassignedCount;
  distSelected.textContent = selectedImageIds.size;
  
  const checkedUsers = [];
  document.querySelectorAll('#assign-users-list .user-check:checked').forEach(el => {
    checkedUsers.push(el.dataset.id);
  });
  distUsers.textContent = checkedUsers.length;
  
  const targetCount = selectedImageIds.size > 0 ? selectedImageIds.size : unassignedCount;
  const isSelectedImages = selectedImageIds.size > 0;
  
  if (checkedUsers.length === 0) {
    distPreviewMsg.innerHTML = '<span style="color:var(--red)">Select at least 1 user (or click "Assign to Me").</span>';
    btnAutoDistribute.disabled = true;
    return;
  }
  
  if (targetCount === 0) {
    distPreviewMsg.innerHTML = '<span style="color:var(--text-secondary)">No unassigned images to distribute.</span>';
    btnAutoDistribute.disabled = true;
    return;
  }
  
  btnAutoDistribute.disabled = false;
  
  const base = Math.floor(targetCount / checkedUsers.length);
  const remainder = targetCount % checkedUsers.length;
  
  let msg = '';
  if (isSelectedImages) {
    msg += `Distributing <strong>${targetCount} selected</strong> images:<br/>`;
  } else {
    msg += `Distributing <strong>all ${targetCount} unassigned</strong> images:<br/>`;
  }
  
  if (base > 0) {
    msg += `Each user gets <strong>${base}</strong> image${base > 1 ? 's' : ''}`;
    if (remainder > 0) {
      msg += `, and <strong>${remainder}</strong> left over (distributed randomly)`;
    }
  } else {
    msg += `Only <strong>${remainder}</strong> image${remainder > 1 ? 's' : ''} to distribute. Some users will get 1, others 0.`;
  }
  
  distPreviewMsg.innerHTML = msg;
};

window.selectAllUsers = () => {
  document.querySelectorAll('#assign-users-list .user-check').forEach(el => {
    el.checked = true;
  });
  updateDistributionPreview();
};

window.selectNoUsers = () => {
  document.querySelectorAll('#assign-users-list .user-check').forEach(el => {
    el.checked = false;
  });
  updateDistributionPreview();
};

window.autoDistribute = async () => {
  const checkedUsers = [];
  document.querySelectorAll('#assign-users-list .user-check:checked').forEach(el => {
    checkedUsers.push(el.dataset.id);
  });
  
  if (checkedUsers.length === 0) {
    IA.toast('Select at least one user', 'error');
    return;
  }
  
  const targetCount = selectedImageIds.size > 0 ? selectedImageIds.size : unassignedCount;
  if (targetCount === 0) {
    IA.toast('No unassigned images to distribute', 'error');
    return;
  }
  
  const payload = {
    user_ids: checkedUsers
  };
  
  if (selectedImageIds.size > 0) {
    payload.image_ids = [...selectedImageIds];
  }
  
  const btn = document.getElementById('btn-auto-distribute');
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Distributing…';
  
  try {
    const res = await IA.api(`/api/projects/${projectId}/images/auto-assign`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    
    if (res && res.ok) {
      const d = await res.json();
      IA.toast(d.detail, 'success');
      selectedImageIds.clear();
      await loadAssignSection();
      await loadStats();
    } else {
      const e = await res?.json();
      IA.toast(IA.formatError(e), 'error');
    }
  } catch (err) {
    IA.toast('Connection error', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
};

// ── Review ────────────────────────────────────────────────────────────

async function loadReviewSection() {
  const res = await IA.api(`/api/projects/${projectId}/images?state=ready_for_export&per_page=200`);
  if (!res || !res.ok) return;
  const data = await res.json();
  renderReviewGrid(data.items);
}

function renderReviewGrid(imgs) {
  const grid = document.getElementById('review-grid');
  if (!grid) return;
  const expBtn = document.getElementById('review-export-btn');
  if (imgs.length === 0) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">✅</div><h3>Queue is empty</h3><p style="font-size:12px;margin-top:4px">No images submitted for review yet.</p></div>';
    if (expBtn) expBtn.classList.add('hidden');
    return;
  }
  if (expBtn) expBtn.classList.remove('hidden');
  grid.innerHTML = imgs.map(img => `
    <div class="review-card" id="rcard-${img.id}">
      <a href="/projects/${projectId}/annotate/${img.id}" target="_blank">
        <img class="review-img" src="${IA.imgUrl(img.id)}" alt="${img.filename}" loading="lazy" onerror="this.style.display='none'"/>
      </a>
      <div class="review-footer">
        <span class="review-name" title="${img.filename}">${img.filename}</span>
        <button class="btn btn-danger btn-sm" onclick="deleteImage('${img.id}')">🗑</button>
      </div>
    </div>
  `).join('');
}

window.deleteImage = async (id) => {
  if (!confirm('Delete this image from the project? The source file will NOT be deleted.')) return;
  const res = await IA.api(`/api/projects/${projectId}/images/${id}`, { method: 'DELETE' });
  if (res && res.ok) {
    document.getElementById(`rcard-${id}`)?.remove();
    IA.toast('Image deleted', 'success');
    await loadStats();
  } else {
    const e = await res?.json();
    IA.toast(IA.formatError(e), 'error');
  }
};

// ── Users ─────────────────────────────────────────────────────────────

async function loadUsers() {
  const res = await IA.api('/api/users');
  if (!res || !res.ok) return;
  users = await res.json();
}

async function renderUsersTable() {
  await loadUsers();
  const tbody = document.getElementById('users-table-body');
  if (!tbody) return;

  if (users.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--text-muted)">No users</td></tr>';
    return;
  }

  // Get workloads via optimized endpoint
  const res = await IA.api(`/api/projects/${projectId}/user-workloads`);
  const workloads = (res && res.ok) ? await res.json() : {};

  tbody.innerHTML = users.map(u => {
    const wl = workloads[u.id] || { assigned: 0, annotated: 0, ready: 0, total: 0 };
    const initials  = u.username.slice(0, 2).toUpperCase();
    return `<tr>
      <td><div style="display:flex;align-items:center;gap:8px">
        <span class="user-avatar">${initials}</span> ${u.username}
      </div></td>
      <td>${u.is_admin ? '<span class="badge badge-ready">Admin</span>' : '<span class="badge badge-assigned">Annotator</span>'}</td>
      <td>${wl.total}</td>
      <td>${wl.annotated}</td>
      <td>${wl.ready}</td>
      <td>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="btn btn-ghost btn-sm" onclick="resetPassword('${u.id}','${u.username}')">🔑 Reset PW</button>
          <button class="btn btn-danger btn-sm" onclick="confirmRemoveFromProject('${u.id}','${u.username}', ${wl.total}, ${wl.assigned}, ${wl.total - wl.assigned})">👤 Remove from Project</button>
          ${!u.is_admin ? `<button class="btn btn-danger btn-sm" onclick="deleteUser('${u.id}','${u.username}')">🗑 Delete User</button>` : ''}
        </div>
      </td>
    </tr>`;
  }).join('');
}

window.confirmRemoveFromProject = (userId, username, total, unannotated, annotated) => {
  const bodyHtml = `
    <div style="font-size:14px;color:var(--text-primary);display:flex;flex-direction:column;gap:12px;">
      <p>How do you want to remove <strong>${username}</strong> from this project?</p>
      <p style="color:var(--text-secondary);font-size:13px;">
        Currently has <strong>${total}</strong> images assigned (<strong>${annotated}</strong> annotated, <strong>${unannotated}</strong> unannotated).
      </p>
      
      <div style="display:flex;flex-direction:column;gap:10px;margin-top:10px">
        <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;background:var(--bg-elevated);padding:12px;border:1px solid var(--border);border-radius:var(--radius);transition:border-color var(--t-fast);">
          <input type="radio" name="remove-option" id="remove-opt-delete" checked style="margin-top:4px;accent-color:var(--accent);"/>
          <div>
            <strong style="color:var(--red);">Option 1: Remove user & delete annotated images</strong>
            <div style="font-size:12px;color:var(--text-secondary);margin-top:4px;">
              Deletes their annotated/submitted/exported images from the project entirely. Any unannotated images assigned to them will return to the unassigned pool.
            </div>
          </div>
        </label>
        
        <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;background:var(--bg-elevated);padding:12px;border:1px solid var(--border);border-radius:var(--radius);transition:border-color var(--t-fast);">
          <input type="radio" name="remove-option" id="remove-opt-keep" style="margin-top:4px;accent-color:var(--accent);"/>
          <div>
            <strong style="color:var(--green);">Option 2: Remove user & keep annotated images</strong>
            <div style="font-size:12px;color:var(--text-secondary);margin-top:4px;">
              Preserves their annotated/submitted/exported images in the project (assignment cleared). Any unannotated images assigned to them will return to the unassigned pool.
            </div>
          </div>
        </label>
      </div>
    </div>
  `;

  IA.openModal('Remove User from Project', bodyHtml, [
    {
      label: 'Cancel',
      cls: 'btn-ghost',
      fn: IA.closeModal
    },
    {
      label: 'Remove User',
      cls: 'btn-danger',
      fn: () => submitRemoveUserFromProject(userId, username)
    }
  ]);
};

window.submitRemoveUserFromProject = async (userId, username) => {
  const keepAnnotated = document.getElementById('remove-opt-keep').checked;
  const modalActions = document.getElementById('modal-actions');
  const removeBtn = modalActions ? modalActions.querySelector('.btn-danger') : null;
  if (removeBtn) {
    removeBtn.disabled = true;
    removeBtn.textContent = 'Removing…';
  }

  try {
    const res = await IA.api(`/api/projects/${projectId}/users/${userId}/remove`, {
      method: 'POST',
      body: JSON.stringify({ keep_annotated: keepAnnotated })
    });
    
    if (res && res.ok) {
      const d = await res.json();
      IA.toast(d.detail, 'success');
      IA.closeModal();
      await renderUsersTable();
      await loadStats();
      // If we are currently in assign section, refresh it as well
      const assignSec = document.getElementById('section-assign');
      if (assignSec && assignSec.classList.contains('active')) {
        await loadAssignSection();
      }
    } else {
      const e = await res?.json();
      IA.toast(IA.formatError(e), 'error');
      if (removeBtn) {
        removeBtn.disabled = false;
        removeBtn.textContent = 'Remove User';
      }
    }
  } catch (err) {
    IA.toast('Connection error', 'error');
    if (removeBtn) {
      removeBtn.disabled = false;
      removeBtn.textContent = 'Remove User';
    }
  }
};

window.showCreateUser = () => {
  document.getElementById('create-user-form').classList.toggle('hidden');
};

window.createUser = async () => {
  const username = document.getElementById('new-username').value.trim();
  const password = document.getElementById('new-password').value;
  const is_admin = document.getElementById('new-is-admin').value === 'true';

  if (!username || !password) { IA.toast('Fill in all fields', 'error'); return; }

  const res = await IA.api('/api/users', {
    method: 'POST',
    body: JSON.stringify({ username, password, is_admin }),
  });
  if (res && res.ok) {
    IA.toast(`User "${username}" created ✓`, 'success');
    document.getElementById('new-username').value = '';
    document.getElementById('new-password').value = '';
    document.getElementById('create-user-form').classList.add('hidden');
    await renderUsersTable();
    populateUserSelect();
  } else {
    const e = await res?.json();
    IA.toast(IA.formatError(e), 'error');
  }
};

window.deleteUser = async (id, username) => {
  if (!confirm(`Delete user "${username}"? Their images will be unassigned.`)) return;
  const res = await IA.api(`/api/users/${id}`, { method: 'DELETE' });
  if (res && res.ok) {
    IA.toast(`User "${username}" deleted`, 'success');
    await renderUsersTable();
    await loadStats();
  } else {
    const e = await res?.json();
    IA.toast(IA.formatError(e), 'error');
  }
};

window.resetPassword = async (id, username) => {
  const pw = prompt(`New password for "${username}" (min 4 chars):`);
  if (!pw || pw.length < 4) { IA.toast('Password too short', 'error'); return; }
  const res = await IA.api(`/api/users/${id}/password`, {
    method: 'PATCH',
    body: JSON.stringify({ password: pw }),
  });
  if (res && res.ok) IA.toast('Password updated ✓', 'success');
  else IA.toast('Failed', 'error');
};

// ── Classes editor ────────────────────────────────────────────────────

function renderClassesEditor() {
  if (!project) return;
  const wrap = document.getElementById('classes-wrap');
  if (!wrap) return;
  classes = [...(project.classes || [])];
  redrawClassTags();
}

function redrawClassTags() {
  const wrap = document.getElementById('classes-wrap');
  if (!wrap) return;
  wrap.innerHTML = classes.map((cls, i) => {
    const hue = (i * 137.508) % 360;
    const color = `hsl(${hue.toFixed(0)}, 85%, 62%)`;
    return `<div class="class-tag">
      <span class="class-dot" style="background:${color}"></span>
      ${cls}
      <span class="class-del" onclick="removeClass(${i})">×</span>
    </div>`;
  }).join('');
}

window.addClass = window.addClassTag = () => {
  const input = document.getElementById('new-class-input');
  const val   = input.value.trim();
  if (!val) return;
  if (classes.includes(val)) { IA.toast('Class already exists', 'error'); return; }
  classes.push(val);
  input.value = '';
  redrawClassTags();
};

window.removeClass = (idx) => {
  classes.splice(idx, 1);
  redrawClassTags();
};

window.saveClasses = async () => {
  if (!project) { IA.toast('No project', 'error'); return; }
  const res = await IA.api(`/api/projects/${projectId}/classes`, {
    method: 'PATCH',
    body: JSON.stringify({ classes }),
  });
  if (res && res.ok) {
    const updated = await res.json();
    project.classes = updated.classes;
    IA.toast('Classes saved ✓', 'success');
  } else {
    const e = await res?.json();
    IA.toast(IA.formatError(e), 'error');
  }
};

// ── Export ────────────────────────────────────────────────────────────

function getSplitValues() {
  return {
    train: parseInt(document.getElementById('sl-train').value) / 100,
    valid: parseInt(document.getElementById('sl-valid').value) / 100,
    test:  parseInt(document.getElementById('sl-test').value)  / 100,
    include_test: document.getElementById('include-test').checked,
  };
}

window.syncSliders = (changed) => {
  const train = parseInt(document.getElementById('sl-train').value);
  const valid = parseInt(document.getElementById('sl-valid').value);
  const test  = parseInt(document.getElementById('sl-test').value);

  let rem = 100 - { train, valid, test }[changed];
  const others = ['train', 'valid', 'test'].filter(s => s !== changed);
  const total_other = { train, valid, test };
  const other_sum = others.reduce((a, s) => a + total_other[s], 0);

  if (other_sum > 0) {
    others.forEach(s => {
      const adjusted = Math.round(total_other[s] * rem / other_sum);
      document.getElementById(`sl-${s}`).value = adjusted;
    });
  }

  ['train', 'valid', 'test'].forEach(s => {
    document.getElementById(`sv-${s}`).textContent = `${document.getElementById(`sl-${s}`).value}%`;
  });

  loadExportPreview();
  buildYamlPreview();
};

async function loadExportPreview() {
  const { train, valid, test, include_test } = getSplitValues();
  const res = await IA.api(`/api/projects/${projectId}/export/preview?train_ratio=${train}&valid_ratio=${valid}&test_ratio=${test}&include_test=${include_test}`);
  if (!res || !res.ok) return;
  const d = await res.json();
  const el = document.getElementById('export-preview');
  if (el) {
    el.innerHTML = `<strong>${d.total_ready}</strong> total images to export:<br>
      <span style="color:var(--green)">Train: ${d.train_count}</span> &nbsp;·&nbsp;
      <span style="color:var(--blue)">Valid: ${d.valid_count}</span> &nbsp;·&nbsp;
      <span style="color:var(--orange)">Test: ${d.test_count}</span>`;
  }
}

function buildYamlPreview() {
  const { include_test } = getSplitValues();
  const cls = project?.classes || [];
  const names = cls.map(c => `'${c}'`).join(', ');
  const safeName = (project?.name || '').replace(/[\\/:*?"<>|]/g, '').trim();
  const folderName = `${safeName}_dataset`;
  let yaml = `path: ./dataset/${folderName}\ntrain: train/images\nval: valid/images\n`;
  if (include_test) yaml += `test: test/images\n`;
  yaml += `\nnc: ${cls.length}\nnames: [${names}]`;
  const el = document.getElementById('yaml-preview');
  if (el) el.textContent = yaml;
}

window.runExport = async () => {
  const { train, valid, test, include_test } = getSplitValues();
  if (Math.abs(train + valid + (include_test ? test : 0) - 1.0) > 0.01) {
    IA.toast('Ratios must sum to 100%', 'error'); return;
  }

  const btn = document.getElementById('export-btn');
  btn.disabled = true;
  document.getElementById('export-progress').classList.remove('hidden');
  document.getElementById('export-result').classList.add('hidden');

  const res = await IA.api(`/api/projects/${projectId}/export`, {
    method: 'POST',
    body: JSON.stringify({ train_ratio: train, valid_ratio: valid, test_ratio: test, include_test }),
  });

  btn.disabled = false;
  document.getElementById('export-progress').classList.add('hidden');

  if (res && res.ok) {
    const d = await res.json();
    const resultEl = document.getElementById('export-result');
    resultEl.classList.remove('hidden');
    resultEl.innerHTML = `<div class="export-result">
      <h3>✓ Export Complete</h3>
      <div style="font-size:13px;display:grid;grid-template-columns:repeat(4,auto);gap:16px;width:fit-content">
        <div>Total: <strong>${d.exported}</strong></div>
        <div style="color:var(--green)">Train: <strong>${d.train}</strong></div>
        <div style="color:var(--blue)">Valid: <strong>${d.valid}</strong></div>
        <div style="color:var(--orange)">Test: <strong>${d.test}</strong></div>
      </div>
      <div style="font-size:12px;color:var(--text-secondary);margin-top:8px">Output: <code>${d.output_path}</code></div>
      ${d.orphan_warnings.length > 0 ? `<div style="color:var(--yellow);font-size:12px;margin-top:8px">⚠ ${d.orphan_warnings.length} annotation(s) skipped (class mismatch)</div>` : ''}
    </div>`;
    await loadStats();
    await loadExportedImages();
  } else {
    const e = await res?.json();
    IA.toast(IA.formatError(e) || 'Export failed', 'error');
  }
};

// ── Socket ────────────────────────────────────────────────────────────

function initSocket() {
  const socket = IA.connectSocket(projectId, () => {});

  socket?.on('stats_update', updateStats);

  socket?.on('annotation_saved', ({ image_id, box_count, saved_by }) => {
    addFeedItem(`<strong>${saved_by}</strong> saved ${box_count} box(es) on image <code>${image_id.slice(0,8)}…</code>`);
  });

  socket?.on('image_state_change', ({ image_id, new_state }) => {
    addFeedItem(`Image state → <strong>${new_state}</strong>`);
  });

  socket?.on('export_progress', ({ percent }) => {
    const fill = document.getElementById('export-prog-fill');
    const pct  = document.getElementById('export-pct');
    if (fill) fill.style.width = `${percent}%`;
    if (pct)  pct.textContent  = `${percent}%`;
  });

  // If project deleted by another admin, bounce home
  socket?.on('projects_changed', async () => {
    const res = await fetch(`/api/projects/${projectId}`, {
      headers: { 'Authorization': `Bearer ${IA.getToken()}` }
    });
    if (res.status === 404) {
      IA.toast('This project was deleted', 'error');
      setTimeout(() => { window.location.href = '/home'; }, 2000);
    }
  });
}

// ── Modal helpers ─────────────────────────────────────────────────────

window.openModal = (title, bodyHtml, actionsHtml) => {
  const titleEl   = document.getElementById('modal-title');
  const bodyEl    = document.getElementById('modal-body');
  const actionsEl = document.getElementById('modal-actions');
  const overlayEl = document.getElementById('modal-overlay');

  if (titleEl) titleEl.textContent = title;
  if (bodyEl)  bodyEl.innerHTML = bodyHtml;
  if (actionsEl) {
    actionsEl.innerHTML = actionsHtml || '';
  }
  if (overlayEl) overlayEl.classList.remove('hidden');
};

window.closeModal = (e) => {
  if (!e || e.target === document.getElementById('modal-overlay') || (e.target && e.target.classList.contains('close-modal-btn'))) {
    document.getElementById('modal-overlay').classList.add('hidden');
  }
};

window.runExportFromReview = async () => {
  const { train, valid, test, include_test } = getSplitValues();
  if (Math.abs(train + valid + (include_test ? test : 0) - 1.0) > 0.01) {
    IA.toast('Ratios must sum to 100%', 'error'); return;
  }

  const btn = document.getElementById('review-export-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Exporting…'; }

  const res = await IA.api(`/api/projects/${projectId}/export`, {
    method: 'POST',
    body: JSON.stringify({ train_ratio: train, valid_ratio: valid, test_ratio: test, include_test }),
  });

  if (btn) { btn.disabled = false; btn.textContent = '📦 Export all to Dataset'; }

  if (res && res.ok) {
    const d = await res.json();
    const bodyHtml = `
      <div style="font-size:14px; color:var(--text-secondary); line-height: 1.6;">
        <p style="margin-bottom:12px; color:var(--green); font-weight:600; font-size:16px;">✓ Export completed successfully!</p>
        <div style="display:grid; grid-template-columns: repeat(4, auto); gap: 16px; margin: 16px 0; padding: 12px; background:var(--bg-elevated); border-radius:var(--radius); border: 1px solid var(--border)">
          <div>Total: <strong style="color:var(--text-primary)">${d.exported}</strong></div>
          <div style="color:var(--green)">Train: <strong>${d.train}</strong></div>
          <div style="color:var(--blue)">Valid: <strong>${d.valid}</strong></div>
          <div style="color:var(--orange)">Test: <strong>${d.test}</strong></div>
        </div>
        <div style="margin-top:12px;">Output Path:</div>
        <code style="display:block; padding:8px; background:var(--bg-elevated); border-radius:var(--radius); font-size:12px; color:var(--text-primary); border: 1px solid var(--border); overflow-x:auto;">${d.output_path}</code>
        ${d.orphan_warnings.length > 0 ? `
          <div style="margin-top:16px; color:var(--yellow); font-size:13px;">
            <strong>⚠ Warnings:</strong>
            <ul style="margin:4px 0 0 16px; padding:0;">
              ${d.orphan_warnings.slice(0, 5).map(w => `<li>${w}</li>`).join('')}
              ${d.orphan_warnings.length > 5 ? `<li>... and ${d.orphan_warnings.length - 5} more</li>` : ''}
            </ul>
          </div>
        ` : ''}
      </div>
    `;
    window.openModal('📦 Export Success', bodyHtml, `<button class="btn btn-primary close-modal-btn" onclick="closeModal()">Close</button>`);
    await loadReviewSection();
    await loadStats();
    await loadExportedImages();
  } else {
    const e = await res?.json();
    IA.toast(IA.formatError(e) || 'Export failed', 'error');
  }
};

async function loadExportedImages() {
  const res = await IA.api(`/api/projects/${projectId}/images?state=exported&per_page=200`);
  if (!res || !res.ok) return;
  const data = await res.json();
  const grid = document.getElementById('exported-image-grid');
  const countEl = document.getElementById('exported-count');
  if (countEl) countEl.textContent = `${data.total} images`;

  if (!grid) return;
  if (data.items.length === 0) {
    grid.innerHTML = '<div class="text-muted text-sm" style="padding:20px;grid-column:1/-1;text-align:center">No images exported yet.</div>';
    return;
  }
  grid.innerHTML = data.items.map(img => `
    <div class="img-thumb" title="${img.filename}" style="cursor:default">
      <img src="${IA.imgUrl(img.id)}" alt="${img.filename}" loading="lazy" onerror="this.style.display='none'"/>
    </div>
  `).join('');
}

// ── All Images Section (Admin Explorer) ────────────────────────────────
let allImagesFilter = 'all';
let allImagesPage = 1;
let allImagesPerPage = 50;
let allImagesTotalPages = 1;
let allImagesShowPreviews = localStorage.getItem('ia_show_previews') !== 'false';

async function loadAllImagesSection() {
  const grid = document.getElementById('all-images-grid');
  const paginationBar = document.getElementById('all-images-pagination');
  if (!grid) return;

  // Sync checkbox state
  const checkbox = document.getElementById('all-images-show-previews');
  if (checkbox) checkbox.checked = allImagesShowPreviews;

  grid.innerHTML = `
    <div style="color:var(--text-muted);font-size:13px;grid-column:1/-1;padding:40px 0;text-align:center">
      <div class="spinner" style="margin:0 auto 12px"></div>
      Loading images…
    </div>`;

  const params = new URLSearchParams({
    page: allImagesPage,
    per_page: allImagesPerPage,
  });
  if (allImagesFilter !== 'all') {
    params.set('state', allImagesFilter);
  }

  const res = await IA.api(`/api/projects/${projectId}/images?${params}`);
  if (!res || !res.ok) {
    grid.innerHTML = '<div class="text-muted" style="grid-column:1/-1;padding:40px;text-align:center">Failed to load images.</div>';
    return;
  }

  const data = await res.json();
  const { items, total, page, per_page, total_pages } = data;

  allImagesTotalPages = total_pages;
  allImagesPage = page;

  // Update count badge
  const countBadge = document.getElementById('all-images-count-badge');
  if (countBadge) {
    countBadge.textContent = `${total} images`;
  }

  if (items.length === 0) {
    grid.innerHTML = '<div class="text-muted text-sm" style="padding:20px;grid-column:1/-1;text-align:center">No images found matching this filter.</div>';
    paginationBar.style.display = 'none';
    return;
  }

  grid.innerHTML = items.map(img => {
    const stateColor = IA.stateColor(img.state);
    const stateBadgeText = img.state.toUpperCase().replace(/_/g, ' ');
    const assigneeText = img.assigned_username 
      ? `Assigned to: <strong>${img.assigned_username}</strong>` 
      : '<span style="color:var(--text-muted)">Unassigned</span>';
    
    const thumbHtml = allImagesShowPreviews
      ? `<img src="${IA.imgUrl(img.id)}" alt="${img.filename}" loading="lazy" onerror="this.style.display='none'"/>`
      : `<div style="width:100%; height:100%; display:flex; flex-direction:column; align-items:center; justify-content:center; background:var(--bg-elevated); color:var(--text-muted); gap:8px;">
           <span style="font-size:24px;">🖼️</span>
           <span style="font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:0.05em;">Click to view/edit</span>
         </div>`;

    return `
      <div class="pv-img-card" onclick="window.open('/projects/${projectId}/annotate/${img.id}', '_blank')">
        <div class="card-thumb">
          ${thumbHtml}
          <div class="state-badge-overlay">
            <span class="badge" style="background:${stateColor}; color:#fff; font-size:10px; font-weight:700; border:1px solid rgba(0,0,0,0.25);">${stateBadgeText}</span>
          </div>
        </div>
        <div class="card-footer" style="display:flex; flex-direction:column; gap:4px;">
          <div class="card-name" title="${img.filename}" style="font-weight:600; font-size:12px; color:var(--text-primary); margin-bottom:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${img.filename}</div>
          <div style="font-size:11px; color:var(--text-secondary);">${assigneeText}</div>
        </div>
      </div>
    `;
  }).join('');

  // Pagination controls
  paginationBar.style.display = 'flex';
  document.getElementById('all-page-current').textContent = allImagesPage;
  document.getElementById('all-page-total').textContent = allImagesTotalPages;
  document.getElementById('all-total-count').textContent = total;
  document.getElementById('all-btn-prev').disabled = allImagesPage <= 1;
  document.getElementById('all-btn-next').disabled = allImagesPage >= allImagesTotalPages;
}

window.setAllImagesFilter = (filter) => {
  allImagesFilter = filter;
  allImagesPage = 1;
  document.querySelectorAll('#section-all-images .filter-btn').forEach(btn => {
    btn.classList.remove('active');
  });
  document.getElementById(`all-img-filter-${filter}`)?.classList.add('active');
  loadAllImagesSection();
};

window.setAllImagesPerPage = (perPage) => {
  allImagesPerPage = perPage;
  allImagesPage = 1;
  document.querySelectorAll('#section-all-images .per-page-btn').forEach(btn => {
    btn.classList.remove('active');
  });
  document.getElementById(`all-pp-${perPage}`)?.classList.add('active');
  loadAllImagesSection();
};

window.goAllImagesPage = (direction) => {
  const targetPage = allImagesPage + direction;
  if (targetPage >= 1 && targetPage <= allImagesTotalPages) {
    allImagesPage = targetPage;
    loadAllImagesSection();
  }
};

window.toggleAllImagesPreviews = (checked) => {
  allImagesShowPreviews = checked;
  localStorage.setItem('ia_show_previews', checked);
  loadAllImagesSection();
};

init();
})();
