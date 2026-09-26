/**
 * training.js — Invictus Annotate Training Page v3.0.0
 * Handles: system info, hardware polling, dataset validation + intelligence,
 *          memory slider, AI recommendations, param override tracking,
 *          warning box, model selection (recommended/mem-warning states),
 *          Socket.IO training events, sparkline charts, log terminal,
 *          results panel, models library, ETA estimation.
 */

'use strict';

// ── Constants ──────────────────────────────────────────────────────────────────

const YOLO_MODELS = {
  v8: [
    { key: 'yolov8n.pt', variant: 'N', name: 'YOLOv8', params: '3.2M', badge: 'nano',   tip: 'Fastest, lowest VRAM', idealDataset: '100 - 1,000 imgs',  memGb: 0.5 },
    { key: 'yolov8s.pt', variant: 'S', name: 'YOLOv8', params: '11.2M', badge: 'small', tip: 'Fast, good accuracy',   idealDataset: '500 - 5,000 imgs',  memGb: 1.0 },
    { key: 'yolov8m.pt', variant: 'M', name: 'YOLOv8', params: '25.9M', badge: 'medium',tip: 'Balanced',              idealDataset: '2,000 - 10k imgs',  memGb: 2.5 },
    { key: 'yolov8l.pt', variant: 'L', name: 'YOLOv8', params: '43.7M', badge: 'large', tip: 'High accuracy',        idealDataset: '5,000 - 20k+ imgs', memGb: 5.0 },
    { key: 'yolov8x.pt', variant: 'X', name: 'YOLOv8', params: '68.2M', badge: 'xlarge',tip: 'Best accuracy',        idealDataset: '10,000 - 50k+ imgs',memGb: 8.0 },
  ],
  v11: [
    { key: 'yolo11n.pt', variant: 'N', name: 'YOLO11', params: '2.6M',  badge: 'nano',   tip: 'Fastest YOLO11',      idealDataset: '100 - 1,000 imgs',  memGb: 0.4 },
    { key: 'yolo11s.pt', variant: 'S', name: 'YOLO11', params: '9.4M',  badge: 'small',  tip: 'Efficient & fast',    idealDataset: '500 - 5,000 imgs',  memGb: 0.9 },
    { key: 'yolo11m.pt', variant: 'M', name: 'YOLO11', params: '20.1M', badge: 'medium', tip: 'Latest balanced',     idealDataset: '2,000 - 10k imgs',  memGb: 2.0 },
    { key: 'yolo11l.pt', variant: 'L', name: 'YOLO11', params: '25.3M', badge: 'large',  tip: 'High accuracy',       idealDataset: '5,000 - 20k+ imgs', memGb: 4.0 },
    { key: 'yolo11x.pt', variant: 'X', name: 'YOLO11', params: '56.9M', badge: 'xlarge', tip: 'Best YOLO11 accuracy',idealDataset: '10,000 - 50k+ imgs',memGb: 7.0 },
  ],
};

const PRESETS = {
  fast:    { epochs: 10,  batch: 8,  imgsz: 320, patience: 10, lr0: 0.01,  optimizer: 'SGD',   workers: 2, device: 'cpu' },
  optimal: { epochs: 100, batch: 16, imgsz: 416, patience: 30, lr0: 0.01,  optimizer: 'SGD',   workers: 2, device: 'cpu' },
  highres: { epochs: 100, batch: 8,  imgsz: 640, patience: 50, lr0: 0.005, optimizer: 'AdamW', workers: 2, device: 'cpu' },
};

// ── State ──────────────────────────────────────────────────────────────────────

const State = {
  socket:           null,
  selectedModel:    null,
  datasetInfo:      null,
  yamlPath:         null,
  datasetName:      null,
  isTraining:       false,
  sessionId:        null,
  startTime:        null,
  autoScroll:       true,
  logLines:         0,
  charts:           { map50: null, loss: null },
  statsInterval:    null,
  elapsedInterval:  null,
  systemInfo:       {},
  recommendation:   null,         // last /recommend response
  userOverrides:    new Set(),    // param field IDs user has manually changed
  memoryBudgetGb:   null,         // current slider value
  totalMemoryGb:    0,            // VRAM or RAM total (for slider max)
  availMemoryGb:    0,            // available for slider context
  hasCuda:          false,
  avgEpochMs:       0,            // rolling average epoch duration from worker
};

// ── Init ───────────────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', () => {
  checkAuth();
  renderModelCards();
  initSocket();
  loadSystemInfo();
  startHardwarePolling();
  checkExistingSession();
  loadModelsLibrary();
  initCharts();
});

function checkAuth() {
  const token = localStorage.getItem('ia_token');
  if (!token) { window.location.href = '/login'; return; }
}

// ── Socket.IO ──────────────────────────────────────────────────────────────────

function initSocket() {
  const token = localStorage.getItem('ia_token');
  State.socket = io({ auth: { token } });

  State.socket.on('connect', () => {
    State.socket.emit('join_training_room');
    setConnected(true);
  });
  State.socket.on('disconnect', () => setConnected(false));
  State.socket.on('connect_error', () => setConnected(false));

  State.socket.on('train_log',      (d) => appendLog(d.line));
  State.socket.on('train_epoch',    (d) => onEpoch(d));
  State.socket.on('train_comment',  (d) => showComment(d.text, d.epoch));
  State.socket.on('train_hardware', (d) => updateHardwareAll(d));
  State.socket.on('train_complete', (d) => onComplete(d));
  State.socket.on('train_error',    (d) => onTrainError(d.message));
  State.socket.on('train_status',   (d) => syncStatus(d));
}

function setConnected(ok) {
  document.getElementById('conn-dot').classList.toggle('connected', ok);
  document.getElementById('conn-label').textContent = ok ? 'Connected' : 'Disconnected';
}

// ── System Info ────────────────────────────────────────────────────────────────

async function loadSystemInfo() {
  try {
    const r = await apiFetch('/api/train/system-info');
    State.systemInfo = r;
    State.hasCuda = r.cuda_available || false;
    // Store memory totals for slider
    State.totalMemoryGb   = r.cuda_available ? (r.vram_total_gb || 0) : (r.ram_total_gb || 0);
    State.availMemoryGb   = r.cuda_available ? (r.vram_total_gb || 0) : (r.ram_available_gb || 0);
    renderSystemInfo(r);
  } catch (e) {
    document.getElementById('si-gpu').textContent = 'Error loading system info';
  }
}

function renderSystemInfo(info) {
  // Top banner
  setText('si-gpu',      info.gpu_name || info.gpu_name_torch || (info.cuda_available ? 'GPU Detected' : 'CPU Mode'));
  setText('si-vram',     info.vram_total_mb ? `${Math.round(info.vram_total_mb / 1024 * 10) / 10} GB` : 'CPU Mode');
  setText('si-cuda-drv', info.cuda_driver_version || 'N/A');
  setText('si-cuda-rt',  info.cuda_version || 'N/A');
  setText('si-torch',    info.torch_version || 'Not installed');
  setText('si-ultr',     info.ultralytics_version || 'Not installed');
  setText('si-cpu',      `${info.cpu_count_physical || '?'}P / ${info.cpu_count || '?'}L`);
  setText('si-ram',      info.ram_total_gb ? `${info.ram_total_gb} GB` : 'N/A');

  if (info.cuda_available) {
    setText('model-vram-hint', info.vram_total_mb ? `${Math.round(info.vram_total_mb / 1024 * 10) / 10} GB VRAM` : '—');
  } else {
    setText('model-vram-hint', `${info.ram_total_gb || 14} GB RAM (CPU Execution Mode)`);
    const devInput = document.getElementById('p-device');
    if (devInput) devInput.value = 'cpu';
  }

  const torch_ok = info.torch_version && !info.torch_version.includes('Not');
  document.getElementById('si-torch').closest('.sys-card').classList.toggle('ok', torch_ok);
  document.getElementById('si-torch').closest('.sys-card').classList.toggle('err', !torch_ok);
  if (info.ultralytics_version && !info.ultralytics_version.includes('Not')) {
    document.getElementById('si-ultr').closest('.sys-card').classList.add('ok');
  }
}

