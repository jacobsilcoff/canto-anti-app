
  const _LANG_NAMES = {};
  const _LANG_FLAGS = {};
  const _COST_PER_CALL = 0.000133;

  async function loadLanguages() {
    try {
      const { languages } = await fetch('/api/languages').then(r => r.json());
      languages.forEach(l => {
        _LANG_NAMES[l.code] = l.name;
        _LANG_FLAGS[l.code] = l.flag || '';
      });
    } catch {}
  }

  function langName(code) {
    return _LANG_NAMES[code] || code;
  }

  function fmtNum(n) {
    if (n == null) return '0';
    return n.toLocaleString();
  }

  function fmtDuration(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }

  // Server timestamps come from SQLite datetime('now'), which is UTC and
  // space-separated with no timezone suffix. The browser would otherwise parse
  // them as LOCAL time, shifting every "ago" by the viewer's UTC offset (so a
  // "just now" event reads as Nh ago). Normalize to explicit UTC before parsing.
  function parseServerDate(s) {
    if (!s) return null;
    let iso = String(s).trim().replace(' ', 'T');
    if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) iso += 'T23:59:59Z';
    else if (!/([zZ])|([+-]\d\d:?\d\d)$/.test(iso)) iso += 'Z';
    const d = new Date(iso);
    return isNaN(d) ? null : d;
  }

  function fmtAgo(dateStr) {
    const d = parseServerDate(dateStr);
    if (!d) return 'Never';
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 0) return 'just now';
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    const days = Math.floor(diff / 86400);
    if (days === 1) return 'yesterday';
    if (days < 30) return days + 'd ago';
    return d.toLocaleDateString();
  }

  function tierTag(user) {
    if (user.is_admin) return '<span class="tag tag-admin">Admin</span>';
    if (user.plan === 'pro' && user.stripe_customer_id) return '<span class="tag tag-pro">Pro</span>';
    if (user.plan === 'pro') return '<span class="tag tag-pro">Comped</span>';
    return '<span class="tag tag-free">Free</span>';
  }

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  async function loadDashboard() {
    const el = document.getElementById('dash-content');
    el.innerHTML = '<div class="loading-msg">Loading dashboard...</div>';
    let data;
    try {
      const res = await fetch('/api/admin/dashboard');
      if (res.status === 403) {
        el.innerHTML = '<div class="loading-msg">Admin access required.</div>';
        return;
      }
      data = await res.json();
    } catch (e) {
      el.innerHTML = '<div class="loading-msg">Failed to load dashboard.</div>';
      return;
    }
    renderDashboard(data);
  }

  function renderDashboard(d) {
    const el = document.getElementById('dash-content');
    const t = d.tiers;
    const ai = d.ai_usage;
    const srv = d.server;
    const fb = d.feedback;
    const act = d.activity;
    const sig = d.signups;

    const estCost = (ai.this_month * _COST_PER_CALL).toFixed(2);
    const estCostLast = (ai.last_month * _COST_PER_CALL).toFixed(2);
    const proRevenue = (t.pro_paid || 0) * 4.99;

    let html = '';

    // ── KPI tiles ──
    html += '<div class="dash-grid">';
    html += kpi('Total Users', t.total, `+${sig.signups_week || 0} this week`);
    html += kpi('DAU / WAU / MAU', `${act.dau}/${act.wau}/${act.mau}`, '');
    html += kpi('AI Calls (month)', fmtNum(ai.this_month), `Last month: ${fmtNum(ai.last_month)}`);
    html += kpi('Est. Cost (month)', `$${estCost}`, `Last month: $${estCostLast}`);
    html += kpi('Pro Revenue', `$${proRevenue.toFixed(2)}/mo`, `${t.pro_paid || 0} paid, ${t.pro_comped || 0} comped`);
    html += kpi('XP Today', fmtNum(d.totals.xp_today), `${fmtNum(d.totals.cards)} total cards`);
    const ramSub = srv.total_ram_mb ? `${Math.round(srv.rss_mb / srv.total_ram_mb * 100)}% of ${Math.round(srv.total_ram_mb)} MB` : '';
    html += kpi('Memory', `${srv.rss_mb} MB`, ramSub || `Up ${fmtDuration(srv.uptime_seconds)}`);
    html += kpi('Feedback Open',
      (fb.by_status.new || 0) + (fb.by_status.triaged || 0) + (fb.by_status.in_progress || 0),
      `${fb.by_status.resolved || 0} resolved`);
    html += '</div>';

    // ── User tiers breakdown ──
    html += '<div class="dash-section">';
    html += '<h3>User Tiers</h3>';
    const tiers = [
      { label: 'Admin', count: t.admins || 0, color: 'var(--primary)' },
      { label: 'Pro (paid)', count: t.pro_paid || 0, color: 'var(--good-strong)' },
      { label: 'Pro (comped)', count: t.pro_comped || 0, color: 'var(--warn-accent)' },
      { label: 'Free', count: t.free || 0, color: 'var(--text-muted)' },
    ];
    const maxTier = Math.max(1, ...tiers.map(x => x.count));
    html += '<div class="bar-chart">';
    tiers.forEach(({ label, count, color }) => {
      const pct = Math.round((count / maxTier) * 100);
      html += `<div class="bar-label">${label}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${color}"></div></div>
        <div class="bar-count">${count}</div>`;
    });
    html += '</div></div>';

    // ── Language breakdown ──
    if (d.languages.cards.length) {
      html += '<div class="dash-section">';
      html += '<h3>Languages (by cards)</h3>';
      const maxLang = Math.max(1, ...d.languages.cards.map(x => x.cnt));
      const colors = ['var(--primary)', 'var(--good-strong)', 'var(--warn-accent)', 'var(--danger)',
        'var(--grammar-strong)', 'var(--text-muted)'];
      html += '<div class="bar-chart">';
      d.languages.cards.forEach((l, i) => {
        const learnerRow = d.languages.learners.find(x => x.target_lang === l.target_lang);
        const learners = learnerRow ? learnerRow.learners : 0;
        const pct = Math.round((l.cnt / maxLang) * 100);
        const flag = _LANG_FLAGS[l.target_lang] || '';
        html += `<div class="bar-label">${flag} ${langName(l.target_lang)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${colors[i % colors.length]}"></div></div>
          <div class="bar-count">${fmtNum(l.cnt)} <span style="color:var(--text-faint);font-size:0.68rem">(${learners})</span></div>`;
      });
      html += '</div></div>';
    }

    // ── Feedback breakdown ──
    html += '<div class="dash-section">';
    html += '<h3>Feedback</h3>';
    html += '<div style="margin-bottom:8px;display:flex;flex-wrap:wrap">';
    const statuses = ['new', 'triaged', 'in_progress', 'resolved', 'closed'];
    statuses.forEach(s => {
      const c = fb.by_status[s] || 0;
      if (c > 0 || s === 'new') {
        const label = s.replace('_', ' ');
        html += `<span class="fb-stat"><span class="fb-dot ${s}"></span>${label}: <b>${c}</b></span>`;
      }
    });
    html += '</div>';
    const bugs = fb.by_type.bug || 0;
    const features = fb.by_type.feature || 0;
    if (bugs || features) {
      html += `<div style="font-size:0.82rem;color:var(--text-muted)">Bugs: ${bugs} &middot; Feature requests: ${features}</div>`;
    }
    if (bugs + features > 0) {
      html += `<div style="margin-top:8px"><a href="/feedback" style="font-size:0.82rem;color:var(--primary);text-decoration:none;font-weight:600">View all feedback &rarr;</a></div>`;
    }
    html += '</div>';

    // ── Users table ──
    html += '<div class="dash-section">';
    html += `<div class="dash-toggle" onclick="toggleSection(this, 'users-table')">
      <h3 style="margin:0">Users (${d.users.length})</h3>
      <span class="chevron">&#9662;</span>
    </div>`;
    html += '<div id="users-table" style="margin-top:12px">';
    html += '<div class="table-scroll">';
    html += '<table class="dash-table">';
    html += '<thead><tr><th>User</th><th>Tier</th><th>Cards</th><th class="num">AI (mo)</th><th class="num">Streak</th><th>Last Active</th><th>Joined</th></tr></thead>';
    html += '<tbody>';
    d.users.forEach(u => {
      const limit = u.is_admin ? '&infin;' : (d.plan_limits[u.plan] || d.plan_limits.free);
      const calls = u.ai_calls_month || 0;
      const pct = u.is_admin ? 0 : Math.round((calls / (d.plan_limits[u.plan] || d.plan_limits.free)) * 100);
      const usageColor = pct >= 90 ? 'color:var(--danger)' : (pct >= 70 ? 'color:var(--warn-accent)' : '');
      const streak = u.streak || 0;
      html += `<tr>
        <td><b>${escHtml(u.username)}</b></td>
        <td>${tierTag(u)}</td>
        <td class="num">${fmtNum(u.card_count || 0)}</td>
        <td class="num" style="${usageColor}">${calls}/${limit}</td>
        <td class="num"><button class="streak-edit" onclick="editStreak(${u.id}, ${streak}, '${escHtml(u.username).replace(/'/g, "\\'")}')" title="Edit streak">🔥 ${streak} ✏️</button></td>
        <td>${fmtAgo(u.last_active)}</td>
        <td>${(parseServerDate(u.created_at) || {toLocaleDateString:()=>''}).toLocaleDateString()}</td>
      </tr>`;
    });
    html += '</tbody></table></div></div></div>';

    // ── Server ──
    html += '<div class="dash-section">';
    html += '<h3>Server <span style="font-weight:400;font-size:0.75rem;color:var(--text-muted)">(Oracle Cloud Free Tier)</span></h3>';
    html += '<div class="res-grid">';
    html += resMeter('Memory (RSS)', srv.rss_mb, srv.total_ram_mb, 'MB');
    html += resMeter('Disk', srv.disk_used_gb, srv.disk_total_gb, 'GB');
    html += `<div class="res-item">
      <div class="res-head"><span class="res-label">CPUs</span><span class="res-val">${srv.cpu_count || '?'}</span></div>
      <div class="res-sub">Uptime: ${fmtDuration(srv.uptime_seconds)} &middot; PID ${srv.pid}</div>
    </div>`;
    html += '</div></div>';

    el.innerHTML = html;
  }

  function kpi(label, val, sub) {
    return `<div class="kpi">
      <div class="kpi-label">${label}</div>
      <div class="kpi-val">${val}</div>
      ${sub ? `<div class="kpi-sub">${sub}</div>` : ''}
    </div>`;
  }

  function resMeter(label, used, total, unit) {
    if (total == null) {
      return `<div class="res-item">
        <div class="res-head"><span class="res-label">${label}</span><span class="res-val">${used} ${unit}</span></div>
      </div>`;
    }
    const pct = Math.min(100, Math.round((used / total) * 100));
    const color = pct >= 90 ? 'var(--danger)' : pct >= 70 ? 'var(--warn-accent)' : 'var(--primary)';
    return `<div class="res-item">
      <div class="res-head"><span class="res-label">${label}</span><span class="res-val">${used} / ${total} ${unit}</span></div>
      <div class="res-bar"><div class="res-fill" style="width:${pct}%;background:${color}"></div></div>
      <div class="res-sub">${pct}% used</div>
    </div>`;
  }

  function toggleSection(toggle, id) {
    const el = document.getElementById(id);
    const collapsed = toggle.classList.toggle('collapsed');
    el.style.display = collapsed ? 'none' : '';
  }

  async function editStreak(userId, current, username) {
    const input = prompt(`Set ${username}'s 🔥 streak (current: ${current} days):`, current);
    if (input === null) return;                       // cancelled
    const days = parseInt(input, 10);
    if (!Number.isInteger(days) || days < 0 || days > 3650) {
      alert('Enter a whole number of days between 0 and 3650.');
      return;
    }
    if (days === current) return;
    try {
      const r = await fetch(`/api/admin/users/${userId}/streak`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days }),
      });
      if (!r.ok) { alert('Failed: ' + (await r.text())); return; }
      await loadDashboard();                           // refresh the table
    } catch (e) {
      alert('Failed: ' + e);
    }
  }

  async function loadStreak() {
    try {
      const d = await fetch('/api/streak').then(r => r.json());
      if (window.renderHeaderStats) window.renderHeaderStats(d.streak || 0, d.points || 0);
    } catch {}
  }

  (async () => {
    await loadLanguages();
    loadStreak();
    loadDashboard();
  })();
