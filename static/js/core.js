/**
 * core.js — shared client library (window.IA).
 * Auth, API, escaping, icons, toasts, modals, sockets, shell and small motion helpers.
 * Every page loads this first; page scripts register their own socket handlers once.
 */
(() => {
'use strict';

const IA = window.IA = window.IA || {};
const SPRITE = '/static/icons/sprite.svg';

// ── Storage (never throws — private windows can block localStorage) ────────────
IA.store = {
  get(key, fallback = null) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } },
  del(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
};

// ── Escaping / DOM ────────────────────────────────────────────────────────────
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };
/** Escape any value for use in HTML text or a quoted attribute. */
IA.esc = (v) => String(v ?? '').replace(/[&<>"'`]/g, (c) => ESC[c]);

/** Create an element tree from an HTML string (first element, or a fragment if several). */
IA.h = (html) => {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.childElementCount === 1 ? t.content.firstElementChild : t.content;
};
IA.$ = (sel, root = document) => root.querySelector(sel);
IA.$$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** Delegated event: IA.on(container, 'click', '[data-act]', (e, el) => ...) */
IA.on = (root, type, selector, fn) => {
  root.addEventListener(type, (e) => {
    const el = e.target.closest(selector);
    if (el && root.contains(el)) fn(e, el);
  });
};

IA.icon = (name, cls = '') => `<svg class="i ${cls}" aria-hidden="true"><use href="${SPRITE}#i-${name}"/></svg>`;

IA.debounce = (fn, ms = 250) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// ── Auth ──────────────────────────────────────────────────────────────────────
IA.getToken = () => { try { return localStorage.getItem('ia_token'); } catch { return null; } };
IA.getUser = () => IA.store.get('ia_user');

IA.setSession = (data) => {
  try { localStorage.setItem('ia_token', data.access_token); } catch { /* ignore */ }
  IA.store.set('ia_user', {
    id: data.user_id, username: data.username, is_admin: data.is_admin,
    must_change_password: !!data.must_change_password,
  });
};

/** Decode a JWT payload (base64url, unpadded). Returns null if malformed. */
IA.decodeJwt = (token) => {
  try {
    let b64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    b64 += '='.repeat((4 - (b64.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return null; }
};

IA.logout = (reason) => {
  const token = IA.getToken();
  // Clear the HttpOnly media cookie too (fire-and-forget; the redirect happens regardless)
  if (token && !reason) fetch('/api/auth/logout', { method: 'POST', keepalive: true, headers: { Authorization: `Bearer ${token}` } }).catch(() => {});
  try { localStorage.removeItem('ia_token'); } catch { /* ignore */ }
  IA.store.del('ia_user');
  const next = location.pathname === '/login' ? '' : `?next=${encodeURIComponent(location.pathname + location.hash)}`;
  location.href = `/login${reason ? (next ? next + '&' : '?') + 'reason=' + reason : next}`;
};

/**
 * Returns the logged-in user or redirects. adminOnly sends annotators Home.
 * Users who must change their password are sent to /account first.
 */
IA.requireAuth = (adminOnly = false) => {
  const token = IA.getToken();
  const user = IA.getUser();
  const payload = token && IA.decodeJwt(token);
  if (!token || !user || !payload) { IA.logout(); return null; }
  if (payload.exp * 1000 < Date.now()) { IA.logout('expired'); return null; }
  if (payload.sub !== user.id) { IA.logout(); return null; }            // another tab logged in as someone else
  if (user.must_change_password && location.pathname !== '/account') { location.href = '/account?first=1'; return null; }
  if (adminOnly && !user.is_admin) { location.href = '/home'; return null; }
  document.documentElement.classList.toggle('is-admin', !!user.is_admin);
  return user;
};

// ── API ───────────────────────────────────────────────────────────────────────
class ApiError extends Error {
  constructor(message, status, body) { super(message); this.status = status; this.body = body; }
}
IA.ApiError = ApiError;

IA.formatError = (err) => {
  if (!err) return 'Something went wrong';
  if (typeof err === 'string') return err;
  if (err instanceof ApiError) return err.message;
  const d = err.detail ?? err.message;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d[0]?.msg) return d.map((x) => x.msg.replace(/^Value error, /, '')).join('; ');
  return 'Something went wrong';
};

/**
 * fetch wrapper → parsed JSON. Throws ApiError with a readable message on failure.
 * A 401 logs out. Nothing here redirects on 404 — callers decide.
 */
IA.request = async (method, url, body) => {
  const token = IA.getToken();
  // Another tab signed in as someone else: never act under the wrong identity
  const who = token && IA.decodeJwt(token)?.sub;
  if (who && IA.getUser()?.id && who !== IA.getUser().id) { location.reload(); throw new ApiError('Signed in as another user', 401, null); }
  const headers = { Authorization: `Bearer ${token}` };
  const opts = { method, headers };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opts); }
  catch { throw new ApiError('Cannot reach the server — check the connection', 0, null); }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = text; } }
  if (res.status === 401) { IA.logout('expired'); throw new ApiError('Session expired', 401, data); }
  if (res.status === 403 && data?.detail === 'Change your password first') { location.href = '/account?first=1'; throw new ApiError(data.detail, 403, data); }
  if (!res.ok) throw new ApiError(IA.formatError(data) || `Request failed (${res.status})`, res.status, data);
  return data;
};
IA.get = (url) => IA.request('GET', url);
IA.post = (url, body = {}) => IA.request('POST', url, body);
IA.put = (url, body = {}) => IA.request('PUT', url, body);
IA.patch = (url, body = {}) => IA.request('PATCH', url, body);
IA.del = (url) => IA.request('DELETE', url);

