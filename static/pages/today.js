
  const _svg = {
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    pencil: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
    book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
    decks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    flame: '<svg viewBox="0 0 16 20" width="13" height="16" aria-hidden="true"><path fill="currentColor" d="M8 0C5.5 3.5 3 6.5 3 10.5a5 5 0 0010 0c0-2-.9-3.8-1.8-4.8-.4 1.6-1.1 2.6-2 2.2.4-2.5.2-5.2-1.2-7.9z"/></svg>',
  };
  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  // ── Local XP fallback (same keys cards.html writes to on every review) ─────
  // The ring should never lag behind flashcard practice that just happened on
  // this device, and should still render something when offline. We keep a
  // same-day cache of the last successful /api/streak response, and take
  // max(serverPointsToday, localXpToday) so neither a stale/offline server
  // fetch nor a not-yet-synced offline review can under-report the ring.
  // Must match cards.html's key and the server's day boundary — all three are
  // the learner's LOCAL day, so the ring rolls over at their midnight.
  function _localDateStr() {
    const d = new Date();
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }
  function _loadLocalXp() {
    let raw = null;
    try { raw = JSON.parse(localStorage.getItem('xpLocalToday') || 'null'); } catch {}
    return (raw && raw.date === _localDateStr()) ? (raw.xp || 0) : 0;
  }
  function _loadStreakCache() {
    let raw = null;
    try { raw = JSON.parse(localStorage.getItem('streakCache') || 'null'); } catch {}
    return (raw && raw.date === _localDateStr()) ? raw : null;
  }
  function _saveStreakCache(streak) {
    try {
      localStorage.setItem('streakCache', JSON.stringify({
        date: _localDateStr(), streak: streak.streak, points_today: streak.points_today, daily_goal: streak.daily_goal,
      }));
    } catch {}
  }

  async function loadStreak() {
    try {
      const { streak, points, streak_freezes } = await fetch('/api/streak').then(r => r.json());
      if (window.renderHeaderStats) { window.renderHeaderStats(streak || 0, points || 0, streak_freezes || 0); return; }
      const parts = [];
      const _flame = `<svg viewBox="0 0 16 20" width="13" height="16" aria-hidden="true"><path fill="#f4702a" d="M8 0C5.5 3.5 3 6.5 3 10.5a5 5 0 0010 0c0-2-.9-3.8-1.8-4.8-.4 1.6-1.1 2.6-2 2.2.4-2.5.2-5.2-1.2-7.9z"/></svg>`;
      const _star  = `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg>`;
      const fmtN = n => n >= 10000 ? Math.round(n/1000)+'k' : n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,'')+'k' : String(n);
      const ic = n => `<span style="display:inline-flex;align-items:center;gap:3px">${n}</span>`;
      if (streak > 0) parts.push(ic(`${fmtN(streak)} ${_flame}`));
      if (points > 0) parts.push(ic(`${fmtN(points)} ${_star}`));
      if (parts.length) {
        document.querySelectorAll('.streak-display').forEach(el => {
          el.innerHTML = parts.join('<span style="opacity:0.4;margin:0 4px">·</span>');
          el.style.display = '';
        });
      }
    } catch {}
  }

  function greeting() {
    const h = new Date().getHours();
    return h < 5 ? 'Up late' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  }

  // Find the next playable AI lesson (falls back to a foundations lesson).
  function nextLesson(course) {
    if (!course) return null;
    let fallback = null;
    for (const u of (course.units || [])) {
      for (const l of (u.lessons || [])) {
        if (l.status !== 'available') continue;
        if (u.theme === 'foundations') { fallback = fallback || { lesson: l, unit: u }; continue; }
        return { lesson: l, unit: u };
      }
    }
    return fallback;
  }

  function ringHtml(today, goal) {
    const met = goal > 0 && today >= goal;
    const C = 2 * Math.PI * 31;
    const off = goal > 0 ? Math.max(0, C * (1 - Math.min(1, today / goal))) : C;
    const n = today >= 1000 ? (today / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(today);
    return `<div class="ring${met ? ' met' : ''}">
      <svg width="74" height="74" viewBox="0 0 74 74">
        <circle class="ring-track" cx="37" cy="37" r="31"/>
        <circle class="ring-arc" cx="37" cy="37" r="31" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"/>
      </svg>
      <div class="ring-num" title="${today.toLocaleString()} XP today">${n}<small>/ ${goal} XP</small></div>
    </div>`;
  }

  async function init() {
    loadStreak();
    const [me, streak, due, courseRes, quests] = await Promise.all([
      fetch('/api/me').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/streak').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/cards/due-count').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/courses/active').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/quests').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);

    const fullName = (me && (me.display_name || me.username)) || '';
    const name = fullName.trim().split(/\s+/)[0] || '';  // first name only
    if (me && me.is_admin) document.querySelectorAll('.more-admin').forEach(el => el.style.display = '');
    document.getElementById('greet-line').textContent = greeting() + (name ? `, ${name}` : '');
    if (streak) _saveStreakCache(streak);
    const cached = _loadStreakCache();
    const localXp = _loadLocalXp();
    const serverToday = streak ? (streak.points_today || 0) : (cached ? cached.points_today : 0);
    const today = Math.max(serverToday, localXp);
    const goal = (streak && streak.daily_goal) || (cached && cached.daily_goal) || 50;
    const remaining = Math.max(0, goal - today);
    document.getElementById('greet-sub').textContent =
      new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })
      + (remaining > 0 ? ` · ${remaining} XP to today's goal` : ' · daily goal met!');

    const s = (streak && streak.streak) || (cached && cached.streak) || 0;
    const goalTitle = remaining <= 0 ? 'Goal complete!' : (today > 0 ? 'Almost there' : 'Start your day');
    const goalSub = remaining <= 0
      ? 'Anything more is bonus XP.'
      : 'A lesson, a chat, or a quick review all count.';
    const streakChip = s > 0
      ? `<span class="streak-chip">${_svg.flame} ${s}-day streak — keep it alive</span>` : '';

    const nl = nextLesson(courseRes && courseRes.course);
    const nextCard = nl
      ? `<a class="tcard next" href="/learn?lesson=${nl.lesson.id}">
           <span class="goal-kicker">Continue learning</span>
           <h3>${esc(nl.lesson.title || 'Next lesson')}</h3>
           <p>${esc(nl.unit.title ? nl.unit.title + ' · ' : '')}~5 min</p>
           <span class="go">Start lesson →</span>
         </a>`
      : `<a class="tcard next" href="/learn">
           <span class="goal-kicker">Learn</span>
           <h3>Start your course</h3>
           <p>AI lessons that adapt to the words you already know.</p>
           <span class="go">Open Learn →</span>
         </a>`;

    const dueN = (due && due.count) || 0;
    let questCard = '';
    if (quests && (quests.quests || []).length) {
      const rows = quests.quests.map(q => `
        <div class="quest${q.done ? ' done' : ''}">
          <span class="qico">${q.icon}</span>
          <span class="qname">${esc(q.name)}</span>
          <span class="qnum">${q.done ? '✓' : `${q.progress}/${q.target}`}</span>
        </div>`).join('');
      const chest = quests.chest_claimed
        ? '<div class="quest-chest">🎉 Chest opened — new quests tomorrow</div>'
        : quests.all_done
          ? '<div class="quest-chest">🎁 Chest ready — open it on the Learn page!</div>'
          : '';
      questCard = `<a class="tcard quests" href="/learn" style="display:block;text-decoration:none;color:inherit">
        <div class="q-kicker">Daily quests</div>${rows}${chest}</a>`;
    }
    document.getElementById('today-body').innerHTML = `
      <div class="tcard goal">
        ${ringHtml(today, goal)}
        <div class="goal-txt">
          <div class="goal-kicker">Daily goal</div>
          <h3>${goalTitle}</h3>
          <p>${goalSub}</p>
          ${streakChip}
        </div>
      </div>
      ${nextCard}
      ${questCard}
      <div class="row2">
        <a class="tcard mini" href="/cards">
          <div class="n">${dueN}</div>
          <div class="lbl">card${dueN === 1 ? '' : 's'} due for review</div>
          <div class="cta">${dueN > 0 ? 'Review now →' : 'Open flashcards →'}</div>
        </a>
        <a class="tcard mini" href="/tutor">
          <div class="n" style="color:var(--primary)">${_svg.chat.replace('<svg ', '<svg width="26" height="26" ')}</div>
          <div class="lbl">practice a real conversation</div>
          <div class="cta">Chat with your tutor →</div>
        </a>
      </div>
      <div class="quick">
        <h3>Quick actions</h3>
        <div class="quick-row">
          <a class="quick-chip" href="/translate">${_svg.pencil} Translate a word</a>
          <a class="quick-chip" href="/reader?new=1">${_svg.book} New story</a>
          <a class="quick-chip" href="/browse?tab=community">${_svg.decks} Browse decks</a>
        </div>
      </div>`;
  }

  init();
  document.addEventListener('langchange', init);

  // We no longer poll, so the ring/streak/due counts go stale when the learner
  // earns XP elsewhere (a flashcard session, a lesson) and comes back Home.
  // Refresh on return: bfcache restore (pageshow.persisted) and tab re-focus
  // (visibilitychange), debounced so rapid fires don't stack fetches.
  let _lastRefresh = Date.now();
  function _refreshOnReturn() {
    if (document.hidden) return;
    if (Date.now() - _lastRefresh < 2500) return;
    _lastRefresh = Date.now();
    init();
  }
  window.addEventListener('pageshow', e => { if (e.persisted) _refreshOnReturn(); });
  document.addEventListener('visibilitychange', _refreshOnReturn);