// Render the detailed system capabilities panel (shown after dataset selection)
function renderSystemDetailPanel(info) {
  const grid = document.getElementById('sys-detail-grid');
  if (!grid) return;

  const hasCuda = info.cuda_available;
  const badge = document.getElementById('sys-mode-badge');
  if (badge) {
    badge.textContent = hasCuda ? 'GPU Mode' : 'CPU Mode';
    badge.classList.toggle('gpu', hasCuda);
  }

  const cells = [
    { label: 'CPU Cores', value: `${info.cpu_count_physical || '?'}P / ${info.cpu_count || '?'}L`,
      sub: info.cpu_freq_mhz ? `${(info.cpu_freq_mhz / 1000).toFixed(1)} GHz max` : '' },
    { label: 'Total RAM',     value: `${info.ram_total_gb || '?'} GB`,
      sub: `${info.ram_available_gb || '?'} GB available` },
    { label: hasCuda ? 'GPU' : 'Mode',
      value: hasCuda ? (info.gpu_name || info.gpu_name_torch || 'GPU') : 'CPU Only',
      sub: hasCuda ? `Driver ${info.driver_version || '—'}` : 'No CUDA detected' },
    { label: hasCuda ? 'VRAM' : 'Effective Mem',
      value: hasCuda ? `${info.vram_total_gb || '?'} GB` : `${info.ram_available_gb || '?'} GB`,
      sub: hasCuda ? 'GPU memory' : 'Available RAM' },
    { label: 'PyTorch',       value: info.torch_version || '—', sub: '' },
    { label: 'Ultralytics',   value: info.ultralytics_version || '—', sub: '' },
  ];

  grid.innerHTML = cells.map(c => `
    <div class="sys-detail-cell">
      <div class="sdc-label">${c.label}</div>
      <div class="sdc-value">${c.value}</div>
      ${c.sub ? `<div class="sdc-sub">${c.sub}</div>` : ''}
    </div>
  `).join('');

  // Memory slider
  buildMemorySlider(info);

  document.getElementById('sys-detail-panel').classList.remove('hidden');
}

// ── Memory Slider ──────────────────────────────────────────────────────────────

function buildMemorySlider(info) {
  const hasCuda = info.cuda_available;
  const totalGb = hasCuda ? (info.vram_total_gb || 0) : (info.ram_total_gb || 0);
  const availGb = hasCuda ? (info.vram_total_gb || 0) : (info.ram_available_gb || 0);

  if (totalGb <= 0) return; // nothing to allocate

  const slider = document.getElementById('mem-slider');
  const label  = document.getElementById('mem-slider-label');
  if (!slider || !label) return;

  label.textContent = hasCuda ? 'Allocate VRAM for Training' : 'Allocate RAM for Training';

  // Set slider range: 0.5 GB to total, step 0.5
  slider.min   = 0.5;
  slider.max   = totalGb;
  slider.step  = 0.5;

  // Default: 75% of available, rounded to nearest 0.5
  const defaultVal = Math.max(0.5, Math.round(availGb * 0.75 * 2) / 2);
  slider.value = Math.min(defaultVal, totalGb);

  onMemorySliderChange(slider.value);
}

function onMemorySliderChange(valStr) {
  const val      = parseFloat(valStr);
  const totalGb  = State.totalMemoryGb;
  if (!totalGb) return;

  State.memoryBudgetGb = val;

  const pct     = Math.round(val / totalGb * 100);
  const readout = document.getElementById('mem-readout');
  if (readout) readout.textContent = `${val.toFixed(1)} GB (${pct}%)`;

  // Colour the capacity bar
  const bar = document.getElementById('mem-capacity-bar');
  if (bar) {
    bar.style.width = Math.min(100, pct) + '%';
    bar.classList.remove('warn', 'danger');
    if (pct >= 90) bar.classList.add('danger');
    else if (pct >= 75) bar.classList.add('warn');
  }

  // Update slider hint
  const hint = document.getElementById('mem-slider-hint');
  const typeStr = State.hasCuda ? 'VRAM' : 'RAM';
  if (hint) {
    if (pct >= 90) {
      hint.textContent = `⚠ Allocating almost all ${typeStr} — leave headroom for the OS.`;
      hint.style.color = 'var(--orange)';
    } else if (pct >= 75) {
      hint.textContent = `This is a comfortable allocation. Batch size will be tuned accordingly.`;
      hint.style.color = 'var(--text-muted)';
    } else {
      hint.textContent = `Conservative allocation — good for background system use.`;
      hint.style.color = 'var(--text-muted)';
    }
  }

  // Update slider track fill colour via CSS gradient on background
  const slider = document.getElementById('mem-slider');
  if (slider) {
    const fillColor = pct >= 90 ? '#ff5f7e' : pct >= 75 ? '#ff9940' : '#7c6dff';
    slider.style.background = `linear-gradient(90deg, ${fillColor} 0%, ${fillColor} ${pct}%, var(--bg-elevated) ${pct}%, var(--bg-elevated) 100%)`;
  }

  // Re-evaluate model card compatibility and warnings
  updateModelCardMemStates();
  evaluateWarnings();
}

// ── Hardware Gauges ────────────────────────────────────────────────────────────

function startHardwarePolling() {
  setInterval(async () => {
    try {
      const stats = await apiFetch('/api/train/system-stats');
      updateHardwareAll(stats);
    } catch (_) {}
  }, 2000);
}

function updateHardwareAll(stats) {
  setGauge('cpu',  stats.cpu_percent  || 0, `${stats.cpu_percent || 0}%`);
  setGauge('ram',  stats.ram_percent  || 0, `${stats.ram_percent || 0}%`);
  setGauge('gpu',  stats.gpu_util     || 0, `${stats.gpu_util || 0}%`, `${stats.gpu_temp || 0}°C`);
  setGauge('vram', stats.vram_percent || 0, `${stats.vram_percent || 0}%`,
    `${stats.vram_used_mb || 0}/${stats.vram_total_mb || 0}MB`);

  setMiniBar('cpu',  stats.cpu_percent  || 0, `${stats.cpu_percent || 0}%`);
  setMiniBar('ram',  stats.ram_percent  || 0, `${Math.round((stats.ram_used_gb||0)*10)/10}/${stats.ram_total_gb||0} GB`);
  setMiniBar('gpu',  stats.gpu_util     || 0, `${stats.gpu_util || 0}%`);
  setMiniBar('vram', stats.vram_percent || 0, `${stats.vram_used_mb || 0} / ${stats.vram_total_mb || 0} MB`);
}

function setGauge(id, pct, valText, subText) {
  const fill = document.getElementById(`gauge-${id}-fill`);
  const val  = document.getElementById(`gauge-${id}-val`);
  const sub  = document.getElementById(`gauge-${id}-sub`);
  if (!fill) return;
  const clampedPct = Math.min(100, Math.max(0, pct));
  fill.style.width = clampedPct + '%';
  if (val && valText !== undefined) val.textContent = valText;
  if (sub && subText !== undefined) sub.textContent = subText;
}

function setMiniBar(id, pct, valText) {
  const bar = document.getElementById(`hw-mini-${id}`);
  const val = document.getElementById(`hw-mini-${id}-val`);
  if (bar) bar.style.width = Math.min(100, pct) + '%';
  if (val && valText !== undefined) val.textContent = valText;
}

// ── Model Cards ────────────────────────────────────────────────────────────────

function renderModelCards() {
  renderModelGrid('grid-v8',  YOLO_MODELS.v8);
  renderModelGrid('grid-v11', YOLO_MODELS.v11);
}

function renderModelGrid(containerId, models) {
  const grid = document.getElementById(containerId);
  if (!grid) return;
  grid.innerHTML = models.map(m => `
    <div class="model-card" id="mcard-${m.key.replace('.','_')}"
         onclick="selectModel('${m.key}')" title="${m.tip}">
      <div class="mc-variant">${m.variant}</div>
      <div class="mc-name">${m.name}</div>
      <div class="mc-params">${m.params}</div>
      <div class="mc-badge ${m.badge}">${m.badge}</div>
      <div class="mc-dataset-size">Ideal: ${m.idealDataset}</div>
    </div>
  `).join('');
}