// Images, videos and downloads authenticate with an HttpOnly cookie — tokens never go in URLs
IA.imgUrl = (id) => `/api/images/${id}/file`;
IA.thumbUrl = (id, size = 320) => `/api/images/${id}/thumb?size=${size}`;

/** (Re)issue the media cookie for this tab's token. Await before showing images. */
let sessionReady = null;
IA.ensureSession = () => {
  if (!sessionReady) sessionReady = IA.post('/api/auth/session').catch(() => { sessionReady = null; });
  return sessionReady;
};

IA.fmtBytes = (b) => {
  if (!b) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
  return `${(b / 1024 ** i).toFixed(i >= 3 ? 1 : 0)} ${u[i]}`;
};

/** Run fn with the button disabled + spinner; always restores it. */
IA.busy = async (btn, fn) => {
  if (btn?.classList.contains('is-busy')) return undefined;
  btn?.classList.add('is-busy');
  if (btn) btn.disabled = true;
  try { return await fn(); }
  finally { if (btn) { btn.classList.remove('is-busy'); btn.disabled = false; } }
};

// ── States ────────────────────────────────────────────────────────────────────
IA.STATES = {
  unassigned:       { label: 'Unassigned',  color: 'var(--st-unassigned)' },
  assigned:         { label: 'Assigned',    color: 'var(--st-assigned)' },
  unannotated:      { label: 'In progress', color: 'var(--st-unannotated)' },
  annotated:        { label: 'Annotated',   color: 'var(--st-annotated)' },
  ready_for_export: { label: 'In dataset',  color: 'var(--st-ready)' },
  exported:         { label: 'Exported',    color: 'var(--st-exported)' },
};
IA.stateColor = (s) => (IA.STATES[s] || IA.STATES.unassigned).color;
IA.stateLabel = (s) => (IA.STATES[s] || { label: s }).label;
IA.stateChip = (s) => `<span class="chip state" style="--c:${IA.stateColor(s)}"><span class="dot"></span>${IA.esc(IA.stateLabel(s))}</span>`;

/** Unified counter definitions used on every page. */
IA.derive = (s = {}) => {
  const n = (k) => s[k] || 0;
  const total = s.total ?? (n('unassigned') + n('assigned') + n('unannotated') + n('annotated') + n('ready_for_export') + n('exported'));
  const done = n('annotated') + n('ready_for_export') + n('exported');
  return { ...s, total, done, pending: n('assigned') + n('unannotated'), pct: total ? Math.round((done / total) * 100) : 0 };
};

