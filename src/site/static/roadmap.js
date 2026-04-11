var ROADMAP = {{ roadmap_json }};
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

(function() {
  document.getElementById('vision-text').textContent = ROADMAP.vision || '';
  var updated = ROADMAP.last_updated || '';
  if (updated) document.getElementById('updated-text').textContent = 'Last updated ' + updated;

  var themes = ROADMAP.themes || [];
  var horizons = ['now', 'next', 'later'];
  var horizonLabels = {now: 'Now', next: 'Next', later: 'Later'};
  var content = document.getElementById('roadmap-content');
  var html = '';

  function goalProgress(goals) {
    var total = 0, done = 0;
    goals.forEach(function(g) {
      (g.milestones || []).forEach(function(m) {
        total++;
        if (m.status === 'done') done++;
      });
    });
    return total > 0 ? Math.round((done / total) * 100) : 0;
  }

  function progressRingSvg(pct) {
    var r = 11, c = 2 * Math.PI * r;
    var offset = c - (pct / 100) * c;
    return '<div class="progress-ring"><svg width="28" height="28" viewBox="0 0 28 28">' +
      '<circle class="ring-bg" cx="14" cy="14" r="' + r + '"/>' +
      '<circle class="ring-fill" cx="14" cy="14" r="' + r + '" ' +
        'stroke-dasharray="' + c.toFixed(1) + '" stroke-dashoffset="' + offset.toFixed(1) + '"/>' +
    '</svg></div>';
  }

  horizons.forEach(function(h) {
    var themeCards = [];
    themes.forEach(function(theme) {
      var goals = (theme.goals || []).filter(function(g) { return g.horizon === h; });
      if (goals.length === 0) return;
      var pct = goalProgress(goals);

      var goalsHtml = goals.map(function(goal) {
        var msNodes = (goal.milestones || []).map(function(m, i) {
          var ids = (m.backlog_ids || []).length > 0
            ? '<span class="ms-ids">' + m.backlog_ids.map(esc).join(', ') + '</span>'
            : '';
          var connector = i > 0 ? '<span class="ms-connector"></span>' : '';
          return connector + '<span class="ms-node ' + esc(m.status) + '">' + esc(m.name) + ids + '</span>';
        }).join('');

        return '<div class="goal-item ' + esc(goal.status) + '">' +
          '<div class="goal-top">' +
            '<span class="goal-name">' + esc(goal.name) + '</span>' +
            '<span class="goal-badge ' + esc(goal.status) + '">' + esc(goal.status.replace(/_/g, ' ')) + '</span>' +
          '</div>' +
          '<div class="goal-desc">' + esc(goal.description || '') + '</div>' +
          (msNodes ? '<div class="milestone-chain">' + msNodes + '</div>' : '') +
        '</div>';
      }).join('');

      themeCards.push(
        '<div class="theme-card">' +
          '<div class="theme-header">' +
            '<span class="theme-name">' + esc(theme.name) + '</span>' +
            '<span class="theme-progress">' + progressRingSvg(pct) + pct + '%</span>' +
          '</div>' +
          '<div class="theme-desc">' + esc(theme.description || '') + '</div>' +
          '<div class="goal-list">' + goalsHtml + '</div>' +
        '</div>'
      );
    });
    if (themeCards.length === 0) return;
    html += '<div class="horizon-group">' +
      '<div class="horizon-marker">' +
        '<span class="horizon-dot ' + h + '"></span>' +
        '<span class="horizon-title ' + h + '">' + horizonLabels[h] + '</span>' +
      '</div>' +
      '<div class="themes-container">' + themeCards.join('') + '</div>' +
    '</div>';
  });

  content.innerHTML = html;
})();