function selectModel(key) {
  document.querySelectorAll('.model-card').forEach(c => c.classList.remove('selected'));
  const card = document.getElementById('mcard-' + key.replace('.', '_'));
  if (card) card.classList.add('selected');
  document.getElementById('custom-model-input').value = '';
  State.selectedModel = key;
  updateStepTag('model', key, 'selected');
  updateStepNum(2, 'active');
  checkLaunchReady();

  // Enable param recommendations now that user selected a model
  const btnParams = document.getElementById('btn-apply-params');
  const recNote   = document.getElementById('rec-note');
  if (btnParams) btnParams.disabled = false;
  if (recNote && State.recommendation) {
    const rec = State.recommendation;
    if (rec.model === key) {
      recNote.textContent = 'This is the recommended model. Apply params below.';
    } else {
      recNote.textContent = `You selected ${key}. Params will be tuned for this model.`;
      // Re-run recommendation with this model as hint
      loadRecommendations(key);
    }
  }
  evaluateWarnings();
}

function onCustomModelInput() {
  const val = document.getElementById('custom-model-input').value.trim();
  if (val) {
    document.querySelectorAll('.model-card').forEach(c => c.classList.remove('selected'));
    State.selectedModel = val;
    updateStepTag('model', val, 'selected');
    updateStepNum(2, 'active');
    const btnParams = document.getElementById('btn-apply-params');
    if (btnParams) btnParams.disabled = false;
  } else if (!State.selectedModel) {
    updateStepTag('model', '', 'pending');
  }
  checkLaunchReady();
  evaluateWarnings();
}

// Update model card visual states based on memory budget
function updateModelCardMemStates() {
  const budget  = State.memoryBudgetGb;
  const recKey  = State.recommendation?.model;
  const allModels = [...YOLO_MODELS.v8, ...YOLO_MODELS.v11];

  allModels.forEach(m => {
    const card = document.getElementById('mcard-' + m.key.replace('.', '_'));
    if (!card) return;

    card.classList.remove('recommended', 'mem-warning');
    // Remove any existing badges
    card.querySelectorAll('.mc-rec-badge, .mc-mem-warn').forEach(b => b.remove());

    if (m.key === recKey) {
      card.classList.add('recommended');
      card.insertAdjacentHTML('beforeend', `<div class="mc-rec-badge">AI Pick</div>`);
    }

    if (budget !== null && budget < m.memGb) {
      card.classList.add('mem-warning');
      if (m.key !== recKey) {
        card.insertAdjacentHTML('beforeend', `<div class="mc-mem-warn">Low Mem</div>`);
      }
    }
  });
}

// ── Dataset Picker ─────────────────────────────────────────────────────────────

async function pickDatasetFolder() {
  try {
    showToast('Opening folder picker…', 'info');
    const r = await apiFetch('/api/train/pick-dataset');
    document.getElementById('dataset-path-input').value = r.path;
    await validateDatasetPath(r.path);
  } catch (e) {
    if (!e.message.includes('No folder')) showToast('Could not open picker: ' + e.message, 'error');
  }
}

function onDatasetPathInput() {
  document.getElementById('dataset-info').classList.add('hidden');
  State.datasetInfo = null;
  State.yamlPath = null;
  updateStepTag('dataset', '', 'pending');
  checkLaunchReady();
}

async function validateCurrentPath() {
  const path = document.getElementById('dataset-path-input').value.trim();
  if (!path) { showToast('Enter a folder path first', 'error'); return; }
  await validateDatasetPath(path);
}

async function validateDatasetPath(folderPath) {
  try {
    const r = await apiFetch('/api/train/validate-dataset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: folderPath }),
    });
    renderDatasetInfo(r);
  } catch (e) {
    showToast('Validation error: ' + e.message, 'error');
  }
}

function renderDatasetInfo(info) {
  State.datasetInfo = info;

  // Split cards
  const grid = document.getElementById('splits-grid');
  const splits = info.splits || {};
  grid.innerHTML = ['train', 'valid', 'test'].map(s => {
    const split = splits[s] || {};
    const isRequired = s === 'train';
    const cls   = split.exists ? 'ok' : (isRequired ? 'miss' : 'warn');
    const icon  = split.exists ? '✅' : (isRequired ? '❌' : '⚠️');
    const count = split.exists ? `${split.count} imgs` : (isRequired ? 'Missing!' : 'Optional');
    return `
      <div class="split-info-card ${cls}">
        <div class="split-icon">${icon}</div>
        <div>
          <div class="split-name">${s}/</div>
          <div class="split-count">${count}</div>
        </div>
      </div>
    `;
  }).join('');

  // YAML row
  const yaml = info.yaml || {};
  const yamlIcon  = document.getElementById('yaml-status-icon');
  const yamlText  = document.getElementById('yaml-status-text');
  const yamlInput = document.getElementById('yaml-path-input');

  if (yaml.exists && yaml.path) {
    yamlIcon.textContent = '📄';
    yamlText.textContent = 'data.yaml found';
    yamlText.style.color = 'var(--green)';
    yamlInput.value = yaml.path;
    document.getElementById('btn-pick-yaml').style.display = 'none';
    State.yamlPath = yaml.path;
  } else {
    yamlIcon.textContent = '⚠️';
    yamlText.textContent = 'No yaml found — select manually:';
    yamlText.style.color = 'var(--yellow)';
    yamlInput.value = '';
    document.getElementById('btn-pick-yaml').style.display = '';
    State.yamlPath = null;
  }

  // Errors / warnings
  const errDiv = document.getElementById('validation-errors');
  const msgs = [
    ...(info.errors   || []).map(e => ({ cls: 'err',  icon: '❌', text: e })),
    ...(info.warnings || []).map(w => ({ cls: 'warn', icon: '⚠️', text: w })),
  ];
  errDiv.innerHTML = msgs.map(m =>
    `<div class="validation-msg ${m.cls}">${m.icon} ${m.text}</div>`
  ).join('');

  document.getElementById('dataset-info').classList.remove('hidden');

  // Dataset name
  State.datasetName = info.path ? info.path.split(/[\\\/]/).pop().replace(/[^\w\-]/g, '_') : 'dataset';

  if (info.is_valid) {
    updateStepTag('dataset', State.datasetName, 'selected');
    updateStepNum(1, 'done');
    updateStepNum(2, 'active');
  } else {
    updateStepTag('dataset', 'Errors found', 'pending');
  }

  // Render extended dataset intelligence
  renderDatasetIntelligence(info);

  // Show system detail panel now that we have context
  renderSystemDetailPanel(State.systemInfo);

  checkLaunchReady();

  // Load AI recommendations
  if (info.is_valid) {
    loadRecommendations();
  }
}

function renderDatasetIntelligence(info) {
  const panel = document.getElementById('dataset-intel');
  if (!panel) return;

  const statsRow   = document.getElementById('intel-stats-row');
  const splitWrap  = document.getElementById('intel-split-bar-wrap');
  const classesRow = document.getElementById('intel-classes-row');

  // Key stats
  const totalImages = info.total_images || 0;
  const sizeMb      = info.total_size_mb || 0;
  const sizeStr     = sizeMb >= 1024 ? `${(sizeMb/1024).toFixed(1)} GB` : `${sizeMb} MB`;
  const nc          = info.num_classes;
  const splits      = info.splits || {};

  statsRow.innerHTML = [
    { label: 'Total Images',  value: totalImages.toLocaleString(), sub: 'across all splits' },
    { label: 'Dataset Size',  value: sizeStr, sub: 'on disk' },
    { label: 'Classes',       value: nc !== null && nc !== undefined ? nc : '—', sub: 'object categories' },
    { label: 'Train Images',  value: (splits.train?.count || 0).toLocaleString(), sub: 'training set' },
    { label: 'Val Images',    value: splits.valid?.exists ? (splits.valid?.count || 0).toLocaleString() : '—', sub: 'validation set' },
  ].map(s => `
    <div class="intel-stat">
      <div class="ist-label">${s.label}</div>
      <div class="ist-value">${s.value}</div>
      <div class="ist-sub">${s.sub}</div>
    </div>
  `).join('');

  // Split ratio bar
  const ratios = info.split_ratios || {};
  if (Object.keys(ratios).length > 0) {
    const trainPct = ratios.train || 0;
    const validPct = ratios.valid || 0;
    const testPct  = ratios.test  || 0;
    const remaining = Math.max(0, 100 - trainPct - validPct - testPct);

    splitWrap.innerHTML = `
      <div class="split-ratio-bar-label">Dataset Split Distribution</div>
      <div class="split-ratio-bar">
        ${trainPct > 0 ? `<div class="split-ratio-seg train" style="flex:${trainPct}" title="Train: ${trainPct}%"></div>` : ''}
        ${validPct > 0 ? `<div class="split-ratio-seg valid" style="flex:${validPct}" title="Valid: ${validPct}%"></div>` : ''}
        ${testPct > 0  ? `<div class="split-ratio-seg test"  style="flex:${testPct}"  title="Test: ${testPct}%"></div>` : ''}
      </div>
      <div class="split-ratio-legend">
        ${trainPct > 0 ? `<span><div class="split-legend-dot" style="background:var(--green)"></div>Train ${trainPct}%</span>` : ''}
        ${validPct > 0 ? `<span><div class="split-legend-dot" style="background:var(--blue)"></div>Valid ${validPct}%</span>` : ''}
        ${testPct  > 0 ? `<span><div class="split-legend-dot" style="background:var(--accent-light)"></div>Test ${testPct}%</span>` : ''}
      </div>
    `;
    splitWrap.classList.remove('hidden');
  }

  // Class name chips (show up to 20, then "+N more")
  const classNames = info.class_names || [];
  if (classNames.length > 0) {
    const MAX_SHOW = 20;
    const shown = classNames.slice(0, MAX_SHOW);
    const extra = classNames.length - MAX_SHOW;
    classesRow.innerHTML = `
      <div class="intel-classes-label">Class Names</div>
      <div class="class-chips">
        ${shown.map(n => `<span class="class-chip">${n}</span>`).join('')}
        ${extra > 0 ? `<span class="class-chip-more">+${extra} more</span>` : ''}
      </div>
    `;
    classesRow.classList.remove('hidden');
  }

  panel.classList.remove('hidden');
}