/** Stacked state bar HTML. */
IA.stateBar = (s, cls = '') => {
  const order = ['exported', 'ready_for_export', 'annotated', 'unannotated', 'assigned', 'unassigned'];
  const parts = order.map((k) => `<span data-k="${k}" style="flex-grow:${s[k] || 0};background:${IA.stateColor(k)}" title="${IA.stateLabel(k)}: ${s[k] || 0}"></span>`).join('');
  return `<div class="statebar ${cls}">${parts}</div>`;
};
IA.updateStateBar = (el, s) => {
  el?.querySelectorAll('span[data-k]').forEach((sp) => {
    const v = s[sp.dataset.k] || 0;
    sp.style.flexGrow = v;
    sp.title = `${IA.stateLabel(sp.dataset.k)}: ${v}`;
  });
};

// ── Class colours: curated, distinguishable on dark frames; cycles with a lightness shift ──
const CLASS_PALETTE = ['#4f9cf7', '#f5a043', '#3fcf8e', '#ef5f6b', '#b08cf5', '#e6c63f',
                       '#42c9d8', '#f28cc8', '#9ccc57', '#d09662', '#7d8cfa', '#8fb4c9'];
IA.classColor = (idx, alpha = 1) => {
  if (idx == null || idx < 0) return alpha === 1 ? '#8b9099' : `rgb(139 144 153 / ${alpha})`;
  const base = CLASS_PALETTE[idx % CLASS_PALETTE.length];
  const round = Math.floor(idx / CLASS_PALETTE.length);
  const r = parseInt(base.slice(1, 3), 16), g = parseInt(base.slice(3, 5), 16), b = parseInt(base.slice(5, 7), 16);
  const k = round % 2 ? 0.72 : 1;           // second lap: slightly darker
  return `rgb(${Math.round(r * k)} ${Math.round(g * k)} ${Math.round(b * k)} / ${alpha})`;
};

