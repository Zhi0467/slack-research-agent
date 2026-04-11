const BACKLOG_DATA = {{ backlog_json }};
const COMPLETED_DATA = {{ completed_json }};
const ACTIVE_DEV_ITEMS = {{ active_dev_json }};

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function toggleSettings() {
  document.getElementById('settings-body').classList.toggle('open');
}

var AGENT_USER_ID = 'U0AFZHQMAHX';

function loadSettings() {
  var wh = localStorage.getItem('dispatch_webhook') || '';
  document.getElementById('cfg-webhook').value = wh;
  updateSettingsStatus();
  // Auto-open settings panel if no webhook configured
  if (!wh) {
    document.getElementById('settings-body').classList.add('open');
  }
}

function saveSettings() {
  var wh = document.getElementById('cfg-webhook').value.trim();
  localStorage.setItem('dispatch_webhook', wh);
  updateSettingsStatus();
}

function updateSettingsStatus() {
  var el = document.getElementById('settings-status');
  var dot = document.getElementById('header-dot');
  var state = document.getElementById('dispatch-state');
  var inline = document.getElementById('settings-inline-status');
  var wh = localStorage.getItem('dispatch_webhook') || '';
  if (wh) {
    el.textContent = '\u2713 Dispatch configured';
    el.className = 'settings-status ok';
    dot.className = 'status-dot ok';
    state.textContent = '\u2713 Ready';
    state.style.borderColor = 'rgba(77,255,180,0.5)';
    state.style.color = '#4dffb4';
    state.style.background = 'rgba(26,76,63,0.4)';
    inline.textContent = 'This browser can dispatch backlog items directly to Slack.';
  } else {
    el.textContent = 'Configure webhook URL to enable dispatch';
    el.className = 'settings-status missing';
    dot.className = 'status-dot missing';
    state.textContent = '\u2699 Configure';
    state.style.borderColor = 'rgba(56,240,255,0.4)';
    state.style.color = '#b8f4ff';
    state.style.background = 'rgba(56,240,255,0.08)';
    inline.textContent = 'No webhook saved on this browser yet.';
  }
}

function scrollToSettings() {
  var el = document.querySelector('.settings');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function canDispatch() {
  return !!localStorage.getItem('dispatch_webhook');
}

var _dispatchItemId = null;

function dispatchItem(itemId) {
  if (!canDispatch()) { alert('Configure dispatch settings first.'); return; }
  _dispatchItemId = itemId;
  document.getElementById('dispatch-modal-title').textContent = 'Dispatch ' + itemId;
  document.getElementById('dispatch-instructions').value = '';
  document.getElementById('dispatch-plan-rounds').value = '2';
  document.getElementById('dispatch-impl-rounds').value = '3';
  document.getElementById('dispatch-confirm-btn').disabled = false;
  document.getElementById('dispatch-confirm-btn').textContent = 'Confirm dispatch';
  document.getElementById('dispatch-modal').classList.add('open');
  document.getElementById('dispatch-instructions').focus();
}

function closeDispatchModal() {
  document.getElementById('dispatch-modal').classList.remove('open');
  _dispatchItemId = null;
}

function confirmDispatch() {
  var itemId = _dispatchItemId;
  if (!itemId) return;
  var extra = (document.getElementById('dispatch-instructions').value || '').trim();
  var planRounds = parseInt(document.getElementById('dispatch-plan-rounds').value, 10) || 2;
  var implRounds = parseInt(document.getElementById('dispatch-impl-rounds').value, 10) || 3;
  var confirmBtn = document.getElementById('dispatch-confirm-btn');
  confirmBtn.textContent = '...';
  confirmBtn.disabled = true;
  var btn = document.querySelector('[data-dispatch="' + itemId + '"]');
  if (btn) { btn.textContent = '...'; btn.disabled = true; }
  var webhook = localStorage.getItem('dispatch_webhook');
  var text = '<@' + AGENT_USER_ID + '> !developer ' + itemId;
  text += ' plan-rounds:' + planRounds + ' impl-rounds:' + implRounds;
  if (extra) text += '\\n' + extra;
  fetch(webhook, {
    method: 'POST',
    mode: 'no-cors',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: text }),
  })
  .then(function() {
    if (btn) { btn.textContent = '✓ Dispatched'; btn.className = 'dispatch-btn success'; btn.disabled = true; }
    var dispatched = JSON.parse(sessionStorage.getItem('dispatched_items') || '[]');
    if (dispatched.indexOf(itemId) < 0) dispatched.push(itemId);
    sessionStorage.setItem('dispatched_items', JSON.stringify(dispatched));
    closeDispatchModal();
  })
  .catch(function(err) {
    confirmBtn.textContent = 'Error — retry';
    confirmBtn.disabled = false;
    if (btn) { btn.textContent = '▶ Dispatch'; btn.className = 'dispatch-btn'; btn.disabled = false; }
  });
}