async function pickYamlFile() {
  try {
    const r = await apiFetch('/api/train/pick-yaml');
    document.getElementById('yaml-path-input').value = r.path;
    State.yamlPath = r.path;
    checkLaunchReady();
  } catch (e) {
    showToast('No file selected', 'error');
  }
}

// ── AI Recommendations ─────────────────────────────────────────────────────────

async function loadRecommendations(overrideModel) {
  const info = State.datasetInfo;
  const sys  = State.systemInfo;
  if (!info || !sys) return;

  try {
    const body = {
      total_images:       info.total_images     || 0,
      num_classes:        info.num_classes       || null,
      cuda_available:     sys.cuda_available     || false,
      vram_total_gb:      sys.vram_total_gb      || 0,
      ram_available_gb:   sys.ram_available_gb   || 8,
      cpu_count_physical: sys.cpu_count_physical || 2,
      memory_budget_gb:   State.memoryBudgetGb   || null,
    };

    const rec = await apiFetch('/api/train/recommend', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });

    State.recommendation = rec;

    // If user has already selected a different model, override the rec model for display
    if (overrideModel) rec._userModelKey = overrideModel;

    renderRecommendations(rec);
    updateModelCardMemStates();

  } catch (e) {
    // Silently fail — recommendations are advisory
    console.warn('Recommendation engine error:', e.message);
  }
}

function renderRecommendations(rec) {
  const card = document.getElementById('rec-card');
  if (!card) return;

  const summary = document.getElementById('rec-summary');
  const fields  = document.getElementById('rec-fields-grid');
  const badge   = document.getElementById('rec-confidence-badge');

  const ds   = State.datasetInfo;
  const sys  = State.systemInfo;
  const cuda = sys.cuda_available;
  const imgs = ds?.total_images || 0;
  const nc   = ds?.num_classes  || 1;
  const mem  = cuda ? (sys.vram_total_gb || 0) : (sys.ram_available_gb || 0);

  // Confidence level
  let confidence = 'Optimal';
  let badgeClass = '';
  if (!cuda && imgs > 5000) { confidence = 'Feasible'; badgeClass = 'feasible'; }
  else if (imgs < 100) { confidence = 'Good'; badgeClass = 'good'; }

  badge.textContent = confidence;
  badge.className   = 'rec-confidence-badge ' + badgeClass;

  if (summary) {
    const modelLabel = rec.model.replace('.pt', '');
    summary.innerHTML = `
      Recommended <strong style="color:var(--text-primary)">${modelLabel}</strong>
      for ${imgs.toLocaleString()} images
      ${nc > 1 ? `across ${nc} classes` : ''}
      on ${cuda ? `GPU (${sys.gpu_name || 'CUDA'}) with ${mem} GB VRAM` : `CPU with ${mem.toFixed(1)} GB RAM`}.
    `;
  }

  // Recommended fields (model shown separately via Apply Recommended Model)
  const fieldDefs = [
    { key: 'epochs',    label: 'Epochs',       val: rec.epochs },
    { key: 'batch',     label: 'Batch Size',    val: rec.batch },
    { key: 'imgsz',     label: 'Image Size',    val: `${rec.imgsz} px` },
    { key: 'patience',  label: 'Patience',      val: rec.patience },
    { key: 'lr0',       label: 'Learning Rate', val: rec.lr0 },
    { key: 'optimizer', label: 'Optimizer',     val: rec.optimizer },
    { key: 'workers',   label: 'Workers',       val: rec.workers },
    { key: 'device',    label: 'Device',        val: rec.device },
  ];

  if (fields) {
    fields.innerHTML = fieldDefs.map(f => `
      <div class="rec-field-row" title="${rec.rationale?.[f.key] || ''}">
        <div class="rec-field-name">${f.label}</div>
        <div class="rec-field-value">${f.val}</div>
        <div class="rec-field-why">${rec.rationale?.[f.key] || ''}</div>
      </div>
    `).join('');
  }

  card.classList.remove('hidden');
}

function applyModelRecommendation() {
  const rec = State.recommendation;
  if (!rec) return;
  selectModel(rec.model);
  showToast(`Recommended model ${rec.model} selected`, 'success');
}

function applyParamRecommendations() {
  const rec = State.recommendation;
  if (!rec) return;

  // Apply all params from recommendation (not the model — that's separate)
  const paramMap = {
    'p-epochs':    rec.epochs,
    'p-batch':     rec.batch,
    'p-imgsz':     rec.imgsz,
    'p-patience':  rec.patience,
    'p-lr0':       rec.lr0,
    'p-optimizer': rec.optimizer,
    'p-workers':   rec.workers,
    'p-device':    rec.device,
  };

  Object.entries(paramMap).forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.tagName === 'SELECT') {
      for (const opt of el.options) {
        if (String(opt.value) === String(val)) { opt.selected = true; break; }
      }
    } else {
      el.value = val;
    }
    // Mark as AI recommended (clear custom override)
    State.userOverrides.delete(id);
    setParamDot(id, 'ai');
  });

  showToast('Recommended parameters applied', 'success');
  evaluateWarnings();
}

// ── Parameter Override Tracking ────────────────────────────────────────────────

function onParamChange(paramName) {
  const idMap = {
    epochs:    'p-epochs',
    batch:     'p-batch',
    imgsz:     'p-imgsz',
    patience:  'p-patience',
    lr0:       'p-lr0',
    optimizer: 'p-optimizer',
    workers:   'p-workers',
    device:    'p-device',
  };
  const fieldId = idMap[paramName];
  if (!fieldId) return;

  const rec = State.recommendation;
  const el  = document.getElementById(fieldId);
  if (!el) return;

  const currentVal = el.value;
  const recVal     = rec ? String(rec[paramName]) : null;

  // Determine if the user has overridden the recommendation
  if (recVal !== null && String(currentVal) !== recVal) {
    State.userOverrides.add(fieldId);
    setParamDot(fieldId, 'custom');
  } else if (recVal !== null) {
    State.userOverrides.delete(fieldId);
    setParamDot(fieldId, 'ai');
  } else {
    setParamDot(fieldId, 'none');
  }

  evaluateWarnings();
}

function setParamDot(fieldId, state) {
  const paramName = fieldId.replace('p-', '');
  const aiDot     = document.getElementById(`ai-dot-${paramName}`);
  const custDot   = document.getElementById(`custom-dot-${paramName}`);

  if (aiDot)   aiDot.classList.toggle('hidden',   state !== 'ai');
  if (custDot) custDot.classList.toggle('hidden', state !== 'custom');
}

// Mark all rec fields as AI when rec applied
function markAllParamDotsAI() {
  ['epochs','batch','imgsz','patience','lr0','optimizer','workers','device'].forEach(p => {
    setParamDot(`p-${p}`, 'ai');
  });
}

// ── Parameters / Presets ───────────────────────────────────────────────────────

