/**
 * socket.js — Shared Socket.IO client setup and auth helpers.
 * v2.0.0 — Multi-project support with project-scoped rooms.
 */

// ── Auth helpers ────────────────────────────────────────────────────

window.IA = window.IA || {};

IA.getToken = () => localStorage.getItem('ia_token');
IA.getUser  = () => {
  try { return JSON.parse(localStorage.getItem('ia_user') || 'null'); }
  catch { return null; }
};

/** Adds Authorization header to all fetch calls. */
IA.api = async (url, opts = {}) => {
  const token = IA.getToken();
  if (!token) { IA.logout(); return; }

  const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`,
    ...(opts.headers || {}),
  };
  const res = await fetch(url, { ...opts, headers });

  if (res.status === 401) { IA.logout(); return; }

  // If project was deleted / not found while on a project page, bounce home
  if (res.status === 404 || res.status === 410) {
    const body = await res.json().catch(() => ({}));
    if (body.detail && body.detail.toLowerCase().includes('project')) {
      IA.toast('Project not found — returning to Home', 'error');
      setTimeout(() => { window.location.href = '/home'; }, 1500);
      return null;
    }
  }

  return res;
};

/** Image URL with token in query param (for <img src="...">). */
IA.imgUrl = (imageId) => `/api/images/${imageId}/file?token=${encodeURIComponent(IA.getToken())}`;

IA.logout = () => {
  localStorage.removeItem('ia_token');
  localStorage.removeItem('ia_user');
  window.location.href = '/login';
};

/** Redirect to login if not authenticated or wrong role.
 *  adminOnly=true → non-admins are sent to /home (their project list)
 */
IA.requireAuth = (adminOnly = false) => {
  const token = IA.getToken();
  const user  = IA.getUser();
  if (!token || !user) { IA.logout(); return null; }
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    if (payload.exp * 1000 < Date.now()) { IA.logout(); return null; }
  } catch { IA.logout(); return null; }
  if (adminOnly && !user.is_admin) {
    window.location.href = '/home';
    return null;
  }
  return user;
};

// ── Toast notifications ─────────────────────────────────────────────

IA.toast = (() => {
  let container = null;
  const getContainer = () => {
    if (!container) {
      container = document.getElementById('toast-container');
      if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
      }
    }
    return container;
  };

  return (msg, type = 'info', duration = 3500) => {
    const icons = { success: '✓', error: '✕', info: 'ℹ' };
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ'}</span><span>${msg}</span>`;
    getContainer().appendChild(el);
    setTimeout(() => {
      el.style.animation = 'toast-out 300ms ease forwards';
      setTimeout(() => el.remove(), 300);
    }, duration);
  };
})();

// ── Socket.IO connection ────────────────────────────────────────────

/**
 * Connect to Socket.IO.
 * @param {string|null} projectId — If provided, client joins the project room.
 * @param {Function} onConnected — Called after successful connection.
 */
IA.connectSocket = (projectId, onConnected) => {
  if (typeof io === 'undefined') return null;

  const token = IA.getToken();
  const authPayload = { token };
  if (projectId) authPayload.project_id = projectId;

  const socket = io({
    auth: authPayload,
    reconnection: true,
    reconnectionDelay: 1000,
    reconnectionAttempts: Infinity,
  });

  socket.on('connect', () => {
    const dot = document.querySelector('.conn-dot');
    if (dot) dot.classList.add('connected');
    const label = document.getElementById('conn-label');
    if (label) label.textContent = 'Live';
    if (onConnected) onConnected(socket);
  });

  socket.on('disconnect', () => {
    const dot = document.querySelector('.conn-dot');
    if (dot) dot.classList.remove('connected');
    const label = document.getElementById('conn-label');
    if (label) label.textContent = 'Offline';
  });

  socket.on('connect_error', () => {
    const dot = document.querySelector('.conn-dot');
    if (dot) dot.classList.remove('connected');
  });

  return socket;
};

// ── State badge helper ──────────────────────────────────────────────

IA.stateBadge = (state) => {
  const map = {
    unassigned:      ['badge-unassigned', '○ Unassigned'],
    assigned:        ['badge-assigned',   '● Assigned'],
    unannotated:     ['badge-unannotated','◎ Unannotated'],
    annotated:       ['badge-annotated',  '◉ Annotated'],
    ready_for_export:['badge-ready',      '✓ Ready'],
    exported:        ['badge-exported',   '⬆ Exported'],
  };
  const [cls, label] = map[state] || ['badge-unassigned', state];
  return `<span class="badge ${cls}">${label}</span>`;
};

IA.stateColor = (state) => ({
  unassigned:      '#5a5a78',
  assigned:        '#4f9eff',
  unannotated:     '#f5c542',
  annotated:       '#ff9940',
  ready_for_export:'#22d3a0',
  exported:        '#9d92ff',
}[state] || '#888');

IA.formatError = (err) => {
  if (!err) return 'Failed';
  if (typeof err === 'string') return err;
  if (err.detail) {
    if (typeof err.detail === 'string') return err.detail;
    if (Array.isArray(err.detail) && err.detail[0]?.msg) return err.detail[0].msg;
  }
  return 'Failed';
};

/** Format ISO timestamp to local time string. */
IA.fmtTime = (iso) => {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return ''; }
};

/** Format ISO date to short date string. */
IA.fmtDate = (iso) => {
  try {
    return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
  } catch { return ''; }
};

// ── Modal helpers (shared across pages) ────────────────────────────

IA.openModal = (title, bodyHtml, actions = []) => {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = bodyHtml;
  const actionsEl = document.getElementById('modal-actions');
  if (actionsEl) {
    actionsEl.innerHTML = '';
    actions.forEach(({ label, cls, fn }) => {
      const btn = document.createElement('button');
      btn.className = `btn ${cls || 'btn-secondary'}`;
      btn.textContent = label;
      btn.onclick = fn;
      actionsEl.appendChild(btn);
    });
  }
  document.getElementById('modal-overlay').classList.remove('hidden');
};

IA.closeModal = () => {
  document.getElementById('modal-overlay').classList.add('hidden');
};

// Allow clicking overlay background to close
function closeModal(e) {
  if (e.target.id === 'modal-overlay') IA.closeModal();
}