function renderBacklog() {
  var items = BACKLOG_DATA || [];
  var completed = COMPLETED_DATA || [];
  var fixes = items.filter(function(i) { return i.queue === 'fix'; });
  var features = items.filter(function(i) { return i.queue === 'feature'; });
  document.getElementById('fix-count').textContent = String(fixes.length);
  document.getElementById('feature-count').textContent = String(features.length);
  document.getElementById('completed-count').textContent = String(completed.length);
  document.getElementById('active-count').textContent = String((ACTIVE_DEV_ITEMS || []).length);
  var groups = [
    ['Fixes (' + fixes.length + ')', fixes, 'active'],
    ['Features (' + features.length + ')', features, 'active'],
    ['Completed (' + completed.length + ')', completed, 'completed'],
  ];

  var tabs = document.getElementById('tab-bar');
  var savedTab = parseInt(sessionStorage.getItem('backlog-tab') || '0');
  if (savedTab >= groups.length) savedTab = 0;
  tabs.innerHTML = groups.map(function(g, i) {
    return '<button class="tab-btn' + (i === savedTab ? ' active' : '') + '" data-tab="' + i + '">' + g[0] + '</button>';
  }).join('') + '<button class="refresh-btn" onclick="location.reload()">&#x21bb; Refresh</button>';

  function renderCard(item) {
    var isActive = ACTIVE_DEV_ITEMS.indexOf(item.id) >= 0;
    var dispatched = JSON.parse(sessionStorage.getItem('dispatched_items') || '[]');
    var wasDispatched = dispatched.indexOf(item.id) >= 0;
    var statusClass = isActive ? 'pill-in_progress' : 'pill-' + item.status.replace(/ /g, '_');
    var statusText = isActive ? 'in progress' : item.status;
    var isDeferred = item.status === 'deferred';
    var priClass = (item.priority === 'Critical' || item.priority === 'High') ? ' pill-priority-' + item.priority : '';
    var badges = '';
    if (item.has_plan) badges += '<button class="doc-btn" data-doc-type="plan" data-doc-id="' + esc(item.id) + '">Plan</button>';
    if (item.has_issue) badges += '<button class="doc-btn" data-doc-type="issue" data-doc-id="' + esc(item.id) + '">Issue</button>';
    var contextHtml = item.context ? '<div class="roadmap-context">' + esc(item.context) + '</div>' : '';

    return '<div class="roadmap-item" data-priority="' + esc(item.priority) + '">' +
      '<div class="roadmap-top">' +
        '<span class="roadmap-id">' + esc(item.id) + '</span>' +
        '<div class="roadmap-controls">' +
          '<span class="pill ' + statusClass + '">' + esc(statusText) + '</span>' +
          '<button class="dispatch-btn' + (wasDispatched ? ' success' : '') + '" data-dispatch="' + esc(item.id) + '" ' +
            (isDeferred || isActive || wasDispatched ? 'disabled title="' + (isActive ? 'In progress' : wasDispatched ? 'Already dispatched' : 'Deferred') + '"' : '') +
            '>' + (isActive ? '⟳ Running' : wasDispatched ? '✓ Dispatched' : '▶ Dispatch') + '</button>' +
        '</div>' +
      '</div>' +
      '<div class="roadmap-task">' + esc(item.task) + '</div>' +
      contextHtml +
      '<div class="roadmap-meta">' +
        '<span' + priClass + '>' + esc(item.priority) + '</span>' +
        '<span>' + esc(item.created) + '</span>' +
        badges +
        (item.session_id || isActive ? '<button class="view-session-btn" data-session="' + esc(item.id) + '">View Session</button>' : '') +
      '</div>' +
    '</div>';
  }

  function renderGroup(arr) {
    if (!arr.length) return '<div class="empty">No items</div>';
    var byAge = function(a, b) { return (a.created || '').localeCompare(b.created || ''); };
    var inProgress = arr.filter(function(i) { return ACTIVE_DEV_ITEMS.indexOf(i.id) >= 0 || i.status === 'in_progress'; }).sort(byAge);
    var rest = arr.filter(function(i) { return ACTIVE_DEV_ITEMS.indexOf(i.id) < 0 && i.status !== 'in_progress'; });
    var high = rest.filter(function(i) { return i.priority === 'Critical' || i.priority === 'High'; }).sort(byAge);
    var medium = rest.filter(function(i) { return i.priority === 'Medium' && i.status !== 'deferred'; }).sort(byAge);
    var low = rest.filter(function(i) { return i.priority === 'Low' && i.status !== 'deferred'; }).sort(byAge);
    var deferred = rest.filter(function(i) { return i.status === 'deferred'; }).sort(byAge);
    var tiers = [
      ['In Progress', inProgress],
      ['High Priority', high],
      ['Medium Priority', medium],
      ['Low Priority', low],
      ['Deferred', deferred],
    ];
    var html = '';
    tiers.forEach(function(tier) {
      if (!tier[1].length) return;
      html += '<div class="backlog-group-header">' + tier[0] + '<span class="group-count">(' + tier[1].length + ')</span></div>';
      html += '<div class="roadmap-grid">' + tier[1].map(renderCard).join('') + '</div>';
    });
    return html || '<div class="empty">No items</div>';
  }

  function renderCompleted(arr) {
    if (!arr.length) return '<div class="empty">No completed items</div>';
    return '<div class="roadmap-grid">' + arr.map(function(item) {
      var badges = '';
      if (item.has_plan) badges += '<button class="doc-btn" data-doc-type="plan" data-doc-id="' + esc(item.id) + '">Plan</button>';
      if (item.has_issue) badges += '<button class="doc-btn" data-doc-type="issue" data-doc-id="' + esc(item.id) + '">Issue</button>';
      return '<div class="roadmap-item" style="border-left:3px solid var(--good);opacity:0.75">' +
        '<div class="roadmap-top">' +
          '<span class="roadmap-id">' + esc(item.id) + '</span>' +
          '<span class="pill pill-open" style="color:var(--good);border-color:rgba(77,255,180,0.4)">done</span>' +
        '</div>' +
        '<div class="roadmap-task">' + esc(item.summary) + '</div>' +
        '<div class="roadmap-meta">' +
          '<span>Created: ' + esc(item.created) + '</span>' +
          '<span>Completed: ' + esc(item.completed) + '</span>' +
          badges +
          (item.session_id ? '<button class="view-session-btn" data-session="' + esc(item.id) + '">View Session</button>' : '') +
        '</div>' +
      '</div>';
    }).join('') + '</div>';
  }

  var body = document.getElementById('roadmap-body');
  var initGroup = groups[savedTab];
  body.innerHTML = initGroup[2] === 'completed' ? renderCompleted(initGroup[1]) : renderGroup(initGroup[1]);

  tabs.onclick = function(e) {
    var btn = e.target.closest('[data-tab]');
    if (!btn) return;
    var idx = parseInt(btn.dataset.tab);
    sessionStorage.setItem('backlog-tab', idx);
    tabs.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    var group = groups[idx];
    body.innerHTML = group[2] === 'completed' ? renderCompleted(group[1]) : renderGroup(group[1]);
  };

  document.querySelector('main').addEventListener('click', function(e) {
    var btn = e.target.closest('.dispatch-btn');
    if (btn && !btn.disabled) dispatchItem(btn.dataset.dispatch);
    var docBtn = e.target.closest('[data-doc-type]');
    if (docBtn) {
      var docType = docBtn.dataset.docType;
      var docId = docBtn.dataset.docId;
      var allItems = (BACKLOG_DATA || []).concat(COMPLETED_DATA || []);
      var item = allItems.find(function(i) { return i.id === docId; });
      if (item) {
        var content = docType === 'plan' ? item.plan_content : item.issue_content;
        var title = docType === 'plan' ? 'Plan: ' + item.id : 'Issue: ' + item.id;
        openDocModal(title, content || 'No content available.');
      }
    }
    var sessBtn = e.target.closest('[data-session]');
    if (sessBtn) openSessionModal(sessBtn.dataset.session);
  });
}

