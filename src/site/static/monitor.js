// Data embedded at file-write time. Used for instant first render in both modes.
const INITIAL_DATA = {{ data_json }};
const API_STATUS_PATH = {{ api_status_path_json }};

const STATUS_STYLES = {
  running_session:       ['pill-running',  'dot-green',  'Running'],
  workers_active:        ['pill-running',  'dot-green',  'Running'],
  draining_queue:        ['pill-running',  'dot-green',  'Draining'],
  sleeping:              ['pill-sleeping', 'dot-grey',   'Sleeping'],
  sleeping_pending:      ['pill-pending',  'dot-yellow', 'Waiting (human)'],
  sleeping_after_failure:['pill-retrying', 'dot-red',    'Failed (sleeping)'],
  retrying_transient:    ['pill-retrying', 'dot-red',    'Retrying'],
  starting:              ['pill-sleeping', 'dot-grey',   'Starting'],
  restarting:            ['pill-pending',  'dot-yellow', 'Restarting'],
};
const STACK_STYLE_BY_BUCKET = {
  queued: 'stack-queued',
  active: 'stack-active',
  incomplete: 'stack-incomplete',
  finished: 'stack-finished',
};

function esc(s) {
  return String(s ?? '')
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}
function ageSeconds(ts) {
  if (!ts) return null;
  const sec = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  return sec < 0 ? 0 : sec;
}
function age(ts) {
  const d = ageSeconds(ts);
  if (d == null) return '—';
  if (d < 60)   return d + 's';
  if (d < 3600) return Math.floor(d/60) + 'm ' + (d%60) + 's';
  return Math.floor(d/3600) + 'h ' + Math.floor((d%3600)/60) + 'm';
}
function taskAge(ts) {
  if (!ts) return '—';
  const d = Math.floor((Date.now() - parseFloat(ts)*1000) / 1000);
  if (d < 0)     return '0s';
  if (d < 60)    return d + 's';
  if (d < 3600)  return Math.floor(d/60) + 'm';
  if (d < 86400) return Math.floor(d/3600) + 'h';
  return Math.floor(d/86400) + 'd';
}
function formatBytes(v) {
  if (v == null || Number.isNaN(v)) return '—';
  const n = Number(v);
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[idx]}`;
}
function sortTasks(items) {
  return [...items].sort((a, b) => {
    const aTs = parseFloat(a.last_update_ts || a.created_ts || '0');
    const bTs = parseFloat(b.last_update_ts || b.created_ts || '0');
    return bTs - aTs;
  });
}
function rawTaskText(t) {
  return String(
    t.task_description ??
    t.summary_preview ??
    t.summary ??
    t.mention_text ??
    '(no text)'
  ).replace(/\s+/g, ' ').trim();
}
function compactTaskText(t, limit=90) {
  const raw = rawTaskText(t);
  if (raw.length <= limit) return raw;
  return raw.substring(0, Math.max(limit - 1, 0)) + '…';
}
function statusPill(s) {
  const m = {done:'pill-done',in_progress:'pill-inprog',waiting_human:'pill-waiting',queued:'pill-queued',failed:'pill-failed'};
  return `<span class="pill ${m[s]||'pill-unknown'}">${esc(s)||'?'}</span>`;
}
function formatTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  return d.toLocaleString();
}
function heartbeatFreshness(ageSec) {
  if (ageSec == null) return 'unknown';
  if (ageSec <= 15) return 'fresh';
  if (ageSec <= 60) return 'delayed';
  return 'stale';
}

function render(data) {
  const hb    = data.heartbeat;
  const tasks = data.tasks || {};

  // Status pill
  const [pillCls, dotCls, label] = (hb && STATUS_STYLES[hb.status]) || (hb ? ['pill-unknown','dot-grey', hb.status || '?'] : ['pill-unknown','dot-grey','?']);
  const pillEl = document.getElementById('status-pill');
  pillEl.className = 'pill ' + pillCls;
  pillEl.textContent = label;

  // Supervisor stats
  const hbGridEl = document.getElementById('hb-grid');
  const hbNoteEl = document.getElementById('hb-note');
  if (hb) {
    const hbAgeSec = ageSeconds(hb.last_updated_utc);
    const freshness = heartbeatFreshness(hbAgeSec);
    hbGridEl.innerHTML = [
      ['Loop',             hb.loop_count ?? '—'],
      ['PID',              hb.pid ?? '—'],
      ['Last exit',        hb.last_exit_code ?? '—'],
      ['Failure kind',     hb.last_failure_kind || '—'],
      ['Retry attempt',    hb.transient_retry_attempt ?? '—'],
      ['Backoff',          hb.pending_backoff_sec != null ? hb.pending_backoff_sec + 's' : '—'],
      ['Next sleep',       hb.next_sleep_sec != null ? hb.next_sleep_sec + 's' : '—'],
      ['Pending decision', hb.pending_decision ? '⚠ Yes' : 'No'],
      ['Heartbeat age',    hbAgeSec == null ? '—' : `${age(hb.last_updated_utc)} (${freshness})`],
      ['Max workers',      hb.max_workers ?? 1],
      ['Heartbeat updated', formatTime(hb.last_updated_utc)],
    ].map(([l, v]) =>
      `<div class="hb-item"><span class="hb-label">${l}</span><span class="hb-val">${esc(v)}</span></div>`
    ).join('');
    hbNoteEl.textContent =
      hbAgeSec == null
        ? 'Heartbeat age is unavailable because last_updated_utc is missing.'
        : `Heartbeat age = seconds since supervisor last wrote .agent/runtime/heartbeat.json (${hbAgeSec}s).`;
    hbNoteEl.className = 'hb-note' + ((hbAgeSec != null && hbAgeSec > 60) ? ' hb-note-warn' : '');
  } else {
    hbGridEl.innerHTML = '<div class="hb-item"><span class="hb-label">State</span><span class="hb-val">No heartbeat data</span></div>';
    hbNoteEl.textContent = 'Heartbeat age is the elapsed time since .agent/runtime/heartbeat.json was last refreshed by the supervisor loop.';
    hbNoteEl.className = 'hb-note';
  }

  // Active task(s) — supports parallel workers
  const active = Object.values(tasks.active || {});
  const workers = (hb && hb.active_workers) || [];
  const maxWorkers = (hb && hb.max_workers) || 1;
  document.getElementById('active-dot').className = 'dot ' + (active.length ? 'dot-green' : 'dot-grey');
  document.getElementById('active-header').textContent = maxWorkers >= 2
    ? `Active Tasks (${active.length}/${maxWorkers} slots)`
    : 'Active Task';
  const abody = document.getElementById('active-body');
  if (active.length) {
    if (maxWorkers >= 2 && workers.length > 0) {
      // Parallel mode: show per-worker cards
      abody.innerHTML = active.map(t => {
        const txt = compactTaskText(t, 220);
        const w = workers.find(w => w.task === t.mention_ts);
        const slotInfo = w ? `<span><b>Slot:</b> ${w.slot}</span><span><b>Elapsed:</b> ${w.elapsed_sec}s</span>` : '';
        return `
          <div style="border-left:3px solid #4caf50;padding:4px 8px;margin-bottom:6px">
            <div class="task-text">${esc(txt)}</div>
            <div class="task-meta">
              ${statusPill(t.status)}
              ${slotInfo}
              <span><b>Age:</b> ${taskAge(t.created_ts)}</span>
              <span><b>Thread:</b> ${esc(t.thread_ts || '—')}</span>
            </div>
          </div>`;
      }).join('');
    } else {
      // Serial mode: single active task
      const t = active[0];
      const txt = compactTaskText(t, 220);
      abody.innerHTML = `
        <div class="task-text">${esc(txt)}</div>
        <div class="task-meta">
          ${statusPill(t.status)}
          <span><b>Requester:</b> [redacted]</span>
          <span><b>Age:</b> ${taskAge(t.created_ts)}</span>
          <span><b>Thread:</b> ${esc(t.thread_ts || '—')}</span>
        </div>`;
    }
  } else {
    abody.innerHTML = '<p style="color:#555;font-style:italic">No active task</p>';
  }

  // Task stacks
  const stackDefs = [
    ['queued', 'Queued'],
    ['active', 'Active'],
    ['incomplete', 'Incomplete'],
    ['finished', 'Finished'],
  ];
  const stackGrid = document.getElementById('stack-grid');
  stackGrid.innerHTML = stackDefs.map(([bucket, label]) => {
    const items = sortTasks(Object.values(tasks[bucket] || {}));
    const listHtml = items.length
      ? items.slice(0, 12).map(t => `
          <div class="stack-item">
            <div class="stack-item-title">${esc(compactTaskText(t, 88))}</div>
            <div class="stack-item-meta">
              <span>[redacted]</span>
              <span>${taskAge(t.created_ts)}</span>
            </div>
          </div>
        `).join('')
      : '<div class="stack-empty">No tasks</div>';
    return `
      <section class="stack-col ${STACK_STYLE_BY_BUCKET[bucket] || ''}">
        <div class="stack-head">
          <span>${esc(label)}</span>
          <span class="stack-count">${items.length}</span>
        </div>
        <div class="stack-list">${listHtml}</div>
      </section>
    `;
  }).join('');

  // System snapshot
  const sys = data.system || {};
  const sysRows = [
    ['Host',              sys.hostname || '—'],
    ['Platform',          sys.platform || '—'],
    ['Python',            sys.python_version || '—'],
    ['CPU cores',         sys.cpu_count != null ? sys.cpu_count : '—'],
    ['Load avg 1/5/15m',  sys.load_avg ? `${sys.load_avg.one} / ${sys.load_avg.five} / ${sys.load_avg.fifteen}` : '—'],
    ['Memory total',      formatBytes(sys.memory_total_bytes)],
    ['Disk free',         formatBytes(sys.disk_free_bytes)],
    ['Disk used',         formatBytes(sys.disk_used_bytes)],
    ['Runner log size',   formatBytes(sys.runner_log_size_bytes)],
    ['Dashboard uptime',  age(sys.dashboard_started_utc)],
    ['Dashboard PID',     sys.dashboard_pid != null ? sys.dashboard_pid : '—'],
    ['Local IPs',         Array.isArray(sys.local_ips) && sys.local_ips.length ? sys.local_ips.join(', ') : '—'],
  ];
  document.getElementById('system-grid').innerHTML = sysRows.map(([l, v]) =>
    `<div class="sys-item"><div class="sys-label">${esc(l)}</div><div class="sys-val">${esc(v)}</div></div>`
  ).join('');

  // GPU snapshot (optional)
  const gpu = data.gpu || {};
  const gpuBody = document.getElementById('gpu-body');
  const gpuNote = document.getElementById('gpu-note');
  if (!gpu.enabled) {
    gpuBody.innerHTML = `
      <div class="gpu-item">
        <div class="gpu-title">Status</div>
        <div class="gpu-val">GPU monitor disabled for this run.</div>
      </div>`;
    gpuNote.textContent = 'Run with --gpu-monitor on to re-enable. Collection is cached and low-frequency.';
    gpuNote.className = 'section-note';
  } else {
    const gpus = Array.isArray(gpu.gpus) ? gpu.gpus : [];
    const gpuCards = gpus.length
      ? gpus.map(row => `
          <div class="gpu-item">
            <div class="gpu-title">GPU ${esc(row.index ?? '?')} · ${esc(row.name || 'Unknown')}</div>
            <div class="gpu-val">util ${esc(row.utilization_gpu_pct != null ? row.utilization_gpu_pct + '%' : '—')} · mem ${esc(row.memory_used_mb != null ? row.memory_used_mb : '—')} / ${esc(row.memory_total_mb != null ? row.memory_total_mb : '—')} MB · temp ${esc(row.temperature_c != null ? row.temperature_c + '°C' : '—')}</div>
          </div>
        `).join('')
      : '<div class="gpu-item"><div class="gpu-title">GPU Data</div><div class="gpu-val">No GPU rows returned from nvidia-smi.</div></div>';

    gpuBody.innerHTML = `<div class="gpu-grid">${gpuCards}</div>`;

    const noteParts = [
      `Node: ${gpu.node_alias || '—'}`,
      `Last check: ${formatTime(gpu.checked_at_utc)}`,
    ];
    if (gpu.cache_age_sec != null) {
      noteParts.push(`cache age ${gpu.cache_age_sec}s`);
    }
    if (gpu.note) {
      noteParts.push(String(gpu.note));
    }
    gpuNote.textContent = noteParts.join(' · ');
    gpuNote.className = 'section-note' + ((gpu.status === 'ok' || gpu.status === 'partial') ? '' : ' hb-note-warn');
  }

  // Visibility + permissions
  const vis = data.visibility || {};
  const visRows = [
    ['Audience', vis.audience || '—'],
    ['Task Text', vis.task_text_access || '—'],
    ['Source-Code Exposure', vis.source_code_exposure || '—'],
  ];
  document.getElementById('visibility-list').innerHTML = visRows.map(([l, v]) =>
    `<li><strong>${esc(l)}:</strong> ${esc(v)}</li>`
  ).join('');

  // Timestamp
  const src = data.server_time_utc ? new Date(data.server_time_utc).toLocaleTimeString() : '?';
  const mode = location.protocol === 'file:' ? 'file · Live Preview' : 'http · live poll';
  document.getElementById('last-updated').textContent = `Updated ${src} (${mode})`;
}

// Render immediately from embedded data (instant, no network round-trip)
render(INITIAL_DATA);

const refreshBtn = document.getElementById('refresh-btn');
const canPoll = Boolean(API_STATUS_PATH) && location.protocol !== 'file:';
refreshBtn.onclick = canPoll ? () => fetchAndRender(true, true) : () => location.reload();

function resolveStatusUrl() {
  const rawPath = String(API_STATUS_PATH || '');
  if (!rawPath) return '';
  const sep = rawPath.includes('?') ? '&' : '?';
  return `${rawPath}${sep}_ts=${Date.now()}`;
}

function resolvePollIntervalMs(data) {
  const fallback = Number(data?.polling?.supervisor_poll_interval_sec ?? 2);
  const raw = Number(data?.polling?.frontend_poll_interval_sec ?? fallback);
  const sec = Number.isFinite(raw) && raw > 0
    ? raw
    : (Number.isFinite(fallback) && fallback > 0 ? fallback : 2);
  return Math.max(1000, Math.round(sec * 1000));
}

let pollTimer = null;
let fetchInFlight = false;
function scheduleNextPoll(data) {
  if (!canPoll) return;
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(() => fetchAndRender(false), resolvePollIntervalMs(data));
}

// Over HTTP: poll /api/status for live DOM updates without page reload.
function fetchAndRender(scheduleNext=true, manual=false) {
  if (!API_STATUS_PATH) return Promise.resolve(null);
  if (fetchInFlight) return Promise.resolve(null);
  fetchInFlight = true;
  if (manual) refreshBtn.disabled = true;
  return fetch(resolveStatusUrl(), {
      cache: 'no-store',
      credentials: 'include',
      headers: {
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
      },
    })
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(data => {
      render(data);
      if (scheduleNext) scheduleNextPoll(data);
      return data;
    })
    .catch(() => {
      // fetch poll failed (e.g. behind Cloudflare Access) — fall back to page reload
      setTimeout(() => location.reload(), resolvePollIntervalMs(INITIAL_DATA));
      return null;
    })
    .finally(() => {
      fetchInFlight = false;
      if (manual) refreshBtn.disabled = false;
    });
}

if (canPoll) {
  scheduleNextPoll(INITIAL_DATA);
  fetchAndRender(true);
}

// Showcase link: random font on each page load, auto-scaled to fit pill
(function() {
  var TARGET_WIDTH = 170;
  var TARGET_HEIGHT = 28;
  var fonts = [
    { family: '"Space Grotesk", sans-serif', weight: '300', spacing: '0.22em', transform: 'uppercase' },
    { family: '"Playfair Display", serif', weight: '700', spacing: '0.06em', transform: 'uppercase', style: 'italic' },
    { family: '"Caveat", cursive', weight: '700', spacing: '0.04em', transform: 'uppercase' },
    { family: '"Permanent Marker", cursive', weight: '400', spacing: '0.06em', transform: 'uppercase' },
    { family: '"Cormorant Garamond", serif', weight: '600', spacing: '0.08em', transform: 'uppercase', style: 'italic' },
    { family: '"Barlow Condensed", sans-serif', weight: '600', spacing: '0.14em', transform: 'uppercase', style: 'italic' },
    { family: '"JetBrains Mono", monospace', weight: '700', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Press Start 2P", monospace', weight: '400', spacing: '0.06em', transform: 'uppercase' },
    { family: '"Audiowide", sans-serif', weight: '400', spacing: '0.12em', transform: 'uppercase' },
    { family: '"Black Ops One", sans-serif', weight: '400', spacing: '0.08em', transform: 'uppercase' },
    { family: '"Monoton", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Major Mono Display", monospace', weight: '400', spacing: '0.10em', transform: 'lowercase' },
    { family: '"Orbitron", sans-serif', weight: '700', spacing: '0.14em', transform: 'uppercase' },
    { family: '"Rajdhani", sans-serif', weight: '700', spacing: '0.16em', transform: 'uppercase' },
    { family: '"Righteous", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Bungee", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' },
    { family: '"Russo One", sans-serif', weight: '400', spacing: '0.08em', transform: 'uppercase' },
    { family: '"Staatliches", sans-serif', weight: '400', spacing: '0.14em', transform: 'uppercase' },
    { family: '"Cinzel", serif', weight: '700', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Gruppo", sans-serif', weight: '400', spacing: '0.18em', transform: 'uppercase' },
    { family: '"Megrim", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Poiret One", sans-serif', weight: '400', spacing: '0.12em', transform: 'uppercase' },
    { family: '"Michroma", sans-serif', weight: '400', spacing: '0.12em', transform: 'uppercase' },
    { family: '"Nova Mono", monospace', weight: '400', spacing: '0.08em', transform: 'uppercase' },
    { family: '"Syncopate", sans-serif', weight: '700', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Vast Shadow", serif', weight: '400', spacing: '0.06em', transform: 'uppercase' },
    { family: '"Silkscreen", sans-serif', weight: '400', spacing: '0.08em', transform: 'uppercase' },
    { family: '"Notable", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' },
    { family: '"Bungee Shade", sans-serif', weight: '400', spacing: '0.04em', transform: 'uppercase' },
    { family: '"Iceland", sans-serif', weight: '400', spacing: '0.14em', transform: 'uppercase' },
    { family: '"Share Tech Mono", monospace', weight: '400', spacing: '0.12em', transform: 'uppercase' },
    { family: '"VT323", monospace', weight: '400', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Special Elite", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' },
    { family: '"Coda", sans-serif', weight: '800', spacing: '0.06em', transform: 'uppercase' },
    { family: '"Teko", sans-serif', weight: '600', spacing: '0.14em', transform: 'uppercase' },
    { family: '"Aldrich", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Electrolize", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Share Tech", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Play", sans-serif', weight: '700', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Quantico", sans-serif', weight: '700', spacing: '0.08em', transform: 'uppercase' },
    { family: '"Kanit", sans-serif', weight: '600', spacing: '0.08em', transform: 'uppercase' },
    { family: '"Tomorrow", sans-serif', weight: '600', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Chakra Petch", sans-serif', weight: '600', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Stint Ultra Expanded", sans-serif', weight: '400', spacing: '0.04em', transform: 'uppercase' },
    { family: '"Abril Fatface", serif', weight: '400', spacing: '0.04em', transform: 'uppercase' },
    { family: '"Fascinate", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' },
    { family: '"Wallpoet", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' },
    { family: '"Nosifer", sans-serif', weight: '400', spacing: '0.04em', transform: 'uppercase' },
    { family: '"Lacquer", sans-serif', weight: '400', spacing: '0.04em', transform: 'uppercase' },
    { family: '"Rubik Glitch", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' },
  ];
  window.addEventListener('DOMContentLoaded', function() {
    var link = document.querySelector('.launchpad-link');
    if (!link) return;
    if (!link.querySelector('span')) {
      var span = document.createElement('span');
      span.textContent = link.textContent;
      link.textContent = '';
      link.appendChild(span);
    }
    var span = link.querySelector('span');
    var f = fonts[Math.floor(Math.random() * fonts.length)];
    span.style.fontFamily = f.family;
    span.style.fontWeight = f.weight;
    span.style.letterSpacing = f.spacing;
    span.style.textTransform = f.transform;
    span.style.fontStyle = f.style || 'normal';
    span.style.fontSize = '20px';
    span.style.whiteSpace = 'nowrap';
    span.style.display = 'inline-block';
    requestAnimationFrame(function() {
      setTimeout(function() {
        var actualW = span.offsetWidth;
        var actualH = span.offsetHeight;
        if (actualW > 0 && actualH > 0) {
          var scaleW = TARGET_WIDTH / actualW;
          var scaleH = TARGET_HEIGHT / actualH;
          var scale = Math.min(scaleW, scaleH);
          var linkRect = link.getBoundingClientRect();
          var spanRect = span.getBoundingClientRect();
          var linkCenterY = linkRect.top + linkRect.height / 2;
          var spanCenterY = spanRect.top + spanRect.height / 2;
          var offsetY = (linkCenterY - spanCenterY) / scale;
          span.style.transform = 'scale(' + scale + ') translateY(' + offsetY + 'px)';
          span.style.transformOrigin = 'center center';
        }
      }, 100);
    });
  });
})();