// ── Formatting ────────────────────────────────────────────────────────────────
IA.fmtNum = (n) => (n ?? 0).toLocaleString();
IA.fmtDate = (iso) => { try { return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }); } catch { return ''; } };
IA.fmtTime = (iso) => { try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch { return ''; } };
IA.fmtRel = (iso) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(s)) return '';
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)}d ago`;
  return IA.fmtDate(iso);
};
IA.plural = (n, one, many = `${one}s`) => `${IA.fmtNum(n)} ${n === 1 ? one : many}`;
IA.avatar = (name = '?', cls = '') => {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `<span class="avatar ${cls}" style="--av-h:${h}" title="${IA.esc(name)}">${IA.esc(name.slice(0, 2).toUpperCase())}</span>`;
};

// ── Motion helpers ────────────────────────────────────────────────────────────
const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Tween a number in el to `to`; shows a small +N delta chip on increases. */
IA.countTo = (el, to, { delta = true, format = IA.fmtNum } = {}) => {
  if (!el) return;
  const from = Number(el.dataset.value ?? NaN);
  el.dataset.value = to;
  if (!isFinite(from) || from === to || reduced()) { el.textContent = format(to); return; }
  const start = performance.now(), dur = 520;
  const step = (t) => {
    const p = Math.min(1, (t - start) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = format(Math.round(from + (to - from) * e));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  const host = el.closest('.stat');
  if (delta && host) {
    host.querySelector('.delta')?.remove();
    const d = to - from;
    host.insertAdjacentHTML('beforeend', `<span class="delta ${d < 0 ? 'neg' : ''}">${d > 0 ? '+' : ''}${IA.fmtNum(d)}</span>`);
    setTimeout(() => host.querySelector('.delta')?.remove(), 1700);
  }
};

/** Mark thumbnail <img data-src> elements as loaded for a soft fade-in. */
IA.lazyFade = (root) => {
  root.querySelectorAll('img[loading]').forEach((img) => {
    if (img.complete && img.naturalWidth) img.classList.add('loaded');
    else img.addEventListener('load', () => img.classList.add('loaded'), { once: true });
  });
};

/** Sliding indicator for .seg controls. Call after changing the active button. */
IA.segSync = (seg) => {
  if (!seg) return;
  let thumb = seg.querySelector('.seg-thumb');
  if (!thumb) { thumb = document.createElement('span'); thumb.className = 'seg-thumb'; seg.prepend(thumb); }
  const active = seg.querySelector('button.active');
  if (!active) { thumb.style.width = '0'; return; }
  thumb.style.width = `${active.offsetWidth}px`;
  thumb.style.transform = `translateX(${active.offsetLeft}px)`;
};

// ── Toasts ────────────────────────────────────────────────────────────────────
const TOAST_ICONS = { success: 'circle-check', error: 'circle-alert', info: 'info', warn: 'circle-alert' };
/**
 * IA.toast(message, type?, { duration, action: { label, fn } })
 * Text is always escaped. Returns { close }.
 */
IA.toast = (msg, type = 'info', opts = {}) => {
  if (typeof opts === 'number') opts = { duration: opts };
  let host = document.getElementById('toasts');
  if (!host) { host = document.createElement('div'); host.id = 'toasts'; document.body.appendChild(host); }
  const el = IA.h(`<div class="toast ${type}" role="status">${IA.icon(TOAST_ICONS[type] || 'info')}<div class="msg">${IA.esc(msg)}</div></div>`);
  let timer;
  const close = () => { clearTimeout(timer); el.classList.add('leaving'); setTimeout(() => el.remove(), 200); };
  if (opts.action) {
    const b = IA.h(`<button class="btn btn-sm act">${IA.esc(opts.action.label)}</button>`);
    b.addEventListener('click', async () => { close(); await opts.action.fn(); });
    el.appendChild(b);
  }
  host.appendChild(el);
  while (host.children.length > 4) host.firstElementChild.remove();
  timer = setTimeout(close, opts.duration ?? (opts.action ? 8000 : type === 'error' ? 6000 : 3500));
  return { close };
};

// ── Modals ────────────────────────────────────────────────────────────────────
/**
 * IA.modal({ title, sub, body (html|Node), actions: [{label, variant, fn, id}], size, onClose, dismissable })
 * Returns { el, body, close, actions: {id: button} }. Action fns receive the handle; return false to keep open.
 */
IA.modal = ({ title, sub = '', body = '', actions = [], size = '', onClose, dismissable = true } = {}) => {
  const prevFocus = document.activeElement;
  const overlay = IA.h(`
    <div class="overlay" role="dialog" aria-modal="true">
      <div class="modal ${size}">
        <div class="modal-head"><h2>${IA.esc(title)}${sub ? `<div class="sub">${IA.esc(sub)}</div>` : ''}</h2>
          ${dismissable ? `<button class="btn btn-ghost btn-icon btn-sm" data-close aria-label="Close">${IA.icon('x')}</button>` : ''}</div>
        <div class="modal-body"></div>
        ${actions.length ? '<div class="modal-foot"></div>' : ''}
      </div>
    </div>`);
  const bodyEl = overlay.querySelector('.modal-body');
  if (typeof body === 'string') bodyEl.innerHTML = body; else bodyEl.appendChild(body);

  let closed = false;
  const handle = { el: overlay, body: bodyEl, actions: {} };
  handle.close = () => {
    if (closed) return;
    closed = true;
    overlay.classList.add('closing');
    document.removeEventListener('keydown', onKey, true);
    setTimeout(() => overlay.remove(), 130);
    onClose?.();
    prevFocus?.focus?.();
  };
  const onKey = (e) => {
    if (e.key === 'Escape' && dismissable) { e.stopPropagation(); handle.close(); }
    if (e.key === 'Tab') {                     // focus trap
      const f = [...overlay.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter((x) => !x.disabled && x.offsetParent);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f.at(-1).focus(); }
      else if (!e.shiftKey && document.activeElement === f.at(-1)) { e.preventDefault(); f[0].focus(); }
    }
  };
  document.addEventListener('keydown', onKey, true);
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay && dismissable) handle.close(); });
  overlay.querySelector('[data-close]')?.addEventListener('click', handle.close);

  const foot = overlay.querySelector('.modal-foot');
  actions.forEach((a) => {
    const b = IA.h(`<button class="btn ${a.variant || ''}">${a.icon ? IA.icon(a.icon) : ''}${IA.esc(a.label)}</button>`);
    if (a.id) handle.actions[a.id] = b;
    b.addEventListener('click', async () => {
      if (!a.fn) { handle.close(); return; }
      let keep;
      try { keep = await IA.busy(b, () => a.fn(handle)); }
      catch (err) { IA.toast(IA.formatError(err), 'error'); return; }   // keep the dialog open
      if (keep !== false) handle.close();
    });
    foot.appendChild(b);
  });
  document.body.appendChild(overlay);
  (overlay.querySelector('[autofocus]') || overlay.querySelector('.modal-foot .btn:last-child') || overlay.querySelector('button'))?.focus();
  return handle;
};

/** Confirmation dialog → Promise<boolean>. requireText forces typing a value (e.g. project name). */
IA.confirm = ({ title, message = '', html = '', confirmLabel = 'Confirm', danger = false, requireText = null } = {}) =>
  new Promise((resolve) => {
    let ok = false;
    const body = `${message ? `<p>${IA.esc(message)}</p>` : ''}${html}
      ${requireText ? `<div class="field"><label>Type <b>${IA.esc(requireText)}</b> to confirm</label><input class="input" data-confirm-text autocomplete="off" autofocus/></div>` : ''}`;
    const m = IA.modal({
      title, body,
      onClose: () => resolve(ok),
      actions: [
        { label: 'Cancel', variant: 'btn-ghost' },
        { id: 'ok', label: confirmLabel, variant: danger ? 'btn-danger-solid' : 'btn-primary', fn: () => { ok = true; } },
      ],
    });
    if (requireText) {
      const inp = m.body.querySelector('[data-confirm-text]');
      const okBtn = m.actions.ok;
      okBtn.disabled = true;
      inp.addEventListener('input', () => { okBtn.disabled = inp.value.trim() !== requireText; });
      // preventDefault: otherwise the same Enter "clicks" whatever gets focus back after closing
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !okBtn.disabled) { e.preventDefault(); okBtn.click(); } });
    }
  });

/** Single-value prompt → Promise<string|null>. validate(value) returns an error string or ''. */
IA.prompt = ({ title, label = '', type = 'text', placeholder = '', value = '', confirmLabel = 'Save', validate } = {}) =>
  new Promise((resolve) => {
    let result = null;
    const m = IA.modal({
      title,
      body: `<div class="field"><label>${IA.esc(label)}</label><input class="input" type="${type}" placeholder="${IA.esc(placeholder)}" value="${IA.esc(value)}" autofocus/><div class="error"></div></div>`,
      onClose: () => resolve(result),
      actions: [
        { label: 'Cancel', variant: 'btn-ghost' },
        { id: 'ok', label: confirmLabel, variant: 'btn-primary', fn: () => {
          const v = inp.value;
          const err = validate?.(v) || '';
          m.body.querySelector('.error').textContent = err;
          inp.classList.toggle('is-invalid', !!err);
          if (err) return false;
          result = v;
          return true;
        } },
      ],
    });
    const inp = m.body.querySelector('input');
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); m.actions.ok.click(); } });
  });

/** Small dropdown menu anchored to an element. items: [{label, icon, fn, danger}] or 'sep'. */
IA.menu = (anchor, items) => {
  document.querySelectorAll('.menu').forEach((m) => m.remove());
  const menu = IA.h(`<div class="menu" role="menu"></div>`);
  items.forEach((it) => {
    if (it === 'sep') { menu.appendChild(document.createElement('hr')); return; }
    const b = IA.h(`<button class="${it.danger ? 'danger' : ''}" role="menuitem">${it.icon ? IA.icon(it.icon) : ''}${IA.esc(it.label)}</button>`);
    b.addEventListener('click', () => { menu.remove(); it.fn(); });
    menu.appendChild(b);
  });
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = `${Math.min(r.left, innerWidth - mw - 8)}px`;
  menu.style.top = `${r.bottom + mh + 8 > innerHeight ? r.top - mh - 4 : r.bottom + 4}px`;
  const away = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('mousedown', away); } };
  setTimeout(() => document.addEventListener('mousedown', away), 0);
  return menu;
};

// ── Socket.IO ─────────────────────────────────────────────────────────────────
IA.reconnectHandlers = [];
/** fn runs after the socket reconnects (not on the first connect). */
IA.onReconnect = (fn) => IA.reconnectHandlers.push(fn);

/** Connect once per page; register handlers on the returned socket (they survive reconnects). */
IA.connectSocket = (projectId = null) => {
  if (typeof io === 'undefined') return null;
  const socket = io({
    auth: (cb) => cb({ token: IA.getToken(), project_id: projectId }),
    reconnection: true, reconnectionDelay: 1000, reconnectionDelayMax: 8000,
  });
  const setConn = (state) => {
    document.querySelectorAll('.conn').forEach((c) => {
      c.classList.toggle('live', state === 'live');
      c.classList.toggle('down', state === 'down');
      const l = c.querySelector('.conn-label');
      if (l) l.textContent = state === 'live' ? 'Live' : state === 'down' ? 'Offline' : 'Connecting';
    });
  };
  let connectedOnce = false;
  socket.on('connect', () => {
    setConn('live');
    // Events missed while offline: let the page reload its data
    if (connectedOnce) IA.reconnectHandlers.forEach((fn) => { try { fn(); } catch { /* ignore */ } });
    connectedOnce = true;
  });
  socket.on('disconnect', () => setConn('down'));
  socket.on('connect_error', (err) => {
    setConn('down');
    if (err?.message === 'unauthorized') { socket.disconnect(); IA.logout('expired'); }
  });
  IA.socket = socket;
  return socket;
};

// ── Shell (rail + topbar) ─────────────────────────────────────────────────────
/**
 * IA.shell({ projectId, section }) — fills user chip, role visibility, project name,
 * active nav item and badges. Returns the user (or null if redirected).
 */
IA.shell = async ({ adminOnly = false, projectId = null, section = null } = {}) => {
  const user = IA.requireAuth(adminOnly);
  if (!user) return null;
  await IA.ensureSession();

  const chip = document.getElementById('user-chip');
  if (chip) {
    chip.querySelector('.who b').textContent = user.username;
    chip.querySelector('.who span').textContent = user.is_admin ? 'Admin' : 'Annotator';
    chip.querySelector('[data-avatar]').outerHTML = IA.avatar(user.username);
    chip.addEventListener('click', () => IA.menu(chip, [
      { label: 'Account & password', icon: 'key-round', fn: () => { location.href = '/account'; } },
      ...(user.is_admin ? [{ label: 'System & storage', icon: 'monitor', fn: () => { location.href = '/system'; } }] : []),
      'sep',
      { label: 'Sign out', icon: 'log-out', danger: true, fn: () => IA.logout() },
    ]));
  }
  if (!user.is_admin) document.querySelectorAll('[data-admin]').forEach((el) => el.remove());
  IA.setActiveNav(section);

  document.querySelector('.rail-toggle')?.addEventListener('click', () => document.getElementById('rail')?.classList.toggle('open'));
  return user;
};

IA.setActiveNav = (section) => {
  document.querySelectorAll('.rail .nav-link').forEach((a) => a.classList.toggle('active', !!section && a.dataset.nav === section));
};

IA.setCrumbs = (items) => {
  const el = document.getElementById('crumbs');
  if (!el) return;
  el.innerHTML = items.map((it, i) => {
    const last = i === items.length - 1;
    const label = IA.esc(it.label);
    return (i ? '<span class="sep">/</span>' : '') + (last || !it.href ? `<span class="${last ? 'here' : ''}">${label}</span>` : `<a href="${it.href}">${label}</a>`);
  }).join('');
};

IA.setNavCount = (key, n, hot = false) => {
  const el = document.querySelector(`[data-count="${key}"]`);
  if (!el) return;
  el.textContent = n ? IA.fmtNum(n) : '';
  el.classList.toggle('hot', hot && n > 0);
};

/** Give a clicked element a view-transition name so it morphs into the next page's hero. */
IA.heroTransition = (el) => { if (el) el.style.viewTransitionName = 'project-hero'; };

// A fast second navigation skips the running cross-page transition; that is expected, not an error
['pageswap', 'pagereveal'].forEach((ev) => window.addEventListener(ev, (e) => {
  e.viewTransition?.finished?.catch(() => {});
  e.viewTransition?.ready?.catch(() => {});
  e.viewTransition?.updateCallbackDone?.catch(() => {});
}));

})();