function openDocModal(title, content, isHtml) {
  document.getElementById('doc-modal-title').textContent = title;
  var body = document.getElementById('doc-modal-body');
  if (isHtml) {
    body.innerHTML = content;
    body.style.fontFamily = 'inherit';
    body.style.whiteSpace = 'normal';
  } else {
    body.textContent = content;
    body.style.fontFamily = "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace";
    body.style.whiteSpace = 'pre-wrap';
  }
  document.getElementById('doc-modal-overlay').classList.add('open');
}
function closeDocModal() {
  document.getElementById('doc-modal-overlay').classList.remove('open');
  document.getElementById('doc-modal-overlay').removeAttribute('data-session-id');
  document.getElementById('doc-modal-refresh').style.display = 'none';
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') { closeDocModal(); closeDispatchModal(); }
});

function renderSessionHtml(entries) {
  if (!entries || !entries.length) return '<div style="padding:12px;color:var(--text-dim)">No session data</div>';
  return entries.map(function(entry) {
    var roleClass = 'role-' + entry.role;
    var ts = entry.timestamp ? entry.timestamp.replace('T', ' ').replace(/\.\d+Z$/, 'Z') : '';
    var blocksHtml = (entry.blocks || []).map(function(b) {
      if (b.type === 'text') {
        return '<div class="session-block session-text">' + esc(b.content) + '</div>';
      } else if (b.type === 'tool_use') {
        return '<div class="session-block session-tool">' +
          '<span class="session-tool-name">⚙ ' + esc(b.tool) + '</span>' +
          '<div class="session-tool-input">' + esc(b.input_preview) + '</div>' +
        '</div>';
      } else if (b.type === 'tool_result') {
        return '<div class="session-block session-result">' + esc(b.content_preview) + '</div>';
      } else if (b.type === 'thinking') {
        return '<div class="session-block session-thinking" data-toggle-thinking="1">' +
          '<span class="session-thinking-label">▸ thinking (' + b.content.length + ' chars)</span>' +
          '<div class="session-thinking-body">' + esc(b.content) + '</div>' +
        '</div>';
      }
      return '';
    }).join('');
    return '<div class="session-entry ' + roleClass + '">' +
      '<div class="session-ts">' + esc(ts) + '</div>' +
      blocksHtml +
    '</div>';
  }).join('');
}

