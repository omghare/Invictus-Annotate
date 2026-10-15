/**
 * video_processor.js — preview a server-side video and export a lossless clip.
 */
(async () => {
'use strict';

const user = await IA.shell({ adminOnly: true, section: 'video' });
if (!user) return;
IA.setCrumbs([{ label: 'Video tools' }]);
IA.connectSocket();

const $ = (id) => document.getElementById(id);
const video = $('video');
let path = null, tIn = 0, tOut = 0, stats = null;

// The browser may not be able to play the file (HEVC, some MKV): fall back to ffprobe's duration
const duration = () => (isFinite(video.duration) && video.duration > 0 ? video.duration : stats?.duration || 0);
/** "1:02.5", "00:01:02.50" or "62.5" → seconds */
const parseTime = (t) => {
  const parts = String(t).trim().split(':').map(Number);
  if (!parts.length || parts.some((x) => !isFinite(x))) return NaN;
  return parts.reduce((acc, x) => acc * 60 + x, 0);
};
const fmt = (s) => {
  if (!isFinite(s)) return '00:00:00.00';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${sec.toFixed(2).padStart(5, '0')}`;
};

function showStats(s, which) {
  $('stats').querySelectorAll('dd').forEach((dd) => {
    const k = dd.dataset.k;
    const val = s ? ({ fps: `${s.fps} fps`, frames: IA.fmtNum(s.frames), avg_size_kb: `${s.avg_size_kb} KB` }[k] ?? s[k]) : '—';
    if (which === 'src') dd.dataset.src = val;
    else dd.dataset.clip = val;
    dd.textContent = dd.dataset.clip ? `${dd.dataset.src} → ${dd.dataset.clip}` : dd.dataset.src || '—';
  });
}

async function open() {
  const p = $('path').value.trim().replace(/^"|"$/g, '');
  if (!p) { IA.toast('Enter a video path or use Browse…', 'warn'); return; }
  try { stats = await IA.post('/api/video/stats', { filepath: p }); }
  catch (e) { IA.toast(e.message, 'error'); return; }
  path = p;
  tIn = 0; tOut = stats.duration || 0;                     // new video → new range, even if it can't play
  $('stats').querySelectorAll('dd').forEach((dd) => { delete dd.dataset.clip; });
  showStats(stats, 'src');
  $('t-total').textContent = fmt(duration());
  updateRange();
  video.src = `/api/video/stream?path=${encodeURIComponent(path)}`;      // media cookie authenticates
  video.classList.remove('hidden');
  $('player-empty').classList.add('hidden');
  $('result').innerHTML = '';
  ['play', 'set-in', 'set-out', 'reset-range', 'export'].forEach((id) => { $(id).disabled = false; });
}

video.addEventListener('loadedmetadata', () => {
  tIn = 0; tOut = duration();                              // new video → reset the selection
  $('t-total').textContent = fmt(duration());
  updateRange();
});
video.addEventListener('timeupdate', () => {
  $('t-cur').textContent = fmt(video.currentTime);
  $('head').style.left = `${(video.currentTime / (video.duration || 1)) * 100}%`;
});
video.addEventListener('play', () => { $('play').querySelector('span').textContent = 'Pause'; });
video.addEventListener('pause', () => { $('play').querySelector('span').textContent = 'Play'; });
video.addEventListener('error', () => { if (path) IA.toast('The browser cannot play this file. Click the start / end times to type them — export still works.', 'warn', 7000); });

function updateRange() {
  const d = duration() || 1;
  const a = Math.min(tIn, tOut), b = Math.max(tIn, tOut);
  $('range').style.left = `${(a / d) * 100}%`;
  $('range').style.width = `${((b - a) / d) * 100}%`;
  $('in-lbl').textContent = fmt(a);
  $('out-lbl').textContent = fmt(b);
  $('len-lbl').textContent = fmt(b - a);
}

let scrubbing = false;
const seek = (e) => {
  const r = $('track').getBoundingClientRect();
  video.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (duration() || 0);
};
$('track').addEventListener('pointerdown', (e) => { if (!path) return; scrubbing = true; $('track').setPointerCapture(e.pointerId); seek(e); });
$('track').addEventListener('pointermove', (e) => { if (scrubbing) seek(e); });
$('track').addEventListener('pointerup', () => { scrubbing = false; });

$('play').addEventListener('click', () => (video.paused ? video.play() : video.pause()));
$('set-in').addEventListener('click', () => { tIn = video.currentTime; updateRange(); });
$('set-out').addEventListener('click', () => { tOut = video.currentTime; updateRange(); });
$('reset-range').addEventListener('click', () => { tIn = 0; tOut = duration(); updateRange(); });
// Type an exact start / end (also the only way when the browser can't play the file)
async function typeTime(which) {
  if (!path) return;
  const v = await IA.prompt({ title: which === 'in' ? 'Clip start' : 'Clip end', label: 'Time (h:mm:ss.ss, m:ss or seconds)',
    value: fmt(which === 'in' ? Math.min(tIn, tOut) : Math.max(tIn, tOut)), confirmLabel: 'Set',
    validate: (t) => { const s = parseTime(t); return !isFinite(s) || s < 0 || (duration() && s > duration()) ? 'Enter a time within the video' : ''; } });
  if (v === null) return;
  if (which === 'in') tIn = parseTime(v); else tOut = parseTime(v);
  updateRange();
}
['in-lbl', 'out-lbl'].forEach((id) => {
  $(id).style.cursor = 'pointer';
  $(id).title = 'Click to type a time';
  $(id).addEventListener('click', () => typeTime(id === 'in-lbl' ? 'in' : 'out'));
});
$('load').addEventListener('click', open);
$('path').addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
$('browse').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  IA.toast('The file dialog opens on the server screen', 'info', 2500);
  try { $('path').value = (await IA.get('/api/video/pick_import')).filepath; await open(); }
  catch (err) { if (err.status !== 400) IA.toast(err.message, 'error'); }
}));

document.addEventListener('keydown', (e) => {
  if (!path || e.target.matches('input, textarea') || document.querySelector('.overlay')) return;
  if (e.code === 'Space') { e.preventDefault(); $('play').click(); }
  if (e.key === 'i' || e.key === 'I') $('set-in').click();
  if (e.key === 'o' || e.key === 'O') $('set-out').click();
  if (e.key === 'ArrowLeft') video.currentTime = Math.max(0, video.currentTime - (e.shiftKey ? 5 : 1 / (stats?.fps || 25)));
  if (e.key === 'ArrowRight') video.currentTime = Math.min(duration(), video.currentTime + (e.shiftKey ? 5 : 1 / (stats?.fps || 25)));
});

$('export').addEventListener('click', (e) => IA.busy(e.currentTarget, async () => {
  const a = Math.min(tIn, tOut), b = Math.max(tIn, tOut);
  if (b - a < 0.05) { IA.toast('Set a start and end first', 'warn'); return; }
  const base = path.split(/[\\/]/).pop();
  const dot = base.lastIndexOf('.');
  const defaultName = `${dot > 0 ? base.slice(0, dot) : base}_cut${dot > 0 ? base.slice(dot) : '.mp4'}`;
  let save;
  try {
    IA.toast('Choose where to save — the dialog opens on the server screen', 'info', 2500);
    save = (await IA.get(`/api/video/pick_export?default_name=${encodeURIComponent(defaultName)}`)).filepath;
  } catch (err) { if (err.status !== 400) IA.toast(err.message, 'error'); return; }
  try {
    await IA.post('/api/video/cut', { filepath: path, save_path: save, start_sec: a, end_sec: b });
    $('result').innerHTML = `<div class="callout success">${IA.icon('circle-check')}<div>Clip saved to <code>${IA.esc(save)}</code></div></div>`;
    try { showStats(await IA.post('/api/video/stats', { filepath: save }), 'clip'); } catch { /* optional */ }
  } catch (err) {
    $('result').innerHTML = `<div class="callout danger">${IA.icon('circle-alert')}<div>${IA.esc(err.message)}</div></div>`;
  }
}));
})();