function loadPreset(name) {
  const p = PRESETS[name];
  if (!p) return;
  setVal('p-epochs',    p.epochs);
  setVal('p-batch',     p.batch);
  setVal('p-imgsz',     p.imgsz, true);
  setVal('p-patience',  p.patience);
  setVal('p-lr0',       p.lr0);
  setVal('p-optimizer', p.optimizer, true);
  setVal('p-workers',   p.workers);
  // Clear all AI/custom dots since preset overrides everything
  State.userOverrides.clear();
  ['epochs','batch','imgsz','patience','lr0','optimizer','workers','device'].forEach(p => setParamDot(`p-${p}`, 'none'));
  showToast(`Preset "${name}" loaded`, 'info');
  evaluateWarnings();
}

function setVal(id, val, isSelect) {
  const el = document.getElementById(id);
  if (!el) return;
  if (isSelect) {
    for (const opt of el.options) {
      if (String(opt.value) === String(val)) { opt.selected = true; break; }
    }
  } else {
    el.value = val;
  }
}

function toggleAdvanced() {
  const panel = document.getElementById('advanced-params');
  const arrow = document.getElementById('advanced-arrow');
  const hidden = panel.classList.toggle('hidden');
  arrow.textContent = hidden ? '▶' : '▼';
}

function getTrainingConfig() {
  const yamlInput = document.getElementById('yaml-path-input').value.trim();
  const yaml      = yamlInput || State.yamlPath;

  return {
    dataset_path:      document.getElementById('dataset-path-input').value.trim(),
    yaml_path:         yaml,
    model:             State.selectedModel,
    dataset_name:      State.datasetName || 'dataset',
    run_name_override: document.getElementById('run-name-input').value.trim() || null,
    epochs:            parseInt(document.getElementById('p-epochs').value)    || 100,
    batch:             parseInt(document.getElementById('p-batch').value)     || 16,
    imgsz:             parseInt(document.getElementById('p-imgsz').value)     || 640,
    patience:          parseInt(document.getElementById('p-patience').value)  || 50,
    lr0:               parseFloat(document.getElementById('p-lr0').value)     || 0.01,
    optimizer:         document.getElementById('p-optimizer').value,
    workers:           parseInt(document.getElementById('p-workers').value)   || 4,
    device:            document.getElementById('p-device').value.trim()       || '0',
    cos_lr:            document.getElementById('p-cos-lr').value === 'true',
    warmup_epochs:     parseFloat(document.getElementById('p-warmup').value)  || 3.0,
    weight_decay:      parseFloat(document.getElementById('p-wd').value)      || 0.0005,
    mosaic:            parseFloat(document.getElementById('p-mosaic').value)  || 1.0,
    memory_budget_gb:  State.memoryBudgetGb || null,
  };
}

// ── Warning Evaluation ─────────────────────────────────────────────────────────

function evaluateWarnings() {
  const sys   = State.systemInfo;
  const ds    = State.datasetInfo;
  const model = State.selectedModel;
  const budget = State.memoryBudgetGb;

  const epochs    = parseInt(document.getElementById('p-epochs')?.value) || 100;
  const batch     = parseInt(document.getElementById('p-batch')?.value)  || 16;
  const imgsz     = parseInt(document.getElementById('p-imgsz')?.value)  || 416;
  const lr0       = parseFloat(document.getElementById('p-lr0')?.value)  || 0.01;
  const workers   = parseInt(document.getElementById('p-workers')?.value) || 2;
  const device    = document.getElementById('p-device')?.value?.trim()   || 'cpu';

  const hasCuda   = sys.cuda_available || false;
  const vram      = sys.vram_total_gb  || 0;
  const ramAvail  = sys.ram_available_gb || 8;
  const cpuPhy    = sys.cpu_count_physical || 2;
  const imgs      = ds?.total_images    || 0;

  const warnings = [];

  const W = (icon, text, severity, fix) => ({ icon, text, severity, fix });

  // ── OOM / memory checks ───────────────────────────────────────────────────
  if (hasCuda && budget !== null) {
    // Rough GPU OOM estimate: batch * ~200MB for 640px medium model
    const modelMem = getAllModelMem(model);
    const estimatedVramGb = modelMem + batch * (imgsz / 640) * 0.2;
    if (estimatedVramGb > budget) {
      warnings.push(W('❌',
        `Batch ${batch} at ${imgsz}px may exceed your ${budget.toFixed(1)} GB VRAM budget (est. ${estimatedVramGb.toFixed(1)} GB needed). Reduce batch or image size.`,
        'danger',
        () => { setVal('p-batch', Math.max(1, Math.floor(batch / 2))); onParamChange('batch'); }
      ));
    }
  }

  if (!hasCuda && batch > 16 && ramAvail < 16) {
    warnings.push(W('⚠️',
      `Batch size ${batch} on CPU with ${ramAvail.toFixed(1)} GB available RAM may cause slowdowns or OOM. Recommended ≤ 16.`,
      'warning',
      () => { setVal('p-batch', 16); onParamChange('batch'); }
    ));
  }

  // ── Epoch checks ──────────────────────────────────────────────────────────
  if (epochs > 300 && imgs < 500) {
    warnings.push(W('⚠️',
      `${epochs} epochs with only ${imgs} images is likely to cause heavy overfitting. Consider ≤ 150 epochs with early stopping.`,
      'warning',
      () => { setVal('p-epochs', 100); onParamChange('epochs'); }
    ));
  }
  if (epochs < 10) {
    warnings.push(W('ℹ️',
      `Only ${epochs} epochs — the model may not converge. Minimum 30–50 epochs is usually needed.`,
      'info',
      null
    ));
  }

  // ── Image size on CPU ─────────────────────────────────────────────────────
  if (!hasCuda && imgsz >= 1280) {
    warnings.push(W('❌',
      `1280 px on CPU will be extremely slow — expect hours per epoch. Use ≤ 640 px for practical CPU training.`,
      'danger',
      () => { setVal('p-imgsz', 416, true); onParamChange('imgsz'); }
    ));
  }
  if (!hasCuda && imgsz > 640) {
    warnings.push(W('⚠️',
      `${imgsz} px on CPU may be very slow. 416 px is the recommended balance for CPU training.`,
      'warning',
      () => { setVal('p-imgsz', 416, true); onParamChange('imgsz'); }
    ));
  }

  // ── Learning rate ─────────────────────────────────────────────────────────
  if (lr0 > 0.05) {
    warnings.push(W('⚠️',
      `Learning rate ${lr0} is very high and may cause unstable training. Typical range is 0.001–0.01.`,
      'warning',
      () => { setVal('p-lr0', 0.01); onParamChange('lr0'); }
    ));
  }

  // ── Workers ───────────────────────────────────────────────────────────────
  if (workers > cpuPhy && !hasCuda) {
    warnings.push(W('ℹ️',
      `Workers (${workers}) exceeds physical CPU cores (${cpuPhy}). This may slow data loading due to context switching.`,
      'info',
      () => { setVal('p-workers', cpuPhy); onParamChange('workers'); }
    ));
  }

  // ── Large model on CPU ────────────────────────────────────────────────────
  if (!hasCuda && model && (model.includes('yolov8l') || model.includes('yolov8x') ||
                             model.includes('yolo11l') || model.includes('yolo11x'))) {
    warnings.push(W('❌',
      `Large/XL models on CPU are prohibitively slow. Use nano or small models for CPU training.`,
      'danger',
      () => {
        const rec = State.recommendation;
        if (rec) selectModel(rec.model);
      }
    ));
  }

  // ── Device mismatch ───────────────────────────────────────────────────────
  if (device !== 'cpu' && device !== '' && !hasCuda) {
    warnings.push(W('⚠️',
      `Device is set to "${device}" but no CUDA GPU was detected. Training will fall back to CPU automatically.`,
      'warning',
      () => { setVal('p-device', 'cpu'); onParamChange('device'); }
    ));
  }

  renderWarningBox(warnings);
}

function getAllModelMem(modelKey) {
  const allModels = [...YOLO_MODELS.v8, ...YOLO_MODELS.v11];
  const m = allModels.find(m => m.key === modelKey);
  return m ? m.memGb : 0.5;
}