function fetchAndRenderSession(itemId, autoScroll) {
  var body = document.getElementById('doc-modal-body');
  var refreshBtn = document.getElementById('doc-modal-refresh');
  var savedScroll = body.scrollTop;
  body.style.opacity = '0.4';
  body.style.pointerEvents = 'none';
  refreshBtn.disabled = true;
  fetch('sessions/' + encodeURIComponent(itemId) + '.json?_ts=' + Date.now())
    .then(function(r) {
      if (!r.ok) throw new Error('not found');
      return r.json();
    })
    .then(function(entries) {
      if (document.getElementById('doc-modal-overlay').getAttribute('data-session-id') !== itemId) return;
      body.innerHTML = renderSessionHtml(entries);
      body.scrollTop = autoScroll ? body.scrollHeight : savedScroll;
    })
    .catch(function() {
      if (document.getElementById('doc-modal-overlay').getAttribute('data-session-id') !== itemId) return;
      if (ACTIVE_DEV_ITEMS.indexOf(itemId) >= 0) {
        return fetch('session.json?_ts=' + Date.now())
          .then(function(r) { return r.json(); })
          .then(function(entries) {
            if (document.getElementById('doc-modal-overlay').getAttribute('data-session-id') !== itemId) return;
            body.innerHTML = renderSessionHtml(entries);
            body.scrollTop = autoScroll ? body.scrollHeight : savedScroll;
          })
          .catch(function(err) {
            body.innerHTML = '<div style="padding:12px;color:var(--bad)">Error: ' + esc(err.message) + '</div>';
          });
      } else {
        body.innerHTML = '<div style="padding:12px;color:var(--text-dim)">No session data available.</div>';
      }
    })
    .finally(function() {
      if (document.getElementById('doc-modal-overlay').getAttribute('data-session-id') !== itemId) return;
      body.style.opacity = '1';
      body.style.pointerEvents = '';
      refreshBtn.disabled = false;
    });
}
function openSessionModal(itemId) {
  openDocModal('Session: ' + itemId, '<div style="padding:12px;color:var(--text-dim)">Loading...</div>', true);
  document.getElementById('doc-modal-overlay').setAttribute('data-session-id', itemId);
  document.getElementById('doc-modal-refresh').style.display = 'flex';
  fetchAndRenderSession(itemId, true);
}
function refreshSession() {
  var itemId = document.getElementById('doc-modal-overlay').getAttribute('data-session-id');
  if (itemId) fetchAndRenderSession(itemId, false);
}

document.getElementById('doc-modal-overlay').addEventListener('click', function(e) {
  var thinking = e.target.closest('[data-toggle-thinking]');
  if (thinking) thinking.classList.toggle('open');
});

loadSettings();
renderBacklog();