function renderWarningBox(warnings) {
  const box   = document.getElementById('warning-box');
  const body  = document.getElementById('warning-box-body');
  const badge = document.getElementById('warning-count-badge');
  if (!box || !body) return;

  if (warnings.length === 0) {
    box.classList.add('hidden');
    return;
  }

  box.classList.remove('hidden');
  if (badge) badge.textContent = warnings.length;

  body.innerHTML = warnings.map((w, i) => `
    <div class="warning-item severity-${w.severity}">
      <span class="wi-icon">${w.icon}</span>
      <span class="wi-text">${w.text}</span>
      ${w.fix ? `<button class="wi-fix-btn" onclick="warningFix(${i})">Fix</button>` : ''}
    </div>
  `).join('');

  // Store fix callbacks globally for onclick
  window._warningFixes = warnings.map(w => w.fix);
}

window.warningFix = function(index) {
  const fix = window._warningFixes?.[index];
  if (typeof fix === 'function') fix();
};

// ── Launch / Stop ──────────────────────────────────────────────────────────────

function checkLaunchReady() {
  const ready = State.datasetInfo?.is_valid
             && State.selectedModel
             && (State.yamlPath || document.getElementById('yaml-path-input')?.value.trim())
             && !State.isTraining;

  const btn  = document.getElementById('btn-launch');
  btn.disabled = !ready;
  const hint = document.getElementById('launch-hint');

  if (!State.datasetInfo?.is_valid) {
    hint.textContent = 'Select and validate a dataset to continue';
  } else if (!State.selectedModel) {
    hint.textContent = 'Select a YOLO model to continue';
  } else if (!State.yamlPath && !document.getElementById('yaml-path-input')?.value.trim()) {
    hint.textContent = 'Provide a data.yaml path to continue';
  } else if (State.isTraining) {
    hint.textContent = 'Training in progress…';
  } else {
    hint.textContent = `Ready to train ${State.selectedModel} on "${State.datasetName}"`;
  }
}

async function beginTraining() {
  const cfg = getTrainingConfig();

  if (!cfg.yaml_path) {
    showToast('Please provide the path to your data.yaml', 'error');
    return;
  }
  if (!cfg.model) {
    showToast('Select a model first', 'error');
    return;
  }

  try {
    document.getElementById('btn-launch').disabled = true;
    document.getElementById('btn-launch').textContent = '⏳ Starting…';

    const r = await apiFetch('/api/train/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(cfg),
    });

    State.isTraining = true;
    State.sessionId  = r.session_id;
    State.startTime  = Date.now();
    State.avgEpochMs = 0;

    enterTrainingPhase(r.total_epochs);
    appendLog(`[INFO] Session ${r.session_id} started — ${cfg.model} for ${r.total_epochs} epochs`);
    showToast('Training started!', 'success');

  } catch (e) {
    showToast('Failed to start training: ' + e.message, 'error');
    document.getElementById('btn-launch').disabled = false;
    document.getElementById('btn-launch').textContent = '🚀 Begin Training';
  }
}

function confirmStop() {
  openModal('⏹ Stop Training', `
    <p style="color:var(--text-secondary);font-size:14px;margin-bottom:8px">
      Are you sure you want to stop training? The current best weights will be saved.
    </p>
  `, [
    { label: 'Cancel',      cls: 'btn-secondary', fn: () => closeModal() },
    { label: '⏹ Stop Now', cls: 'btn-danger',    fn: () => { closeModal(); stopTraining(); } },
  ]);
}

async function stopTraining() {
  try {
    await apiFetch('/api/train/stop', { method: 'POST' });
    showToast('Stop signal sent…', 'info');
    appendLog('[INFO] Stop requested — waiting for process to exit…');
  } catch (e) {
    showToast('Error stopping training: ' + e.message, 'error');
  }
}

// ── Training Phase UI ──────────────────────────────────────────────────────────

function enterTrainingPhase(totalEpochs) {
  show('console-container', true);
  document.getElementById('setup-container').style.opacity = '0.5';
  document.getElementById('setup-container').style.pointerEvents = 'none';

  show('train-status-banner', true);
  show('stats-stop-card', true);

  setText('sc-total', totalEpochs);
  updateEpochProgress(0, totalEpochs);

  const btn = document.getElementById('btn-launch');
  btn.textContent = '⏳ Training…';
  btn.disabled = true;

  State.startTime = Date.now();
  State.elapsedInterval = setInterval(updateElapsed, 1000);
}

function updateElapsed() {
  if (!State.startTime) return;
  const secs = Math.floor((Date.now() - State.startTime) / 1000);
  const h = Math.floor(secs / 3600).toString().padStart(2, '0');
  const m = Math.floor((secs % 3600) / 60).toString().padStart(2, '0');
  const s = (secs % 60).toString().padStart(2, '0');
  setText('train-elapsed', `${h}:${m}:${s}`);
}

function updateEpochProgress(epoch, total) {
  const pct = total > 0 ? Math.round(epoch / total * 100) : 0;
  setText('ep-label-text', `Epoch ${epoch} / ${total}`);
  setText('ep-pct-text', `${pct}%`);
  const fill = document.getElementById('ep-fill');
  if (fill) fill.style.width = pct + '%';
  const scFill = document.getElementById('sc-fill');
  if (scFill) scFill.style.width = pct + '%';
  setText('sc-epoch', epoch);
}

// Update ETA in both the top banner and stats panel
function updateETA(epochsDone, totalEpochs) {
  if (!State.avgEpochMs || epochsDone === 0) return;

  const remaining     = totalEpochs - epochsDone;
  const etaMs         = remaining * State.avgEpochMs;
  const etaStr        = formatDuration(Math.round(etaMs / 1000));

  // Stats panel ETA
  const etaRow = document.getElementById('eta-row');
  const scEta  = document.getElementById('sc-eta');
  if (etaRow && scEta) {
    etaRow.classList.remove('hidden');
    scEta.textContent = etaStr;
  }

  // Top banner ETA
  const bannerEta    = document.getElementById('train-eta');
  const bannerEtaVal = document.getElementById('train-eta-val');
  if (bannerEta && bannerEtaVal) {
    bannerEta.classList.remove('hidden');
    bannerEtaVal.textContent = etaStr;
  }
}

function formatDuration(totalSecs) {
  if (totalSecs <= 0) return '< 1s';
  const h = Math.floor(totalSecs / 3600);
  const m = Math.floor((totalSecs % 3600) / 60);
  const s = totalSecs % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ── Socket.IO Event Handlers ───────────────────────────────────────────────────

function onEpoch(data) {
  const epoch   = data.epoch || 0;
  const total   = data.total_epochs || parseInt(document.getElementById('sc-total').textContent) || 0;
  const metrics = data.metrics || {};
  const losses  = data.loss_items || [];

  // Update rolling ETA
  if (data.avg_epoch_ms && data.avg_epoch_ms > 0) {
    State.avgEpochMs = data.avg_epoch_ms;
  }

  updateEpochProgress(epoch, total);
  updateETA(epoch, total);

  const map50   = metrics['metrics/mAP50(B)']    ?? metrics['mAP50']    ?? null;
  const map5095 = metrics['metrics/mAP50-95(B)'] ?? metrics['mAP50-95'] ?? null;
  const prec    = metrics['metrics/precision(B)'] ?? metrics['precision'] ?? null;
  const rec     = metrics['metrics/recall(B)']    ?? metrics['recall']    ?? null;

  if (map50 !== null) {
    setText('sc-map50',       map50.toFixed(4));
    setText('chart-map50-val',  map50.toFixed(4));
    setText('chart-map50-val2', map50.toFixed(4));
    State.charts.map50.push(map50, 0);

    const ch = State.charts.map50;
    const bestMap = Math.max(...ch.series[0].data);
    setText('chart-map50-best', bestMap.toFixed(4));

    const prim = ch.series[0].data;
    if (prim.length >= 5) {
      const delta = prim[prim.length - 1] - prim[prim.length - 6];
      const sign  = delta >= 0 ? '+' : '';
      const span  = document.getElementById('chart-map50-delta');
      if (span) {
        span.textContent = sign + delta.toFixed(4);
        span.style.color = delta >= 0 ? 'var(--green)' : 'var(--red)';
      }
    }
  }
  if (map5095 !== null) {
    setText('chart-map5095-val', map5095.toFixed(4));
    State.charts.map50.push(map5095, 1);
  }
  if (prec !== null) { setText('chart-prec-val', prec.toFixed(3)); }
  if (rec  !== null) { setText('chart-rec-val',  rec.toFixed(3)); }
  if (data.lr !== undefined)         { setText('sc-lr', data.lr.toExponential(2)); }
  if (data.best_epoch !== undefined) { setText('sc-best-epoch', data.best_epoch); }

  const [boxLoss, clsLoss, dflLoss] = losses;
  if (boxLoss !== undefined) {
    setText('sc-boxloss',    boxLoss.toFixed(4));
    setText('chart-box-val', boxLoss.toFixed(4));
    setText('chart-loss-val', boxLoss.toFixed(4));
    State.charts.loss.push(boxLoss, 0);

    const lch = State.charts.loss;
    const bestBox = Math.min(...lch.series[0].data);
    setText('chart-loss-best', bestBox.toFixed(4));

    const ldata = lch.series[0].data;
    if (ldata.length >= 5) {
      const delta = ldata[ldata.length - 1] - ldata[ldata.length - 6];
      const sign  = delta >= 0 ? '+' : '';
      const span  = document.getElementById('chart-loss-delta');
      if (span) {
        span.textContent = sign + delta.toFixed(4);
        span.style.color = delta <= 0 ? 'var(--green)' : 'var(--red)';
      }
    }
  }
  if (clsLoss !== undefined) {
    setText('chart-cls-val', clsLoss.toFixed(4));
    State.charts.loss.push(clsLoss, 1);
  }
  if (dflLoss !== undefined) {
    setText('chart-dfl-val', dflLoss.toFixed(4));
    State.charts.loss.push(dflLoss, 2);
  }
}

function showComment(text, epoch) {
  const el = document.getElementById('commentary-text');
  const ep = document.getElementById('commentary-epoch');
  if (el) { el.textContent = text; el.style.color = 'var(--text-primary)'; el.style.fontStyle = 'normal'; }
  if (ep) { ep.textContent = `After epoch ${epoch}`; ep.classList.remove('hidden'); }
}

function onComplete(data) {
  State.isTraining = false;
  clearInterval(State.elapsedInterval);

  show('train-status-banner', false);
  show('stats-stop-card', false);
  show('results-container', true);

  const m       = data.final_metrics || {};
  const map50   = m['metrics/mAP50(B)']     ?? m['mAP50']    ?? null;
  const map5095 = m['metrics/mAP50-95(B)']  ?? m['mAP50-95'] ?? null;
  const prec    = m['metrics/precision(B)']  ?? null;
  const rec     = m['metrics/recall(B)']     ?? null;

  setText('res-map50',   map50   !== null ? map50.toFixed(4)   : '—');
  setText('res-map5095', map5095 !== null ? map5095.toFixed(4) : '—');
  setText('res-prec',    prec    !== null ? prec.toFixed(3)    : '—');
  setText('res-rec',     rec     !== null ? rec.toFixed(3)     : '—');

  const savedPath = data.saved_model_path;
  if (savedPath) {
    setText('res-model-path', savedPath);
    const fname = savedPath.split(/[\\\/]/).pop();
    const dl    = document.getElementById('res-download-btn');
    dl.href = `/api/train/models/${encodeURIComponent(fname)}`;
    dl.setAttribute('download', fname);
    dl.style.display = '';
  } else {
    setText('res-model-path', data.best_pt || 'Model not saved — check logs');
    document.getElementById('res-download-btn').style.display = 'none';
  }

  appendLog('[INFO] ✅ Training complete! Best weights saved to models/');
  showToast('Training complete! Model saved.', 'success');

  document.getElementById('setup-container').style.opacity = '';
  document.getElementById('setup-container').style.pointerEvents = '';
  const btn = document.getElementById('btn-launch');
  btn.textContent = '🚀 Begin Training';
  btn.disabled = false;

  loadModelsLibrary();
  document.getElementById('results-container').scrollIntoView({ behavior: 'smooth' });
}

function onTrainError(message) {
  State.isTraining = false;
  clearInterval(State.elapsedInterval);
  appendLog(`[ERROR] ${message}`, 'error');
  showToast('Training error: ' + message, 'error');
  show('train-status-banner', false);
  show('stats-stop-card', false);

  document.getElementById('setup-container').style.opacity = '';
  document.getElementById('setup-container').style.pointerEvents = '';
  const btn = document.getElementById('btn-launch');
  btn.textContent = '🚀 Begin Training';
  checkLaunchReady();
}

function syncStatus(data) {
  if (data.status === 'running') {
    State.isTraining = true;
    enterTrainingPhase(data.total_epochs || 100);
    updateEpochProgress(data.epoch || 0, data.total_epochs || 100);
    appendLog('[INFO] Reconnected — training is still in progress');
    show('stats-stop-card', true);
  }
}

// ── Check existing session on page load ───────────────────────────────────────

async function checkExistingSession() {
  try {
    const r = await apiFetch('/api/train/status');
    if (r.status === 'running') {
      State.isTraining = true;
      State.sessionId  = r.session_id;
      State.startTime  = Date.now() - (r.elapsed_sec || 0) * 1000;
      enterTrainingPhase(r.total_epochs || 100);
      updateEpochProgress(r.epoch || 0, r.total_epochs || 100);
      show('stats-stop-card', true);
      appendLog('[INFO] Training session already in progress — live view resumed');
      showToast('Reconnected to active training session', 'info');
    }
  } catch (_) {}
}

// ── Log Terminal ───────────────────────────────────────────────────────────────

function cleanAndFormatLine(line) {
  if (!line) return '';
  const parts = line.split('\r');
  let result = '';
  for (let part of parts) {
    let cleaned = part.replace(/[\u001b\x1b]\[[0-9;]*[A-Za-z]/g, '');
    cleaned = cleaned.replace(/\[[0-9;]*[KmHJfABCD]/g, '');
    cleaned = cleaned.replace(/[\u0000-\u0008\u000B-\u000C\u000E-\u001F\uFFFD]/g, '');
    if (cleaned.length >= result.length) {
      result = cleaned;
    } else {
      result = cleaned + result.substring(cleaned.length);
    }
  }
  return result.replace(/\s+$/, '');
}

function appendLog(line, forceClass) {
  const terminal = document.getElementById('log-terminal');
  if (!terminal) return;

  const cleanedLine = cleanAndFormatLine(line);
  if (line && !cleanedLine.trim() && line.trim()) return;

  const el = document.createElement('span');
  el.className = 'log-line ' + (forceClass || classifyLogLine(cleanedLine));
  el.textContent = cleanedLine;

  terminal.appendChild(el);
  terminal.appendChild(document.createTextNode('\n'));

  State.logLines++;
  if (State.logLines > 2500) {
    const lines = terminal.querySelectorAll('.log-line');
    for (let i = 0; i < 500; i++) {
      if (lines[i]) lines[i].nextSibling?.remove();
      if (lines[i]) lines[i].remove();
    }
    State.logLines -= 500;
  }

  if (State.autoScroll) {
    terminal.scrollTop = terminal.scrollHeight;
  }
}

function classifyLogLine(line) {
  const l = line.toLowerCase();
  if (l.includes('[error]') || l.includes('error:') || l.includes('traceback')) return 'error';
  if (l.includes('[warn]')  || l.includes('warning')) return 'warn';
  if (l.includes('[info]'))  return 'info';
  if (/epoch\s+\d+/.test(l)) return 'epoch';
  if (l.includes('map50') || l.includes('results')) return 'epoch';
  if (l.startsWith('  ') || l.startsWith('\t')) return 'meta';
  return 'info';
}

function clearLog() {
  const t = document.getElementById('log-terminal');
  if (t) { t.innerHTML = ''; State.logLines = 0; }
}

function setAutoScroll(val) {
  State.autoScroll = val;
}

// ── Multi-series Detail Chart ───────────────────────────────────────────────────

class DetailChart {
  constructor(canvasId, series, fixedMin = null, fixedMax = null) {
    this.canvas   = document.getElementById(canvasId);
    this.ctx      = this.canvas?.getContext('2d');
    this.series   = series;
    this.fixedMin = fixedMin;
    this.fixedMax = fixedMax;
    this.MAX_PTS  = 200;
  }

  push(value, seriesIndex = 0) {
    const s = this.series[seriesIndex];
    if (!s) return;
    s.data.push(value);
    if (s.data.length > this.MAX_PTS) s.data.shift();
    this.render();
  }

  render() {
    if (!this.ctx) return;
    const primary = this.series[0];
    if (!primary || primary.data.length < 2) return;

    const { width, height } = this.canvas;
    const ctx = this.ctx;
    ctx.clearRect(0, 0, width, height);

    let allVals = [];
    for (const s of this.series) allVals = allVals.concat(s.data);
    const dataMin = this.fixedMin !== null ? this.fixedMin : Math.min(...allVals);
    const dataMax = this.fixedMax !== null ? this.fixedMax : Math.max(...allVals);
    const range   = dataMax - dataMin || 0.001;

    const padTop = 14, padBottom = 4, padLeft = 32, padRight = 6;
    const chartW = width - padLeft - padRight;
    const chartH = height - padTop - padBottom;

    const getX = (i, len) => padLeft + (i / Math.max(len - 1, 1)) * chartW;
    const getY = (v)       => padTop  + (1 - (v - dataMin) / range) * chartH;

    const gridLines = 3;
    ctx.save();
    for (let g = 0; g <= gridLines; g++) {
      const fraction = g / gridLines;
      const yPos = padTop + fraction * chartH;
      const val  = dataMax - fraction * range;
      ctx.beginPath();
      ctx.moveTo(padLeft, yPos);
      ctx.lineTo(width - padRight, yPos);
      ctx.strokeStyle = 'rgba(255,255,255,0.06)';
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = 'rgba(255,255,255,0.3)';
      ctx.font = '8px JetBrains Mono, monospace';
      ctx.textAlign = 'right';
      ctx.fillText(val.toFixed(val < 1 ? 3 : 2), padLeft - 3, yPos + 3);
    }
    ctx.restore();

    const len = primary.data.length;
    for (let si = this.series.length - 1; si >= 0; si--) {
      const s = this.series[si];
      if (s.data.length < 2) continue;
      const sOffset = len - s.data.length;

      if (si === 0) {
        const grad = ctx.createLinearGradient(0, padTop, 0, padTop + chartH);
        grad.addColorStop(0, s.color + '40');
        grad.addColorStop(1, s.color + '00');
        ctx.beginPath();
        s.data.forEach((v, i) => {
          const x = getX(sOffset + i, len);
          const y = getY(v);
          i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        });
        const lx = getX(sOffset + s.data.length - 1, len);
        ctx.lineTo(lx, padTop + chartH);
        ctx.lineTo(getX(sOffset, len), padTop + chartH);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();
      }

      ctx.beginPath();
      s.data.forEach((v, i) => {
        const x = getX(sOffset + i, len);
        const y = getY(v);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      ctx.strokeStyle = s.color;
      ctx.lineWidth   = si === 0 ? 2 : 1.5;
      ctx.lineJoin    = 'round';
      ctx.globalAlpha = si === 0 ? 1 : 0.75;
      ctx.stroke();
      ctx.globalAlpha = 1;

      const lastVal = s.data[s.data.length - 1];
      const lx = getX(sOffset + s.data.length - 1, len);
      ctx.beginPath();
      ctx.arc(lx, getY(lastVal), si === 0 ? 3.5 : 2.5, 0, Math.PI * 2);
      ctx.fillStyle = s.color;
      ctx.fill();
    }

    if (primary.data.length >= 3) {
      const isMAPChart = (this.fixedMin === 0);
      let bestIdx, bestVal;
      if (isMAPChart) {
        bestVal = Math.max(...primary.data);
        bestIdx = primary.data.lastIndexOf(bestVal);
      } else {
        bestVal = Math.min(...primary.data);
        bestIdx = primary.data.indexOf(bestVal);
      }
      const sOffset = len - primary.data.length;
      const bx = getX(sOffset + bestIdx, len);
      const by = getY(bestVal);

      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(bx, padTop);
      ctx.lineTo(bx, padTop + chartH);
      ctx.strokeStyle = primary.color + '60';
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();

      ctx.beginPath();
      ctx.arc(bx, by, 5, 0, Math.PI * 2);
      ctx.fillStyle = primary.color;
      ctx.fill();
      ctx.beginPath();
      ctx.arc(bx, by, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.fill();
    }
  }

  reset() {
    for (const s of this.series) s.data = [];
  }
}

function initCharts() {
  State.charts.map50 = new DetailChart('chart-map50', [
    { color: '#22d3a0', label: 'mAP50',    data: [] },
    { color: '#4f9eff', label: 'mAP50-95', data: [] },
  ], 0, 1);

  State.charts.loss = new DetailChart('chart-loss', [
    { color: '#ff9940', label: 'Box Loss', data: [] },
    { color: '#9d92ff', label: 'Cls Loss', data: [] },
    { color: '#f5c542', label: 'DFL Loss', data: [] },
  ], 0, null);
}

// ── Models Library ─────────────────────────────────────────────────────────────

async function loadModelsLibrary() {
  try {
    const r = await apiFetch('/api/train/models');
    const list = document.getElementById('models-library-list');
    if (!r.models || r.models.length === 0) {
      list.innerHTML = '<div class="text-muted text-sm" style="padding:12px;text-align:center">No models saved yet.</div>';
      return;
    }
    list.innerHTML = r.models.map(m => `
      <div class="saved-model-row">
        <span style="font-size:18px">🧠</span>
        <div class="saved-model-name" title="${m.path}">${m.name}</div>
        <span class="saved-model-meta">${m.size_mb} MB</span>
        <a href="/api/train/models/${m.name.split('/').map(encodeURIComponent).join('/')}" download="${m.name.split('/').pop()}"
           class="btn btn-secondary btn-sm">⬇</a>
      </div>
    `).join('');
  } catch (_) {}
}

function trainAnother() {
  show('results-container', false);
  show('console-container', false);
  clearLog();
  State.isTraining   = false;
  State.sessionId    = null;
  State.avgEpochMs   = 0;

  // Hide ETA rows
  const etaRow  = document.getElementById('eta-row');
  const bannerEta = document.getElementById('train-eta');
  if (etaRow)    etaRow.classList.add('hidden');
  if (bannerEta) bannerEta.classList.add('hidden');

  initCharts();
  ['chart-map50-val', 'chart-map50-val2', 'chart-map5095-val', 'chart-prec-val', 'chart-rec-val',
   'chart-map50-best', 'chart-map50-delta',
   'chart-box-val', 'chart-cls-val', 'chart-dfl-val', 'chart-loss-val',
   'chart-loss-best', 'chart-loss-delta'].forEach(id => setText(id, '—'));
  checkLaunchReady();
}

// ── Step UI helpers ────────────────────────────────────────────────────────────

function updateStepTag(id, text, state) {
  const tag = document.getElementById(`steptag-${id}`);
  if (!tag) return;
  tag.textContent = text || (state === 'pending' ? 'Not selected' : text);
  tag.className = 'step-status-tag ' + (state === 'selected' ? 'selected' : 'pending');
}

function updateStepNum(num, state) {
  const el = document.getElementById(`stepnum-${num}`);
  if (!el) return;
  el.classList.remove('active', 'done');
  if (state) el.classList.add(state);
}

// ── Modal helpers ──────────────────────────────────────────────────────────────

function openModal(title, bodyHtml, actions) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML    = bodyHtml;
  const actionsEl = document.getElementById('modal-actions');
  actionsEl.innerHTML = '';
  actions.forEach(a => {
    const btn = document.createElement('button');
    btn.className = 'btn ' + a.cls;
    btn.textContent = a.label;
    btn.onclick = a.fn;
    actionsEl.appendChild(btn);
  });
  document.getElementById('modal-overlay').classList.remove('hidden');
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('modal-overlay')) return;
  document.getElementById('modal-overlay').classList.add('hidden');
}

// ── Toast ──────────────────────────────────────────────────────────────────────

function showToast(msg, type = 'info') {
  const icons = { success: '✅', error: '❌', info: 'ℹ️', warn: '⚠️' };
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ️'}</span><span>${msg}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = 'toast-out 300ms ease forwards';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// ── Utilities ──────────────────────────────────────────────────────────────────

async function apiFetch(url, opts = {}) {
  const token = localStorage.getItem('ia_token');
  const res = await fetch(url, {
    ...opts,
    headers: {
      ...(opts.headers || {}),
      'Authorization': `Bearer ${token}`,
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function show(id, visible) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('hidden', !visible);
}

// ── IA namespace ───────────────────────────────────────────────────────────────
const IA = {
  logout() {
    localStorage.removeItem('ia_token');
    window.location.href = '/login';
  },
};
