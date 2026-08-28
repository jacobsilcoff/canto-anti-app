
  let selectedLevel = 'A1';
  let selectedFocus = 'balanced';
  let _lessonLength = 'standard';   // A3 · mirrors the lesson_length setting
  let _aiSpeak = true;              // mirrors the lesson_ai_speak setting (AI Speak drills on/off)
  let _warmup = true;               // mirrors lesson_warmup (optional first review step)
  let _speakDrills = true;          // mirrors speaking_drills (🎤 say-it-aloud drills in lessons)
  let _speechDead = false;          // this device's recogniser answered nothing — stop offering it
  const _cdPreload = {};           // construction → Promise<opener data>, warmed while the learner works
  let langName = 'your language';
  let LANGS = [];
  let currentCourse = null;
  let isAdmin = false;
  let unlockAll = localStorage.getItem('learn_unlock_all') === '1';
  let _currentState = 'loading';
  let _generating = false;    // true while a /next API call is in-flight
  let _genCount = 0;          // lessons still to author this batch (for the label)
  let _genError = null;       // last error message from generation
  let _tbGenerating = null;   // textbook unit_id currently authoring its next lesson
  let _tbError = null;        // last error from a textbook-unit generation
  let _tbProgress = '';       // "Built 2 · 3 to go…" during a generate-all run
  let _tbBooks = null;        // books + chapters (null until the shelf loads)
  let _tbShelfLoading = false;
  let _tbBuilding = null;     // "bookId:chapterIdx" while a chapter becomes a unit

  function show(state) {
    // Leaving the drill screens stops any clip still playing — a prompt that
    // follows you out to the course map (or onto the results screen) reads as
    // the app talking to itself.
    if (state !== 'player' && state !== 'teach' && _currentState !== state) {
      try { stopTTS(); } catch {}
    }
    _currentState = state;
    // Full-focus lesson states reclaim the mobile tab bar's space.
    document.body.classList.toggle('hide-tabbar', state === 'teach' || state === 'player');
    ['loading', 'empty', 'error', 'generating', 'course', 'lesson-loading', 'teach', 'player', 'results'].forEach(s =>
      document.getElementById('state-' + s).style.display = s === state ? '' : 'none');
    // The path connectors need real layout geometry, so (re)draw once the course
    // view is actually visible (it may have rendered while hidden).
    if (state === 'course') requestAnimationFrame(drawPathConnectors);
  }

  function esc(s) {
    const d = document.createElement('div'); d.textContent = s == null ? '' : s;
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  const _ic = {
    flame: `<svg class="icon-svg" viewBox="0 0 16 20" width="13" height="16" aria-hidden="true"><path fill="#f4702a" d="M8 0C5.5 3.5 3 6.5 3 10.5a5 5 0 0010 0c0-2-.9-3.8-1.8-4.8-.4 1.6-1.1 2.6-2 2.2.4-2.5.2-5.2-1.2-7.9z"/></svg>`,
    star:  `<svg class="icon-svg" viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg>`,
    crown: `<svg class="icon-svg" viewBox="0 0 20 16" width="18" height="13" aria-hidden="true"><path fill="#f0b429" d="M1 15V11L3 5 7 9 10 2 13 9 17 5 19 11V15Z"/></svg>`,
  };

  async function init() {
    show('loading');
    try {
      const [settings, langs] = await Promise.all([
        fetch('/api/settings').then(r => r.json()).catch(() => ({})),
        fetch('/api/languages').then(r => r.json()).catch(() => ({ languages: [] })),
      ]);
      LANGS = langs.languages || [];
      isAdmin = !!settings.is_admin;
      document.getElementById('unlock-toggle').style.display = isAdmin ? '' : 'none';
      // A past bug force-enabled unlock-all for everyone after generating; clear
      // the stale flag for non-admins so sequential locking works again.
      if (!isAdmin && unlockAll) { unlockAll = false; localStorage.removeItem('learn_unlock_all'); }
      _lessonBuffer = Math.max(0, parseInt(settings.lesson_buffer || 3, 10) || 3);
      _lessonLength = settings.lesson_length || 'standard';
      _aiSpeak = settings.lesson_ai_speak !== false && settings.lesson_ai_speak !== 'false';
      _warmup = settings.lesson_warmup !== false && settings.lesson_warmup !== 'false';
      _speakDrills = settings.speaking_drills !== false && settings.speaking_drills !== 'false';
      const code = settings.default_target_lang || 'yue';
      const l = LANGS.find(x => x.code === code);
      langName = l ? l.name : code;
    } catch {}
    document.getElementById('empty-lang').textContent = langName;

    try {
      // A failed request (network blip, 5xx from lock contention) must NOT be
      // rendered as "no course yet" — that made the learn page falsely claim you'd
      // never generated a lesson until a reload. Only a real null course is empty;
      // a fetch failure gets a distinct retry state.
      const res = await fetch('/api/courses/active');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const { course } = await res.json();
      if (course) { renderCourse(course); show('course'); }
      else { show('empty'); }
      // Deep link from Home's "Start lesson": open the player immediately.
      const lessonParam = new URLSearchParams(location.search).get('lesson');
      if (course && lessonParam) {
        history.replaceState(null, '', '/learn');   // refresh returns to the map
        resumeLesson(parseInt(lessonParam, 10));    // continue a saved attempt if there is one
      }
    } catch {
      show('error');
    }
    loadStreak();
    loadQuests();
    loadLeague();
  }

  // B5 · lightweight toast (streak-freeze earned / consumed messages).
  let _toastTimer = null;
  function learnToast(msg) {
    let el = document.getElementById('learn-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'learn-toast';
      el.className = 'learn-toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
  }

  // Toast once when a streak freeze was just consumed to bridge yesterday.
  function _checkFreezeConsumed(info) {
    try {
      const used = info && info.streak_freeze_used_date;
      if (!used) return;
      // The server stamps this as (the learner's local today − 1), so compare
      // in local time — a UTC date here misses by a day either side of midnight.
      const d = new Date(Date.now() - 864e5);
      const p = n => String(n).padStart(2, '0');
      const yest = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
      if (used !== yest) return;   // only a freshly-used freeze (yesterday)
      if (localStorage.getItem('freezeUsedSeen') === used) return;
      localStorage.setItem('freezeUsedSeen', used);
      learnToast('🛡 Streak freeze used — your streak is safe!');
    } catch {}
  }

  let _streakInfo = null;
  async function loadStreak() {
    try {
      const info = await fetch('/api/streak').then(r => r.json());
      _streakInfo = info;
      _checkFreezeConsumed(info);
      if (window.renderHeaderStats) { window.renderHeaderStats(info.streak || 0, info.points || 0, info.streak_freezes || 0); renderDailyRing(); return; }
      const fmtN = n => n >= 10000 ? Math.round(n/1000)+'k' : n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,'')+'k' : String(n);
      const numSpan = n => { const s=fmtN(n),f=n.toLocaleString(); return s===f?s:`<span style="cursor:pointer" title="${f}" onclick="this.textContent=this.textContent==='${s}'?'${f}':'${s}'">${s}</span>`; };
      const ic = n => `<span style="display:inline-flex;align-items:center;gap:3px">${n}</span>`;
      const parts = [];
      if (info.streak > 0) parts.push(ic(`${numSpan(info.streak)} ${_ic.flame}`));
      if (info.points > 0) parts.push(ic(`${numSpan(info.points)} ${_ic.star}`));
      if (parts.length) {
        document.querySelectorAll('.streak-display').forEach(el => {
          el.innerHTML = parts.join('<span style="opacity:0.4;margin:0 4px">·</span>');
          el.style.display = '';
        });
      }
      renderDailyRing();
    } catch {}
  }

  // Daily-goal XP ring on the course header (today's XP vs the daily goal).
  function renderDailyRing() {
    const el = document.getElementById('daily-ring');
    if (!el || !_streakInfo) return;
    const today = _streakInfo.points_today || 0;
    const goal = _streakInfo.daily_goal || 50;
    const frac = Math.max(0, Math.min(1, today / goal));
    const R = 18, C = 2 * Math.PI * R;
    const met = today >= goal;
    el.classList.toggle('met', met);
    el.innerHTML = `
      <div class="dr-ringwrap">
        <svg viewBox="0 0 46 46" aria-hidden="true">
          <circle class="dr-track" cx="23" cy="23" r="${R}"></circle>
          <circle class="dr-arc" cx="23" cy="23" r="${R}"
            stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${(C * (1 - frac)).toFixed(1)}"></circle>
        </svg>
        <span class="dr-num">${met ? '✓' : today}</span>
      </div>
      <span class="dr-label">${met ? '<b>Goal met!</b>' : `<b>${today}</b> / ${goal} XP today`}</span>`;
    el.style.display = '';
  }

  // ── B1 · Daily quests ───────────────────────────────────────────────────────
  let _questData = null;

  async function loadQuests() {
    try {
      _questData = await fetch('/api/quests').then(r => r.ok ? r.json() : null);
    } catch { _questData = null; }
    renderQuestCard();
  }

  function _questRows(data, compact) {
    return (data.quests || []).map(q => `
      <div class="quest${q.done ? ' done' : ''}">
        <span class="qico">${q.icon}</span>
        <div class="qbody"><div class="qname">${esc(q.name)}</div>
          ${compact ? '' : `<div class="qbar"><div class="qfill" style="width:${Math.round(q.progress / q.target * 100)}%"></div></div>`}
        </div>
        <span class="qnum">${q.done ? '✓' : `${q.progress}/${q.target}`}</span>
      </div>`).join('');
  }

  function _chestHtml(data) {
    if (data.chest_claimed) {
      return `<div class="chest"><span class="ci">🎉</span>
        <div><b>Chest opened</b><small>Come back tomorrow for new quests</small></div></div>`;
    }
    if (data.all_done) {
      return `<div class="chest"><span class="ci">🎁</span>
        <div><b>All quests complete!</b><small>Open the chest for bonus XP</small></div>
        <button class="chest-open" onclick="claimChest(this)">Open</button></div>`;
    }
    const left = (data.quests || []).filter(q => !q.done).length;
    return `<div class="chest"><span class="ci">🎁</span>
      <div><b>${left} more to open today's chest</b><small>Bonus XP for finishing all three</small></div></div>`;
  }

  function _questResetLabel() {
    const now = new Date();
    const h = 23 - now.getUTCHours();
    return `resets in ${Math.max(1, h)} h`;
  }

  function renderQuestCard() {
    const el = document.getElementById('quest-card');
    if (!el) return;
    if (!_questData || !(_questData.quests || []).length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="quests">
      <div class="q-head"><span class="q-kicker">Daily quests</span>
        <span class="q-timer">${_questResetLabel()}</span></div>
      ${_questRows(_questData)}
      ${_chestHtml(_questData)}
    </div>`;
  }

  async function claimChest(btn) {
    btn.disabled = true;
    showChestReveal(
      fetch('/api/quests/claim', { method: 'POST' }).then(r => {
        if (!r.ok) throw new Error();
        return r.json();
      }),
      () => { btn.disabled = false; },   // re-enable if the claim fails
    );
  }

  // Animated chest-opening: the chest shakes (anticipation) while the claim POST
  // resolves, then bursts open with sunburst rays + confetti to reveal the XP
  // prize. Self-contained (emoji + CSS), tap or auto-dismiss to continue.
  function showChestReveal(claimPromise, onError) {
    const ov = document.createElement('div');
    ov.className = 'chest-reveal';
    ov.innerHTML = `
      <div class="cr-stage">
        <div class="cr-rays"></div>
        <div class="cr-chest cr-shake">🎁</div>
        <div class="cr-prize"></div>
        <div class="cr-hint">Opening…</div>
      </div>`;
    document.body.appendChild(ov);
    requestAnimationFrame(() => ov.classList.add('show'));
    try { sfx.tap(); } catch {}

    let closed = false;
    const close = () => {
      if (closed) return; closed = true;
      ov.classList.remove('show');
      setTimeout(() => ov.remove(), 260);
    };

    // Let the shake play for a beat even if the network returns instantly.
    const minDelay = new Promise(r => setTimeout(r, 850));
    Promise.all([claimPromise, minDelay]).then(([data]) => {
      const xp = (data && data.xp) || 0;
      if (_questData) _questData.chest_claimed = true;
      renderQuestCard();
      loadStreak();   // refresh the ⭐ total + daily ring with the bonus XP
      const chest = ov.querySelector('.cr-chest');
      chest.classList.remove('cr-shake');
      chest.classList.add('cr-pop');
      ov.querySelector('.cr-rays').classList.add('on');
      const prize = ov.querySelector('.cr-prize');
      prize.innerHTML = `<span class="cr-star">⭐</span><span class="cr-xp">+${xp}</span><span class="cr-lbl">bonus XP</span>`;
      prize.classList.add('show');
      ov.querySelector('.cr-hint').textContent = 'Tap to continue';
      try { sfx.complete(); } catch {}
      confetti(); setTimeout(confetti, 260);
      ov.addEventListener('click', close);
      setTimeout(close, 4500);   // backstop auto-dismiss
    }).catch(() => { close(); if (onError) onError(); });
  }

  // ── B2 · friends weekly XP league ──────────────────────────────────────────
  let _leagueData = null;
  const _AV_COLORS = ['#e4572e', '#146b5c', '#4f46a5', '#dfa32e', '#a5459b', '#2b7fb2'];
  function _avColor(name) {
    let h = 0; for (const c of name || '') h = (h * 31 + c.charCodeAt(0)) & 0xffff;
    return _AV_COLORS[h % _AV_COLORS.length];
  }
  // A real profile picture when set, else a colored initial (same fallback
  // pattern used for chat/friends avatars elsewhere in the app).
  function _avatarInner(r) {
    return r.avatar_url ? `<img src="${esc(r.avatar_url)}" alt="" loading="lazy" decoding="async">` : esc((r.username || '?')[0].toUpperCase());
  }

  async function loadLeague() {
    try {
      _leagueData = await fetch('/api/league').then(r => r.ok ? r.json() : null);
    } catch { _leagueData = null; }
    renderLeagueStrip();
  }

  function renderLeagueStrip() {
    const el = document.getElementById('league-strip');
    if (!el) return;
    const rows = (_leagueData && _leagueData.league) || [];
    if (rows.length < 2) { el.innerHTML = ''; return; }   // solo users see nothing
    const me = rows.find(r => r.you);
    const rank = me ? me.rank : rows.length;
    const medal = rank === 1 ? '🥇' : rank === 2 ? '🥈' : rank === 3 ? '🥉' : '🏅';
    const days = _leagueData.days_left || 0;
    let sub;
    if (rank === 1) {
      const runnerUp = rows.find(r => !r.you);
      sub = runnerUp ? `${me.xp - runnerUp.xp} XP ahead of ${runnerUp.username}` : 'You lead';
    } else {
      const above = rows[rank - 2];
      sub = `${above.xp - me.xp} XP behind ${above.username}`;
    }
    sub += ` · ${days} day${days === 1 ? '' : 's'} left`;
    const avatars = rows.slice(0, 4).map(r =>
      `<span style="background:${_avColor(r.username)}">${_avatarInner(r)}</span>`).join('');
    el.innerHTML = `<div class="league" onclick="openLeague()" title="Weekly XP among friends">
      <span class="medal">${medal}</span>
      <div class="lbody">
        <div class="lline">#${rank} of ${rows.length} friends this week</div>
        <div class="lsub">${esc(sub)}</div>
      </div>
      <div class="avatars">${avatars}</div>
    </div>`;
  }

  function openLeague() {
    const rows = (_leagueData && _leagueData.league) || [];
    if (!rows.length) return;
    const days = _leagueData.days_left || 0;
    document.getElementById('league-sub').textContent =
      `XP earned this week · resets in ${days} day${days === 1 ? '' : 's'}`;
    document.getElementById('league-list').innerHTML = rows.map(r => `
      <div class="lg-row${r.you ? ' you' : ''}">
        <span class="lg-rank">${r.rank === 1 ? '🥇' : r.rank === 2 ? '🥈' : r.rank === 3 ? '🥉' : r.rank}</span>
        <span class="lg-av" style="background:${_avColor(r.username)}">${_avatarInner(r)}</span>
        <span class="lg-name">${esc(r.username)}${r.you ? ' (you)' : ''}</span>
        <span class="lg-xp">${r.xp} XP</span>
      </div>`).join('');
    document.getElementById('league-overlay').classList.add('open');
  }
  function closeLeague() { document.getElementById('league-overlay').classList.remove('open'); }

  // Client-only quest signals the server can't observe (best combo, listening
  // hits) — reported once per lesson at finish; the server clamps them.
  async function reportQuestSignals() {
    if (!player) return;
    const posts = [];
    if (player.maxCombo > 0) {
      posts.push(fetch('/api/quests/progress', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'combo', value: player.maxCombo }),
      }));
    }
    if (player.listeningHits > 0) {
      posts.push(fetch('/api/quests/progress', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'listening', amount: player.listeningHits }),
      }));
    }
    if (player.lightning) {   // B4 · finishing a lightning round ticks its quest
      posts.push(fetch('/api/quests/progress', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'lightning', amount: 1 }),
      }));
    }
    try { await Promise.all(posts); } catch {}
  }

  document.getElementById('level-row').addEventListener('click', e => {
    const chip = e.target.closest('.level-chip');
    if (!chip) return;
    selectedLevel = chip.dataset.level;
    document.querySelectorAll('#level-row .level-chip').forEach(c => c.classList.toggle('active', c === chip));
  });

  // D2 · course focus dial at creation: writes the course_focus setting the
  // planner reads on every future lesson (changeable later in Settings).
  document.getElementById('focus-row').addEventListener('click', e => {
    const chip = e.target.closest('.level-chip');
    if (!chip) return;
    selectedFocus = chip.dataset.focus;
    document.querySelectorAll('#focus-row .level-chip').forEach(c => c.classList.toggle('active', c === chip));
  });

  async function createCourse() {
    document.getElementById('create-err').style.display = 'none';
    document.getElementById('create-btn').disabled = true;
    try {
      // Persist the focus dial first — the planner reads it per lesson.
      if (selectedFocus !== 'balanced') {
        await fetch('/api/settings', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ course_focus: selectedFocus }),
        }).catch(() => {});
      }
      const res = await fetch('/api/courses', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level: selectedLevel }),
      });
      if (!res.ok) {
        const msg = (await res.json().catch(() => ({}))).detail || 'Failed — please try again.';
        throw new Error(msg);
      }
      const course = await res.json();
      currentCourse = course;
      renderCourse(course);
      show('course');
      // Auto-buffer kicks in from renderCourse; no explicit call needed.
    } catch (e) {
      show('empty');
      document.getElementById('create-btn').disabled = false;
      const el = document.getElementById('create-err');
      el.textContent = e.message || 'Failed — please try again.';
      el.style.display = '';
    }
  }

  async function resetAiLessons() {
    if (!confirm('Restart the course? All AI-generated lessons are cleared and the path starts fresh. Your reading track and flashcards are kept.')) return;
    const id = document.getElementById('state-course').dataset.courseId;
    if (!id) return;
    await fetch('/api/courses/' + id + '/ai_lessons', { method: 'DELETE' }).catch(() => {});
    await refreshAndShowCourse();
  }

  async function openVocabReview() {
    const id = document.getElementById('state-course').dataset.courseId;
    if (!id) return;
    const body = document.getElementById('vocab-body');
    body.innerHTML = '<div class="vocab-empty"><span class="spinner"></span></div>';
    document.getElementById('vocab-count').textContent = '';
    document.getElementById('vocab-overlay').classList.add('open');
    try {
      const res = await fetch('/api/courses/' + id + '/vocab');
      if (!res.ok) throw new Error();
      const data = await res.json();
      const vocab = data.vocab || [];
      if (!vocab.length) {
        body.innerHTML = '<div class="vocab-empty">No vocab taught yet. Complete some lessons first!</div>';
        return;
      }
      document.getElementById('vocab-count').textContent = vocab.length + ' words';
      const lang = currentCourse && currentCourse.target_lang;
      body.innerHTML = '';
      if (lang) body.classList.add(scriptClassFor(lang));

      // Group words by the lesson that introduced them so the list reads in
      // teaching order instead of one undifferentiated pile. Words with no known
      // lesson fall into a trailing "Other" group.
      const groups = [];
      const byKey = new Map();
      vocab.forEach(v => {
        const key = v.lesson_title || '';
        let g = byKey.get(key);
        if (!g) {
          g = { title: v.lesson_title || 'Other words', num: v.lesson_num || 9999, items: [] };
          byKey.set(key, g);
          groups.push(g);
        }
        g.items.push(v);
      });
      groups.sort((a, b) => a.num - b.num);

      groups.forEach(g => {
        const head = document.createElement('div');
        head.className = 'vocab-group-head';
        head.innerHTML = `<span>${esc(g.title)}</span>` +
          `<span class="vg-count">${g.items.length} word${g.items.length === 1 ? '' : 's'}</span>`;
        body.appendChild(head);
        const wrap = document.createElement('div');
        wrap.className = 'vocab-group';
        g.items.forEach(v => {
          const row = document.createElement('div');
          row.className = 'vocab-row';
          // The word carries a `.needs-ruby` hook so applyRuby annotates it with
          // romanization (offline oracle) — non-Latin scripts are unreadable on
          // mobile without it.
          const wordHtml = lang && needsRuby(lang)
            ? `<span class="needs-ruby" data-text="${esc(v.label)}" data-lang="${esc(lang)}">${esc(v.label)}</span>`
            : esc(v.label);
          row.innerHTML =
            `<div class="vocab-word">${wordHtml}</div>` +
            `<div class="vocab-gloss">${esc(v.gloss)}</div>`;
          if (v.in_deck) {
            row.insertAdjacentHTML('beforeend', '<span class="vocab-deck-badge in">✓ In deck</span>');
          } else {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'vocab-deck-badge out vocab-add-btn';
            btn.textContent = '＋ Add';
            btn.onclick = () => addVocabWord(v, btn, lang);
            row.appendChild(btn);
          }
          wrap.appendChild(row);
        });
        body.appendChild(wrap);
      });
      // Resolve all the romanization ruby in one batched pass.
      if (lang && needsRuby(lang)) applyRuby(body, null, false, false);
    } catch {
      body.innerHTML = '<div class="vocab-empty">Failed to load vocab.</div>';
    }
  }

  function closeVocabReview() {
    document.getElementById('vocab-overlay').classList.remove('open');
  }

  // Add a word from the vocab review drawer straight to the SRS deck.
  async function addVocabWord(v, btn, lang) {
    if (btn.disabled) return;
    btn.disabled = true; btn.textContent = '…';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: v.gloss, target_text: v.label,
          target_lang: lang, priority: 3,
          notes: 'From lesson: ' + (v.lesson_title || ''),
        }),
      });
      if (!res.ok) throw new Error();
      v.in_deck = true;
      btn.outerHTML = '<span class="vocab-deck-badge in">✓ In deck</span>';
    } catch {
      btn.disabled = false; btn.textContent = '＋ Add';
      learnToast('Failed to add word — try again');
    }
  }

  function toggleUnlockAll() {
    unlockAll = !unlockAll;
    localStorage.setItem('learn_unlock_all', unlockAll ? '1' : '0');
    if (currentCourse) renderCourse(currentCourse);
  }

  // Per-unit collapse toggle. Key: 'learn_unit_<id>'; '1'=open '0'=closed. No entry=auto.
  function toggleUnit(id) {
    const body    = document.getElementById('unit-body-' + id);
    const chevron = document.getElementById('unit-chevron-' + id);
    const nowOpen = body && body.style.display !== 'none';
    localStorage.setItem('learn_unit_' + id, nowOpen ? '0' : '1');
    if (body)    body.style.display = nowOpen ? 'none' : '';
    if (chevron) chevron.style.transform = nowOpen ? '' : 'rotate(90deg)';
  }

  // AI course path collapse — default EXPANDED; '1' = collapsed. The trail's
  // connectors are measured from laid-out nodes, so they must be redrawn when
  // it reopens (they can't be measured while display:none).
  function toggleAiPath() {
    const isNowCollapsed = localStorage.getItem('learn_ai_collapsed') !== '1';
    localStorage.setItem('learn_ai_collapsed', isNowCollapsed ? '1' : '0');
    const body    = document.getElementById('ai-body');
    const chevron = document.getElementById('ai-chevron');
    if (body)    body.style.display = isNowCollapsed ? 'none' : '';
    if (chevron) chevron.style.transform = isNowCollapsed ? '' : 'rotate(90deg)';
    if (!isNowCollapsed) requestAnimationFrame(drawPathConnectors);
  }

  // Units inside the AI path collapse independently. A missing preference is
  // automatic: only the unit containing the learner's next available lesson is
  // open. Explicit taps are remembered per course/unit.
  function toggleAiUnit(key) {
    const el = document.querySelector(`.ai-path-unit[data-ai-unit="${key}"]`);
    if (!el) return;
    const open = !el.classList.contains('open');
    el.classList.toggle('open', open);
    localStorage.setItem('aiunit:' + key, open ? '1' : '0');
    requestAnimationFrame(drawPathConnectors);
  }

  function _activeAiUnitIndex(units) {
    const withLessons = (units || []).filter(u => (u.lessons || []).length);
    let idx = withLessons.findIndex(u =>
      u.in_progress || (u.lessons || []).some(l => l.status === 'available'));
    if (idx < 0) idx = withLessons.findIndex(u =>
      (u.lessons || []).some(l => l.status !== 'done'));
    return idx < 0 ? Math.max(0, withLessons.length - 1) : idx;
  }

  // Foundations section collapse — default collapsed (hidden); '0' = expanded.
  function toggleFoundations() {
    const isNowCollapsed = localStorage.getItem('learn_foundations_collapsed') === '0';
    localStorage.setItem('learn_foundations_collapsed', isNowCollapsed ? '1' : '0');
    const body    = document.getElementById('foundations-body');
    const chevron = document.getElementById('foundations-chevron');
    if (body)    body.style.display = isNowCollapsed ? 'none' : '';
    if (chevron) chevron.style.transform = isNowCollapsed ? '' : 'rotate(90deg)';
  }

  // ── Debug panel ─────────────────────────────────────────────────────────────

  function openDebug(lessonId, lessonNum) {
    fetch('/api/lessons/' + lessonId)
      .then(r => r.json())
      .then(lesson => {
        const debug = lesson.llm_debug || {};
        document.getElementById('debug-lesson-num').textContent = lessonNum || lessonId;
        document.getElementById('debug-prompt').textContent = debug.prompt || '(no prompt stored)';
        document.getElementById('debug-response').textContent = debug.response || '(no response stored)';
        document.getElementById('debug-modal').style.display = '';
      })
      .catch(() => alert('Could not load debug info.'));
  }

  function closeDebug() {
    document.getElementById('debug-modal').style.display = 'none';
  }

  document.getElementById('debug-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('debug-modal')) closeDebug();
  });

  // ── Generate next lesson ─────────────────────────────────────────────────────

  let _lessonBuffer = 3;   // auto-keep this many unvisited lessons ahead (loaded from settings)

  // A failed fetch (no response) throws a TypeError; Safari words it "Load failed",
  // Chrome "Failed to fetch". On the slow premium model one lesson can take ~45s,
  // so we never want a single request to author the whole batch — that would run
  // 2+ minutes and trip a network/proxy timeout. Turn those into a clear message.
  function _humanizeGenError(e, madeSoFar) {
    const m = (e && e.message) || '';
    const networkish = (e instanceof TypeError) || /load failed|failed to fetch|networkerror|timed? ?out/i.test(m);
    if (networkish) {
      return madeSoFar > 0
        ? 'The connection dropped before the batch finished — but the lessons made so far were saved. Tap Generate to continue.'
        : 'The lesson took too long and the connection dropped (this can happen on the premium model). It may have still been saved — tap Generate to retry.';
    }
    return m || 'Could not generate lesson — please try again.';
  }

  // Author lessons ONE request at a time (each request = one model call) and
  // refresh the map after each, so a slow model can't make a single request long
  // enough to time out, and partial progress is always kept + shown.
  async function generateNextLesson(courseId, count = 1) {
    if (_generating) return;
    _generating = true;
    _genCount = count;
    _genError = null;
    // Stay on course page — show inline spinner, never navigate away.
    if (_currentState === 'generating') show('course');
    if (currentCourse) renderCourse(currentCourse);

    let made = 0;
    try {
      for (let i = 0; i < count; i++) {
        _genCount = count - i;                 // remaining, for the skeleton label
        if (currentCourse) renderCourse(currentCourse);
        const res = await fetch(`/api/courses/${courseId}/next?count=1`, { method: 'POST' });
        if (!res.ok) {
          // Quota (402) / rate-limit (429) / generation (502): surface the server's
          // detail. If earlier lessons in this batch succeeded, stop quietly with a note.
          const msg = (await res.json().catch(() => ({}))).detail || 'Generation failed — please try again.';
          if (made > 0) { _genError = `Stopped after ${made} lesson${made === 1 ? '' : 's'}: ${msg}`; break; }
          throw new Error(msg);
        }
        made++;
        // Refresh the roadmap progressively so each new lesson appears as it lands.
        const { course } = await fetch('/api/courses/active').then(r => r.json()).catch(() => ({}));
        if (course) {
          currentCourse = course;
          renderCourse(course);
          if (!['teach', 'player', 'results'].includes(_currentState)) show('course');
        }
      }
    } catch (e) {
      _genError = _humanizeGenError(e, made);
      if (currentCourse) renderCourse(currentCourse);
    } finally {
      _generating = false;
      if (currentCourse) renderCourse(currentCourse);
    }
  }

  // ── Add lesson: choose AI or approve a textbook source ─────────────────────

  const TEXTBOOK_SOURCE_CHAR_LIMIT = 24000;
  const TEXTBOOK_SOURCE_PAGE_LIMIT = 20;
  let _books = [];
  let _booksCourseId = null;
  let _bookBusy = false;
  let _bookMsg = '';
  let _makerStep = 'choose';
  let _sourceBookId = null;
  let _sourceSection = 'custom';
  let _sourceStart = 1;
  let _sourceEnd = 1;
  let _sourceText = '';
  let _sourceVisuals = [];
  let _selectedVisualIds = new Set();
  let _sourceGuidance = '';
  let _sourceLoading = false;
  let _sourceError = '';
  let _sourceTimer = null;
  let _sourceRequest = 0;
  let _reReading = false;
  let _pendingBookFile = null;
  let _pendingBookTitle = '';
  let _bookProgress = '';
  // Chapter → vocab deck review state.
  let _vocabItems = [];        // [{target, gloss, romanization, cefr, in_deck, _pick}]
  let _vocabMeta = null;       // {section, book, start, end, lang}
  let _vocabName = '';
  let _vocabSaveShared = false;
  let _vocabBusy = false;
  let _vocabExtracting = false; // true only during the LLM extraction call (not the commit)
  let _vocabExpanded = false;
  let _vocabDone = null;       // {created, skipped, label_id, deck_id} after commit

  function _sourceBook() { return _books.find(b => b.id === _sourceBookId) || null; }

  function openLessonMaker(courseId) {
    _booksCourseId = courseId;
    _makerStep = 'choose';
    _sourceError = '';
    document.getElementById('books-overlay').classList.add('open');
    renderLessonMaker();
  }

  // Kept as a small compatibility alias for old inline links / cached HTML.
  function openBooks(courseId) {
    openLessonMaker(courseId);
    chooseLessonSource('textbook');
  }

  function closeBooks() {
    if (_sourceTimer) clearTimeout(_sourceTimer);
    document.getElementById('books-overlay').classList.remove('open');
  }

  function lessonMakerBack() {
    if (_bookBusy || _vocabBusy) return;
    if (_makerStep === 'manage') _makerStep = 'source';
    else if (_makerStep === 'vocab') { _makerStep = 'source'; _vocabDone = null; }
    else if (_makerStep === 'source') _makerStep = 'books';
    else _makerStep = 'choose';
    _sourceError = '';
    renderLessonMaker();
  }

  function chooseLessonSource(kind) {
    if (kind === 'ai') {
      closeBooks();
      generateNextLesson(_booksCourseId, 1);
      return;
    }
    _makerStep = 'books';
    document.getElementById('books-body').innerHTML =
      '<div class="vocab-empty"><span class="spinner"></span> Loading textbooks…</div>';
    loadBooks();
  }

  async function loadBooks(render = true) {
    try {
      const r = await fetch(`/api/courses/${_booksCourseId}/textbooks`);
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || 'Failed to load textbooks');
      _books = body.textbooks || [];
      if (_sourceBookId && !_sourceBook()) _sourceBookId = null;
    } catch (e) {
      _books = [];
      _sourceError = e.message || 'Failed to load textbooks.';
    }
    if (render) renderLessonMaker();
  }

  function _makerHeader(title, back = true) {
    document.getElementById('books-title').textContent = title;
    document.getElementById('books-back').style.display = back ? '' : 'none';
  }

  function renderLessonMaker() {
    const body = document.getElementById('books-body');
    if (_makerStep === 'choose') {
      _makerHeader('Add a lesson', false);
      body.innerHTML = `<p class="maker-intro">Continue with one AI-planned lesson, or turn a reviewed textbook chapter into a complete multi-lesson unit.</p>
        <div class="maker-options">
          <button class="maker-option" onclick="chooseLessonSource('ai')"${_generating ? ' disabled' : ''}>
            <span class="maker-option-icon">✨</span><b>Continue my course</b>
            <small>AI chooses the next topic from your progress, deck, and course focus.</small>
          </button>
          <button class="maker-option" onclick="window.location.href='/textbooks'">
            <span class="maker-option-icon">📕</span><b>From a textbook</b>
            <small>Read your books by chapter, then turn a chapter into a vocab deck or a lesson unit.</small>
          </button>
        </div>`;
      return;
    }
    if (_makerStep === 'books') { renderBookPicker(body); return; }
    if (_makerStep === 'manage') { renderSectionManager(body); return; }
    if (_makerStep === 'vocab') { renderVocabReview(body); return; }
    renderSourceReview(body);
  }

  function renderBookPicker(body) {
    _makerHeader('Choose a textbook');
    let html = `<p class="maker-intro">Use a textbook you've already parsed, or upload another PDF. Uploaded books stay here for future lessons.</p>`;
    if (_bookProgress) {
      html = `<div class="maker-progress" role="status" aria-live="polite"><span class="spinner"></span><span>${esc(_bookProgress)}</span></div>` + html;
    }
    if (_pendingBookFile) {
      html += `<div class="maker-upload-card">
        <div class="maker-upload-file">PDF: ${esc(_pendingBookFile.name)}</div>
        <label class="maker-field" style="margin-bottom:9px"><span>Textbook name</span>
          <input class="maker-select" maxlength="200" value="${esc(_pendingBookTitle)}" oninput="_pendingBookTitle=this.value" placeholder="Name this textbook">
        </label>
        <div class="maker-upload-row">
          <button class="course-regen" onclick="cancelPendingBook()"${_bookBusy ? ' disabled' : ''}>Choose another</button>
          <button class="cta-btn" style="margin:0" onclick="uploadBook()"${_bookBusy ? ' disabled' : ''}>${_bookBusy ? '<span class="spinner"></span> Extracting text and visuals…' : 'Upload and parse'}</button>
        </div>
      </div>`;
    } else {
      html += `<button class="maker-upload" onclick="chooseBookFile()"${_bookBusy ? ' disabled' : ''}>＋ Upload another textbook PDF</button>`;
    }
    if (_sourceError) html += `<div class="maker-error">${esc(_sourceError)}</div>`;
    if (!_books.length && !_bookBusy) html += '<div class="vocab-empty">No textbooks uploaded yet.</div>';
    html += '<div class="maker-book-list">';
    _books.forEach((b, bi) => {
      html += `<button class="maker-book" onclick="selectBook(${b.id})">
        <span class="maker-book-icon">📕</span>
        <span class="maker-book-copy"><b>${esc(b.title)}</b><small>${b.num_pages} pages · ${(b.chapters || []).length} unit${(b.chapters || []).length === 1 ? '' : 's'}${b.visual_count ? ` · ${b.visual_count} visual${b.visual_count === 1 ? '' : 's'}` : ''}</small></span>
        <span class="maker-book-arrow">›</span>
      </button>
      <div style="display:flex;justify-content:flex-end;gap:12px;margin:-8px 8px 2px">
        ${!(b.chapters || []).length ? `<button class="maker-link" style="color:var(--primary)" onclick="analyzeBook(${b.id})">Detect units</button>` : ''}
        <button class="maker-link" style="color:var(--text-muted)" onclick="renameBook(${bi})">Rename</button>
        <button class="maker-link" style="color:var(--text-muted)" onclick="deleteBook(${bi})">Remove</button>
      </div>`;
    });
    body.innerHTML = html + '</div>';
  }

  function selectBook(bookId) {
    const b = _books.find(x => x.id === bookId);
    if (!b) return;
    _sourceBookId = b.id;
    _sourceGuidance = '';
    _sourceError = '';
    const firstIdx = (b.chapters || []).findIndex(ch => ch.lesson_enabled !== false);
    const first = firstIdx >= 0 ? b.chapters[firstIdx] : null;
    _sourceSection = first ? String(firstIdx) : 'custom';
    _sourceStart = first ? first.start : 1;
    _sourceEnd = first ? first.end : Math.min(5, b.num_pages);
    if (_sourceEnd - _sourceStart + 1 > TEXTBOOK_SOURCE_PAGE_LIMIT) {
      _sourceEnd = _sourceStart + TEXTBOOK_SOURCE_PAGE_LIMIT - 1;
      _sourceSection = 'custom';
    }
    _makerStep = 'source';
    loadSourcePreview();
  }

  function renderSourceReview(body) {
    const b = _sourceBook();
    if (!b) { _makerStep = 'books'; renderBookPicker(body); return; }
    _makerHeader('Review textbook unit');
    const sectionOptions = (b.chapters || []).map((ch, i) =>
      ch.lesson_enabled === false ? ''
        : `<option value="${i}"${_sourceSection === String(i) ? ' selected' : ''}>${esc(ch.title)} · pages ${ch.start}–${ch.end}</option>`).join('');
    const visuals = _sourceVisuals.length ? `<label class="maker-field"><span>Images and diagrams from these pages</span>
        <div class="maker-help" style="margin-top:0"><span>Choose up to 6 relevant visuals for the unit planner; leave decorative images unselected.</span></div>
        <div class="maker-visual-grid">${_sourceVisuals.map(v => {
          const selected = _selectedVisualIds.has(v.id);
          const pages = (v.pages || []).join(', ');
          return `<label class="maker-visual${selected ? ' selected' : ''}"><img src="${esc(v.url)}" alt="Extracted textbook visual from page ${esc(pages)}" loading="lazy">
            <input type="checkbox"${selected ? ' checked' : ''} onchange="toggleSourceVisual('${esc(v.id)}',this.checked)"><span class="maker-visual-page">p. ${esc(pages)}</span></label>`;
        }).join('')}</div>
      </label>` : '';
    body.innerHTML = `<div class="maker-source-head">
        <div><div class="maker-kicker">Textbook</div><h3>${esc(b.title)}</h3><p>${b.num_pages} PDF pages</p></div>
        <div style="display:flex;flex-direction:column;align-items:flex-end"><button class="maker-link" onclick="renameBookById(${b.id})">Rename</button><button class="maker-link" onclick="_makerStep='books';renderLessonMaker()">Change book</button></div>
      </div>
      <label class="maker-field"><span>Start from a detected textbook unit</span>
        <select class="maker-select" onchange="selectBookSection(this.value)">
          ${sectionOptions}<option value="custom"${_sourceSection === 'custom' ? ' selected' : ''}>Custom page range</option>
        </select>
      </label>
      <div class="maker-range">
        <label class="maker-page"><span>Start page</span><input id="source-start" type="number" min="1" max="${b.num_pages}" value="${_sourceStart}" onchange="sourceRangeChanged()"></label>
        <span class="maker-range-dash">to</span>
        <label class="maker-page"><span>End page</span><input id="source-end" type="number" min="1" max="${b.num_pages}" value="${_sourceEnd}" onchange="sourceRangeChanged()"></label>
      </div>
      <label class="maker-field"><span>Extracted text for this unit</span>
        <textarea id="source-text" class="maker-textarea" oninput="_sourceText=this.value;updateSourceCount()"${(_sourceLoading || _reReading) ? ' disabled' : ''}>${esc(_reReading ? 'AI is re-reading these pages…' : _sourceLoading ? 'Loading extracted text…' : _sourceText)}</textarea>
        <span class="maker-help"><span>Review this carefully. You can remove irrelevant text or fix extraction errors.</span><span class="maker-count" id="source-count"></span></span>
        <button class="maker-link" onclick="reReadSource()"${(_bookBusy || _sourceLoading || _reReading) ? ' disabled' : ''}>${_reReading ? '<span class="spinner"></span> AI is re-reading these pages…' : '✨ Re-read these pages with AI'}</button>
        <span class="maker-help"><span>If the text above is garbled, out of order, or shows only romanization with no ${esc(langName || 'native')} characters, let AI read the page images and rewrite it cleanly.</span></span>
      </label>
      ${visuals}
      <label class="maker-field"><span>Guide the unit <span style="font-weight:400;color:var(--text-muted)">(optional)</span></span>
        <textarea class="maker-guidance" maxlength="1000" placeholder="For example: focus on the classifier examples, use exercise 3, and make the practice conversation-heavy." oninput="_sourceGuidance=this.value">${esc(_sourceGuidance)}</textarea>
      </label>
      <button class="maker-link" onclick="openSectionManager()">Edit how this textbook is divided into units</button>
      ${_bookMsg ? `<div class="learn-note" style="margin-top:8px">${esc(_bookMsg)}</div>` : ''}
      ${_sourceError ? `<div class="maker-error">${esc(_sourceError)}</div>` : ''}
      <div class="maker-actions">
        <button class="course-regen" onclick="lessonMakerBack()"${_bookBusy ? ' disabled' : ''}>Back</button>
        <button class="cta-btn secondary" onclick="buildVocabDeck()"${(_bookBusy || _sourceLoading || _reReading || _vocabBusy) ? ' disabled' : ''} title="Pull every word from these pages into a flashcard deck">${_vocabBusy ? '<span class="spinner"></span> Reading chapter…' : '📇 Build vocab deck'}</button>
        <button class="cta-btn" onclick="createTextbookLesson()"${(_bookBusy || _sourceLoading) ? ' disabled' : ''}>${_bookBusy ? '<span class="spinner"></span> Planning unit…' : 'Create multi-lesson unit'}</button>
      </div>`;
    updateSourceCount();
  }

  // ── Chapter → vocab deck ────────────────────────────────────────────────────

  async function buildVocabDeck() {
    const b = _sourceBook();
    if (!b || _bookBusy || _sourceLoading || _reReading || _vocabBusy) return;
    if ((_sourceText || '').length > TEXTBOOK_SOURCE_CHAR_LIMIT) {
      _sourceError = `This selection is too long. Trim it below ${TEXTBOOK_SOURCE_CHAR_LIMIT.toLocaleString()} characters or choose fewer pages.`;
      renderLessonMaker();
      return;
    }
    // Switch to the vocab step right away so the learner sees a loading screen
    // (the LLM extraction can take many seconds over a multi-page chapter).
    _vocabBusy = true; _vocabExtracting = true; _sourceError = '';
    _vocabItems = []; _vocabDone = null; _vocabExpanded = false;
    _makerStep = 'vocab';
    renderLessonMaker();
    try {
      const res = await textbookFetch(`/api/textbooks/${b.id}/vocab`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          start: _sourceStart, end: _sourceEnd,
          source_text: _sourceText, guidance: _sourceGuidance,
        }),
      }, 300000);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not extract vocabulary.');
      _vocabItems = (data.items || []).map(it => ({ ...it, _pick: !it.in_deck }));
      _vocabMeta = { section: data.section, book: data.book,
                     start: data.start, end: data.end, lang: data.lang };
      _vocabName = data.section || `Pages ${data.start}–${data.end}`;
      _vocabSaveShared = false;
    } catch (e) {
      _sourceError = e.message || 'Could not extract vocabulary.';
    } finally {
      _vocabBusy = false; _vocabExtracting = false;
      renderLessonMaker();
    }
  }

  function _vocabSelCount() { return _vocabItems.filter(it => it._pick).length; }

  function renderVocabReview(body) {
    _makerHeader('Build vocab deck');
    if (_vocabExtracting) {
      const b = _sourceBook();
      const where = b ? `pages ${_sourceStart}–${_sourceEnd} of ${esc(b.title)}` : 'these pages';
      body.innerHTML = `<div class="vocab-loading">
          <span class="spinner"></span>
          <h3>Reading ${where} for vocabulary…</h3>
          <p>Pulling every word out of the chapter and checking it against your deck. This can take a little while for longer chapters.</p>
        </div>`;
      return;
    }
    if (_vocabDone) { renderVocabDone(body); return; }
    if (!_vocabItems.length) {
      body.innerHTML = `<div class="vocab-loading">
          <div class="vocab-done-icon">🔍</div>
          <h3>No vocabulary found</h3>
          <p>${_sourceError
            ? esc(_sourceError)
            : `We couldn't pull any words from these pages. Try a different page range, or re-read the pages with AI if the extracted text looked garbled.`}</p>
          <div style="margin-top:12px"><button class="cta-btn secondary" onclick="lessonMakerBack()">Back to pages</button></div>
        </div>`;
      return;
    }
    const total = _vocabItems.length;
    const inDeck = _vocabItems.filter(it => it.in_deck).length;
    const newCount = total - inDeck;
    const sel = _vocabSelCount();
    const CAP = 12;
    const shown = _vocabExpanded ? _vocabItems : _vocabItems.slice(0, CAP);
    const rows = shown.map((it, i) => {
      const idx = _vocabItems.indexOf(it);
      const rom = it.romanization ? `<div class="vocab-row-rom">${esc(it.romanization)}</div>` : '';
      const badge = it.in_deck
        ? '<span class="vocab-badge have">✓ in deck</span>'
        : (it.cefr ? `<span class="vocab-badge">${esc(it.cefr)}</span>` : '');
      return `<label class="vocab-row${it.in_deck ? ' in-deck' : ''}">
        <input type="checkbox"${it._pick ? ' checked' : ''}${it.in_deck ? ' disabled' : ''} onchange="toggleVocabItem(${idx},this.checked)">
        <div class="vocab-row-main">${rom}
          <div class="vocab-row-word">${esc(it.target)} <span class="gl">— ${esc(it.gloss)}</span></div>
        </div>${badge}</label>`;
    }).join('');
    const more = (!_vocabExpanded && total > CAP)
      ? `<div class="vocab-more"><button class="maker-link" onclick="_vocabExpanded=true;renderLessonMaker()">Show all ${total} words</button></div>` : '';
    body.innerHTML = `<p class="maker-intro">Every word ${_vocabMeta.section ? `from <b>${esc(_vocabMeta.section)}</b>` : 'in these pages'}, ready to study. Uncheck any you don't want, then add them to your deck.</p>
      <div class="vocab-stats">
        <div class="vocab-stat new"><b>${newCount}</b><span>new word${newCount === 1 ? '' : 's'}</span></div>
        <div class="vocab-stat"><b>${inDeck}</b><span>already in deck</span></div>
        <div class="vocab-stat"><b>${total}</b><span>found</span></div>
      </div>
      <div class="vocab-selbar"><span>${sel} selected</span>
        <span><button onclick="selectAllVocab(true)">Select all new</button> · <button onclick="selectAllVocab(false)">Clear</button></span>
      </div>
      <div class="vocab-list">${rows}</div>${more}
      <label class="maker-field" style="margin-top:14px"><span>Deck name</span>
        <input class="maker-select" maxlength="80" value="${esc(_vocabName)}" oninput="_vocabName=this.value">
      </label>
      <label class="vocab-toggle"><input type="checkbox"${_vocabSaveShared ? ' checked' : ''} onchange="_vocabSaveShared=this.checked"> Also save as a shareable deck in Browse</label>
      ${_sourceError ? `<div class="maker-error">${esc(_sourceError)}</div>` : ''}
      <div class="maker-actions">
        <button class="course-regen" onclick="lessonMakerBack()"${_vocabBusy ? ' disabled' : ''}>Back</button>
        <button class="cta-btn secondary" onclick="commitVocabDeck(false)"${(_vocabBusy || !sel) ? ' disabled' : ''}>Add to deck</button>
        <button class="cta-btn" onclick="commitVocabDeck(true)"${(_vocabBusy || !sel) ? ' disabled' : ''}>${_vocabBusy ? '<span class="spinner"></span> Adding…' : `Add ${sel} &amp; study →`}</button>
      </div>`;
  }

  function toggleVocabItem(idx, checked) {
    if (_vocabItems[idx]) _vocabItems[idx]._pick = checked;
    renderLessonMaker();
  }

  function selectAllVocab(onlyNew) {
    _vocabItems.forEach(it => { it._pick = onlyNew ? !it.in_deck : false; });
    renderLessonMaker();
  }

  async function commitVocabDeck(study) {
    if (_vocabBusy) return;
    const picks = _vocabItems.filter(it => it._pick && !it.in_deck);
    if (!picks.length) { _sourceError = 'Select at least one new word.'; renderLessonMaker(); return; }
    const name = (_vocabName || '').trim();
    if (!name) { _sourceError = 'Give the deck a name.'; renderLessonMaker(); return; }
    _vocabBusy = true; _sourceError = '';
    renderLessonMaker();
    try {
      const res = await fetch('/api/vocab-deck', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          lang: _vocabMeta.lang, deck_name: name, save_shared: _vocabSaveShared,
          items: picks.map(it => ({
            target_text: it.target, source_text: it.gloss,
            romanization: it.romanization || '', cefr_level: it.cefr || null,
            notes: it.example || null,
          })),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not build the deck.');
      _vocabDone = { ...data, name, study };
      if (study && data.label_id) {
        window.location.href = `/cards?label_id=${data.label_id}&study=1`;
        return;
      }
      renderLessonMaker();
    } catch (e) {
      _sourceError = e.message || 'Could not build the deck.';
    } finally {
      _vocabBusy = false;
      if (!_vocabDone) renderLessonMaker();
    }
  }

  function renderVocabDone(body) {
    const d = _vocabDone;
    const skipNote = d.skipped ? ` <span style="color:var(--text-muted)">(${d.skipped} already in your deck)</span>` : '';
    body.innerHTML = `<div class="vocab-done">
        <div class="vocab-done-icon">📇</div>
        <h3>Added ${d.created} word${d.created === 1 ? '' : 's'} to your deck</h3>
        <p>Tagged 📕 ${esc(d.name)}${skipNote}. Study them any time from the Cards tab.</p>
        <button class="cta-btn" onclick="window.location.href='/cards?label_id=${d.label_id}&study=1'">Study ${d.created} word${d.created === 1 ? '' : 's'} →</button>
        <div style="margin-top:10px"><button class="maker-link" onclick="closeBooks()">Done</button></div>
      </div>`;
  }

  function selectBookSection(value) {
    const b = _sourceBook();
    _sourceSection = value;
    if (value !== 'custom' && b && b.chapters[Number(value)]) {
      const ch = b.chapters[Number(value)];
      _sourceStart = ch.start;
      _sourceEnd = Math.min(ch.end, ch.start + TEXTBOOK_SOURCE_PAGE_LIMIT - 1);
      if (_sourceEnd !== ch.end) {
        _sourceSection = 'custom';
        _sourceError = `“${ch.title}” is longer than ${TEXTBOOK_SOURCE_PAGE_LIMIT} pages. Split it into smaller textbook units before generating.`;
      } else _sourceError = '';
      loadSourcePreview();
    }
  }

  function sourceRangeChanged() {
    const b = _sourceBook();
    if (!b) return;
    _sourceStart = Math.max(1, Math.min(b.num_pages, parseInt(document.getElementById('source-start').value, 10) || 1));
    _sourceEnd = Math.max(_sourceStart, Math.min(b.num_pages, parseInt(document.getElementById('source-end').value, 10) || _sourceStart));
    _sourceSection = 'custom';
    _sourceError = '';
    if (_sourceEnd - _sourceStart + 1 > TEXTBOOK_SOURCE_PAGE_LIMIT) {
      _sourceEnd = Math.min(b.num_pages, _sourceStart + TEXTBOOK_SOURCE_PAGE_LIMIT - 1);
      _sourceError = `One textbook unit can use up to ${TEXTBOOK_SOURCE_PAGE_LIMIT} pages. The end page was adjusted.`;
    }
    if (_sourceTimer) clearTimeout(_sourceTimer);
    renderLessonMaker();
    _sourceTimer = setTimeout(loadSourcePreview, 300);
  }

  async function loadSourcePreview() {
    const b = _sourceBook();
    if (!b) return;
    const requestId = ++_sourceRequest;
    _sourceLoading = true;
    _bookMsg = '';
    renderLessonMaker();
    try {
      const r = await fetch(`/api/textbooks/${b.id}/pages?start=${_sourceStart}&end=${_sourceEnd}`);
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Could not load these pages.');
      if (requestId !== _sourceRequest) return;
      _sourceStart = data.start;
      _sourceEnd = data.end;
      _sourceText = (data.pages || []).map(p => `— PDF page ${p.page} —\n${(p.text || '').trim() || '(no extracted text on this page)'}`).join('\n\n');
      _sourceVisuals = data.visuals || [];
      _selectedVisualIds = new Set(_sourceVisuals.slice(0, 6).map(v => v.id));
      _sourceError = _sourceText.length > TEXTBOOK_SOURCE_CHAR_LIMIT
        ? `This selection is ${_sourceText.length.toLocaleString()} characters. Trim it below ${TEXTBOOK_SOURCE_CHAR_LIMIT.toLocaleString()} or choose fewer pages.` : '';
    } catch (e) {
      if (requestId !== _sourceRequest) return;
      _sourceText = '';
      _sourceError = e.message || 'Could not load these pages.';
    } finally {
      if (requestId === _sourceRequest) {
        _sourceLoading = false;
        renderLessonMaker();
      }
    }
  }

  async function reReadSource() {
    // Vision re-extraction: render the selected pages and let the model
    // transcribe them into clean, native-script text (fixes 2-up / interlinear /
    // romanization-only / scanned pages that pypdf mangles). Overwrites the
    // stored page text for this range, then reloads the preview.
    const b = _sourceBook();
    if (!b || _bookBusy || _sourceLoading || _reReading) return;
    _reReading = true;
    _sourceError = '';
    renderLessonMaker();
    try {
      const res = await textbookFetch(`/api/textbooks/${b.id}/transcribe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: _sourceStart, end: _sourceEnd }),
      }, 300000);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not re-read these pages.');
      _sourceText = (data.pages || []).map(
        p => `— PDF page ${p.page} —\n${(p.text || '').trim() || '(no text on this page)'}`
      ).join('\n\n');
      const n = (data.updated_pages || []).length;
      _bookMsg = n ? `AI re-read ${n} page${n === 1 ? '' : 's'}. Review the text below before generating.`
                   : 'AI re-reading produced no changes for these pages.';
    } catch (e) {
      _sourceError = e.name === 'AbortError'
        ? 'AI re-reading took too long. Try a smaller page range.'
        : (e.message || 'Could not re-read these pages.');
    } finally {
      _reReading = false;
      renderLessonMaker();
      updateSourceCount();
    }
  }

  function updateSourceCount() {
    const count = document.getElementById('source-count');
    if (!count) return;
    const n = (_sourceText || '').length;
    count.textContent = `${n.toLocaleString()} / ${TEXTBOOK_SOURCE_CHAR_LIMIT.toLocaleString()}`;
    count.classList.toggle('over', n > TEXTBOOK_SOURCE_CHAR_LIMIT);
  }

  function toggleSourceVisual(id, selected) {
    if (selected && _selectedVisualIds.size >= 6) {
      _sourceError = 'Choose at most 6 visuals for one textbook unit.';
    } else if (selected) {
      _selectedVisualIds.add(id); _sourceError = '';
    } else {
      _selectedVisualIds.delete(id); _sourceError = '';
    }
    renderLessonMaker();
  }

  async function createTextbookLesson() {
    const b = _sourceBook();
    if (!b || _bookBusy || _sourceLoading) return;
    const textEl = document.getElementById('source-text');
    if (textEl) _sourceText = textEl.value;
    if (!_sourceText.trim()) { _sourceError = 'There is no source text to create a unit from.'; renderLessonMaker(); return; }
    if (_sourceText.length > TEXTBOOK_SOURCE_CHAR_LIMIT) { _sourceError = `Trim the source below ${TEXTBOOK_SOURCE_CHAR_LIMIT.toLocaleString()} characters.`; renderLessonMaker(); return; }
    const ch = _sourceSection !== 'custom' ? (b.chapters || [])[Number(_sourceSection)] : null;
    _bookBusy = true;
    _generating = true;
    _genCount = 1;
    _sourceError = '';
    renderLessonMaker();
    if (currentCourse) renderCourse(currentCourse);
    try {
      const res = await fetch(`/api/textbooks/${b.id}/unit`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: _sourceStart, end: _sourceEnd,
          source_text: _sourceText, guidance: _sourceGuidance,
          visual_ids: [..._selectedVisualIds],
          section_title: ch ? ch.title : `Pages ${_sourceStart}–${_sourceEnd}` }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not create the textbook unit.');
      const unit = data.unit_plan || {};
      _bookMsg = `Created “${unit.title || ch?.title || 'textbook unit'}” with ${unit.lesson_count || 1} lesson${unit.lesson_count === 1 ? '' : 's'} covering ${unit.concept_count || 'the selected'} concept${unit.concept_count === 1 ? '' : 's'} from ${b.title}, pages ${_sourceStart}–${_sourceEnd}.`;
      closeBooks();
      const { course } = await fetch('/api/courses/active').then(r => r.json()).catch(() => ({}));
      if (course) { currentCourse = course; renderCourse(course); show('course'); }
    } catch (e) {
      _sourceError = e.message || 'Could not create the textbook unit.';
    } finally {
      _bookBusy = false;
      _generating = false;
      if (document.getElementById('books-overlay').classList.contains('open')) renderLessonMaker();
      if (currentCourse) renderCourse(currentCourse);
    }
  }

  function openSectionManager() {
    _makerStep = 'manage';
    _sourceError = '';
    renderLessonMaker();
  }

  function renderSectionManager(body) {
    const b = _sourceBook();
    if (!b) { _makerStep = 'books'; renderBookPicker(body); return; }
    _makerHeader('Edit textbook units');
    const bi = _books.indexOf(b);
    body.innerHTML = `<p class="maker-intro">Each textbook unit becomes one app unit with multiple lessons. Rename, split, merge, or re-range these to match the real structure of <b>${esc(b.title)}</b>.</p>
      <div class="bk-card">${(b.chapters || []).map((ch, ci) => _sectionRow(bi, ci, ch)).join('') || '<div class="vocab-empty">No units yet.</div>'}
        <div class="bk-actions"><button class="ch-btn" onclick="addChapter(${bi})">＋ Add unit</button></div>
      </div>
      ${_sourceError ? `<div class="maker-error">${esc(_sourceError)}</div>` : ''}
      <div class="maker-actions"><button class="course-regen" onclick="_makerStep='source';renderLessonMaker()">Cancel</button>
        <button class="cta-btn" onclick="saveChapters(${bi})"${_bookBusy ? ' disabled' : ''}>${_bookBusy ? '<span class="spinner"></span> Saving…' : 'Save units'}</button></div>`;
  }

  function _sectionRow(bi, ci, ch) {
    return `<div class="ch-row">
      <input class="ch-title" value="${esc(ch.title)}" onchange="chEdit(${bi},${ci},'title',this.value)" aria-label="Unit title">
      <input class="ch-pg" type="number" min="1" max="${_books[bi].num_pages}" value="${ch.start}" onchange="chEdit(${bi},${ci},'start',this.value)" aria-label="Start page">
      <span class="ch-dash">–</span>
      <input class="ch-pg" type="number" min="1" max="${_books[bi].num_pages}" value="${ch.end}" onchange="chEdit(${bi},${ci},'end',this.value)" aria-label="End page">
      <label class="ch-lessons" title="Hide this chapter from the lesson tree and prevent lesson generation">
        <input type="checkbox"${ch.lesson_enabled !== false ? ' checked' : ''} onchange="chEdit(${bi},${ci},'lesson_enabled',this.checked)">
        <span>Use for lessons</span>
      </label>
      <button class="ch-btn" onclick="removeChapter(${bi},${ci})" title="Remove unit">✕</button>
      ${ch.skip_hint ? '<span class="ch-skip">Suggested skip</span>' : ''}
    </div>`;
  }

  function chEdit(bi, ci, field, value) {
    const ch = _books[bi].chapters[ci];
    if (field === 'title') ch.title = value;
    else if (field === 'lesson_enabled') ch.lesson_enabled = !!value;
    else ch[field] = Math.max(1, Math.min(_books[bi].num_pages, parseInt(value, 10) || 1));
    ch._dirty = true;
    renderLessonMaker();
  }

  function addChapter(bi) {
    const b = _books[bi];
    const last = b.chapters[b.chapters.length - 1];
    const start = last ? Math.min(last.end + 1, b.num_pages) : 1;
    b.chapters.push({ title: 'New unit', start, end: Math.min(start + 4, b.num_pages), skip_hint: false, status: '', lesson_enabled: true, queued: 0, _dirty: true });
    renderLessonMaker();
  }

  function removeChapter(bi, ci) {
    _books[bi].chapters.splice(ci, 1);
    _books[bi].chapters.forEach(c => { c._dirty = true; });
    renderLessonMaker();
  }

  async function saveChapters(bi) {
    const b = _books[bi];
    _bookBusy = true;
    renderLessonMaker();
    try {
      const res = await fetch(`/api/textbooks/${b.id}/chapters`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapters: b.chapters.map(({ _dirty, queued, ...c }) => c) }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not save units.');
      b.chapters = (data.chapters || []).map(c => ({ ...c, queued: 0 }));
      const shelfBook = (_tbBooks || []).find(x => x.id === b.id);
      if (shelfBook) shelfBook.chapters = b.chapters.map(c => ({ ...c }));
      const selected = b.chapters.findIndex(ch => ch.lesson_enabled !== false
        && ch.start === _sourceStart && ch.end === _sourceEnd);
      _sourceSection = selected >= 0 ? String(selected) : 'custom';
      _makerStep = 'source';
      if (currentCourse) renderCourse(currentCourse);
    } catch (e) {
      _sourceError = e.message || 'Could not save units.';
    } finally {
      _bookBusy = false;
      renderLessonMaker();
    }
  }

  function chooseBookFile() {
    if (_bookBusy) return;
    const inp = document.getElementById('book-file-input');
    // Reset first so choosing the exact same file again still fires `change`.
    inp.value = '';
    inp.click();
  }

  function bookFileChosen(inp) {
    const f = inp.files && inp.files[0];
    if (!f) return;
    _pendingBookFile = f;
    _pendingBookTitle = f.name.replace(/\.pdf$/i, '');
    _sourceError = '';
    renderLessonMaker();
  }

  function cancelPendingBook() {
    _pendingBookFile = null; _pendingBookTitle = '';
    const inp = document.getElementById('book-file-input');
    if (inp) inp.value = '';
    renderLessonMaker();
  }

  async function textbookFetch(url, options, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { ...(options || {}), signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  async function requestBookAnalysis(bookId) {
    const res = await textbookFetch(`/api/textbooks/${bookId}/analyze`, {
      method: 'POST',
    }, 120000);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Could not detect textbook units.');
    return data;
  }

  async function analyzeBook(bookId) {
    if (_bookBusy) return;
    _bookBusy = true;
    _sourceError = '';
    _bookProgress = 'Finding textbook unit boundaries…';
    renderLessonMaker();
    try {
      await requestBookAnalysis(bookId);
      await loadBooks(false);
      _bookBusy = false;
      _bookProgress = '';
      selectBook(bookId);
    } catch (e) {
      _sourceError = e.name === 'AbortError'
        ? 'Unit detection took too long. The PDF is saved; tap Detect units to retry.'
        : (e.message || 'The PDF is saved, but unit detection failed. Tap Detect units to retry.');
    } finally {
      _bookBusy = false;
      _bookProgress = '';
      if (document.getElementById('books-overlay').classList.contains('open')) renderLessonMaker();
    }
  }

  async function uploadBook() {
    if (_bookBusy) return;
    if (!_pendingBookFile) {
      _sourceError = 'Choose a PDF before uploading.';
      renderLessonMaker();
      return;
    }
    if (!_pendingBookTitle.trim()) { _sourceError = 'Give this textbook a name.'; renderLessonMaker(); return; }
    // Snapshot before re-rendering. Keeping the real input in the DOM plus this
    // reference avoids intermittent mobile Safari file-handle loss.
    const file = _pendingBookFile;
    const title = _pendingBookTitle.trim();
    _bookBusy = true; _sourceError = '';
    _bookProgress = 'Saving and parsing the PDF…';
    renderLessonMaker();
    try {
      const fd = new FormData();
      fd.append('file', file, file.name);
      fd.append('title', title);
      const res = await textbookFetch(`/api/courses/${_booksCourseId}/textbooks`, {
        method: 'POST', body: fd,
      }, 180000);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Upload failed.');
      _pendingBookFile = null; _pendingBookTitle = '';
      const inp = document.getElementById('book-file-input');
      if (inp) inp.value = '';
      await loadBooks(false);
      _bookProgress = 'PDF saved. Finding textbook unit boundaries…';
      renderLessonMaker();
      try {
        await requestBookAnalysis(data.id);
        await loadBooks(false);
        _bookBusy = false;
        _bookProgress = '';
        _bookMsg = data.low_text_quality
          ? (data.low_text_quality_reason === 'missing_native_script'
              ? `This book's text seems to be romanization only, with no ${langName || 'native'} characters. Use “✨ Re-read these pages with AI” below to recover the native script before generating.`
              : 'The extracted text looks garbled or incomplete. Use “✨ Re-read these pages with AI” below to clean it up before generating.')
          : '';
        selectBook(data.id);
      } catch (analysisError) {
        await loadBooks(false);
        _sourceError = analysisError.name === 'AbortError'
          ? 'The PDF was saved, but unit detection took too long. Tap Detect units to retry.'
          : `The PDF was saved, but unit detection failed: ${analysisError.message || 'try again.'}`;
      }
    } catch (e) {
      _sourceError = e.name === 'AbortError'
        ? 'PDF parsing took too long. Check the textbook library before retrying; the server may still finish saving it.'
        : (e.message || 'Upload failed.');
    } finally {
      _bookBusy = false;
      _bookProgress = '';
      if (document.getElementById('books-overlay').classList.contains('open')) renderLessonMaker();
    }
  }

  async function renameBookById(bookId) {
    const bi = _books.findIndex(b => b.id === bookId);
    if (bi >= 0) await renameBook(bi);
  }

  async function renameBook(bi) {
    const b = _books[bi];
    const title = prompt('Textbook name', b.title);
    if (title === null || !title.trim() || title.trim() === b.title) return;
    try {
      const res = await fetch(`/api/textbooks/${b.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not rename textbook.');
      b.title = data.title;
      renderLessonMaker();
    } catch (e) {
      _sourceError = e.message || 'Could not rename textbook.';
      renderLessonMaker();
    }
  }

  async function deleteBook(bi) {
    const b = _books[bi];
    if (!confirm(`Remove “${b.title}”? Lessons already created from it are kept.`)) return;
    await fetch(`/api/textbooks/${b.id}`, { method: 'DELETE' }).catch(() => {});
    await loadBooks();
  }

  function toggleTbBook(id) {
    const el = document.querySelector(`.tb-book[data-book="${id}"]`);
    if (!el) return;
    const open = !el.classList.contains('open');
    el.classList.toggle('open', open);
    localStorage.setItem('tbbook:' + id, open ? '1' : '0');
  }

  function toggleTextbooks() {
    const isNowCollapsed = localStorage.getItem('learn_textbooks_collapsed') !== '1';
    localStorage.setItem('learn_textbooks_collapsed', isNowCollapsed ? '1' : '0');
    const body    = document.getElementById('textbooks-body');
    const chevron = document.getElementById('textbooks-chevron');
    if (body)    body.style.display = isNowCollapsed ? 'none' : '';
    if (chevron) chevron.style.transform = isNowCollapsed ? '' : 'rotate(90deg)';
  }

  async function _refreshCourse() {
    const { course } = await fetch('/api/courses/active').then(r => r.json()).catch(() => ({}));
    if (course) currentCourse = course;
    return course;
  }

  // The learner's books + their chapters, so the textbook section can list EVERY
  // chapter (built or not) rather than only the units that already exist. Loaded
  // once per page and re-rendered when it lands; a failure just leaves the
  // section showing the units, exactly as before.
  async function loadTextbookShelf(courseId, force) {
    if (!courseId || _tbShelfLoading || (_tbBooks && !force)) return;
    _tbShelfLoading = true;
    try {
      const res = await fetch(`/api/courses/${courseId}/textbooks`);
      if (!res.ok) return;
      _tbBooks = (await res.json()).textbooks || [];
      if (currentCourse) renderCourse(currentCourse);
    } catch { /* units still render */ }
    finally { _tbShelfLoading = false; }
  }

  // Turn ONE chapter into a multi-lesson unit, straight from the course map.
  // Same route the textbook page uses; the server extracts the source text for
  // the chapter's pages (honouring any mid-page split saved on it), plans the
  // whole unit and authors lesson one.
  async function buildChapterUnit(bookId, chapterIdx) {
    if (_tbBuilding || _tbGenerating) return;
    const book = (_tbBooks || []).find(b => b.id === bookId);
    const ch = book && (book.chapters || [])[chapterIdx];
    if (!ch) return;
    _tbBuilding = `${bookId}:${chapterIdx}`; _tbError = null;
    if (currentCourse) renderCourse(currentCourse);
    try {
      const res = await textbookFetch(`/api/textbooks/${bookId}/unit`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: ch.start, end: ch.end, chapter_idx: chapterIdx,
                               section_title: ch.title }),
      }, 300000);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(_detailText(data.detail)
        || 'Could not build lessons from this chapter.');
      const plan = data.unit_plan || {};
      _bookMsg = `Built “${plan.title || ch.title}” — ${plan.lesson_count || 1} lesson${plan.lesson_count === 1 ? '' : 's'} from ${book.title}.`;
      await _refreshCourse();
      await loadTextbookShelf(currentCourse && currentCourse.id, true);
    } catch (e) {
      _tbError = e.name === 'AbortError'
        ? 'Building this chapter took too long. It may still finish — reload in a minute before retrying.'
        : (e.message || 'Could not build lessons from this chapter.');
    } finally {
      _tbBuilding = null;
      if (currentCourse) renderCourse(currentCourse);
    }
  }

  // FastAPI details are usually strings, but the one-unit-per-chapter guard
  // raises a dict ({message, unit_id, …}) so the caller can offer "regenerate".
  function _detailText(detail) {
    if (!detail) return '';
    return typeof detail === 'string' ? detail : (detail.message || '');
  }

  // Author queued lessons for ONE textbook unit (explicit, unit-scoped — never
  // the AI course path). `mode === 'all'` keeps going until the unit's queue is
  // empty, refreshing the map between lessons so each one appears as it lands
  // (one long request instead would show nothing for minutes and risk a
  // gateway timeout).
  async function generateTextbookLesson(unitId, mode) {
    if (_tbGenerating) return;
    _tbGenerating = unitId; _tbError = null; _tbProgress = '';
    if (currentCourse) renderCourse(currentCourse);
    let built = 0;
    try {
      for (let guard = 0; guard < 12; guard++) {
        const res = await fetch(`/api/course-units/${unitId}/next-lesson`, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Could not generate the next lesson.');
        built++;
        const left = data.queued_remaining || 0;
        await _refreshCourse();
        if (mode !== 'all' || !left) break;
        _tbProgress = `Built ${built} · ${left} to go…`;
        renderCourse(currentCourse);
      }
    } catch (e) {
      _tbError = built
        ? `${e.message || 'Generation stopped'} — ${built} lesson${built === 1 ? '' : 's'} built. Tap to continue.`
        : (e.message || 'Could not generate the next lesson.');
      await _refreshCourse();
    } finally {
      _tbGenerating = null; _tbProgress = '';
      if (currentCourse) renderCourse(currentCourse);
    }
  }

  async function clearTextbookUnitQueue(unitId) {
    if (!confirm('Drop the remaining un-generated lessons for this textbook unit? Lessons already built are kept.')) return;
    await fetch(`/api/course-units/${unitId}/queue`, { method: 'DELETE' }).catch(() => {});
    const course = await _refreshCourse();
    if (course) renderCourse(course);
  }

  // Find a unit / lesson in the loaded course map (titles for confirm prompts
  // come from here, never interpolated into an onclick attribute).
  function _findUnit(unitId) {
    return (currentCourse && (currentCourse.units || []).find(u => u.id === unitId)) || null;
  }
  function _findLesson(lessonId) {
    for (const u of (currentCourse && currentCourse.units) || []) {
      const l = (u.lessons || []).find(x => x.id === lessonId);
      if (l) return l;
    }
    return null;
  }

  // Delete a whole unit (its lessons + anything still queued for it). The book
  // keeps its pages, so the chapter can be turned into lessons again later.
  async function deleteCourseUnit(unitId) {
    const unit = _findUnit(unitId);
    const title = (unit && unit.title) || 'this unit';
    const count = ((unit && unit.lessons) || []).length;
    if (!confirm(`Delete “${title}” and its ${count} lesson${count === 1 ? '' : 's'}? The textbook itself is kept, so you can rebuild this chapter any time.`)) return;
    _tbError = null;
    try {
      const res = await fetch(`/api/course-units/${unitId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(((await res.json().catch(() => ({}))).detail) || 'Could not delete this unit.');
    } catch (e) { _tbError = e.message || 'Could not delete this unit.'; }
    const course = await _refreshCourse();
    if (course) renderCourse(course);
  }

  // Delete ONE lesson. In a textbook unit the slot isn't lost — the unit can
  // re-author it from its queue; here it simply removes a lesson you don't want.
  async function deleteLessonRow(lessonId) {
    const lesson = _findLesson(lessonId);
    if (!confirm(`Delete the lesson “${(lesson && lesson.title) || 'this lesson'}”? Your XP and streak are kept.`)) return;
    _tbError = null;
    try {
      const res = await fetch(`/api/lessons/${lessonId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(((await res.json().catch(() => ({}))).detail) || 'Could not delete this lesson.');
    } catch (e) { _tbError = e.message || 'Could not delete this lesson.'; }
    const course = await _refreshCourse();
    if (course) renderCourse(course);
  }

  // ── Course map ───────────────────────────────────────────────────────────────

  // Shared: build the HTML for one lesson row (used for both foundations &
  // textbook units). `opts.deletable` adds a ✕ (textbook units, where lessons
  // are yours to redo); `opts.intro` opens the lesson INTRO SHEET rather than
  // playing straight away.
  //
  // That last one is not cosmetic: the intro sheet is the only place "▶ Resume"
  // is offered, so a textbook lesson opened directly always restarted from the
  // beginning — the snapshot was being written on every quit and never read.
  // (Foundations lessons stay direct: they're two minutes long and the sheet's
  // options — length, AI Speak, test-out — don't apply to them.)
  function _lessonHtml(l, li, opts) {
    const st = l.status || 'locked';
    const displayNum = li + 1;                          // 1-based position within this unit
    const dbgNum = l.lesson_num || displayNum;          // course-wide sequence (debug only)
    const node = st === 'done' ? '✓' : (st === 'locked' ? '🔒' : displayNum);
    const meta = l.concept_count
      ? `${l.concept_count} new concept${l.concept_count === 1 ? '' : 's'}` : '';
    const clickable = unlockAll || st === 'available' || st === 'done';
    const badge = (st === 'done' && l.score != null) ? `✓ ${l.score}%` : st;
    const open = (opts && opts.intro) ? 'openLessonIntro' : 'openLesson';
    const resumable = !!(opts && opts.intro && _loadResume(l.id));
    return `<div class="lesson ${st}${clickable ? ' clickable' : ''}" style="position:relative"
        ${clickable ? `onclick="${open}(${l.id})"` : ''}>
      <div class="lesson-node">${node}</div>
      <div class="lesson-body">
        <div class="lesson-name">${esc(l.title)}</div>
        ${l.objective ? `<div class="lesson-obj">${esc(l.objective)}</div>` : ''}
        ${meta ? `<div class="lesson-meta">${meta}</div>` : ''}
        ${st === 'done' ? '<div class="lesson-replay">↻ Play again</div>'
          : (resumable ? '<div class="lesson-replay">⏸ Resume where you left off</div>' : '')}
      </div>
      <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
        <div class="lesson-status ${st}">${badge}</div>
        ${isAdmin ? `<button onclick="event.stopPropagation();openDebug(${l.id},${dbgNum})"
          title="LLM debug" style="background:none;border:none;cursor:pointer;font-size:.9rem;opacity:.45;padding:2px 4px;line-height:1">🔍</button>` : ''}
        ${(opts && opts.deletable) ? `<button class="lesson-del" title="Delete this lesson"
          onclick="event.stopPropagation();deleteLessonRow(${l.id})">✕</button>` : ''}
      </div>
    </div>`;
  }

  // A lesson this unit will have but hasn't authored yet. Reserving the row is
  // the point: a textbook unit shows its real shape ("5 lessons, 2 built")
  // instead of a bare counter, and the next one is one tap away.
  function _queuedLessonHtml(q, position, unitId, isNext, busy) {
    const label = q.title || 'Next lesson';
    const action = isNext
      ? (busy ? '<span class="spinner"></span> Building…' : '＋ Generate this lesson')
      : 'Queued from your textbook';
    return `<div class="lesson queued${isNext && !busy ? ' clickable' : ''}"
        ${isNext && !busy ? `onclick="generateTextbookLesson(${unitId})"` : ''}>
      <div class="lesson-node queued">${position}</div>
      <div class="lesson-body">
        <div class="lesson-name">${esc(label)}</div>
        <div class="lesson-meta">${action}</div>
      </div>
      <div class="lesson-status locked">not built</div>
    </div>`;
  }

  // A book's chapters are long — a unit can hold a dozen lessons plus its build
  // actions — and a book has many of them, so drawn open they buried the page.
  // Each chapter is therefore a collapsible row: a title, its progress, and a
  // body that opens on tap. Open/closed is remembered per chapter; the default
  // opens exactly the one the learner is working through.
  function _tbChapterShell(key, title, meta, pill, pillClass, openByDefault, body, extra) {
    const stored = localStorage.getItem('tbch:' + key);
    const open = stored == null ? !!openByDefault : stored === '1';
    return `<div class="tb-chapter${open ? ' open' : ''}" data-ch="${esc(key)}">
      <button class="tb-chapter-head" type="button" onclick="toggleTbChapter('${esc(key)}')">
        <span class="tb-chapter-chevron">›</span>
        <span class="tb-chapter-text">
          <span class="tb-chapter-title">${esc(title)}</span>
          ${meta ? `<span class="tb-chapter-meta">${esc(meta)}</span>` : ''}
        </span>
        <span class="tb-pill ${pillClass}">${esc(pill)}</span>
        ${extra || ''}
      </button>
      <div class="tb-chapter-body">${body}</div>
    </div>`;
  }

  function toggleTbChapter(key) {
    const el = document.querySelector(`.tb-chapter[data-ch="${key}"]`);
    if (!el) return;
    const open = !el.classList.contains('open');
    el.classList.toggle('open', open);
    localStorage.setItem('tbch:' + key, open ? '1' : '0');
  }

  // One built textbook unit: its lessons, its still-queued lesson slots, and the
  // actions that author the rest.
  function _textbookUnitHtml(u, openByDefault) {
    const lessons   = u.lessons || [];
    const queued    = u.queued || [];
    const remaining = queued.length;
    const total     = lessons.length + remaining;
    const gen = _tbGenerating === u.id;
    const done = lessons.filter(l => l.status === 'done').length;
    let html = u.summary ? `<div class="unit-obj">${esc(u.summary)}</div>` : '';
    lessons.forEach((l, li) => { html += _lessonHtml(l, li, { deletable: true, intro: true }); });
    queued.forEach((q, qi) => {
      html += _queuedLessonHtml(q, lessons.length + qi + 1, u.id, qi === 0, gen);
    });
    if (remaining > 0) {
      html += `<div class="tb-gen">
        <button class="course-regen"${gen ? ' disabled' : ''} onclick="generateTextbookLesson(${u.id},'all')">
          ${gen ? `<span class="spinner"></span> ${esc(_tbProgress || 'Building next lesson…')}`
                : `⚡ Build all ${remaining} remaining`}</button>
        ${gen ? '' : `<button class="tb-gen-clear" onclick="generateTextbookLesson(${u.id})">Just the next one</button>
        <button class="tb-gen-clear" onclick="clearTextbookUnitQueue(${u.id})">Clear rest</button>`}
      </div>
      ${gen ? '' : `<div class="tb-gen-note">One AI call per lesson — about ${remaining === 1 ? 'half a minute' : `${remaining} × half a minute`}. You can leave this page; finishing a lesson also builds the next one.</div>`}`;
    }
    if (gen && _tbError) html += `<div class="gen-label" style="color:var(--danger)">${esc(_tbError)}</div>`;
    const allDone = total > 0 && done === lessons.length && !remaining;
    return _tbChapterShell(
      'u' + u.id,
      u.title || 'Textbook unit',
      total ? `${lessons.length} of ${total} lesson${total === 1 ? '' : 's'} built` : '',
      allDone ? '✓ done' : `${done}/${total || lessons.length}`,
      allDone ? 'tb-pill-done' : (gen ? 'tb-pill-busy' : ''),
      openByDefault || gen,
      html,
      `<span class="unit-del" title="Delete this unit" role="button"
         onclick="event.stopPropagation();deleteCourseUnit(${u.id})">🗑</span>`);
  }

  // A chapter the learner hasn't turned into lessons yet. Reserving the row is
  // what makes the book's real shape visible here, and puts first-time
  // generation one tap away instead of a trip to the textbook page.
  function _emptyChapterHtml(book, ch, ci, openByDefault) {
    const key  = `${book.id}:${ci}`;
    const busy = _tbBuilding === key;
    const pages = ch.start === ch.end ? `Page ${ch.start}` : `Pages ${ch.start}–${ch.end}`;
    const err = (_tbError && busy) ? `<div class="gen-label" style="color:var(--danger)">${esc(_tbError)}</div>` : '';
    const body = `<div class="tb-gen">
        <button class="course-regen"${busy || _tbBuilding || _tbGenerating ? ' disabled' : ''}
          onclick="buildChapterUnit(${book.id},${ci})">
          ${busy ? '<span class="spinner"></span> Reading the chapter…'
                 : '＋ Build lessons from this chapter'}</button>
      </div>
      ${busy ? '<div class="tb-gen-note">Planning the whole chapter, then writing lesson one — this takes a minute.</div>' : ''}
      ${err}`;
    return _tbChapterShell(key, ch.title || `Chapter ${ci + 1}`, pages,
                           busy ? 'building…' : 'not built',
                           busy ? 'tb-pill-busy' : 'tb-pill-todo',
                           openByDefault || busy, body);
  }

  // Missing means an older saved chapter, which must keep behaving exactly as
  // before. Only an explicit false opts a chapter out of the lesson curriculum.
  function _chapterUsesLessons(ch) { return !ch || ch.lesson_enabled !== false; }

  // ── Skill-tree path rendering (AI lessons) ──
  function _unitBanner(u, unitNo, ach) {
    if (u.in_progress) {
      // Show the active chapter's title + lesson budget ("Lesson 2 of ~4")
      // instead of a bare "In progress" (chapters now carry a planned length).
      const nDone = (u.lessons || []).length;
      const label = (ach && ach.title) ? esc(ach.title) : 'In progress';
      const pill = (ach && ach.budget)
        ? `Lesson ${Math.min(nDone, ach.budget)} of ~${ach.budget}` : 'Up next';
      return `<div class="lbanner"><span class="lb-unit">${label}</span><span class="lb-pill">${pill}</span></div>`;
    }
    return `<div class="lbanner">
      <span class="lb-unit">Unit ${unitNo}</span>
      <span class="lb-pill">${esc(u.title || 'Lessons')}</span>
      ${u.summary ? `<span class="lb-sub">${esc(u.summary)}</span>` : ''}
    </div>`;
  }

  function _pathRow(l, wave) {
    const st = l.status || 'locked';
    const clickable = unlockAll || st === 'available' || st === 'done';
    const glyph = st === 'done' ? '✓' : (st === 'locked' ? '🔒' : '★');
    const level = st === 'done' ? Math.max(1, l.crown_level || 1) : 0;
    const crowns = level ? `<div class="lcrowns">${Array(Math.min(level, 3)).fill(`<span style="color:var(--warn-accent,#f0962a)">${_ic.crown}</span>`).join('')}</div>` : '';
    const start = st === 'available' ? '<div class="lstart">START</div>' : '';
    const meta = l.concept_count ? `${l.concept_count} new` : '';
    const dbg = isAdmin
      ? `<button class="ldbg" title="LLM debug" onclick="event.stopPropagation();openDebug(${l.id},${l.lesson_num || 0})">🔍</button>` : '';
    // D3 · tapping a node opens the lesson intro sheet (the old per-row
    // ⚡ Practice / 🎯 AI Drills mini-buttons moved into the sheet).
    return `<div class="lrow w${wave & 3}${st === 'locked' ? ' is-locked' : ''}">
      ${start}
      <div class="lnode ${st}${clickable ? ' clickable' : ''}" ${clickable ? `onclick="openLessonIntro(${l.id})"` : ''}>
        ${glyph}${crowns}
      </div>
      <div class="lcap">${esc(l.title)}${meta ? `<div class="lcap-meta">${meta}</div>` : ''}</div>
      ${dbg}
    </div>`;
  }

  // B3 · checkpoint node after a closed unit's lessons: available once every
  // lesson in the unit is done; gold shield once passed.
  function _checkpointRow(u, wave) {
    const lessons = u.lessons || [];
    const allDone = lessons.length > 0 && lessons.every(l => l.status === 'done');
    const passed = !!u.checkpoint_passed;
    const st = passed ? 'done' : (allDone || unlockAll ? 'available' : 'locked');
    const clickable = st !== 'locked';
    const sub = passed
      ? `Passed${u.checkpoint_score != null ? ` · ${u.checkpoint_score}%` : ''}`
      : (st === 'available' ? 'Seal this unit · +40 XP' : 'Finish the unit to unlock');
    return `<div class="lrow w${wave & 3}${st === 'locked' ? ' is-locked' : ''}">
      <div class="lnode checkpoint ${st}${clickable ? ' clickable' : ''}"
        ${clickable ? `onclick="openCheckpoint(${u.id})"` : ''}>🛡</div>
      <div class="lcap">Checkpoint<div class="lcap-meta">${esc(sub)}</div></div>
    </div>`;
  }

  async function openCheckpoint(unitId) {
    show('lesson-loading');
    try {
      const res = await fetch('/api/units/' + unitId + '/checkpoint');
      if (!res.ok) {
        const msg = (await res.json().catch(() => ({}))).detail || 'Could not load the checkpoint.';
        throw new Error(msg);
      }
      const quiz = await res.json();
      const content = quiz.content || {};
      const segments = content.segments || [];
      const segTotals = segments.map(sg => (sg.exercises || []).length);
      const total = segTotals.reduce((a, b) => a + b, 0);
      if (!total) throw new Error('Empty checkpoint');
      _prefetchLesson(content, quiz.target_lang);
      player = {
        lessonId: 0, lang: quiz.target_lang, title: quiz.title || 'Checkpoint',
        segments, segTotals, segIdx: 0, segAnswered: 0, queue: [], idx: 0, total, answered: 0,
        firstPassCorrect: 0, mistakes: [], reviewStarted: false,
        combo: 0, maxCombo: 0, xp: 0, listeningHits: 0,
        controller: null, graded: false,
        conceptResults: {}, vocabGlossary: {},
        concepts: [], theme: 'checkpoint', skipTeach: true, drillsOnly: false,
        checkpointUnitId: unitId, checkpointPassPct: quiz.pass_pct || 80,
      };
      const sc = scriptClassFor(player.lang);
      document.getElementById('state-player').className = sc;
      document.getElementById('state-teach').className = sc;
      document.getElementById('state-results').className = 'learn-center ' + sc;
      startSegment(0);
    } catch (e) {
      alert(e.message || 'Could not load the checkpoint — please try again.');
      refreshAndShowCourse();
    }
  }

  // Draw the angled trail as an SVG polyline through the node centres. Re-run on
  // render / show / resize since node positions depend on layout (offsets + caption
  // heights vary), so a fixed CSS spine can't follow the wave.
  function drawPathConnectors() {
    const path = document.querySelector('#units .lpath');
    if (!path) return;
    const old = path.querySelector('.lpath-svg');
    if (old) old.remove();
    // Collapsed AI units keep their nodes in the DOM. Exclude those zero-layout
    // nodes or their (0,0) rectangles pull the visible trail into the corner.
    const nodes = [...path.querySelectorAll('.lnode')]
      .filter(n => n.offsetParent !== null);
    if (nodes.length < 2) return;
    const pr = path.getBoundingClientRect();
    if (!pr.width) return;
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'lpath-svg');
    svg.setAttribute('viewBox', `0 0 ${pr.width} ${pr.height}`);
    const pts = nodes.map(n => {
      const r = n.getBoundingClientRect();
      return [r.left + r.width / 2 - pr.left, r.top + r.height / 2 - pr.top];
    });
    // Catmull-Rom → cubic bezier: smooth curve through all node centres.
    const n = pts.length;
    let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
    for (let i = 0; i < n - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)];
      const p1 = pts[i], p2 = pts[i + 1];
      const p3 = pts[Math.min(n - 1, i + 2)];
      const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
      const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
      const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
      const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ` C${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    const line = document.createElementNS(NS, 'path');
    line.setAttribute('class', 'lpath-line');
    line.setAttribute('d', d);
    svg.appendChild(line);
    path.insertBefore(svg, path.firstChild);
  }
  window.addEventListener('resize', () => requestAnimationFrame(drawPathConnectors));

  function renderCourse(course) {
    currentCourse = course;
    renderDailyRing();   // show the daily-XP ring if streak data is already loaded
    document.getElementById('mute-toggle').textContent = muted ? '🔇' : '🔊';
    const ut = document.getElementById('unlock-toggle');
    ut.textContent = unlockAll ? '🔓' : '🔒';
    ut.title = unlockAll ? 'All lessons unlocked (testing) — tap to re-lock' : 'Unlock every lesson for testing';
    document.getElementById('state-course').dataset.courseId = course.id;
    document.getElementById('course-title').textContent =
      (LANGS.find(x => x.code === course.target_lang) || {}).name || course.target_lang;

    const allUnits  = course.units || [];
    const foundUnits    = allUnits.filter(u => u.theme === 'foundations');
    const textbookUnits = allUnits.filter(u => u.theme === 'textbook');
    const aiUnits       = allUnits.filter(u => u.theme !== 'foundations' && u.theme !== 'textbook');

    // B6 · the Practice hub needs at least one finished AI lesson to remix.
    const aiDone = aiUnits.reduce((n, u) =>
      n + (u.lessons || []).filter(l => l.status === 'done').length, 0);
    document.getElementById('practice-hub-btn').style.display = aiDone > 0 ? '' : 'none';

    const nLessons = course.lesson_count || 0;
    const nDone    = course.done_count   || 0;
    // Count only AI units for the "N units" display (foundations is a fixed track).
    const nAiUnits = aiUnits.filter(u => !u.in_progress).length;
    document.getElementById('course-sub').textContent =
      nLessons === 0
        ? 'No lessons yet'
        : `${nAiUnits} unit${nAiUnits !== 1 ? 's' : ''} · ${nLessons} lesson${nLessons !== 1 ? 's' : ''} · ${nDone}/${nLessons} done`;

    const wrap = document.getElementById('units');
    wrap.innerHTML = '';

    // ── Foundations collapsible section ────────────────────────────────────────
    if (foundUnits.length > 0) {
      // Collapse state: default collapsed (hidden). '0' = expanded, anything else = collapsed.
      const collapsed = localStorage.getItem('learn_foundations_collapsed') !== '0';
      const fTotal = foundUnits.reduce((n, u) => n + (u.lessons || []).length, 0);
      const fDone  = foundUnits.reduce((n, u) =>
        n + (u.lessons || []).filter(l => l.status === 'done').length, 0);

      const section = document.createElement('div');
      section.className = 'foundations-section';
      section.innerHTML = `
        <button class="foundations-toggle" onclick="toggleFoundations()">
          <span class="foundations-icon">📖</span>
          <span class="foundations-label">Reading Track</span>
          <span class="foundations-badge">optional</span>
          <span class="foundations-progress">${fDone}/${fTotal}</span>
          <span class="foundations-chevron" id="foundations-chevron"
            style="transform:${collapsed ? '' : 'rotate(90deg)'}">›</span>
        </button>
        <div class="foundations-body" id="foundations-body"
          style="${collapsed ? 'display:none' : ''}"></div>`;

      const body = section.querySelector('#foundations-body');
      foundUnits.forEach(u => {
        const unitDiv = document.createElement('div');
        unitDiv.className = 'unit';
        let html = `<div class="unit-head">
          <div class="unit-title" style="color:var(--text-muted)">${esc(u.title)}</div>
          ${u.summary ? `<div class="unit-obj">${esc(u.summary)}</div>` : ''}
        </div>`;
        (u.lessons || []).forEach((l, li) => { html += _lessonHtml(l, li); });
        unitDiv.innerHTML = html;
        body.appendChild(unitDiv);
      });

      // Practice games — standalone mini-games using all learned items
      const practiceDiv = document.createElement('div');
      practiceDiv.className = 'fp-section';
      practiceDiv.innerHTML = `<div class="fp-label">Practice Games</div>
        <div class="fp-buttons">
          <button class="fp-btn" onclick="openPracticeGame(${course.id},'speed_round')">
            <span class="fp-icon">⚡</span><span>Speed Round</span></button>
          <button class="fp-btn" onclick="openPracticeGame(${course.id},'audio_blitz')">
            <span class="fp-icon">🔊</span><span>Audio Blitz</span></button>
          <button class="fp-btn" onclick="showMemoryMatchSettings(${course.id})">
            <span class="fp-icon">🃏</span><span>Memory Match</span></button>
        </div>`;
      body.appendChild(practiceDiv);
      wrap.appendChild(section);
    }

    // ── AI (vocab) lessons — vertical skill-tree path ─────────────────────────
    // Collapsible like the other two tracks: once a book's units are what you're
    // working through, a long AI trail is just scrolling. Default EXPANDED
    // ('1' = collapsed), since it's the main course.
    if (aiUnits.some(u => (u.lessons || []).length)) {
      const collapsed = localStorage.getItem('learn_ai_collapsed') === '1';
      const aiTotal = aiUnits.reduce((n, u) => n + (u.lessons || []).length, 0);

      const section = document.createElement('div');
      section.className = 'foundations-section';
      section.innerHTML = `
        <button class="foundations-toggle" onclick="toggleAiPath()">
          <span class="foundations-icon">✨</span>
          <span class="foundations-label">Your AI course</span>
          <span class="foundations-progress">${aiDone}/${aiTotal}</span>
          <span class="foundations-chevron" id="ai-chevron"
            style="transform:${collapsed ? '' : 'rotate(90deg)'}">›</span>
        </button>
        <div class="foundations-body" id="ai-body"
          style="${collapsed ? 'display:none' : ''}"></div>`;

      const path = document.createElement('div');
      path.className = 'lpath';
      const activeUnitIdx = _activeAiUnitIndex(aiUnits);
      let unitNo = 0, wave = 0, html = '', renderedUnitIdx = 0;
      aiUnits.forEach(u => {
        const lessons = u.lessons || [];
        if (!lessons.length) return;
        if (!u.in_progress) unitNo += 1;
        const key = `${course.id}:${u.id || 'active'}`;
        const stored = localStorage.getItem('aiunit:' + key);
        const open = stored == null ? renderedUnitIdx === activeUnitIdx : stored === '1';
        let unitBody = '';
        lessons.forEach(l => { unitBody += _pathRow(l, wave); wave++; });
        // Closed units get a checkpoint node sealing the unit (B3).
        if (!u.in_progress && u.id) { unitBody += _checkpointRow(u, wave); wave++; }
        html += `<section class="ai-path-unit${open ? ' open' : ''}" data-ai-unit="${key}">
          <button class="ai-unit-toggle" type="button" onclick="toggleAiUnit('${key}')"
            aria-label="${open ? 'Collapse' : 'Expand'} ${esc(u.title || 'unit')}">
            <span class="ai-unit-chevron">›</span>${_unitBanner(u, unitNo, course.active_chapter)}
          </button>
          <div class="ai-unit-body">${unitBody}</div>
        </section>`;
        renderedUnitIdx++;
      });
      path.innerHTML = html;
      section.querySelector('#ai-body').appendChild(path);
      wrap.appendChild(section);
      if (!collapsed) requestAnimationFrame(drawPathConnectors);
    }

    // ── Textbooks — every CHAPTER of every book, whether or not it has lessons ──
    // A chapter with no unit yet still gets a row with a "build lessons" action,
    // so a book can be worked through end-to-end from here without detouring to
    // the /textbooks page for each new chapter.
    loadTextbookShelf(course.id);
    const shelf = _tbBooks || [];
    const shelfIds = new Set(shelf.map(b => b.id));
    const hiddenTextbookUnitIds = new Set();
    textbookUnits.forEach(u => {
      const book = shelf.find(b => b.id === u.textbook_id);
      const ch = book && u.chapter_idx != null && (book.chapters || [])[u.chapter_idx];
      if (ch && !_chapterUsesLessons(ch)) hiddenTextbookUnitIds.add(u.id);
    });
    const visibleTextbookUnits = textbookUnits.filter(u => !hiddenTextbookUnitIds.has(u.id));
    // A divided book with every chapter opted out contributes nothing to the
    // lesson tree. An undivided book remains visible so the learner can set it up.
    const visibleShelf = shelf.filter(b => !(b.chapters || []).length
      || (b.chapters || []).some(_chapterUsesLessons));
    if (visibleTextbookUnits.length > 0 || visibleShelf.length > 0) {
      // Default EXPANDED (the learner just built these). '1' = collapsed.
      const collapsed = localStorage.getItem('learn_textbooks_collapsed') === '1';
      const tTotal = visibleTextbookUnits.reduce((n, u) => n + (u.lessons || []).length, 0);
      const tDone  = visibleTextbookUnits.reduce((n, u) =>
        n + (u.lessons || []).filter(l => l.status === 'done').length, 0);

      const section = document.createElement('div');
      section.className = 'foundations-section';
      section.innerHTML = `
        <button class="foundations-toggle" onclick="toggleTextbooks()">
          <span class="foundations-icon">📕</span>
          <span class="foundations-label">From your textbooks</span>
          <span class="foundations-progress">${tDone}/${tTotal}</span>
          <span class="foundations-chevron" id="textbooks-chevron"
            style="transform:${collapsed ? '' : 'rotate(90deg)'}">›</span>
        </button>
        <div class="foundations-body" id="textbooks-body"
          style="${collapsed ? 'display:none' : ''}"></div>`;

      const body = section.querySelector('#textbooks-body');
      const add = (html) => body.insertAdjacentHTML('beforeend', html);

      // Which unit (if any) each chapter of each book produced. Only units whose
      // book is on the shelf are claimed here — otherwise a unit from a deleted
      // book (or every unit, on the first paint before the shelf loads) would be
      // marked as rendered and then never drawn.
      const claimed = new Set();
      const unitFor = new Map();      // `${bookId}:${chapterIdx}` → unit
      textbookUnits.forEach(u => {
        // Hidden chapters also claim their existing unit so it cannot fall
        // through into the legacy "Other units" bucket below.
        if (hiddenTextbookUnitIds.has(u.id)) { claimed.add(u.id); return; }
        if (shelfIds.has(u.textbook_id) && u.chapter_idx != null) {
          unitFor.set(`${u.textbook_id}:${u.chapter_idx}`, u);
          claimed.add(u.id);
        }
      });

      // Open exactly ONE chapter per book by default: the one the learner is
      // working through (the first that isn't finished). Everything else starts
      // collapsed, so a two-book shelf is a readable list rather than a wall.
      const nextChapterOf = new Map();
      visibleShelf.forEach(b => {
        const chapters = b.chapters || [];
        for (let ci = 0; ci < chapters.length; ci++) {
          if (!_chapterUsesLessons(chapters[ci])) continue;
          const u = unitFor.get(`${b.id}:${ci}`);
          if (!u) { nextChapterOf.set(b.id, ci); break; }        // not built yet
          const ls = u.lessons || [];
          const unfinished = (u.queued || []).length
            || ls.some(l => l.status !== 'done');
          if (unfinished) { nextChapterOf.set(b.id, ci); break; }
        }
      });

      visibleShelf.forEach(b => {
        const chapters = b.chapters || [];
        const lessonChapters = chapters.filter(_chapterUsesLessons);
        const built = chapters.reduce((n, c, ci) => n + (_chapterUsesLessons(c)
          && unitFor.has(`${b.id}:${ci}`) ? 1 : 0), 0);
        const bookOpen = localStorage.getItem('tbbook:' + b.id) !== '0';
        add(`<div class="tb-book${bookOpen ? ' open' : ''}" data-book="${b.id}">
          <button class="tb-book-head" type="button" onclick="toggleTbBook(${b.id})">
            <span class="tb-chapter-chevron">›</span>
            <span class="tb-book-title">📕 ${esc(b.title)}</span>
            <span class="tb-book-meta">${built}/${lessonChapters.length || '–'} lesson chapters</span>
          </button>
          <div class="tb-book-body" id="tb-book-${b.id}"></div>
        </div>`);
        const bodyEl = body.querySelector('#tb-book-' + b.id);
        const addBook = html => bodyEl.insertAdjacentHTML('beforeend', html);
        if (!chapters.length) {
          // Detection hasn't run or found nothing; splitting the book into
          // chapters lives on the textbook page.
          addBook(`<div class="unit tb-chapter-todo">
            <div class="unit-obj">No chapters detected yet.</div>
            <div class="tb-gen"><button class="course-regen"
              onclick="window.location.href='/textbooks'">Open the book to divide it</button></div>
          </div>`);
        }
        const openCi = nextChapterOf.get(b.id);
        chapters.forEach((ch, ci) => {
          if (!_chapterUsesLessons(ch)) return;
          const u = unitFor.get(`${b.id}:${ci}`);
          addBook(u ? _textbookUnitHtml(u, ci === openCi)
                    : _emptyChapterHtml(b, ch, ci, ci === openCi));
        });
        // Units from this book with no chapter link (custom page ranges).
        visibleTextbookUnits
          .filter(u => u.textbook_id === b.id && !claimed.has(u.id))
          .forEach(u => { claimed.add(u.id); addBook(_textbookUnitHtml(u)); });
      });
      // Units the shelf didn't account for: a deleted book, or the first paint
      // before the shelf has loaded. Their lessons are still the learner's, so
      // they keep the old grouping by book name.
      let lastBook = null;
      visibleTextbookUnits.filter(u => !claimed.has(u.id)).forEach(u => {
        const name = u.book_title || 'Other units';
        if (name !== lastBook) {
          add(`<div class="tb-book open"><div class="tb-book-head static">
            <span class="tb-book-title">📕 ${esc(name)}</span></div></div>`);
        }
        lastBook = name;
        add(_textbookUnitHtml(u));
      });
      if (_tbError && !_tbGenerating && !_tbBuilding)
        add(`<div class="gen-label" style="color:var(--danger);padding:0 8px 8px">${esc(_tbError)}</div>`);
      wrap.appendChild(section);
    }

    // ── Inline generation area (path-style skeleton matching lrow/lnode) ────────
    const genArea = document.getElementById('course-gen-area');
    if (_generating) {
      const nAiLessons = aiUnits.reduce((s, u) => s + (u.lessons || []).length, 0);
      const skelRow = (i) => `<div class="gen-path-row w${(nAiLessons + 1 + i) & 3}" style="animation-delay:${i * 220}ms">
        <div class="gen-path-node" style="animation-delay:${i * 220}ms"></div>
        <div class="gen-path-cap" style="animation-delay:${i * 220 + 120}ms"></div>
      </div>`;
      genArea.innerHTML = skelRow(0) +
        `<p class="gen-label"><span class="spinner"></span> Generating lesson…</p>`;
    } else if (_genError) {
      genArea.innerHTML = `<p class="gen-label" style="color:var(--danger)">${esc(_genError)}
        <button class="course-regen" style="margin-left:8px"
          onclick="_genError=null;generateNextLesson(${course.id},1)">Retry</button></p>`;
    } else {
      genArea.innerHTML = '';
    }

    // One entry point for lesson creation. The sheet then offers the adaptive
    // AI path or an explicitly reviewed textbook source.
    const cta = document.getElementById('course-cta');
    cta.innerHTML = `<div style="text-align:center;padding:16px 0 4px">
      <button class="course-regen"${_generating ? ' disabled' : ''}
        onclick="openLessonMaker(${course.id})">＋ Add lesson</button>
      ${_bookMsg ? `<div class="learn-note" style="margin-top:8px">${esc(_bookMsg)}</div>` : ''}
    </div>`;
    document.getElementById('course-note').style.display = nLessons > 0 ? '' : 'none';

    // Auto-buffer: silently generate to keep _lessonBuffer unvisited lessons ahead.
    const nUnvisited = nLessons - nDone;
    if (!_generating && nUnvisited < _lessonBuffer && course.id
        && !['teach', 'player', 'results'].includes(_currentState)) {
      setTimeout(() => generateNextLesson(course.id, 1), 400);
    }
  }

  // ── Lesson player ───────────────────────────────────────────────────────────
  let player = null;
  let _audio = null;
  // Cleanup function set by construction_drill.render(); called on finish/quit so
  // visualViewport + touchmove listeners never outlive the drill that added them.
  let _cdDrillCleanup = null;

  function shuffle(a) { a = [...a]; for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }
  // No space between word-bank tokens for scripts written without word spaces.
  const NO_SPACE_LANGS = new Set(['yue', 'cmn', 'ja', 'th']);
  function joinSep(lang) { return NO_SPACE_LANGS.has(lang) ? '' : ' '; }
  // Exercise types that run their own loop/submit (no standard Check/Continue).
  const SELF_MANAGED = new Set(['construction_drill', 'speed_round', 'audio_blitz', 'memory_match']);

  // Mirror of tutor._norm_for_compare. Kept in step with the server so a typed
  // answer the server would accept never costs a round trip (or an LLM call) —
  // and so an exactly-right answer still grades while offline.
  // Canonical form for comparing an answer to the accept-set. Casefold, then
  // drop EVERY punctuation mark, symbol and space: a translation drill isn't
  // testing punctuation or spacing, so a learner who typed the right sentence
  // with a comma in it must not wait on (or pay for) a grader to be told they
  // were right. MIRRORS tutor._norm_for_compare — keep them in step, or an
  // answer accepted here gets re-graded on the server anyway.
  function _normTyped(s) {
    const out = (s || '').normalize('NFC').toLowerCase();
    try { return out.replace(/[\s\p{P}\p{S}]/gu, ''); }
    catch { return out.replace(/[\s.,!?;:'"()\[\]{}<>«»…—–\-。，、！？；：「」『』（）]/g, ''); }
  }
  // Which accepted form an answer is: 'exact' for the lesson's own canonical
  // answer, 'accept' for one of the listed alternatives, '' for neither. The
  // distinction is what shows the learner the taught form whenever they produced
  // something else — a valid variant is still worth seeing it beside.
  // Mirrors tutor.match_kind.
  function _matchKind(typed, expected, accept) {
    const got = _normTyped(typed);
    if (!got) return '';
    if ((expected || '').trim() && _normTyped(expected) === got) return 'exact';
    return (accept || []).some(c => (c || '').trim() && _normTyped(c) === got)
      ? 'accept' : '';
  }
  function _typedMatches(typed, expected, accept) {
    return !!_matchKind(typed, expected, accept);
  }
  function scriptClassFor(code) { const l = LANGS.find(x => x.code === code); return 'script-' + ((l && l.script_family) || 'latin'); }

  // TTS pre-load cache: key → Audio element (preload='auto').
  //
  // Two rules keep "the audio randomly doesn't play" from happening:
  //
  //  1. A cache entry that FAILED to load must never be kept. An <audio> whose
  //     fetch errored is permanently dead — every later play() on it rejects.
  //     Caching it unconditionally meant one transient /api/tts hiccup silenced
  //     that one clip for the rest of the session while every other clip worked.
  //  2. Pre-warming is throttled. _prefetchLesson asks for EVERY clip in the
  //     lesson at once (20–30 of them); each is a separate edge-tts websocket to
  //     Microsoft's free endpoint on the server, and that burst is exactly what
  //     provokes the throttling that rule 1 then made permanent.
  const _ttsCache = {};
  const _ttsQueue = [];
  const _ttsPending = new Set();
  let _ttsInFlight = 0;
  const TTS_PREWARM_CONCURRENCY = 3;
  const TTS_PREWARM_TIMEOUT_MS = 15000;

  function _ttsKey(text, lang) { return lang + '\0' + text; }
  function _ttsUrl(text, lang) {
    return '/api/tts?text=' + encodeURIComponent(text) + '&lang=' + encodeURIComponent(lang);
  }

  // Health of each clip: key → 'ok' | 'fail' (absent = not known yet). A clip
  // that can't be fetched has to SHOW as unavailable — a 🔊 button that looks
  // live and does nothing reads as the app being broken, and an ear-only drill
  // behind it is simply unanswerable. Watchers let a button grey itself out (and
  // a listening drill drop itself) the moment the fetch settles either way.
  const _ttsHealth = {};
  let _ttsWatchers = [];
  let _ttsOkCount = 0, _ttsFailCount = 0;

  function _setTTSHealth(key, state) {
    if (_ttsHealth[key] === state) return;
    _ttsHealth[key] = state;
    if (state === 'ok') _ttsOkCount++; else _ttsFailCount++;
    const live = [];
    for (const w of _ttsWatchers) {
      if (!w.alive()) continue;                 // detached button / abandoned drill
      live.push(w);
      if (w.key === key) { try { w.fn(state); } catch {} }
    }
    _ttsWatchers = live;
  }

  function ttsHealth(text, lang) {
    if (!text) return 'fail';
    return _ttsHealth[_ttsKey(text, lang)] || 'unknown';
  }

  // Call `fn` whenever this clip's health changes (and once now if it's known).
  // `alive` is polled before each call so watchers can't pile up forever.
  function onTTSHealth(text, lang, alive, fn) {
    if (!text) { fn('fail'); return; }
    const key = _ttsKey(text, lang);
    _ttsWatchers.push({ key, alive, fn });
    if (_ttsHealth[key]) fn(_ttsHealth[key]);
  }

  // Wipe what we know about a clip and fetch it again. Used before a drill is
  // dropped for its audio: /api/tts failures are often transient (edge-tts is a
  // free endpoint that throttles), so one clean retry costs little and saves the
  // drill more often than not.
  function _retryTTS(text, lang) {
    const key = _ttsKey(text, lang);
    delete _ttsCache[key];
    delete _ttsHealth[key];
    _prewarmTTS(text, lang);
  }

  // An element that evicts itself from the cache the moment its fetch fails, so
  // the next play rebuilds instead of replaying a corpse.
  function _makeTTSAudio(text, lang, key) {
    const a = new Audio(_ttsUrl(text, lang));
    a.addEventListener('error', () => {
      if (_ttsCache[key] === a) delete _ttsCache[key];
      _setTTSHealth(key, 'fail');
    });
    a.addEventListener('loadeddata', () => _setTTSHealth(key, 'ok'), { once: true });
    return a;
  }

  function _pumpTTSQueue() {
    while (_ttsInFlight < TTS_PREWARM_CONCURRENCY && _ttsQueue.length) {
      const { text, lang, key } = _ttsQueue.shift();
      _ttsPending.delete(key);
      if (_ttsCache[key]) continue;
      const a = _makeTTSAudio(text, lang, key);
      a.preload = 'auto';
      _ttsInFlight++;
      let released = false;
      const release = () => {
        if (released) return;
        released = true;
        _ttsInFlight--;
        _pumpTTSQueue();
      };
      a.addEventListener('loadeddata', release, { once: true });
      a.addEventListener('error', release, { once: true });
      // A request that never settles must not wedge the queue behind it.
      setTimeout(release, TTS_PREWARM_TIMEOUT_MS);
      _ttsCache[key] = a;
      // load() forces the browser to start the HTTP request now — without it,
      // browsers may defer fetching until play() is called, defeating the cache.
      try { a.load(); } catch { release(); }
    }
  }

  function _prewarmTTS(text, lang) {
    if (!text) return;
    const key = _ttsKey(text, lang);
    if (_ttsCache[key] || _ttsPending.has(key)) return;
    _ttsPending.add(key);
    _ttsQueue.push({ text, lang, key });
    _pumpTTSQueue();
  }

  function playTTS(text, lang) {
    if (!text) return;
    stopTTS();
    const url = _ttsUrl(text, lang);
    // Volume boost, when the learner has one: plays the clip's decoded bytes
    // through the gain graph. It never touches the <audio> element, so a null
    // return (no boost, sleeping graph, no Web Audio) or a rejected promise
    // (fetch/decode failure) simply falls through to ordinary playback.
    let boosted = null;
    try { boosted = CantoShell.playBoosted(url); } catch {}
    if (boosted) { boosted.catch(() => _playTTSElement(text, lang)); return; }
    _playTTSElement(text, lang);
  }

  function _playTTSElement(text, lang) {
    const key = _ttsKey(text, lang);
    let el = _ttsCache[key];
    // A cached element that failed to load will never play — rebuild it.
    if (el && el.error) { delete _ttsCache[key]; el = null; }
    if (!el) {
      el = _makeTTSAudio(text, lang, key);
      _ttsCache[key] = el;
    }
    _audio = el;
    try { _audio.currentTime = 0; } catch {}
    // Volume boost (Settings). Wrapped because the booster must never be able
    // to decide whether a clip plays — louder is a nicety, audible is the product.
    _audio.play().catch(err => {
      // NotAllowedError is the browser's autoplay policy, not a broken clip —
      // the element is fine and retrying fails identically, so leave it cached.
      if (err && err.name === 'NotAllowedError') return;
      // Otherwise the source didn't load. Retry ONCE on a fresh element: the
      // learner tapping 🔊 and getting silence is the bug being fixed here.
      if (_ttsCache[key] === el) delete _ttsCache[key];
      const retry = _makeTTSAudio(text, lang, key);
      _ttsCache[key] = retry;
      _audio = retry;
      retry.play().catch(() => {});
    });
  }

  // Wire a 🔊 button to a clip AND keep it honest: while the clip is unavailable
  // the button greys out, and it comes back on its own if a later fetch works.
  // How long a clip may stay "loading" before the button goes live anyway. A
  // browser that defers loading despite load() must not leave a permanently
  // disabled speaker; after this the learner can tap and the play path retries.
  const AUDIO_READY_TIMEOUT_MS = 6000;

  function bindAudioBtn(btn, text, lang) {
    if (!btn) return;
    btn.onclick = () => playTTS(text, lang);
    // Three states, because "tapped it and nothing happened" is the complaint
    // this exists to answer: not fetched YET (dim, not tappable — the clip is
    // coming), ready (normal), and failed (greyed out for good).
    const paint = st => {
      const dead = st === 'fail';
      const waiting = st === 'unknown';
      btn.disabled = dead || waiting;
      btn.classList.toggle('audio-dead', dead);
      btn.classList.toggle('audio-loading', waiting);
      btn.title = dead ? "Audio isn't available right now"
        : (waiting ? 'Loading audio…' : '');
    };
    // Several callers bind before inserting the button, so "not in the document"
    // can't mean "gone" until we've seen it in there once.
    let seen = false;
    onTTSHealth(text, lang, () => (btn.isConnected ? (seen = true) : !seen), paint);
    paint(ttsHealth(text, lang));
    setTimeout(() => {
      if (btn.isConnected && ttsHealth(text, lang) === 'unknown') paint('ok');
    }, AUDIO_READY_TIMEOUT_MS);
    // Fetch it now so the button's state is known before the learner taps —
    // a no-op for a clip _prefetchLesson already warmed.
    _prewarmTTS(text, lang);
  }

  // Silence whatever is playing. Leaving a lesson used to leave its clip running
  // (and, with the volume boost routing into a not-yet-running audio context,
  // several of them arriving at once the moment the context woke up).
  function stopTTS() {
    try { if (_audio) { _audio.pause(); _audio.currentTime = 0; } } catch {}
    _audio = null;
    try { CantoShell.stopBoosted(); } catch {}
  }

  // ── 🎤 Speaking ─────────────────────────────────────────────────────────────
  // Speaking drills use the browser's own speech recogniser and an offline
  // comparison, so a speaking round costs nothing: no LLM call, and no server
  // round trip at all unless a non-Latin answer needs romanizing to be judged
  // fairly. Unsupported browsers never see a speaking drill (see startExercises).
  const _SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  function speechSupported() { return !!_SpeechRec; }

  // A recogniser that never answers is the freeze these bound. `start()` can
  // return into total silence — no onstart, no onresult, no onerror, no onend —
  // and there is no event left that could ever release the drill: an Android
  // build that exposes webkitSpeechRecognition with no recognition service
  // behind it, an iOS session where something else already holds the
  // microphone, any browser whose speech backend is unreachable. The screen
  // then sits on "Listening…" with Check disabled forever, which is exactly
  // what "the app just froze" describes. So every listen is bounded twice: the
  // recogniser must SAY it started, and it must finish.
  const SPEECH_START_MS = 3000;    // no onstart by now → treat as unusable
  const SPEECH_MAX_MS = 20000;     // one utterance can't run longer than this
  // The grading round trips are bounded too — a hung fetch would otherwise hold
  // the Check button disabled with no way out (see gradeFreeText / _fetchTokens).
  const GRADE_TIMEOUT_MS = 15000;
  const RUBY_TIMEOUT_MS = 8000;

  // fetch that always settles. An aborted request rejects, which every caller
  // here already treats as "couldn't check / no romanization".
  function _timedFetch(url, options, timeoutMs) {
    let ctl = null;
    try { ctl = new AbortController(); } catch { ctl = null; }
    const opts = { ...(options || {}) };
    if (ctl) opts.signal = ctl.signal;
    const timer = ctl ? setTimeout(() => { try { ctl.abort(); } catch {} }, timeoutMs) : null;
    return fetch(url, opts).finally(() => { if (timer) clearTimeout(timer); });
  }
  function speechLangFor(lang) {
    const l = LANGS.find(x => x.code === lang);
    return (l && l.speech_lang) || '';
  }

  // Everything that can't be heard — the same canonical form a typed answer is
  // compared in, so speaking and typing never disagree about the same words.
  function _speechNorm(s) { return _normTyped(s); }
  function _stripTones(s) { return (s || '').replace(/\d/g, ''); }

  function _editDistance(a, b) {
    if (a === b) return 0;
    if (!a.length || !b.length) return Math.max(a.length, b.length);
    let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i];
      for (let j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1,
                          prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      prev = cur;
    }
    return prev[b.length];
  }

  // Permissive on purpose. A recogniser routinely returns a HOMOPHONE of what
  // was said — near-universal for tonal languages, where a dozen characters
  // share one reading — and a learner who pronounced the phrase perfectly must
  // not be marked wrong because the machine picked a different character for the
  // same sound. Partial credit for a mostly-right utterance is the point: this
  // is pronunciation practice, not dictation.
  function _speechClose(said, expected) {
    const a = _speechNorm(said), b = _speechNorm(expected);
    if (!a || !b) return false;
    if (a === b) return true;
    if (a.includes(b) || b.includes(a)) return true;
    const longest = Math.max(a.length, b.length);
    return _editDistance(a, b) <= Math.max(1, Math.floor(longest * 0.34));
  }

  // How a string SOUNDS, via the offline romanization oracle (one tokenizer call,
  // no AI). Tone digits are dropped: which character the recogniser chose decides
  // the tone it reports, and that choice isn't the learner's.
  async function _spokenRoman(text, lang) {
    try {
      const toks = await _fetchTokens(text, lang);
      return _stripTones((toks || []).map(t => t.roman || '').join(' ')).trim();
    } catch { return ''; }
  }

  // Grade a free-text answer the way a typed drill does: the offline accept-set
  // first (free, instant, works with no key), then the server judge only when
  // that misses. Shared by the typed drill and by the speaking drill's spoken
  // transcript and "type it instead" box, so there is ONE notion of whether an
  // answer is right — a learner shouldn't be graded differently for saying a
  // sentence than for writing it.
  async function gradeFreeText(text, ex, lang, onNetwork) {
    const expected = ex.answer != null ? ex.answer : (ex.target || '');
    const kind = _matchKind(text, expected, ex.accept);
    // `exact` false for an author-listed alternative, so the feedback shows the
    // canonical answer beside it — for free, with no judge call.
    if (kind) return { checked: true, correct: true, exact: kind === 'exact' };
    if (onNetwork) onNetwork();
    try {
      // Bounded: a request that never settles would leave onAction holding
      // `_grading` and the Check button disabled for the rest of the session.
      const res = await _timedFetch('/api/lesson/check', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: ex.prompt || '', expected, answer: text,
          accept: ex.accept || [], is_cloze: !!ex.is_cloze, lang,
        }),
      }, GRADE_TIMEOUT_MS);
      return res.ok ? await res.json() : { checked: false };
    } catch { return { checked: false }; }
  }

  // Grade an utterance against a speak drill. Every alternative the recogniser
  // offered counts — its top pick is often not its best one.
  async function gradeSpoken(heard, ex, lang) {
    const said = [ex.target, ...(ex.accept || [])].filter(Boolean);
    if (heard.some(h => said.some(t => _speechClose(h, t)))) return true;
    if (!needsRuby(lang) || _isLatin(ex.target)) return false;
    // One round trip per string, all at once. Walked one await at a time this
    // was up to four sequential fetches before the judge call even started —
    // seconds of a disabled Check button on a phone, with nothing on screen
    // saying anything was happening.
    const [want, ...gots] = await Promise.all(
      [ex.target, ...heard].map(t => _spokenRoman(t, lang)));
    if (!want) return false;
    return gots.some(got => got && _speechClose(got, want));
  }

  // ── Audio-only drills ───────────────────────────────────────────────────────
  // Every clip an exercise needs to be answerable.
  function _exClips(ex) {
    if (!ex) return [];
    if (ex.type === 'audio_blitz') return (ex.items || []).map(i => i.audio).filter(Boolean);
    if (ex.type === 'memory_match') {
      return ex.audio_mode ? (ex.pairs || []).map(p => p.audio).filter(Boolean) : [];
    }
    return ex.audio ? [ex.audio] : [];
  }

  // True when the question exists ONLY in the audio: a listening drill, an audio
  // blitz, a sound-matching game, or a foundations drill ("which tone is this?",
  // "spell what you hear") whose screen shows no prompt to read. Without sound
  // these can't be answered at all, so they're dropped rather than failed.
  function _audioOnly(ex) {
    if (!ex) return false;
    if (ex.type === 'audio_blitz') return true;
    if (ex.type === 'memory_match') return !!ex.audio_mode;
    if (!ex.audio) return false;
    if (ex.type === 'listening') return true;
    const written = (ex.prompt || '').trim()
      || (!ex.hide_roman && ((ex.prompt_roman || '').trim() || (ex.roman || '').trim()));
    return !written;
  }

  // ── Sound effects (synthesized, no asset files) ─────────────────────────────
  let muted = localStorage.getItem('learn_muted') === '1';
  let _sfxCtx = null;
  function _sfx() {
    try {
      if (!_sfxCtx) _sfxCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (_sfxCtx.state === 'suspended') _sfxCtx.resume();
      return _sfxCtx;
    } catch { return null; }
  }
  function _beep(freq, start, dur, type = 'sine', vol = 0.16) {
    if (muted) return;
    const ac = _sfx(); if (!ac) return;
    const o = ac.createOscillator(), g = ac.createGain();
    o.type = type; o.frequency.value = freq;
    o.connect(g); g.connect(ac.destination);
    const t = ac.currentTime + start;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(vol, t + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.start(t); o.stop(t + dur + 0.02);
  }
  const sfx = {
    correct() { _beep(587, 0, 0.13, 'sine', 0.16); _beep(880, 0.08, 0.2, 'sine', 0.16); },
    wrong() { _beep(196, 0, 0.18, 'sine', 0.15); _beep(150, 0.07, 0.24, 'sine', 0.13); },
    complete() { [523, 659, 784, 1047].forEach((f, i) => _beep(f, i * 0.1, 0.32, 'triangle', 0.16)); },
    tap() { _beep(520, 0, 0.04, 'sine', 0.05); },
  };
  function toggleMute() {
    muted = !muted;
    localStorage.setItem('learn_muted', muted ? '1' : '0');
    const b = document.getElementById('mute-toggle');
    if (b) b.textContent = muted ? '🔇' : '🔊';
    if (!muted) sfx.tap();
  }
  function confetti() {
    if (muted) { /* still show confetti even if muted */ }
    const colors = ['#f94144', '#f9c74f', '#90be6d', '#43aa8b', '#577590', '#f3722c', '#ff6ec7', '#1a73e8'];
    for (let i = 0; i < 40; i++) {
      const p = document.createElement('div');
      p.className = 'confetti-piece';
      p.style.left = Math.random() * 100 + 'vw';
      p.style.background = colors[i % colors.length];
      document.body.appendChild(p);
      const dur = 1500 + Math.random() * 1300;
      p.animate([
        { transform: `translateY(0) rotate(0deg)`, opacity: 1 },
        { transform: `translateY(${88 + Math.random() * 12}vh) rotate(${360 + Math.random() * 540}deg)`, opacity: 0.9 },
      ], { duration: dur, easing: 'cubic-bezier(.2,.6,.4,1)' });
      setTimeout(() => p.remove(), dur);
    }
  }

  // ── Hangul jamo composition (for block_build) ───────────────────────────────
  // Compose a sequence of typed jamo into one syllable block, like a Korean IME:
  // consonant + vowel(+vowel compound) + optional final(+final compound).
  const HJ = {
    CHO: ['ㄱ','ㄲ','ㄴ','ㄷ','ㄸ','ㄹ','ㅁ','ㅂ','ㅃ','ㅅ','ㅆ','ㅇ','ㅈ','ㅉ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ'],
    JUNG: ['ㅏ','ㅐ','ㅑ','ㅒ','ㅓ','ㅔ','ㅕ','ㅖ','ㅗ','ㅘ','ㅙ','ㅚ','ㅛ','ㅜ','ㅝ','ㅞ','ㅟ','ㅠ','ㅡ','ㅢ','ㅣ'],
    JONG: ['','ㄱ','ㄲ','ㄳ','ㄴ','ㄵ','ㄶ','ㄷ','ㄹ','ㄺ','ㄻ','ㄼ','ㄽ','ㄾ','ㄿ','ㅀ','ㅁ','ㅂ','ㅄ','ㅅ','ㅆ','ㅇ','ㅈ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ'],
    CV: { 'ㅗㅏ':'ㅘ','ㅗㅐ':'ㅙ','ㅗㅣ':'ㅚ','ㅜㅓ':'ㅝ','ㅜㅔ':'ㅞ','ㅜㅣ':'ㅟ','ㅡㅣ':'ㅢ' },
    CF: { 'ㄱㅅ':'ㄳ','ㄴㅈ':'ㄵ','ㄴㅎ':'ㄶ','ㄹㄱ':'ㄺ','ㄹㅁ':'ㄻ','ㄹㅂ':'ㄼ','ㄹㅅ':'ㄽ','ㄹㅌ':'ㄾ','ㄹㅍ':'ㄿ','ㄹㅎ':'ㅀ','ㅂㅅ':'ㅄ' },
  };
  function composeJamo(seq) {
    if (!seq.length) return '';
    const isV = j => HJ.JUNG.includes(j), isC = j => HJ.CHO.includes(j);
    let i = 0;
    if (!isC(seq[0])) return seq.join('');
    const cho = seq[i++];
    if (i >= seq.length || !isV(seq[i])) return seq.join('');
    let jung = seq[i++];
    if (i < seq.length && isV(seq[i]) && HJ.CV[jung + seq[i]]) { jung = HJ.CV[jung + seq[i]]; i++; }
    let jong = '';
    if (i < seq.length && HJ.JONG.includes(seq[i]) && seq[i]) {
      jong = seq[i++];
      if (i < seq.length && HJ.CF[jong + seq[i]]) { jong = HJ.CF[jong + seq[i]]; i++; }
    }
    if (i < seq.length) return seq.join('');   // leftover → not a single clean block
    const ci = HJ.CHO.indexOf(cho), ji = HJ.JUNG.indexOf(jung), ki = jong ? HJ.JONG.indexOf(jong) : 0;
    if (ci < 0 || ji < 0) return seq.join('');
    return String.fromCharCode(0xAC00 + (ci * 21 + ji) * 28 + ki);
  }
  function isHangulSyllable(s) { return s.length === 1 && s >= '가' && s <= '힣'; }

  // Each exercise type: render(ex, root, lang) -> controller
  //   controller: { isReady():bool, grade():bool, answerText():string, lock(correct):void }
  // To add a type: add an entry here + its schema in learning.py's _EXERCISE_CONTRACT.
  const EXERCISE_TYPES = {
    // Inline LLM-graded construction drill: a few turns of "translate this English
    // phrase", judged by the tutor backend. Self-managed (its own submit button +
    // turn loop); calls onDone() when finished so the player advances. Bypasses the
    // one-shot grade contract — the player special-cases ex.type in renderExercise.
    construction_drill: {
      render(ex, root, lang, onDone) {
        const construction = (ex.construction || ex.skill || '').trim();
        let maxTurns = 3;   // matches tutor.LESSON_DRILL_TURNS; the plan's length overrides
        let planItems = [];      // [{english, target}] — drill plan with reference translations
        let turn = 0, busy = false;
        let currentPhrase = null, lastFeedback = null, lastAnswer = null;
        root.innerHTML = `<div class="cd-wrap">
            <div class="cd-topbar">
              <div style="flex:1;min-width:0">
                <div class="cd-kicker">🎯 Construction practice</div>
                <div class="cd-construction">${esc(construction)}</div>
              </div>
              <div class="cd-progress" id="cd-progress"></div>
            </div>
            <div class="cd-scroll" id="cd-scroll">
              <div class="cd-hist" id="cd-hist"></div>
              <div class="cd-prompt" id="cd-prompt"><div class="cd-loading"><span class="cd-dots"><span></span><span></span><span></span></span> Starting drill…</div></div>
              <div class="cd-feed" id="cd-feed"></div>
            </div>
            <div class="cd-input-wrap" id="cd-input-wrap" style="display:none">
              <input type="text" id="cd-input" class="cd-input ${scriptClassFor(lang)}" placeholder="Type your answer…" autocomplete="off" autocapitalize="off" spellcheck="false">
              <button class="cd-send" id="cd-send" type="button" title="Submit">➤</button>
            </div>
            <button class="cta-btn cd-continue" id="cd-continue" type="button" style="display:none">Continue →</button>
            <button class="cd-skip-btn" id="cd-skip-btn" type="button">Skip AI practice</button>
          </div>`;
        const feedEl    = root.querySelector('#cd-feed');
        const promptEl  = root.querySelector('#cd-prompt');
        const inputWrap = root.querySelector('#cd-input-wrap');
        const inputEl   = root.querySelector('#cd-input');
        const sendEl    = root.querySelector('#cd-send');
        const contEl    = root.querySelector('#cd-continue');
        const progEl    = root.querySelector('#cd-progress');
        const histEl    = root.querySelector('#cd-hist');
        const scrollEl  = root.querySelector('#cd-scroll');
        const setProgress = () => { progEl.textContent = `${Math.min(turn, maxTurns)} / ${maxTurns}`; };
        setProgress();

        const playerEl = document.getElementById('state-player');
        const vv = window.visualViewport;
        const fitKb = () => {
          if (!vv || Math.abs((vv.scale || 1) - 1) > 0.05) return;
          playerEl.style.height = Math.round(vv.height) + 'px';
          playerEl.style.top = Math.round(vv.offsetTop) + 'px';
        };
        _cdDrillCleanup = () => {
          playerEl.classList.remove('cd-drilling');
          if (vv) { vv.removeEventListener('resize', fitKb); vv.removeEventListener('scroll', fitKb); }
          playerEl.style.height = ''; playerEl.style.top = '';
          _cdDrillCleanup = null;
        };
        playerEl.classList.add('cd-drilling');
        if (vv) {
          vv.addEventListener('resize', fitKb);
          vv.addEventListener('scroll', fitKb);
          inputEl.addEventListener('focus', () => setTimeout(() => { fitKb(); scrollEl.scrollTop = scrollEl.scrollHeight; }, 120));
          fitKb();
        }

        async function call(answer) {
          busy = true; sendEl.disabled = true;
          try {
            const res = await fetch('/api/lesson/drill', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ construction, plan_items: planItems, answer, turn: turn + 1, lang }),
            });
            if (!res.ok) throw new Error();
            return await res.json();
          } catch { return null; } finally { busy = false; sendEl.disabled = false; }
        }

        const cdNorm = s => (s || '').trim().toLowerCase().replace(/\s+/g, ' ').replace(/[.,!?;:]+$/, '');
        const cdDiffers = (a, b) => !!a && !!b && cdNorm(a) !== cdNorm(b);

        function appendHistory(phrase, fb, yourAnswer) {
          if (!phrase) return;
          const ok = fb && fb.correct;
          const skipped = !fb;
          const yours = (yourAnswer || '').trim();
          const flawed = ok && fb && cdDiffers(fb.corrected, yours);
          const item = document.createElement('div');
          item.className = `cd-hist-item ${skipped ? 'skip' : (ok ? 'ok' : 'miss')}`;
          let inner = `<div class="cd-hist-phrase">${esc(phrase)}</div>`;
          if (yours) inner += `<div class="cd-hist-you${ok || skipped ? '' : ' struck'}">${targetSpan(yours, lang)}</div>`;
          if (skipped) {
            inner += `<div class="cd-hist-note">Skipped — couldn't check</div>`;
          } else {
            if ((!ok || flawed) && fb && fb.corrected) {
              const rom = (fb.corrected_roman && !needsRuby(lang)) ? ` <span class="cd-rom">${esc(fb.corrected_roman)}</span>` : '';
              inner += `<div class="cd-hist-ans">${targetSpan(fb.corrected, lang)}${rom}</div>`;
            }
            if (fb && fb.alt && ok) {
              const altRom = (fb.alt_roman && !needsRuby(lang)) ? ` <span class="cd-rom">${esc(fb.alt_roman)}</span>` : '';
              inner += `<div class="cd-hist-ans cd-hist-alt">${targetSpan(fb.alt, lang)}${altRom}</div>`;
            }
            if (fb && fb.note) inner += `<div class="cd-hist-note">${esc(fb.note)}</div>`;
          }
          item.innerHTML = inner;
          histEl.appendChild(item);
          if (needsRuby(lang)) applyRuby(item, null, false);
          scrollEl.scrollTop = scrollEl.scrollHeight;
        }

        function showPrompt(data) {
          if (!data || data.done || !data.phrase) return finish();
          appendHistory(currentPhrase, lastFeedback, lastAnswer);
          lastFeedback = null; lastAnswer = null;
          currentPhrase = data.phrase;
          promptEl.innerHTML = `<div class="cd-task"><span class="cd-task-label">Translate</span><b>${esc(data.phrase)}</b></div>`;
          feedEl.innerHTML = '';
          inputWrap.style.display = ''; inputEl.value = '';
          scrollEl.scrollTop = scrollEl.scrollHeight;
          inputEl.focus();
          setTimeout(() => scrollEl.scrollTop = scrollEl.scrollHeight, 350);
        }

        function showFeedback(fb, yourAnswer) {
          lastFeedback = fb; lastAnswer = yourAnswer;
          const yours = (yourAnswer || '').trim();
          if (!fb) {
            feedEl.innerHTML = `<div class="cd-fb cd-skip">
              <div class="cd-fb-head">Couldn't check this one</div>
              <div class="cd-fb-note">Your answer has been recorded. Moving on.</div></div>`;
            scrollEl.scrollTop = scrollEl.scrollHeight;
            return;
          }
          const rom = (fb.corrected_roman && !needsRuby(lang)) ? `<span class="cd-rom">${esc(fb.corrected_roman)}</span>` : '';
          const flawed = fb.correct && cdDiffers(fb.corrected, yours);
          const youRow = yours
            ? `<div class="cd-fb-you"><span class="cd-fb-tag">You wrote</span><span class="cd-fb-yourtext${fb.correct ? '' : ' cd-struck'}">${targetSpan(yours, lang)}</span></div>`
            : '';
          const ansRow = (!fb.correct || flawed)
            ? `<div class="cd-fb-ans"><span class="cd-fb-tag">${flawed ? 'Should be' : 'Answer'}</span><span class="cd-fb-anstext">${targetSpan(fb.corrected, lang)}${rom}</span></div>`
            : '';
          const altRom = (fb.alt_roman && !needsRuby(lang)) ? ` <span class="cd-rom">${esc(fb.alt_roman)}</span>` : '';
          const altRow = (fb.alt && fb.correct)
            ? `<div class="cd-fb-ans cd-fb-alt"><span class="cd-fb-tag">Using ${esc(construction)}</span><span class="cd-fb-anstext">${targetSpan(fb.alt, lang)}${altRom}</span></div>`
            : '';
          feedEl.innerHTML = `<div class="cd-fb ${fb.correct ? 'cd-correct' : 'cd-wrong'}">
              <div class="cd-fb-head">${fb.correct ? (flawed ? 'Almost!' : 'Correct!') : 'Not quite'}</div>
              ${youRow}${ansRow}${altRow}
              ${fb.note ? `<div class="cd-fb-note">${esc(fb.note)}</div>` : ''}</div>`;
          if (needsRuby(lang)) applyRuby(feedEl, null, false);
          scrollEl.scrollTop = scrollEl.scrollHeight;
          try { if (navigator.vibrate) navigator.vibrate(fb.correct ? 15 : [0, 30, 30, 30]); } catch {}
        }

        async function start() {
          // Reuse the opener warmed by _preloadDrills if it's ready (consume once —
          // a fresh opener would generate a different set of phrases).
          let data = null;
          const pre = construction && _cdPreload[construction];
          if (pre) { data = await pre; delete _cdPreload[construction]; }
          if (!data) data = await call(null);
          // The learner may have skipped past this drill while the opener call was
          // in flight — promptEl is detached then, and the retry button null.
          if (!data || !data.phrase) {
            promptEl.innerHTML = `<div class="cd-err">Couldn't start the drill — <button id="cd-retry" type="button">retry</button></div>`;
            const retryBtn = promptEl.querySelector('#cd-retry');
            if (retryBtn) retryBtn.onclick = start;
            return;
          }
          planItems = Array.isArray(data.plan_items) ? data.plan_items : [];
          if (planItems.length) maxTurns = planItems.length;
          showPrompt(data);
        }

        async function submit() {
          if (busy) return;
          const ans = inputEl.value.trim();
          if (!ans) return;
          inputWrap.style.display = 'none';
          promptEl.innerHTML = `<div class="cd-loading"><span class="cd-dots"><span></span><span></span><span></span></span> Checking…</div>`;
          feedEl.innerHTML = '';
          const data = await call(ans);
          if (!data) {
            promptEl.innerHTML = `<div class="cd-task"><span class="cd-task-label">Translate</span><b>${esc(currentPhrase)}</b></div>`;
            feedEl.innerHTML = `<div class="cd-fb cd-wrong"><div class="cd-fb-head">Connection error</div><div class="cd-fb-note">Tap retry to try again.</div></div>`;
            const retryBtn = document.createElement('button');
            retryBtn.className = 'cd-fb-next';
            retryBtn.textContent = 'Retry';
            retryBtn.onclick = () => {
              inputEl.value = ans;
              inputWrap.style.display = '';
              feedEl.innerHTML = '';
              promptEl.innerHTML = `<div class="cd-task"><span class="cd-task-label">Translate</span><b>${esc(currentPhrase)}</b></div>`;
            };
            feedEl.querySelector('.cd-fb').appendChild(retryBtn);
            scrollEl.scrollTop = scrollEl.scrollHeight;
            return;
          }
          turn++; setProgress();
          promptEl.innerHTML = `<div class="cd-task"><span class="cd-task-label">Translate</span><b>${esc(currentPhrase)}</b></div>`;
          showFeedback(data.feedback, ans);
          const isDone = turn >= maxTurns || data.done || !data.phrase;
          if (isDone) return finish();
          // Always show a Next button — never auto-advance. The user reads
          // the feedback at their own pace and taps to continue.
          const nextBtn = document.createElement('button');
          nextBtn.className = 'cd-fb-next';
          nextBtn.textContent = 'Next';
          nextBtn.onclick = () => showPrompt(data);
          const fbBox = feedEl.querySelector('.cd-fb');
          if (fbBox) fbBox.appendChild(nextBtn);
          else showPrompt(data);
          scrollEl.scrollTop = scrollEl.scrollHeight;
        }

        function finish() {
          // Fold the final exchange into the compact history (it shows the phrase,
          // the learner's answer and the correction), then clear the live feedback
          // box — otherwise the same feedback appeared twice, once in each place.
          appendHistory(currentPhrase, lastFeedback, lastAnswer);
          currentPhrase = null; lastFeedback = null; lastAnswer = null;
          if (_cdDrillCleanup) _cdDrillCleanup();
          inputWrap.style.display = 'none'; promptEl.innerHTML = '';
          skipEl.style.display = 'none';
          feedEl.innerHTML = `<div class="cd-done">Drill complete!</div>`;
          contEl.style.display = ''; contEl.onclick = () => onDone();
          scrollEl.scrollTop = scrollEl.scrollHeight;
          try { sfx.complete(); } catch {}
        }

        // A2 · the AI step is skippable: counts as a formative pass, same as the
        // LLM-failure path (never penalizes the learner).
        const skipEl = root.querySelector('#cd-skip-btn');
        skipEl.onclick = () => {
          if (_cdDrillCleanup) _cdDrillCleanup();
          onDone();
        };
        sendEl.onclick = submit;
        inputEl.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } };
        start();
        return { custom: true, isReady: () => false, grade: () => true, lock: () => {} };
      },
    },
    choice: {
      render(ex, root, lang) {
        let sel = null;
        const targetPrompt = ex.prompt_lang === 'target';
        const optsTarget = ex.prompt_lang === 'english';
        let html = `<div class="ex-instruction">${esc(ex.instruction || 'Choose the correct answer')}</div>`;
        if (ex.prompt) {
          const pHtml = targetPrompt ? targetSpan(ex.prompt, lang) : esc(ex.prompt);
          html += `<div class="ex-prompt ${targetPrompt ? '' : 'english'}">${pHtml}</div>`;
          if (targetPrompt && ex.prompt_roman && !needsRuby(lang) && !ex.hide_roman) html += `<div class="ex-roman">${esc(ex.prompt_roman)}</div>`;
          // Cloze: show the English sentence translation before the options so the
          // learner knows which word is needed without ambiguity (textbook style).
          if (ex.is_cloze && ex.tip) html += `<div class="ex-translation">${esc(ex.tip)}</div>`;
        }
        if (ex.audio) html += `<div class="audio-center"><button class="audio-play" type="button" id="ex-audio">🔊 Play</button></div>`;
        html += `<div class="opt-list" id="opts"></div>`;
        root.innerHTML = html;
        if (ex.audio) bindAudioBtn(document.getElementById('ex-audio'), ex.audio, lang);
        const list = document.getElementById('opts');
        (ex.options || []).forEach((o, i) => {
          const b = document.createElement('button');
          b.className = 'opt' + (optsTarget ? '' : ' english');
          b.type = 'button';
          b.innerHTML = optsTarget ? targetSpan(o, lang) : esc(o);
          b.onclick = () => { if (player.graded) return; sel = i; [...list.children].forEach((c, ci) => c.classList.toggle('selected', ci === i)); updateAction(); };
          list.appendChild(b);
        });
        return {
          isReady: () => sel !== null,
          grade: () => sel === ex.answer,
          answerText: () => esc(ex.options[ex.answer]),
          lock: () => [...list.children].forEach((c, ci) => { c.disabled = true; if (ci === ex.answer) c.classList.add('correct'); else if (ci === sel) c.classList.add('wrong'); }),
        };
      },
    },
    // Typed free production. The only drill where the learner writes the target
    // language from nothing — every other gradeable kind shows the answer among
    // the options. Graded by /api/lesson/check: accept-set match first (free,
    // instant, works offline), LLM judgement only when that misses.
    type_answer: {
      render(ex, root, lang) {
        const isCloze = !!ex.is_cloze;
        let html = `<div class="ex-instruction">${esc(ex.instruction || 'Write your answer')}</div>`;
        if (ex.prompt) {
          const targetPrompt = ex.prompt_lang === 'target';
          const pHtml = targetPrompt ? targetSpan(ex.prompt, lang) : esc(ex.prompt);
          html += `<div class="ex-prompt ${targetPrompt ? '' : 'english'}">${pHtml}</div>`;
          if (targetPrompt && ex.prompt_roman && !needsRuby(lang) && !ex.hide_roman)
            html += `<div class="ex-roman">${esc(ex.prompt_roman)}</div>`;
          if (isCloze && ex.gloss) html += `<div class="ex-translation">${esc(ex.gloss)}</div>`;
        }
        if (ex.hint) html += `<div class="ex-hint">💡 ${esc(ex.hint)}</div>`;
        html += `<div class="type-wrap">
            <textarea class="type-input ${scriptClassFor(lang)}" id="type-input" rows="${isCloze ? 1 : 2}"
              autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
              enterkeyhint="done" aria-label="Your answer"
              placeholder="${isCloze ? 'the missing word…' : 'type your answer…'}"></textarea>
            <div class="type-status" id="type-status" role="status" aria-live="polite"></div>
          </div>`;
        root.innerHTML = html;

        const input = document.getElementById('type-input');
        const status = document.getElementById('type-status');
        input.oninput = () => { if (!player.graded) updateAction(); };
        // Enter submits (Shift+Enter newlines); the footer button is the other path.
        input.onkeydown = (e) => {
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onAction(); }
        };
        setTimeout(() => { try { input.focus({ preventScroll: true }); } catch {} }, 60);

        let result = null;      // server/offline verdict for lock() + onAction
        return {
          isReady: () => input.value.trim().length > 0,
          async grade() {
            // The offline accept-set answers the common case for free; only a
            // genuinely different rendering reaches the server judge.
            result = await gradeFreeText(input.value.trim(), ex, lang, () => {
              status.textContent = 'Checking…';
              status.className = 'type-status checking';
            });
            status.textContent = ''; status.className = 'type-status';
            return !!(result && result.checked && result.correct);
          },
          // Neither right nor wrong: the grader was unreachable. onAction leaves
          // the score, combo and mastery ledger untouched rather than guessing.
          uncheckable: () => !!(result && result.checked === false),
          feedback: () => result,
          answerText: () => targetSpan(ex.answer, lang)
            + (ex.answer_roman && !needsRuby(lang) ? ` <em>${esc(ex.answer_roman)}</em>` : ''),
          // What the lesson had in mind. Shown next to an accepted-but-different
          // answer: "that works too" alone leaves the learner with no idea what
          // the other way of saying it even was.
          expectedText() { return this.answerText(); },
          lock() {
            input.disabled = true;
            input.classList.add(result && result.correct ? 'correct' : 'wrong');
          },
        };
      },
    },
    // 🎤 Say it out loud. The one drill where the learner PRODUCES the language
    // with their mouth: the target is shown (this is pronunciation practice, not
    // recall), the browser transcribes, and gradeSpoken compares permissively.
    // Nothing here costs an AI call. When the mic is refused or the recogniser
    // errors the drill reports `uncheckable`, so the run's score, combo and
    // mastery ledger are left alone rather than punishing a learner who spoke.
    // 🎤 Say it out loud. The learner PRODUCES the language with their mouth —
    // so the answer is HIDDEN, like the typed drill: an English prompt, and they
    // say it. (A `read_aloud` item, built from material that carries no English
    // gloss, shows the line instead and is pure pronunciation practice.)
    //
    // Grading is the typed drill's, reached through gradeFreeText: the offline
    // accept-set first, the server judge only on a miss. Before that a spoken
    // answer gets the extra permissiveness it needs — a recogniser picks the
    // characters, the learner only supplies the sound.
    //
    // Nobody is ever stuck: ⌨️ types the answer instead, Skip reveals it. A
    // refused mic, an unusable recogniser and a skip all report `uncheckable`,
    // so score, combo, mistakes and mastery are left alone rather than
    // punishing a learner who couldn't speak just then.
    speak: {
      render(ex, root, lang) {
        const reveal = !!ex.read_aloud || !(ex.prompt || '').trim();
        let heard = [];              // every alternative the recogniser offered
        let shown = '';              // what we tell the learner we heard
        let blocked = '';            // non-empty → can't listen; grade as unchecked
        let typing = false, skipped = false;
        let rec = null, listening = false, result = null;

        root.innerHTML = `<div class="ex-instruction">${esc(ex.instruction || (reveal ? 'Say it out loud' : 'Say this out loud'))}</div>
          ${ex.prompt ? `<div class="ex-prompt english">${esc(ex.prompt)}</div>` : ''}
          ${reveal ? `<div class="speak-target">${targetSpan(ex.target, lang)}</div>
            ${ex.target_roman && !needsRuby(lang) ? `<div class="ex-roman">${esc(ex.target_roman)}</div>` : ''}` : ''}
          ${ex.hint ? `<div class="ex-hint">💡 ${esc(ex.hint)}</div>` : ''}
          <div class="speak-row">
            <button class="speak-mic" type="button" id="sp-mic">
              <span class="sp-ico">🎤</span><span class="sp-label" id="sp-label">Tap and speak</span>
            </button>
            ${reveal ? `<button class="audio-play sm" type="button" id="sp-listen" title="Hear it">🔊</button>` : ''}
          </div>
          <div class="speak-heard" id="sp-heard" role="status" aria-live="polite"></div>
          <div class="type-wrap" id="sp-type-wrap" style="display:none">
            <textarea class="type-input ${scriptClassFor(lang)}" id="sp-input" rows="1"
              autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
              enterkeyhint="done" aria-label="Your answer" placeholder="type your answer…"></textarea>
          </div>
          <div class="speak-alt">
            <button type="button" class="speak-alt-btn" id="sp-type">⌨️ Type it instead</button>
            <button type="button" class="speak-alt-btn" id="sp-skip">Skip</button>
          </div>`;
        if (reveal) bindAudioBtn(document.getElementById('sp-listen'), ex.target, lang);

        const mic = document.getElementById('sp-mic');
        const label = document.getElementById('sp-label');
        const out = document.getElementById('sp-heard');
        const input = document.getElementById('sp-input');
        const say = (html, cls) => {
          out.className = 'speak-heard' + (cls ? ' ' + cls : '');
          out.innerHTML = html;
        };
        // Grading a spoken answer can reach the network (romanization, then the
        // judge). Say it out loud rather than leaving a disabled button.
        const checking = () => {
          const was = shown ? `I heard: <b class="${scriptClassFor(lang)}">${esc(shown)}</b> ` : '';
          say(was + '<span class="sp-checking">Checking…</span>', '');
        };
        let startTimer = null, maxTimer = null;
        const clearTimers = () => {
          if (startTimer) { clearTimeout(startTimer); startTimer = null; }
          if (maxTimer) { clearTimeout(maxTimer); maxTimer = null; }
        };
        const idle = () => {
          listening = false;
          mic.classList.remove('live');
          label.textContent = shown ? 'Tap to try again' : 'Tap and speak';
        };
        // Release the microphone for real. `stop()` alone only asks the
        // recogniser to finish, so it keeps the mic (and, on iOS, a recording
        // audio session that silences our own playback) for as long as it likes
        // — after the learner has already left the drill. Handlers are detached
        // first so a late event from a dead recogniser can't reopen the UI.
        const stop = () => {
          clearTimers();
          const r = rec;
          rec = null;
          if (r) {
            r.onstart = r.onresult = r.onerror = r.onend = null;
            try { r.abort(); } catch {}
            try { r.stop(); } catch {}
          }
          idle();
        };

        function listen() {
          if (player.graded || typing) return;
          if (listening) { stop(); updateAction(); return; }
          stop();                      // never leave a previous recogniser running
          // A fresh attempt clears the last one's verdict: a learner who retries
          // after a timeout and is heard perfectly must not still be graded as
          // "couldn't listen". The watchdogs set it again if this try fails too.
          heard = []; shown = ''; blocked = '';
          let r;
          try { r = new _SpeechRec(); } catch { r = null; }
          if (!r) {
            blocked = 'unsupported'; _speechDead = true;
            say("This browser can't listen — type it instead, or skip.", 'muted');
            updateAction(); return;
          }
          rec = r;
          // Every handler ignores a recogniser that is no longer the current
          // one: `stop()` detaches, but a browser can still deliver a queued
          // event, and an old instance's onend would otherwise flip the label
          // back to "Tap and speak" while a newer one is genuinely listening.
          const mine = () => rec === r;
          const giveUp = (msg, dead) => {
            blocked = 'error';
            if (dead) _speechDead = true;   // don't queue more drills it can't run
            say(msg, 'muted');
            stop();
            updateAction();
          };
          r.lang = speechLangFor(lang) || lang;
          r.interimResults = true;
          r.maxAlternatives = 3;
          r.continuous = false;
          r.onstart = () => {
            if (!mine()) return;
            if (startTimer) { clearTimeout(startTimer); startTimer = null; }
            listening = true; mic.classList.add('live'); label.textContent = 'Listening…'; say('', '');
          };
          r.onresult = e => {
            if (!mine()) return;
            const alts = [];
            let best = '';
            for (let i = e.resultIndex; i < e.results.length; i++) {
              const res = e.results[i];
              for (let j = 0; j < res.length; j++) if (res[j].transcript) alts.push(res[j].transcript);
              if (res.isFinal && res[0]) best = res[0].transcript;
            }
            if (alts.length) heard = alts;
            shown = (best || alts[0] || shown || '').trim();
            if (shown) say(`I heard: <b class="${scriptClassFor(lang)}">${esc(shown)}</b>`, '');
            updateAction();
          };
          r.onerror = e => {
            if (!mine()) return;
            const err = (e && e.error) || '';
            if (err === 'not-allowed' || err === 'service-not-allowed') {
              blocked = 'denied';
              say('Microphone access is off — type it instead, or skip.', 'muted');
            } else if (err === 'no-speech') {
              say("Didn't catch that — tap the mic and try again.", 'muted');
            } else if (err !== 'aborted') {
              blocked = 'error';
              say("Couldn't listen just now — type it instead, or skip.", 'muted');
            }
            stop();
            updateAction();
          };
          r.onend = () => { if (!mine()) return; stop(); updateAction(); };
          // Watchdogs. Without them a recogniser that answers nothing leaves the
          // drill on "Listening…" with Check disabled and no event coming.
          startTimer = setTimeout(() => {
            if (!mine() || listening) return;
            giveUp("Couldn't reach the microphone — type it instead, or skip.", true);
          }, SPEECH_START_MS);
          maxTimer = setTimeout(() => {
            if (!mine()) return;
            if (shown) { stop(); updateAction(); }   // keep what was heard
            else giveUp("Didn't hear anything — tap the mic to retry, type it, or skip.");
          }, SPEECH_MAX_MS);
          try { r.start(); } catch { giveUp("Couldn't listen just now — type it instead, or skip.", true); }
        }

        // ⌨️ — the same answer, written. Graded identically.
        function typeInstead() {
          if (player.graded) return;
          stop();
          typing = true; blocked = '';
          document.getElementById('sp-type-wrap').style.display = '';
          document.querySelector('.speak-row').style.display = 'none';
          document.getElementById('sp-type').style.display = 'none';
          say('', '');
          input.oninput = () => { if (!player.graded) updateAction(); };
          input.onkeydown = e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onAction(); }
          };
          setTimeout(() => { try { input.focus({ preventScroll: true }); } catch {} }, 60);
          updateAction();
        }

        function skip() {
          if (player.graded) return;
          stop();
          skipped = true;
          updateAction();
          onAction();          // grade immediately: reveals the answer, costs nothing
        }

        mic.onclick = listen;
        document.getElementById('sp-type').onclick = typeInstead;
        document.getElementById('sp-skip').onclick = skip;
        // Recognition must not keep running once the learner moves on; the
        // player calls this before rendering anything else and on quit.
        player._mgCleanup = stop;

        return {
          isReady: () => skipped || (typing ? input.value.trim().length > 0
                                            : (!!shown.trim() || !!blocked)),
          async grade() {
            stop();
            if (skipped) {
              result = { checked: false, reason: 'Skipped — here it is.' };
              return false;
            }
            if (typing) {
              result = await gradeFreeText(input.value.trim(), ex, lang, checking);
              return !!(result && result.checked && result.correct);
            }
            if (blocked) {
              result = { checked: false, reason: blocked === 'denied'
                ? "Microphone access is off, so this one wasn't checked."
                : "Couldn't listen on this device, so this one wasn't checked." };
              return false;
            }
            // Spoken answers get the extra latitude first (homophones, ASR
            // noise); a genuinely different rendering falls through to the same
            // judge a typed answer would meet. Both can go to the network, so
            // say so — a Check button that greys out and sits there for a few
            // seconds with no explanation is indistinguishable from a hang.
            checking();
            if (await gradeSpoken(heard.length ? heard : [shown], ex, lang)) {
              // Said it right, but the recogniser's transcript is rarely the
              // canonical line — show that line unless they nailed it verbatim.
              result = { checked: true, correct: true,
                         exact: _matchKind(shown, ex.target, ex.accept) === 'exact' };
              return true;
            }
            result = await gradeFreeText(shown, ex, lang, checking);
            if (result && result.checked && !result.correct && shown) {
              result.note = `We heard “${shown}”. ` + (result.note || '');
            }
            return !!(result && result.checked && result.correct);
          },
          uncheckable: () => !!(result && result.checked === false),
          feedback: () => result,
          answerText: () => targetSpan(ex.target, lang)
            + (ex.target_roman && !needsRuby(lang) ? ` <em>${esc(ex.target_roman)}</em>` : ''),
          expectedText: () => targetSpan(ex.target, lang)
            + (ex.target_roman && !needsRuby(lang) ? ` <em>${esc(ex.target_roman)}</em>` : ''),
          lock() {
            stop();
            if (player) player._mgCleanup = null;
            if (out.innerHTML.indexOf('sp-checking') >= 0) {
              say(shown ? `I heard: <b class="${scriptClassFor(lang)}">${esc(shown)}</b>` : '', '');
            }
            mic.disabled = true;
            input.disabled = true;
            document.getElementById('sp-type').style.display = 'none';
            document.getElementById('sp-skip').style.display = 'none';
            mic.classList.add(result && result.correct ? 'correct' : 'wrong');
          },
        };
      },
    },
    listening: {
      render(ex, root, lang) {
        let sel = null;
        root.innerHTML = `<div class="ex-instruction">${esc(ex.instruction || 'What did you hear?')}</div>
          <div class="audio-center" id="listen-audio"><button class="audio-play big" type="button" id="ex-audio">🔊</button></div>
          <button class="listen-fallback" type="button" id="listen-fallback">Can’t listen? Show romanization</button>
          <div class="listen-roman" id="listen-roman" aria-live="polite"></div>
          <div class="opt-list" id="opts"></div>`;
        bindAudioBtn(document.getElementById('ex-audio'), ex.audio, lang);
        const fallback = document.getElementById('listen-fallback');
        const roman = document.getElementById('listen-roman');
        const audioWrap = document.getElementById('listen-audio');
        const instruction = root.querySelector('.ex-instruction');
        let fallbackShowing = false;
        async function showRomanization() {
          if (player.graded || fallbackShowing) return;
          fallbackShowing = true;
          ex._usedAudioFallback = true;
          stopTTS();
          fallback.disabled = true;
          fallback.textContent = 'Loading romanization…';
          const text = await _listeningRomanization(ex, lang);
          // The learner may have advanced while the romanizer was loading.
          if (!player || player.queue[player.idx] !== ex) return;
          audioWrap.style.display = 'none';
          fallback.style.display = 'none';
          instruction.textContent = 'Which phrase matches this romanization?';
          roman.textContent = text || ex.audio || 'Pronunciation unavailable';
          roman.classList.add('shown');
        }
        fallback.onclick = showRomanization;
        if (ex._audioUnavailable || ex._usedAudioFallback) showRomanization();
        // Guard the delayed auto-play: don't speak a stale exercise if the learner
        // already advanced (or quit) before the timer fired.
        setTimeout(() => {
          if (player && player.queue[player.idx] === ex && !ex._usedAudioFallback)
            playTTS(ex.audio, lang);
        }, 300);
        const list = document.getElementById('opts');
        (ex.options || []).forEach((o, i) => {
          const b = document.createElement('button'); b.className = 'opt'; b.type = 'button';
          b.innerHTML = targetSpan(o, lang);
          b.onclick = () => { if (player.graded) return; sel = i; [...list.children].forEach((c, ci) => c.classList.toggle('selected', ci === i)); updateAction(); };
          list.appendChild(b);
        });
        return {
          isReady: () => sel !== null,
          grade: () => sel === ex.answer,
          answerText: () => esc(ex.options[ex.answer]) + (ex.audio_roman ? ` (${esc(ex.audio_roman)})` : ''),
          lock: () => [...list.children].forEach((c, ci) => { c.disabled = true; if (ci === ex.answer) c.classList.add('correct'); else if (ci === sel) c.classList.add('wrong'); }),
        };
      },
    },
    word_bank: {
      render(ex, root, lang) {
        const answer = ex.answer_tokens || [];
        root.innerHTML = `<div class="ex-instruction">${esc(ex.instruction || 'Build the sentence')}</div>
          ${ex.prompt ? `<div class="ex-prompt english">${esc(ex.prompt)}</div>` : ''}
          ${ex.audio ? `<div class="audio-center"><button class="audio-play" type="button" id="ex-audio">🔊 Play</button></div>` : ''}
          <div class="wb-answer" id="wb-answer"></div>
          <div class="wb-bank" id="wb-bank"></div>`;
        if (ex.audio) bindAudioBtn(document.getElementById('ex-audio'), ex.audio, lang);
        const ansEl = document.getElementById('wb-answer');
        const bankEl = document.getElementById('wb-bank');
        // Each tile lives in a fixed home "slot" in the bank. Moving a tile into
        // the answer row leaves the slot in place (frozen at its rendered size) so
        // the bank never reflows — like Duolingo's empty word boxes.
        const glossary = ex.glossary || {};
        shuffle([...answer, ...(ex.distractor_tokens || [])]).forEach(tok => {
          const slot = document.createElement('span'); slot.className = 'wb-slot';
          const t = document.createElement('button'); t.className = 'tile'; t.type = 'button';
          t.dataset.tok = tok;                       // grade against the raw token, not ruby-mangled textContent
          // Untaught helper word → show its English/POS gloss under the tile.
          const gl = glossary[tok];
          t.innerHTML = targetSpan(tok, lang) + (gl ? `<small class="tile-gloss">${esc(gl)}</small>` : '');
          t.onclick = () => {
            if (player.graded) return;
            if (t.parentElement === slot) {          // bank → answer: freeze the slot's footprint
              slot.style.width = slot.offsetWidth + 'px';
              slot.style.height = slot.offsetHeight + 'px';
              slot.classList.add('empty');
              ansEl.appendChild(t);
            } else {                                  // answer → bank: restore the slot
              slot.classList.remove('empty');
              slot.style.width = ''; slot.style.height = '';
              slot.appendChild(t);
            }
            updateAction();
          };
          slot.appendChild(t);
          bankEl.appendChild(slot);
        });
        const placed = () => [...ansEl.querySelectorAll('.tile')].map(c => c.dataset.tok);
        // Strip pure-punctuation tokens (periods, commas, 。！？ etc.) from both
        // sides before grading — learners shouldn't lose marks for missing a trailing
        // period tile; the word order is what's being tested.
        const isPunct = t => /^[\s.,!?。！？、，;；:：…""''「」【】()\[\]]+$/.test(t);
        const gradeToks = arr => arr.filter(t => !isPunct(t));
        return {
          isReady: () => gradeToks(placed()).length > 0,
          grade: () => { const p = gradeToks(placed()); const a = gradeToks(answer); return p.length === a.length && p.every((t, i) => t === a[i]); },
          answerText: () => esc(answer.join(joinSep(lang))) + (ex.answer_roman ? ` (${esc(ex.answer_roman)})` : ''),
          lock: () => root.querySelectorAll('.tile').forEach(c => c.disabled = true),
        };
      },
    },
    match: {
      render(ex, root, lang) {
        const pairs = ex.pairs || [];
        const left = shuffle(pairs.map((p, i) => ({ txt: p.target, roman: p.target_roman, id: i, side: 't' })));
        const right = shuffle(pairs.map((p, i) => ({ txt: p.english, id: i, side: 'e' })));
        let selEl = null, selItem = null, matched = 0;
        root.innerHTML = `<div class="ex-instruction">${esc(ex.instruction || 'Match the pairs')}</div><div class="match-grid" id="mg"></div>`;
        const grid = document.getElementById('mg');
        const rows = Math.max(left.length, right.length);
        for (let i = 0; i < rows; i++) {
          [left[i], right[i]].forEach(item => {
            if (!item) return;
            const el = document.createElement('button');
            el.className = 'match-item' + (item.side === 't' ? ' target' : '');
            el.type = 'button';
            el.innerHTML = item.side === 't' ? targetSpan(item.txt, lang) : esc(item.txt);
            el.onclick = () => {
              if (player.graded || el.classList.contains('matched')) return;
              if (!selEl) { selEl = el; selItem = item; el.classList.add('selected'); return; }
              if (selEl === el) { el.classList.remove('selected'); selEl = null; return; }
              if (selItem.side === item.side) { selEl.classList.remove('selected'); selEl = el; selItem = item; el.classList.add('selected'); return; }
              if (selItem.id === item.id) {
                selEl.classList.remove('selected'); selEl.classList.add('matched'); el.classList.add('matched');
                selEl = null; matched++; updateAction();
              } else {
                const a = selEl, b = el; a.classList.add('flash-wrong'); b.classList.add('flash-wrong');
                setTimeout(() => { a.classList.remove('flash-wrong', 'selected'); b.classList.remove('flash-wrong'); }, 450);
                selEl = null;
              }
            };
            grid.appendChild(el);
          });
        }
        return { isReady: () => matched === pairs.length, grade: () => true, answerText: () => '', lock: () => {} };
      },
    },
    block_build: {
      render(ex, root, lang) {
        // Tap letters that auto-compose into the syllable. Two composition modes:
        //   'concat' (abugida — Devanagari/Telugu): code points just concatenate.
        //   default (Hangul): jamo compose into a precomposed block via composeJamo.
        const isConcat = ex.compose === 'concat';
        const compose = isConcat ? (seq => seq.join('')) : composeJamo;
        const isComplete = isConcat
          ? (s => !!s)                       // any non-empty string is a valid attempt
          : isHangulSyllable;                // Hangul must be a real syllable block
        const consonants = ex.consonants || [...new Set([...(ex.initials || []).map(x => x.j),
          ...(ex.finals || []).map(x => x.j).filter(j => j && j !== '∅')])];
        const vowels = ex.vowels || (ex.medials || []).map(x => x.j);
        let typed = [];
        root.innerHTML = `<div class="ex-instruction">${esc(ex.instruction || 'Spell the syllable')}</div>
          <div class="audio-center"><button class="audio-play big" type="button" id="ex-audio">🔊</button>
            ${ex.roman && !ex.hide_roman ? `<div class="ex-roman">${esc(ex.roman)}</div>` : ''}</div>
          <div class="bb-preview" id="bb-preview">·</div>
          <div class="bb-jamo" id="bb-typed"></div>
          <div class="bb-label">Consonants</div><div class="opt-list bb-row" id="bb-cons"></div>
          <div class="bb-label">Vowels</div><div class="opt-list bb-row" id="bb-vows"></div>
          <button class="course-regen" type="button" id="bb-back" style="margin-top:14px">⌫ Backspace</button>`;
        bindAudioBtn(document.getElementById('ex-audio'), ex.audio, lang);
        setTimeout(() => { if (player && player.queue[player.idx] === ex) playTTS(ex.audio, lang); }, 300);
        const preview = document.getElementById('bb-preview');
        const typedEl = document.getElementById('bb-typed');
        const refresh = () => {
          const c = compose(typed);
          preview.textContent = c || '·';
          typedEl.textContent = typed.join(' ');
          updateAction();
        };
        const mkKey = (containerId, jamo) => {
          const el = document.getElementById(containerId);
          jamo.forEach(j => {
            const b = document.createElement('button'); b.className = 'tile'; b.type = 'button'; b.textContent = j;
            b.onclick = () => { if (player.graded) return; if (typed.length < 5) { typed.push(j); sfx.tap(); refresh(); } };
            el.appendChild(b);
          });
        };
        mkKey('bb-cons', consonants);
        mkKey('bb-vows', vowels);
        document.getElementById('bb-back').onclick = () => { if (player.graded) return; typed.pop(); refresh(); };
        return {
          isReady: () => isComplete(compose(typed)),
          grade: () => compose(typed) === ex.target,
          answerText: () => esc(ex.target) + (ex.roman ? ` (${esc(ex.roman)})` : ''),
          lock: () => { root.querySelectorAll('.tile, #bb-back').forEach(c => c.disabled = true); },
        };
      },
    },

    // ── Mini-games (self-managed, like construction_drill) ──

    speed_round: {
      render(ex, root, lang, onDone) {
        let timeLeft = ex.time_limit * 1000, idx = 0, score = 0;
        const total = ex.items.length;
        let locked = false, iv = null;

        function fmt(ms) { const s = Math.max(0, Math.ceil(ms / 1000)); return '0:' + String(s).padStart(2, '0'); }

        function renderRound() {
          if (idx >= total || timeLeft <= 0) { finish(); return; }
          const item = ex.items[idx];
          const opts = [item.roman, ...item.distractors];
          for (let i = opts.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [opts[i], opts[j]] = [opts[j], opts[i]]; }
          root.innerHTML = '<div class="minigame speed-round">' +
            '<div class="mg-header"><span class="mg-timer" id="sr-timer">⏱ ' + fmt(timeLeft) + '</span>' +
            '<span class="mg-score" id="sr-score">' + score + '/' + total + '</span></div>' +
            '<div class="mg-timer-bar"><div class="mg-timer-fill" id="sr-bar" style="width:' +
              Math.max(0, timeLeft / (ex.time_limit * 1000) * 100) + '%"></div></div>' +
            '<div class="mg-prompt">' + esc(item.symbol) + '</div>' +
            '<div class="mg-options" id="sr-opts"></div></div>';
          const wrap = document.getElementById('sr-opts');
          opts.forEach(o => {
            const b = document.createElement('button'); b.className = 'mg-opt'; b.textContent = o;
            b.onclick = () => pick(o === item.roman, b);
            wrap.appendChild(b);
          });
          if (needsRuby(lang)) applyRuby(root, null, true);
        }

        function pick(ok, btn) {
          if (locked) return; locked = true;
          btn.classList.add(ok ? 'correct' : 'wrong');
          if (ok) {
            score++; timeLeft += 1500;
            player.combo++; player.maxCombo = Math.max(player.maxCombo, player.combo);
            const g = comboXp(player.combo); player.xp += g; bumpCombo(g); sfx.correct();
          } else {
            timeLeft = Math.max(0, timeLeft - 1000); player.combo = 0; updateComboChip(); sfx.wrong();
            root.querySelectorAll('.mg-opt').forEach(b => { if (b.textContent === ex.items[idx].roman) b.classList.add('correct'); });
          }
          setTimeout(() => { idx++; locked = false; renderRound(); }, ok ? 350 : 700);
        }

        function tick() {
          timeLeft -= 100;
          const el = document.getElementById('sr-timer'), bar = document.getElementById('sr-bar');
          if (el) { el.textContent = '⏱ ' + fmt(timeLeft); if (timeLeft <= 5000) el.classList.add('urgent'); }
          if (bar) { bar.style.width = Math.max(0, timeLeft / (ex.time_limit * 1000) * 100) + '%'; if (timeLeft <= 5000) bar.classList.add('urgent'); }
          if (timeLeft <= 0) finish();
        }

        function finish() {
          if (iv) { clearInterval(iv); iv = null; }
          if (player) player._mgCleanup = null;
          const pct = total ? Math.round(score / total * 100) : 0;
          root.innerHTML = '<div class="mg-results">' +
            '<div style="font-size:2.5rem">' + (pct >= 80 ? '🏆' : pct >= 50 ? '🎉' : '💪') + '</div>' +
            '<div class="mg-results-score">' + score + ' / ' + total + '</div>' +
            '<div class="mg-results-detail">Speed round complete!</div>' +
            '<button class="mg-continue" id="sr-done">Continue</button></div>';
          if (pct >= 60) sfx.complete();
          if (pct >= 100) confetti();
          document.getElementById('sr-done').onclick = onDone;
        }

        renderRound();
        iv = setInterval(tick, 100);
        player._mgCleanup = () => { if (iv) { clearInterval(iv); iv = null; } };
        return { isReady: () => false, grade: () => true, answerText: () => '', lock: () => {} };
      },
    },

    audio_blitz: {
      render(ex, root, lang, onDone) {
        let idx = 0, score = 0;
        const total = ex.items.length;
        let locked = false, iv = null;

        function renderRound() {
          if (idx >= total) { finish(); return; }
          const item = ex.items[idx];
          root.innerHTML = '<div class="minigame audio-blitz">' +
            '<div class="mg-header"><span>Round ' + (idx + 1) + '/' + total + '</span>' +
            '<span class="mg-score" id="ab-score">' + score + '</span></div>' +
            '<div class="mg-timer-bar"><div class="mg-timer-fill" id="ab-bar" style="width:100%"></div></div>' +
            '<button class="mg-play-btn" id="ab-play">🔊</button>' +
            '<div class="mg-grid" id="ab-grid"></div></div>';
          const grid = document.getElementById('ab-grid');
          const cols = item.options.length <= 4 ? 2 : 3;
          grid.style.gridTemplateColumns = 'repeat(' + cols + ', 1fr)';
          item.options.forEach(sym => {
            const b = document.createElement('button'); b.className = 'mg-cell'; b.textContent = sym;
            b.onclick = () => pick(sym === item.correct, b, item.correct);
            grid.appendChild(b);
          });
          if (needsRuby(lang)) applyRuby(root, null, true);
          bindAudioBtn(document.getElementById('ab-play'), item.audio, lang);
          playTTS(item.audio, lang);
          let elapsed = 0; const ms = ex.round_time * 1000;
          if (iv) clearInterval(iv);
          iv = setInterval(() => {
            elapsed += 50;
            const bar = document.getElementById('ab-bar');
            if (bar) { const pct = Math.max(0, (1 - elapsed / ms) * 100); bar.style.width = pct + '%'; if (pct < 25) bar.classList.add('urgent'); }
            if (elapsed >= ms) { clearInterval(iv); iv = null; timeout(ex.items[idx].correct); }
          }, 50);
        }

        function pick(ok, btn, correct) {
          if (locked) return; locked = true;
          if (iv) { clearInterval(iv); iv = null; }
          btn.classList.add(ok ? 'correct' : 'wrong');
          if (ok) {
            score++;
            player.combo++; player.maxCombo = Math.max(player.maxCombo, player.combo);
            const g = comboXp(player.combo); player.xp += g; bumpCombo(g); sfx.correct();
          } else {
            player.combo = 0; updateComboChip(); sfx.wrong();
            root.querySelectorAll('.mg-cell').forEach(b => { if (b.textContent === correct) b.classList.add('correct'); });
          }
          setTimeout(() => { idx++; locked = false; renderRound(); }, ok ? 400 : 900);
        }

        function timeout(correct) {
          if (locked) return; locked = true;
          player.combo = 0; updateComboChip(); sfx.wrong();
          root.querySelectorAll('.mg-cell').forEach(b => { if (b.textContent === correct) b.classList.add('correct'); });
          setTimeout(() => { idx++; locked = false; renderRound(); }, 900);
        }

        function finish() {
          if (iv) { clearInterval(iv); iv = null; }
          if (player) player._mgCleanup = null;
          const pct = total ? Math.round(score / total * 100) : 0;
          root.innerHTML = '<div class="mg-results">' +
            '<div style="font-size:2.5rem">' + (pct >= 80 ? '🏆' : pct >= 50 ? '🎉' : '💪') + '</div>' +
            '<div class="mg-results-score">' + score + ' / ' + total + '</div>' +
            '<div class="mg-results-detail">Audio blitz complete!</div>' +
            '<button class="mg-continue" id="ab-done">Continue</button></div>';
          if (pct >= 60) sfx.complete();
          if (pct >= 100) confetti();
          document.getElementById('ab-done').onclick = onDone;
        }

        renderRound();
        player._mgCleanup = () => { if (iv) { clearInterval(iv); iv = null; } };
        return { isReady: () => false, grade: () => true, answerText: () => '', lock: () => {} };
      },
    },

    memory_match: {
      render(ex, root, lang, onDone) {
        const totalPairs = ex.pairs.length;
        const audioMode = !!ex.audio_mode;
        let matched = 0, flips = 0, first = null, locked = false;
        const startTime = performance.now();
        let iv = null;

        const cards = [];
        ex.pairs.forEach((p, i) => {
          cards.push({ type: 'symbol', text: p.symbol, pairId: i, audio: p.audio, label: p.label || p.roman, matched: false });
          if (audioMode) {
            cards.push({ type: 'audio', text: '🔊', pairId: i, audio: p.audio, label: p.label || p.roman, matched: false });
          } else {
            cards.push({ type: 'label', text: p.label || p.roman, pairId: i, audio: p.audio, label: p.label || p.roman, matched: false });
          }
        });
        for (let i = cards.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [cards[i], cards[j]] = [cards[j], cards[i]]; }

        const cols = cards.length <= 6 ? 3 : 4;

        function render() {
          const elapsed = Math.floor((performance.now() - startTime) / 1000);
          root.innerHTML = '<div class="minigame memory-match">' +
            '<div class="mg-header"><span>' + matched + '/' + totalPairs + ' matched</span>' +
            '<span class="mg-timer" id="mm-timer">⏱ ' + Math.floor(elapsed / 60) + ':' + String(elapsed % 60).padStart(2, '0') + '</span></div>' +
            '<div class="mm-grid" id="mm-grid" style="grid-template-columns:repeat(' + cols + ',1fr)"></div></div>';
          const grid = document.getElementById('mm-grid');
          cards.forEach((c, ci) => {
            const b = document.createElement('button');
            b.className = 'mm-card' + (c.matched ? ' matched flipped' : '');
            if (c.matched) {
              b.textContent = (c.type === 'audio') ? c.label : c.text;
            } else {
              b.textContent = '?';
            }
            b.onclick = () => flip(ci, b);
            grid.appendChild(b);
          });
        }

        function flip(ci, btn) {
          if (locked || cards[ci].matched) return;
          if (first !== null && first === ci) return;
          flips++; sfx.tap();
          const c = cards[ci];
          btn.classList.add('flipped');
          if (c.type === 'audio') {
            btn.textContent = '🔊';
            playTTS(c.audio, lang);
          } else {
            btn.textContent = c.text;
          }

          if (first === null) { first = ci; return; }
          locked = true;
          const fi = first; first = null;
          const allBtns = root.querySelectorAll('.mm-card');

          if (cards[fi].pairId === cards[ci].pairId) {
            cards[fi].matched = true; cards[ci].matched = true; matched++;
            allBtns[fi].classList.add('matched'); allBtns[ci].classList.add('matched');
            // Reveal romanization on matched audio cards
            if (cards[fi].type === 'audio') allBtns[fi].textContent = cards[fi].label;
            if (cards[ci].type === 'audio') allBtns[ci].textContent = cards[ci].label;
            player.combo++; player.maxCombo = Math.max(player.maxCombo, player.combo);
            const g = comboXp(player.combo); player.xp += g; bumpCombo(g);
            sfx.correct(); playTTS(cards[ci].audio, lang);
            root.querySelector('.mg-header span').textContent = matched + '/' + totalPairs + ' matched';
            locked = false;
            if (matched >= totalPairs) finish();
          } else {
            player.combo = 0; updateComboChip();
            allBtns[fi].classList.add('wrong-flash'); allBtns[ci].classList.add('wrong-flash');
            setTimeout(() => {
              allBtns[fi].classList.remove('flipped', 'wrong-flash'); allBtns[fi].textContent = '?';
              allBtns[ci].classList.remove('flipped', 'wrong-flash'); allBtns[ci].textContent = '?';
              locked = false;
            }, 800);
          }
        }

        function updateTimer() {
          const el = document.getElementById('mm-timer');
          if (!el) return;
          const s = Math.floor((performance.now() - startTime) / 1000);
          el.textContent = '⏱ ' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
        }

        function finish() {
          if (iv) { clearInterval(iv); iv = null; }
          if (player) player._mgCleanup = null;
          const elapsed = Math.floor((performance.now() - startTime) / 1000);
          const perfect = flips === totalPairs * 2;
          root.innerHTML = '<div class="mg-results">' +
            '<div style="font-size:2.5rem">' + (perfect ? '🏆' : elapsed < 30 ? '🎉' : '👍') + '</div>' +
            '<div class="mg-results-score">' + elapsed + 's · ' + flips + ' flips</div>' +
            '<div class="mg-results-detail">' + (perfect ? 'Perfect memory!' : 'All pairs matched!') + '</div>' +
            '<button class="mg-continue" id="mm-done">Continue</button></div>';
          sfx.complete(); if (perfect) confetti();
          document.getElementById('mm-done').onclick = onDone;
        }

        render();
        iv = setInterval(updateTimer, 1000);
        player._mgCleanup = () => { if (iv) { clearInterval(iv); iv = null; } };
        return { isReady: () => false, grade: () => true, answerText: () => '', lock: () => {} };
      },
    },
  };

  // How many tap-through teach cards a step shows. Mirrors renderTeach's paging
  // rule, because the bar has to agree with what the learner actually walks
  // through: a 5-block step is five screens, not one.
  function _teachCards(sg) {
    const teach = sg && sg.teach;
    if (!teach) return 0;
    const blocks = teach.blocks || [];
    const items = teach.items || [];
    if (!blocks.length && !items.length && !teach.intro) return 0;
    const paged = player && player.theme !== 'foundations' && !items.length && blocks.length > 1;
    return paged ? blocks.length : 1;
  }

  // Everything the learner must get through in a step: teach cards + the drills
  // this run will actually play.
  function _segWeight(sg, i) {
    return _teachCards(sg) + (((player.segTotals || [])[i]) || 0);
  }

  // Segmented step bar: one pill per lesson step.
  //
  // Pills are WEIGHTED by how much work the step holds, and teach cards count as
  // work. Equal-width pills advanced by drills alone made the bar lie twice over:
  // a step with 8 drills crawled while a step with 1 jumped, and paging through
  // five teach cards moved nothing at all. A step left with no content (its
  // drills were all trimmed for the chosen lesson length) is dropped entirely
  // rather than shown as a pill that can never fill.
  function updateBar() {
    if (!player) return;
    const segs = player.segments || [];
    const onTeach = _currentState === 'teach';
    // Visible steps, keeping their real index so segIdx comparisons still work.
    const shown = segs.map((sg, i) => ({ sg, i, w: _segWeight(sg, i) })).filter(s => s.w > 0);

    // ONE continuous track for the whole lesson, with a thin tick where each step
    // ends. Separate per-step pills couldn't be both even and honest: sized
    // equally they advance at wildly different rates, and sized by content they
    // render a one-drill AI Speak step as an unreadable sliver next to an
    // eight-drill step. A single bar measures the thing the learner actually
    // wants to know — how much of THE LESSON is left — and only ever moves
    // forward, at a rate proportional to the work each answer represents.
    const total = shown.reduce((sum, s) => sum + s.w, 0);
    let done = 0;
    for (const { sg, i, w } of shown) {
      if (player.reviewStarted || i < player.segIdx) { done += w; continue; }
      if (i === player.segIdx) {
        // Teach cards first, then drills. While on the teach screen the learner
        // is partway through the cards; once drilling, all of them are behind.
        const cards = _teachCards(sg);
        const teachDone = onTeach ? Math.min(player.teachIdx || 0, cards) : cards;
        done += Math.min(w, teachDone + player.segAnswered);
      }
    }
    const pct = total ? Math.min(100, Math.round(done / total * 100)) : 0;

    let ticks = '';
    let acc = 0;
    for (let k = 0; k < shown.length - 1; k++) {          // no tick at the very end
      acc += shown[k].w;
      ticks += `<i class="step-tick" style="left:${(acc / total * 100).toFixed(2)}%"></i>`;
    }
    const speak = (segs[player.segIdx] || {}).speak && !player.reviewStarted;
    const html = `<div class="step-track${speak ? ' speak' : ''}">`
      + `<div class="step-fill" style="width:${pct}%"></div>${ticks}</div>`;
    ['player-stepbar', 'teach-stepbar'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = html;
    });

    const seg = segs[player.segIdx] || {};
    let label = '';
    if (shown.length > 1 && !player.reviewStarted) {
      // Number by VISIBLE steps — "Step 3 of 4" has to match the pills on screen.
      const pos = shown.findIndex(s => s.i === player.segIdx);
      const t = seg.title || (seg.speak ? 'AI Speak' : '');
      if (pos >= 0) {
        label = `Step ${pos + 1} of ${shown.length}` + (t ? ` · ${seg.speak ? '✨ ' : ''}${t}` : '');
      }
    }
    ['step-tag', 'teach-step-tag'].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = label;
      el.style.display = label ? '' : 'none';
      el.classList.toggle('speak', !!seg.speak);
    });
  }

  // ── Gamification: combo meter + XP ──
  const XP_BASE = 10;
  // base + escalating combo bonus (capped): combo 1→+2, 2→+4 … 5+→+10.
  function comboXp(combo) { return XP_BASE + Math.min(combo, 5) * 2; }

  function updateComboChip() {
    const chip = document.getElementById('combo-chip');
    if (!chip) return;
    if (!player || player.combo < 2) { chip.classList.remove('on'); return; }
    document.getElementById('combo-n').textContent = '×' + player.combo;
    chip.classList.add('on');
  }

  function bumpCombo(gained) {
    updateComboChip();
    const chip = document.getElementById('combo-chip');
    if (chip && player.combo >= 2) { chip.classList.remove('bump'); void chip.offsetWidth; chip.classList.add('bump'); }
    // Quick checks bump XP from the teach screen — float over whichever top bar is visible.
    const top = document.querySelector(
      (_currentState === 'teach' ? '#state-teach' : '#state-player') + ' .player-top');
    if (top) {
      const f = document.createElement('div');
      f.className = 'xp-float'; f.textContent = '+' + gained;
      top.appendChild(f);
      void f.offsetWidth; f.classList.add('go');
      setTimeout(() => f.remove(), 1050);
    }
  }
  function updateAction() {
    if (!player || player.graded) return;
    document.getElementById('player-action').disabled = !(player.controller && player.controller.isReady());
  }

  function showMemoryMatchSettings(courseId) {
    // Fetch pool size to set max grid, then show settings overlay
    fetch('/api/courses/' + courseId + '/foundations-practice?game=memory_match&count=3')
      .then(r => r.json()).then(data => {
        const maxPairs = Math.min(data.pool_size || 12, 12);
        const ov = document.createElement('div');
        ov.className = 'mm-settings-overlay';
        const sizeOpts = [];
        for (let n = 3; n <= maxPairs; n++) {
          const total = n * 2;
          sizeOpts.push(`<option value="${n}"${n === 6 ? ' selected' : ''}>${total} cards (${n} pairs)</option>`);
        }
        ov.innerHTML = `<div class="mm-settings-card">
          <div class="mm-settings-title">Memory Match</div>
          <label class="mm-settings-label">Grid size
            <select class="settings-select" id="mm-size">${sizeOpts.join('')}</select></label>
          <label class="mm-settings-label mm-settings-toggle">
            <input type="checkbox" id="mm-audio-mode">
            <span>Audio-only mode</span></label>
          <div class="mm-settings-hint">Match characters to sounds instead of text</div>
          <div class="mm-settings-actions">
            <button class="cta-btn secondary mm-settings-cancel">Cancel</button>
            <button class="cta-btn mm-settings-start">Play</button></div></div>`;
        document.body.appendChild(ov);
        ov.querySelector('.mm-settings-cancel').onclick = () => ov.remove();
        ov.onclick = e => { if (e.target === ov) ov.remove(); };
        ov.querySelector('.mm-settings-start').onclick = () => {
          const count = parseInt(ov.querySelector('#mm-size').value, 10) || 6;
          const audio = ov.querySelector('#mm-audio-mode').checked;
          ov.remove();
          openPracticeGame(courseId, 'memory_match', { count, audio_mode: audio });
        };
      }).catch(() => openPracticeGame(courseId, 'memory_match'));
  }

  async function openPracticeGame(courseId, gameType, opts) {
    show('lesson-loading');
    try {
      let url = '/api/courses/' + courseId + '/foundations-practice?game=' + gameType;
      if (opts) {
        if (opts.count) url += '&count=' + opts.count;
        if (opts.audio_mode) url += '&audio_mode=true';
      }
      const res = await fetch(url);
      if (!res.ok) throw new Error();
      const lesson = await res.json();
      const content = lesson.content || {};
      let segments = content.segments;
      if (!segments) segments = [{ teach: null, exercises: content.exercises || [] }];
      const segTotals = segments.map(sg => (sg.exercises || []).length);
      const total = segTotals.reduce((a, b) => a + b, 0);
      if (!total) throw new Error('empty');
      _prefetchLesson(content, lesson.target_lang);
      player = {
        lessonId: 0, lang: lesson.target_lang, title: lesson.title || 'Practice',
        segments, segTotals, segIdx: 0, segAnswered: 0, queue: [], idx: 0, total, answered: 0,
        firstPassCorrect: 0, mistakes: [], reviewStarted: false,
        combo: 0, maxCombo: 0, xp: 0, listeningHits: 0,
        controller: null, graded: false,
        conceptResults: {}, vocabGlossary: {},
        concepts: [], theme: 'foundations', skipTeach: false, drillsOnly: false,
        practiceGame: gameType, practiceCourseId: courseId, practiceOpts: opts || null,
      };
      const sc = scriptClassFor(player.lang);
      document.getElementById('state-player').className = sc;
      document.getElementById('state-teach').className = sc;
      document.getElementById('state-results').className = 'learn-center ' + sc;
      startSegment(0);
    } catch {
      alert('Could not load practice game — please try again.');
      show('course');
    }
  }

  // ── B4 · Lightning round (AI-lesson material) ───────────────────────────────
  // A 60-second timed remix built from recognition/production choices plus
  // clozes whose uniqueness we can PROVE. Conjugation clozes are assembled from
  // our deterministic grammar engine and carry `lightning_safe`; free semantic
  // blanks do not. Thus "Nous ___ français" with conjugated forms is eligible,
  // while "The store is ___ the bank" (several relations fit) never is.
  function _lightningItems(segments) {
    const drills = [];
    (segments || []).forEach(sg => (sg.exercises || []).forEach(ex => {
      const hasBlank = !!ex.is_cloze || /_{2,}/.test(ex.prompt || '');
      const safeBlank = !!ex.is_cloze && ex.lightning_safe === true;
      if (ex.type === 'choice' && ex.prompt && Array.isArray(ex.options)
          && ex.answer != null && ex.options[ex.answer]
          && (!hasBlank || safeBlank)) drills.push(ex);
    }));
    return shuffle(drills).slice(0, 12).map(ex => {
      const correct = ex.options[ex.answer];
      const distractors = ex.options.filter((_, i) => i !== ex.answer).slice(0, 2);
      return { symbol: ex.prompt, roman: correct, distractors };
    }).filter(it => it.distractors.length >= 1);
  }

  function _lightningCount(segments) { return _lightningItems(segments).length; }

  // Launch a lightning round from a set of already-fetched segments.
  function _startLightning(segments, lang, title) {
    const items = _lightningItems(segments);
    if (items.length < 4) { alert('Not enough material for a lightning round yet.'); show('course'); return false; }
    const speed = { type: 'speed_round', time_limit: 60, items, hide_roman: true };
    const content = { segments: [{ teach: null, exercises: [speed] }] };
    _prefetchLesson(content, lang);
    player = {
      lessonId: 0, lang, title: title || '⚡ Lightning',
      segments: content.segments, segTotals: [1], segIdx: 0, segAnswered: 0,
      queue: [], idx: 0, total: 1, answered: 0,
      firstPassCorrect: 0, mistakes: [], reviewStarted: false,
      combo: 0, maxCombo: 0, xp: 0, listeningHits: 0,
      controller: null, graded: false,
      conceptResults: {}, vocabGlossary: {},
      concepts: [], theme: 'lightning', skipTeach: true, drillsOnly: false,
      practiceGame: 'lightning', lightning: true,
      lightningSource: segments, lightningTitle: title,
    };
    const sc = scriptClassFor(lang);
    document.getElementById('state-player').className = sc;
    document.getElementById('state-teach').className = sc;
    document.getElementById('state-results').className = 'learn-center ' + sc;
    startSegment(0);
    return true;
  }

  async function openLightning(lessonId) {
    show('lesson-loading');
    try {
      const res = await fetch('/api/lessons/' + lessonId);
      if (!res.ok) throw new Error();
      const lesson = await res.json();
      const content = lesson.content || {};
      _startLightning(content.segments || [], lesson.target_lang,
                      '⚡ Lightning · ' + (lesson.title || 'Lesson'));
    } catch {
      alert('Could not start the lightning round — please try again.');
      show('course');
    }
  }

  // ── B6 · Practice hub ───────────────────────────────────────────────────────
  // A single sheet that recombines a course's OWN stored content: a course-wide
  // lightning round, a weak-concept mistakes review, and (if present) the
  // foundations mini-games. No LLM, no new content.
  function openPracticeHub() {
    const c = currentCourse;
    if (!c) return;
    // Foundations mini-games draw on the whole reading track, so they're available
    // as soon as the track exists (no need to have finished a reading lesson).
    const foundations = (c.units || []).some(u => u.theme === 'foundations'
      && (u.lessons || []).length);
    const sheet = document.getElementById('practice-sheet');
    const card = (icon, name, sub, onclick) => `
      <button class="practice-card" onclick="${onclick}">
        <span class="pc-ico">${icon}</span>
        <span class="pc-text"><span class="pc-name">${name}</span><span class="pc-sub">${sub}</span></span>
        <span class="pc-arrow">›</span>
      </button>`;
    let games = '';
    if (foundations) {
      games = `<div class="practice-group-label">Reading games</div>
        ${card('⚡', 'Speed Round', 'Rapid character → sound', `_practiceGame('speed_round')`)}
        ${card('🔊', 'Audio Blitz', 'Hear it, tap the character', `_practiceGame('audio_blitz')`)}
        ${card('🃏', 'Memory Match', 'Flip-card concentration', `_practiceMemory()`)}`;
    }
    sheet.className = 'intro-sheet ' + scriptClassFor(c.target_lang);
    sheet.innerHTML = `
      <div class="intro-grab"></div>
      <div class="intro-kicker">Practice</div>
      <h2>🎯 Practice hub</h2>
      <p class="intro-obj">Sharpen what you've learned — built from your own lessons.</p>
      <div class="practice-cards">
        ${card('⚡', 'Lightning round', '60-second timed remix of your drills', `_practiceCourse('lightning')`)}
        ${card('🔁', 'Review mistakes', 'Re-drill the concepts you find hardest', `_practiceCourse('mistakes')`)}
        ${speechSupported()
          ? card('🎤', 'Speaking practice', 'Say your words and sentences out loud', `_practiceCourse('speaking')`)
          : ''}
        ${games}
      </div>`;
    document.getElementById('practice-overlay').classList.add('open');
  }
  function closePracticeHub() {
    document.getElementById('practice-overlay').classList.remove('open');
  }
  function _practiceCourse(mode) {
    const id = currentCourse && currentCourse.id;
    closePracticeHub();
    if (id) openCoursePractice(id, mode);
  }
  function _practiceGame(game) {
    const id = currentCourse && currentCourse.id;
    closePracticeHub();
    if (id) openPracticeGame(id, game);
  }
  function _practiceMemory() {
    const id = currentCourse && currentCourse.id;
    closePracticeHub();
    if (id) showMemoryMatchSettings(id);
  }

  // Fetch a course practice set (mistakes / lightning) and play it. Lightning is
  // remixed client-side into a speed round; mistakes plays as a drills-only run.
  async function openCoursePractice(courseId, mode, lessonId) {
    show('lesson-loading');
    try {
      const res = await fetch('/api/courses/' + courseId + '/practice?mode=' + mode
                              + (lessonId ? '&lesson_id=' + lessonId : ''));
      if (!res.ok) {
        const m = (await res.json().catch(() => ({}))).detail || 'Could not start practice.';
        throw new Error(m);
      }
      const data = await res.json();
      const content = data.content || {};
      const segments = content.segments || [];
      if (mode === 'lightning') {
        _startLightning(segments, data.target_lang, data.title || '⚡ Lightning');
        return;
      }
      const segTotals = segments.map(sg => (sg.exercises || []).length);
      const total = segTotals.reduce((a, b) => a + b, 0);
      if (!total) throw new Error('empty');
      _prefetchLesson(content, data.target_lang);
      player = {
        lessonId: 0, lang: data.target_lang, title: data.title || 'Practice',
        segments, segTotals, segIdx: 0, segAnswered: 0, queue: [], idx: 0, total, answered: 0,
        firstPassCorrect: 0, mistakes: [], reviewStarted: false,
        combo: 0, maxCombo: 0, xp: 0, listeningHits: 0,
        controller: null, graded: false,
        conceptResults: {}, vocabGlossary: {},
        concepts: [], theme: 'practice', skipTeach: true, drillsOnly: false,
        practiceGame: mode, practiceCourseId: courseId, practiceLessonId: lessonId || 0,
      };
      const sc = scriptClassFor(player.lang);
      document.getElementById('state-player').className = sc;
      document.getElementById('state-teach').className = sc;
      document.getElementById('state-results').className = 'learn-center ' + sc;
      startSegment(0);
    } catch (e) {
      alert(e.message || 'Could not start practice — please try again.');
      show('course');
    }
  }

  // ── D3 · lesson intro sheet ─────────────────────────────────────────────────
  let _introLesson = null;      // the fetched lesson behind the open sheet
  const GRADEABLE_TYPES = new Set(['choice', 'listening', 'word_bank', 'match', 'type_answer']);

  function _introSegments(content) {
    const segments = content.segments
      || [{ teach: content.teach || null, exercises: content.exercises || [] }];
    return segments;
  }

  // ── AI Speak (construction-drill) helpers ──────────────────────────────────
  // AI practice lives in two shapes: new lessons carry a dedicated `speak:true`
  // segment; older lessons bury a `construction_drill` exercise inside a regular
  // step. Both count as "AI practice" for the toggle + the intro breakdown.
  function _isAiSpeakEx(e) { return e && e.type === 'construction_drill'; }
  function _segHasAiSpeak(sg) {
    return !!(sg && (sg.speak || (sg.exercises || []).some(_isAiSpeakEx)));
  }
  function _hasAiSpeak(segments) { return (segments || []).some(_segHasAiSpeak); }

  // Remove all AI-Speak content: drop dedicated speak segments, filter embedded
  // construction drills out of the rest, and discard any segment left with no
  // teach and no exercises.
  function _stripAiSpeak(segments) {
    return (segments || []).filter(sg => !sg.speak).map(sg => ({
      ...sg, exercises: (sg.exercises || []).filter(e => !_isAiSpeakEx(e)),
    })).filter(sg => (sg.exercises || []).length
      || (sg.teach && ((sg.teach.items || []).length || sg.teach.intro || (sg.teach.blocks || []).length)));
  }

  function _isWarmupSegment(sg, index = 0) {
    return index === 0 && /warm[ -]?up|quick review|review first/i.test((sg && sg.title) || '');
  }
  function _hasWarmup(segments) {
    return (segments || []).length > 1 && _isWarmupSegment(segments[0], 0);
  }
  function _stripWarmup(segments) {
    return _hasWarmup(segments) ? segments.slice(1) : segments;
  }

  // Warm the opener (question + plan) for every construction drill in the lesson,
  // in parallel, so by the time the learner reaches the AI Speak step the phrases
  // are already generated. Cached by construction label; the drill widget reuses
  // the SAME plan (a fresh opener would generate different phrases). Best-effort.
  function _preloadDrills(segments, lang) {
    if (!_aiSpeak) return;
    for (const sg of (segments || [])) {
      for (const ex of (sg.exercises || [])) {
        if (!_isAiSpeakEx(ex)) continue;
        const construction = (ex.construction || ex.skill || '').trim();
        if (!construction || _cdPreload[construction]) continue;
        _cdPreload[construction] = fetch('/api/lesson/drill', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ construction, plan_items: [], answer: null, turn: 1, lang }),
        }).then(r => r.ok ? r.json() : null).catch(() => null);
      }
    }
  }

  function _estMinutes(segments) {
    let m = 0;
    segments.forEach(sg => {
      m += ((sg.teach && (sg.teach.blocks || []).length) || 0) * 0.4;
      (sg.exercises || []).forEach(e => { m += SELF_MANAGED.has(e.type) ? 2 : 0.5; });
    });
    return Math.max(1, Math.round(m));
  }

  function _gradeableExercises(segments) {
    const out = [];
    segments.forEach(sg => (sg.exercises || []).forEach(e => {
      if (GRADEABLE_TYPES.has(e.type)) out.push(e);
    }));
    return out;
  }

  // A3 · length is applied at PLAY time as a subset of the (always max-depth)
  // authored lesson. Every drill is tagged with a tier by the author — quick plays
  // 'core', standard plays 'core'+'standard', thorough plays everything. So
  // "thorough" is honest (all the content was authored) and switching length
  // reshapes ANY stored lesson, not just a freshly-generated one. Self-managed
  // drills (AI Speak) are always kept, and a step that had drills never ends up
  // with zero. Lessons authored before tiers existed (no `tier` on any exercise)
  // fall back to a proportional trim so they still shorten sensibly.
  const _TIER_ALLOW = {
    quick: new Set(['core']),
    standard: new Set(['core', 'standard']),
    thorough: new Set(['core', 'standard', 'extra']),
  };
  const _LEN_FACTOR = { quick: 0.5, standard: 0.8, thorough: 1.0 };  // legacy fallback

  function _hasTiers(segments) {
    return segments.some(sg => (sg.exercises || [])
      .some(e => !SELF_MANAGED.has(e.type) && e.tier));
  }

  function _trimForLength(segments, len) {
    if (len === 'thorough') return segments;
    return _hasTiers(segments)
      ? _trimByTier(segments, len)
      : _trimByFactor(segments, len);
  }

  function _trimByTier(segments, len) {
    const allow = _TIER_ALLOW[len] || _TIER_ALLOW.standard;
    return segments.map(sg => {
      const ex = sg.exercises || [];
      if (!ex.length) return sg;
      const out = ex.filter(e => SELF_MANAGED.has(e.type) || allow.has(e.tier || 'standard'));
      const hadGraded = ex.some(e => !SELF_MANAGED.has(e.type));
      const keepsGraded = out.some(e => !SELF_MANAGED.has(e.type));
      // Never leave a step that had drills with none — keep its first graded drill.
      if (hadGraded && !keepsGraded) out.push(ex.find(e => !SELF_MANAGED.has(e.type)));
      return { ...sg, exercises: out };
    });
  }

  function _trimByFactor(segments, len) {
    const f = _LEN_FACTOR[len] ?? 1;
    if (f >= 1) return segments;
    return segments.map(sg => {
      const ex = sg.exercises || [];
      if (ex.length <= 1) return sg;
      const graded = ex.filter(e => !SELF_MANAGED.has(e.type)).length;
      const target = Math.max(1, Math.round(graded * f));
      let kept = 0;
      const out = ex.filter(e => {
        if (SELF_MANAGED.has(e.type)) return true;
        return kept++ < target;
      });
      return { ...sg, exercises: out.length ? out : ex.slice(0, 1) };
    });
  }

  function _lenNoteText(len) {
    return len === 'thorough'
      ? 'Plays every drill · applies to all your lessons'
      : 'Plays a shorter subset of each lesson · applies to all your lessons';
  }

  async function openLessonIntro(lessonId) {
    try {
      const res = await fetch('/api/lessons/' + lessonId);
      if (!res.ok) throw new Error();
      _introLesson = await res.json();
      renderLessonIntro(_introLesson);
      document.getElementById('intro-overlay').classList.add('open');
    } catch {
      openLesson(lessonId);   // sheet is sugar — fall back to playing directly
    }
  }

  function closeLessonIntro() {
    document.getElementById('intro-overlay').classList.remove('open');
  }

  function _introPlay(mode) {
    const id = _introLesson && _introLesson.id;
    closeLessonIntro();
    if (id) openLesson(id, mode);
  }
  function _introResume() {
    const id = _introLesson && _introLesson.id;
    closeLessonIntro();
    if (id) resumeLesson(id);
  }
  function _introLightning() {
    const id = _introLesson && _introLesson.id;
    closeLessonIntro();
    if (id) openLightning(id);
  }
  function _introSpeaking() {
    const id = _introLesson && _introLesson.id;
    const courseId = currentCourse && currentCourse.id;
    closeLessonIntro();
    if (id && courseId) openCoursePractice(courseId, 'speaking', id);
  }

  // Flatten segments into displayed step rows. Non-speak segments surface their
  // embedded AI practice (older lessons bury a construction_drill in a step) as
  // its own "✨ AI Speak" row, so the breakdown reads the same as new lessons'.
  function _introStepRows(segments) {
    const rows = [];
    (segments || []).forEach((sg, i) => {
      const speakSeg = !!sg.speak;
      const exs = sg.exercises || [];
      const cds = speakSeg ? exs.length : exs.filter(_isAiSpeakEx).length;
      const blocks = (sg.teach && (sg.teach.blocks || []).length) || 0;
      const drills = exs.length - (speakSeg ? 0 : cds);
      if (!speakSeg && (blocks || drills)) {
        const n = rows.filter(r => !r.speak).length + 1;
        rows.push({
          speak: false,
          icon: (i === 0 && !blocks) ? '🔥' : (blocks ? '📖' : '✏️'),
          name: sg.title || `Step ${n}`,
          sub: [blocks ? `${blocks} card${blocks === 1 ? '' : 's'}` : '',
                drills ? `${drills} drill${drills === 1 ? '' : 's'}` : ''].filter(Boolean).join(' + '),
          t: Math.max(1, Math.round(blocks * 0.4 + drills * 0.5)),
        });
      }
      if (speakSeg || cds) {
        rows.push({ speak: true, icon: '✨', name: 'AI Speak',
          sub: 'Short phrases, graded by your tutor',
          t: Math.max(1, Math.round((speakSeg ? exs.length : cds) * 2)) });
      }
    });
    return rows.map((r, i) => `${i ? '<div class="intro-rail"></div>' : ''}
      <div class="intro-step${r.speak ? ' speak' : ''}">
        <span class="sico">${r.icon}</span>
        <div><div class="sname">${esc(r.name)}</div>${r.sub ? `<div class="ssub">${esc(r.sub)}</div>` : ''}</div>
        <span class="stime">${r.t} min</span>
      </div>`).join('');
  }

  function renderLessonIntro(lesson) {
    const sheet = document.getElementById('intro-sheet');
    const content = lesson.content || {};
    const fullSegments = _introSegments(content);
    const isFoundations = lesson.theme === 'foundations';
    const hasAi = !isFoundations && _hasAiSpeak(fullSegments);
    // The step list + estimate reflect what will actually play: the chosen length
    // (Quick/Standard/Thorough) and the AI-Speak toggle both reshape it live.
    let segments = isFoundations ? fullSegments : _trimForLength(fullSegments, _lessonLength);
    const hasWarmup = !isFoundations && _hasWarmup(segments);
    if (!_warmup) segments = _stripWarmup(segments);
    if (!_aiSpeak) segments = _stripAiSpeak(segments);
    const mins = _estMinutes(segments);
    const hasSpeak = _aiSpeak && _hasAiSpeak(segments);
    const canTestOut = !lesson.completed && _gradeableExercises(fullSegments).length >= 4;
    // B4 · lightning round is offered on lessons the learner has finished.
    const canLightning = lesson.completed && _lightningCount(fullSegments) >= 4;
    // 🎤 Speaking round over this lesson's own sentences — same rule as lightning
    // (material the learner has already worked through), plus a working mic.
    const canSpeak = lesson.completed && !isFoundations && speechSupported()
      && !!speechLangFor(lesson.target_lang);
    const resume = _loadResume(lesson.id);

    const stepRows = _introStepRows(segments);

    const concepts = (lesson.concepts || []).filter(c => (c.label || '').trim());
    const chips = concepts.slice(0, 4).map(c =>
      `<span class="chip">${esc(c.label)}${c.gloss ? ` <small>${esc(c.gloss)}</small>` : ''}</span>`).join('')
      + (concepts.length > 4 ? `<span class="chip">+${concepts.length - 4} more</span>` : '');

    const lenChip = (v, label, sub) =>
      `<button type="button" class="intro-len${_lessonLength === v ? ' on' : ''}" data-len="${v}"
        onclick="setLessonLength('${v}')"><b>${label}</b><small>${sub}</small></button>`;

    const aiToggle = hasAi ? `
      <div class="intro-toggle">
        <div class="it-text">
          <div class="it-title">✨ AI Speak practice</div>
          <div class="it-sub">Tutor-graded translation drills. Off is faster and uses fewer AI calls.</div>
        </div>
        <button type="button" class="intro-switch${_aiSpeak ? ' on' : ''}" onclick="toggleAiSpeak()" aria-label="Toggle AI Speak practice"></button>
      </div>` : '';
    const warmupToggle = hasWarmup ? `
      <div class="intro-toggle">
        <div class="it-text">
          <div class="it-title">🔥 Warm-up</div>
          <div class="it-sub">A short optional review step before the main lesson.</div>
        </div>
        <button type="button" class="intro-switch${_warmup ? ' on' : ''}" onclick="toggleWarmup()" aria-label="Toggle lesson warm-up"></button>
      </div>` : '';

    sheet.className = 'intro-sheet ' + scriptClassFor(lesson.target_lang);
    sheet.innerHTML = `
      <div class="intro-grab"></div>
      <div class="intro-kicker">${lesson.completed ? `Completed${lesson.score != null ? ` · ${lesson.score}%` : ''}` : 'Lesson'} · ~${mins} min</div>
      <h2>${esc(lesson.title || 'Lesson')}</h2>
      ${lesson.objective ? `<p class="intro-obj">${esc(lesson.objective)}</p>` : ''}
      ${resume ? `<div class="intro-resume">⏸ You stopped partway through — resume where you left off, or start over.</div>` : ''}
      <div class="intro-steps">${stepRows}</div>
      ${chips ? `<div class="intro-concepts">${chips}</div>` : ''}
      ${isFoundations ? '' : `<div class="intro-lenrow">
        ${lenChip('quick', 'Quick', '~4 min')}${lenChip('standard', 'Standard', '~8 min')}${lenChip('thorough', 'Thorough', '~12 min')}
      </div>
      <div class="intro-len-note" id="intro-len-note">${esc(_lenNoteText(_lessonLength))}</div>`}
      ${warmupToggle}
      ${aiToggle}
      <div class="intro-actions">
        ${resume
          ? `<button class="cta-btn" onclick="_introResume()">▶ Resume lesson</button>`
          : `<button class="cta-btn" onclick="_introPlay('')">${lesson.completed ? '↻ Play again' : 'Start lesson'}</button>`}
        <div class="row2">
          ${resume ? `<button class="cta-btn secondary" onclick="_introPlay('')">↻ Start over</button>` : ''}
          <button class="cta-btn secondary" onclick="_introPlay('skip')">⚡ Practice only</button>
          ${hasSpeak ? `<button class="cta-btn secondary" onclick="_introPlay('llm')">✨ AI Speak</button>` : ''}
          ${canTestOut ? `<button class="cta-btn secondary" onclick="startTestOut()">🎓 Test out</button>` : ''}
          ${canLightning ? `<button class="cta-btn secondary" onclick="_introLightning()">⚡ Lightning</button>` : ''}
          ${canSpeak ? `<button class="cta-btn secondary" onclick="_introSpeaking()">🎤 Speaking</button>` : ''}
        </div>
      </div>`;
  }

  // Toggle AI Speak practice for every lesson; re-render the sheet so the step
  // breakdown + estimate update live, and persist the choice.
  async function toggleAiSpeak() {
    _aiSpeak = !_aiSpeak;
    if (_introLesson) renderLessonIntro(_introLesson);
    try {
      await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lesson_ai_speak: _aiSpeak }),
      });
    } catch {}
  }

  async function toggleWarmup() {
    _warmup = !_warmup;
    if (_introLesson) renderLessonIntro(_introLesson);
    try {
      await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lesson_warmup: _warmup }),
      });
    } catch {}
  }

  // A3 · length chips choose how much of the (always max-depth) lesson to play,
  // as a tier subset — re-render the sheet so the step list + estimate update live,
  // and persist the choice so it applies to every lesson the learner opens.
  async function setLessonLength(v) {
    if (_lessonLength === v) return;
    _lessonLength = v;
    if (_introLesson) renderLessonIntro(_introLesson);
    try {
      await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lesson_length: v }),
      });
    } catch {}
  }

  // ── A4 · test out of a lesson ───────────────────────────────────────────────
  // A 4-question quiz drawn from the lesson's own hardest stored drills
  // (deterministic, no LLM). Pass ≥3/4 → complete (half XP, crown 1);
  // fail → drop into the full lesson.
  function _testOutRank(ex) {
    // Typed production is the hardest thing in the lesson — nothing is shown to
    // recognise — so it leads a test-out. Tile assembly next, then the choice
    // kinds by how much they give away.
    if (ex.type === 'type_answer') return ex.is_cloze ? 1 : 0;
    if (ex.type === 'word_bank') return 2;
    if (ex.type === 'choice' && ex.is_cloze) return 3;
    if (ex.type === 'choice' && ex.prompt_lang === 'english') return 4;
    if (ex.type === 'listening') return 5;
    if (ex.type === 'choice') return 6;
    return 7;   // match
  }

  function _pickTestOut(segments, n = 4) {
    const byRank = new Map();
    _gradeableExercises(segments).forEach(ex => {
      const r = _testOutRank(ex);
      if (!byRank.has(r)) byRank.set(r, []);
      byRank.get(r).push(ex);
    });
    const ranks = [...byRank.keys()].sort((a, b) => a - b);
    const picked = [];
    // Rounds over the hardness ranks, taking the LAST stored drill of each
    // type first (later drills are the hardest by the authoring contract).
    while (picked.length < n && ranks.some(r => byRank.get(r).length)) {
      for (const r of ranks) {
        const pool = byRank.get(r);
        if (pool.length && picked.length < n) picked.push(pool.pop());
      }
    }
    return picked;
  }

  function startTestOut() {
    const lesson = _introLesson;
    if (!lesson) return;
    closeLessonIntro();
    const segments = _introSegments(lesson.content || {});
    const quiz = _pickTestOut(segments).map(e => ({ ...e }));
    if (quiz.length < 4) { openLesson(lesson.id); return; }
    _prefetchLesson({ segments: [{ exercises: quiz }] }, lesson.target_lang);
    player = {
      lessonId: lesson.id, lang: lesson.target_lang, title: lesson.title || 'Lesson',
      segments: [{ title: 'Test out', teach: null, exercises: quiz }],
      segTotals: [quiz.length], segIdx: 0, segAnswered: 0,
      queue: [], idx: 0, total: quiz.length, answered: 0,
      firstPassCorrect: 0, mistakes: [], reviewStarted: false,
      combo: 0, maxCombo: 0, xp: 0, listeningHits: 0,
      controller: null, graded: false,
      conceptResults: {}, vocabGlossary: (lesson.content || {}).vocab_glossary || {},
      concepts: lesson.concepts || [], theme: lesson.theme || '',
      mode: '', skipTeach: true, drillsOnly: false, testOut: true,
    };
    const sc = scriptClassFor(player.lang);
    document.getElementById('state-player').className = sc;
    document.getElementById('state-teach').className = sc;
    document.getElementById('state-results').className = 'learn-center ' + sc;
    startSegment(0);
  }

  async function openLesson(lessonId, mode = '', resume = null) {
    // mode: '' = full lesson, 'skip' = skip teach, 'llm' = LLM drills only
    // resume: a saved snapshot to continue from (see _loadResume); else fresh.
    show('lesson-loading');
    // Starting a lesson fresh replaces any earlier saved attempt.
    if (!resume) _clearResume(lessonId);
    try {
      const res = await fetch('/api/lessons/' + lessonId);
      if (!res.ok) throw new Error();
      const lesson = await res.json();
      const content = lesson.content || {};
      // Normalise to segments (back-compat with old single-block lessons).
      let segments = content.segments;
      if (!segments) segments = [{ teach: content.teach || null, exercises: content.exercises || [] }];
      // A3 · trim to the chosen length for this run (never for the curated
      // foundations reading track, whose drills each teach a specific letter).
      if (lesson.theme !== 'foundations') segments = _trimForLength(segments, _lessonLength);
      if (!_warmup && lesson.theme !== 'foundations') segments = _stripWarmup(segments);
      // AI Speak toggle: when off, drop the construction-drill practice entirely
      // (unless doing so would leave nothing to play).
      if (!_aiSpeak && mode !== 'llm') {
        const stripped = _stripAiSpeak(segments);
        if (stripped.length) segments = stripped;
      }
      // Count only the exercises this mode will actually play — 'llm' filters to
      // self-managed drills, so an unfiltered total would deflate the score/bar.
      const segTotals = segments.map(sg => (sg.exercises || [])
        .filter(e => mode !== 'llm' || SELF_MANAGED.has(e.type)).length);
      const total = segTotals.reduce((a, b) => a + b, 0);
      if (mode === 'llm' && !total) {
        alert('This lesson has no AI practice drills — they\'re added for grammar lessons.');
        show('course'); return;
      }
      // Allow info-only lessons (teach with no exercises); only fail if truly empty.
      const hasContent = segments.some(sg => (sg.exercises || []).length ||
        (sg.teach && ((sg.teach.items || []).length || sg.teach.intro || (sg.teach.blocks || []).length)));
      if (!hasContent) throw new Error('empty lesson');
      // Fire-and-forget: kick off TTS + ruby fetches for the whole lesson so
      // everything is cached by the time the learner reaches each exercise.
      _prefetchLesson(content, lesson.target_lang);
      // Warm the AI-Speak openers in parallel while the learner works the lesson.
      _preloadDrills(segments, lesson.target_lang);

      player = {
        lessonId, lang: lesson.target_lang, title: lesson.title || 'Lesson',
        segments, segTotals, segIdx: 0, segAnswered: 0, queue: [], idx: 0, total, answered: 0,
        firstPassCorrect: 0, mistakes: [], reviewStarted: false,
        combo: 0, maxCombo: 0, xp: 0, listeningHits: 0,   // gamification: in-lesson combo + earned XP
        controller: null, graded: false,
        conceptResults: {},   // concept_key → {correct, total} for mastery ledger
        vocabGlossary: content.vocab_glossary || {},
        concepts: lesson.concepts || [],   // for the results "Add to deck" panel
        theme: lesson.theme || '',         // 'foundations' → no live word lookup
        mode,                             // remembered so "Practice again" replays the same mode
        skipTeach: mode === 'skip' || mode === 'llm',
        drillsOnly: mode === 'llm',       // LLM drills only: filter to self-managed exercises
      };
      const sc = scriptClassFor(player.lang);
      document.getElementById('state-player').className = sc;
      document.getElementById('state-teach').className = sc;
      document.getElementById('state-results').className = 'learn-center ' + sc;
      // Resume a saved attempt (see quitLesson): restore counters + jump into the
      // segment/exercise the learner stopped on. Guarded so a stale/mismatched
      // snapshot (settings changed, lesson re-authored) just starts fresh.
      if (resume && _applyResume(resume)) return;
      startSegment(0);
    } catch {
      alert('Could not load this lesson — please try again.');
      show('course');
    }
  }

  function startSegment(i) {
    player.segIdx = i;
    player.segAnswered = 0;
    // Reset before the bar reads it — a stale index from the previous step would
    // show this one already part-done.
    player.teachIdx = 0;
    updateBar();
    const seg = player.segments[i];
    const teach = seg.teach;
    const hasTeach = teach && ((teach.items || []).length || teach.intro || (teach.blocks || []).length);
    if (hasTeach && !player.skipTeach) {
      document.getElementById('teach-title').textContent = player.title;
      renderTeach(teach);
      show('teach'); updateBar();
      window.scrollTo(0, 0);
    } else {
      startExercises();
    }
  }

  // Teach "Continue/Start" button: advance the teach-card pager, then exercises.
  function onTeachAction() {
    if (player && player.teachPaged && player.teachIdx < player.teachBlocks.length - 1) {
      player.teachIdx++;
      renderTeachCard();
    } else {
      startExercises();
    }
  }

  // Label for the teach action button. `last` = the pager is on its final card
  // (always true for the single-scroll fallback).
  function setTeachAction(last) {
    const hasEx = ((player.segments[player.segIdx] || {}).exercises || []).length > 0;
    document.getElementById('teach-action').textContent =
      !last ? 'Continue →'
        : hasEx ? 'Start exercises →'
        : (player.segIdx < player.segments.length - 1 ? 'Continue →' : 'Got it →');
  }

  // Advance past a finished segment: next segment, then end-of-lesson review, then finish.
  // Quizzes (test-out, checkpoints) skip the mistake-review lap — they end when
  // the questions do; the score is already decided.
  function afterSegment() {
    if (player.segIdx < player.segments.length - 1) {
      startSegment(player.segIdx + 1);
    } else if (player.mistakes.length && !player.testOut && !player.checkpointUnitId) {
      player.reviewStarted = true;
      player.queue = player.mistakes.map(e => ({ ...e, _review: true }));
      player.mistakes = [];
      player.idx = 0;
      show('player');
      renderExercise();
    } else {
      finishLesson();
    }
  }

  // Token cache: (lang, text) → Promise<token[]>.  Each token: {text, roman, is_word}.
  // Separated from HTML rendering so the glossary (session-specific) doesn't pollute the cache.
  const _tokenCache = {};

  // True when text is mostly Latin alphabet — ruby would be meaningless/wrong.
  function _isLatin(text) {
    const stripped = (text || '').replace(/\s/g, '');
    if (!stripped) return true;
    const latin = (stripped.match(/[A-Za-zÀ-öø-ÿ]/g) || []).length;
    return latin / stripped.length > 0.5;
  }

  // Fetch per-token data from /api/ruby, cached for the page lifetime.
  function _fetchTokens(text, lang) {
    const key = lang + '\0' + text;
    if (!_tokenCache[key]) {
      // Bounded, and a failure is NOT kept: the cache holds the promise, so a
      // single flaky lookup used to leave that string un-romanizable — and
      // un-gradeable as a homophone — for the rest of the session.
      _tokenCache[key] = _timedFetch(
        `/api/ruby?text=${encodeURIComponent(text)}&lang=${encodeURIComponent(lang)}`,
        null, RUBY_TIMEOUT_MS)
        .then(r => r.json())
        .catch(() => { delete _tokenCache[key]; return null; });
    }
    return _tokenCache[key];
  }

  // Resolve the readable substitute for a listening prompt. Server-authored
  // drills normally include the offline romanization already; client-created
  // variants use the same token oracle on demand. For a Latin-script language,
  // the written phrase is itself the useful pronunciation fallback.
  async function _listeningRomanization(ex, lang) {
    const supplied = ((ex && ex.audio_roman) || '').trim();
    if (supplied) return supplied;
    const audioText = ((ex && ex.audio) || '').trim();
    if (!audioText) return '';
    if (!needsRuby(lang)) return audioText;
    const tokens = await _fetchTokens(audioText, lang);
    if (!Array.isArray(tokens)) return '';
    return tokens.filter(t => t.is_word).map(t => t.roman || t.text).join(' ').trim();
  }

  // Pre-warm TTS audio + ruby token caches for an entire lesson, fired
  // immediately after the lesson JSON loads (before the first screen renders).
  // This way audio/ruby are in the browser cache by the time the learner
  // reaches each exercise — no per-exercise loading delay.
  function _prefetchLesson(content, lang) {
    const segments = content.segments || [];
    for (const seg of segments) {
      // Teach items: pre-load audio + ruby for each target
      for (const it of ((seg.teach || {}).items || [])) {
        _prewarmTTS(it.audio || it.target, lang);
        if (it.target && needsRuby(lang) && !_isLatin(it.target)) _fetchTokens(it.target, lang);
      }
      // Teach blocks (table cells, example native text, quick-check options)
      for (const blk of ((seg.teach || {}).blocks || [])) {
        for (const row of (blk.rows || []))
          for (const cell of row)
            if (cell && needsRuby(lang) && !_isLatin(cell)) _fetchTokens(cell, lang);
        for (const ex of (blk.examples || []))
          if (ex.native) { _prewarmTTS(ex.native, lang); if (needsRuby(lang) && !_isLatin(ex.native)) _fetchTokens(ex.native, lang); }
        for (const opt of (blk.options || []))
          if (opt && needsRuby(lang) && !_isLatin(opt)) _fetchTokens(opt, lang);
      }
      // Exercises — since all exercise text now gets hover/tap tooltips (hideRoman=true
      // with vocabGlossary), pre-warm ruby for every native-language string that will
      // receive a .needs-ruby span: prompt, all option strings, match targets, tiles.
      for (const ex of (seg.exercises || [])) {
        if (ex.audio) _prewarmTTS(ex.audio, lang);
        if (ex.prompt && needsRuby(lang) && !_isLatin(ex.prompt)) _fetchTokens(ex.prompt, lang);
        // All option strings (listening choices are native; choice options may be native
        // when prompt_lang is 'english' and optsTarget is true)
        for (const opt of (ex.options || []))
          if (opt && needsRuby(lang) && !_isLatin(opt)) _fetchTokens(opt, lang);
        // Match exercise target column + memory_match pairs
        for (const pair of (ex.pairs || [])) {
          if (pair.target && needsRuby(lang) && !_isLatin(pair.target)) _fetchTokens(pair.target, lang);
          if (pair.symbol && needsRuby(lang) && !_isLatin(pair.symbol)) _fetchTokens(pair.symbol, lang);
          if (pair.audio) _prewarmTTS(pair.audio, lang);
        }
        // Word-bank / reorder tiles
        for (const tok of [...(ex.answer_tokens || []), ...(ex.distractor_tokens || [])])
          if (tok && needsRuby(lang) && !_isLatin(tok)) _fetchTokens(tok, lang);
        // Mini-game items (speed_round, audio_blitz)
        for (const item of (ex.items || [])) {
          if (item.audio) _prewarmTTS(item.audio, lang);
          if (item.symbol && needsRuby(lang) && !_isLatin(item.symbol)) _fetchTokens(item.symbol, lang);
        }
      }
    }
  }

  // Build <ruby> HTML from a token array.
  //   - normally: romanization shows inline as ruby; an optional `glossary` adds a
  //     hover/tap `.gl` tooltip (used for TEACH text, never exercise prompts).
  //   - hideRoman: romanization is NOT shown inline — it's tucked into the `.gl`
  //     tooltip instead (combined with the gloss, "rom · meaning"). Used in the
  //     reading (foundations) exercises so the romanization isn't a spoiler.
  function _tokensToHtml(tokens, glossary, hideRoman, liveLookup) {
    if (!Array.isArray(tokens) || !tokens.length) return '';
    const hasRoman = tokens.some(t => t.roman);
    return tokens.map(t => {
      const inner = (!hideRoman && hasRoman && t.is_word && t.roman)
        ? `<ruby>${esc(t.text)}<rt>${esc(t.roman)}</rt></ruby>`
        : esc(t.text);
      const parts = [];
      if (hideRoman && t.is_word && t.roman) parts.push(t.roman);
      const g = t.is_word ? (glossary || {})[t.text] : null;
      if (g) parts.push(g);
      const tip = parts.join(' · ');
      if (tip)
        return `<span class="gl" tabindex="0" data-gloss="${esc(tip)}">${inner}</span>`;
      // Teach word with no stored gloss → make it tap-to-translate (hybrid:
      // free stored glosses above, live AI fallback here). Any real word in any
      // script (Latin words just have no ruby). Never for exercises.
      if (liveLookup && t.is_word)
        return `<span class="gl gl-live" tabindex="0" data-lw="${esc(t.text)}">${inner}</span>`;
      return inner;
    }).join('');
  }

  // Return <ruby>-annotated HTML for `text`. `glossary` enables hover/tap glosses;
  // `hideRoman` moves romanization from inline ruby into that tooltip; `liveLookup`
  // marks ungloss­ed teach words for tap-to-translate.
  async function rubyHtml(text, lang, glossary, hideRoman, liveLookup) {
    if (!text) return '';
    // Latin scripts need no ruby — but with liveLookup on we still tokenise so
    // each word can be tapped for a translation (just without romanization).
    if (_isLatin(text) && !liveLookup) return esc(text);
    const tokens = await _fetchTokens(text, lang);
    if (!tokens || !tokens.length) return esc(text);
    return _tokensToHtml(tokens, glossary, hideRoman, liveLookup);
  }

  // Apply ruby to every .needs-ruby element inside `container`. Pass `glossary`
  // (the lesson's vocab_glossary) ONLY for teach content so words get a hover/tap
  // gloss; omit it for exercise prompts so answers aren't revealed. Pass
  // `hideRoman` for reading exercises so romanization is hidden behind the tooltip.
  // Pass `liveLookup` for teach text so ungloss­ed words become tap-to-translate.
  async function applyRuby(container, glossary, hideRoman, liveLookup) {
    const els = container.querySelectorAll('.needs-ruby');
    await Promise.all([...els].map(async el => {
      el.innerHTML = await rubyHtml(el.dataset.text, el.dataset.lang, glossary, hideRoman, liveLookup);
      el.classList.remove('needs-ruby');
    }));
  }

  // Text nodes inside markdown-rendered teach prose/notes mix English with
  // inline target-language examples (e.g. 詩·史·試·時·市·事). Walk the text nodes
  // and ruby-annotate only those containing target script, so each example gets
  // jyutping + a meaning tooltip (stored gloss, else live tap-to-translate) —
  // without disturbing surrounding English or the markdown (bold/italic) nodes.
  const _HAS_TARGET = /[\p{Script=Han}\p{Script=Hangul}\p{Script=Devanagari}\p{Script=Telugu}\p{Script=Thai}\p{Script=Bengali}\p{Script=Arabic}\p{Script=Cyrillic}\p{Script=Greek}\p{Script=Hebrew}\p{Script=Hiragana}\p{Script=Katakana}]/u;
  // A maximal run of target-script characters (optionally joined by middle dots,
  // 詩·史·試…). We ruby-ize only these runs and leave surrounding English alone —
  // tokenising a whole mixed "the syllable si: 詩·史" node merges the Latin and
  // the first character into one un-romanizable token.
  const _TARGET_RUN = /[\p{Script=Han}\p{Script=Hangul}\p{Script=Devanagari}\p{Script=Telugu}\p{Script=Thai}\p{Script=Bengali}\p{Script=Arabic}\p{Script=Cyrillic}\p{Script=Greek}\p{Script=Hebrew}\p{Script=Hiragana}\p{Script=Katakana}·・‧•]+/gu;
  async function applyProseRuby(container, glossary, liveLookup) {
    const lang = player && player.lang;
    if (!lang || !needsRuby(lang)) return;
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!n.nodeValue || !_HAS_TARGET.test(n.nodeValue)) return NodeFilter.FILTER_REJECT;
        if (n.parentElement && n.parentElement.closest('ruby, .gl')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    await Promise.all(nodes.map(async n => {
      const text = n.nodeValue;
      // Split into alternating English / target-run segments, keeping order.
      const segs = [];
      let cursor = 0, m;
      _TARGET_RUN.lastIndex = 0;
      while ((m = _TARGET_RUN.exec(text)) !== null) {
        if (m.index > cursor) segs.push({ run: false, v: text.slice(cursor, m.index) });
        segs.push({ run: true, v: m[0] });
        cursor = m.index + m[0].length;
      }
      if (cursor < text.length) segs.push({ run: false, v: text.slice(cursor) });
      const html = await Promise.all(segs.map(s =>
        s.run ? rubyHtml(s.v, lang, glossary, false, liveLookup) : Promise.resolve(esc(s.v))));
      const span = document.createElement('span');
      span.innerHTML = html.join('');
      n.replaceWith(span);
    }));
  }

  // Render basic markdown (bold/italic) safely: escape HTML first, then mark up.
  function renderMarkdown(text) {
    return esc(text || '')
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
  }

  // Return just the target-language portion of a (possibly mixed) string for TTS.
  // Strips parenthetical romanization (which contains tone numbers), markdown markers,
  // and for CJK/Hangul scripts, trailing ASCII text the generator sometimes appends.
  // Languages where we have offline romanizers → ruby annotations make sense.
  const RUBY_LANGS = new Set(['yue', 'cmn', 'ko', 'hi', 'te', 'ja', 'bn', 'ur', 'ar', 'ru', 'fa', 'uk', 'el', 'th', 'he']);
  function needsRuby(lang) { return RUBY_LANGS.has(lang); }

  function cleanForTTS(text, lang) {
    let t = (text || '')
      .replace(/\([^)]*\)/g, '')   // strip all parentheticals (romanization/notes)
      .replace(/\*+/g, '')          // strip markdown markers
      .trim();
    if (RUBY_LANGS.has(lang)) {
      // Trim trailing Latin/ASCII after the last non-Latin script character.
      const m = t.match(/^([\s\S]*[\u0900-\u0DFF\u0C00-\u0C7F\u3000-\u9FFF\uAC00-\uD7AF\u3400-\u4DBF\u0E01-\u0E7F\u0980-\u09FF\u0600-\u06FF\u0400-\u04FF\u0370-\u03FF\u0590-\u05FF\u3040-\u309F\u30A0-\u30FF。！？、，；：“”‘’「」【】〔〕《》·…—～]+)/);
      if (m) t = m[1].trim();
    }
    return t;
  }

  // Wrap target-language text in a .needs-ruby span (resolved by applyRuby).
  // Falls back to esc(text) for Latin-script languages.
  function targetSpan(text, lang) {
    const clean = cleanForTTS(text, lang);
    if (!clean) return '';
    if (!needsRuby(lang)) return esc(clean);
    return `<span class="needs-ruby" data-text="${esc(clean)}" data-lang="${esc(lang)}">${esc(clean)}</span>`;
  }


  // Speakable target line: ruby-annotated text + 🔊 button.
  // The .needs-ruby span is resolved asynchronously by applyRuby().
  function speakLine(text, lang) {
    const line = document.createElement('div');
    line.className = 'tb-line';
    const l = lang || (player && player.lang);
    line.innerHTML = `<button class="audio-play sm" type="button">🔊</button>
      <div class="tb-target"><span class="needs-ruby" data-text="${esc(cleanForTTS(text, l))}" data-lang="${esc(l)}">${esc(text || '')}</span></div>`;
    bindAudioBtn(line.querySelector('button'), cleanForTTS(text, l), l);
    return line;
  }

  // Render one LLM-authored teach block (prose / table / examples / contrast / note).
  function renderBlock(b) {
    if (b.type === 'prose') {
      const d = document.createElement('div'); d.className = 'tb-prose';
      d.innerHTML = renderMarkdown(b.text || '');
      // liveLookup ON even for foundations: prose is explanatory teach text (not a
      // graded exercise), so tapping an example word for its meaning is welcome.
      applyProseRuby(d, player && player.vocabGlossary, true);
      return d;
    }
    if (b.type === 'note') {
      const d = document.createElement('div'); d.className = 'tb-note';
      d.innerHTML = renderMarkdown(b.text || '');
      applyProseRuby(d, player && player.vocabGlossary, true);
      return d;
    }
    if (b.type === 'table') {
      const wrap = document.createElement('div'); wrap.className = 'teach-table-wrap';
      const l = player && player.lang;
      // Store clean TTS text as data-tts BEFORE applyRuby runs — afterwards
      // td.textContent includes the <rt> romanization content and would be read aloud.
      const cell = c => {
        // English/Latin cells (the meaning column) must NOT be tokenised by the
        // target-language tokenizer — a CJK/Thai tokenizer segments English text
        // and drops the spaces ("not is" → "notis"). Render them as plain text.
        if (!needsRuby(l) || _isLatin(c)) return `<td>${esc(c)}</td>`;
        const clean = cleanForTTS(c, l);
        return `<td data-tts="${esc(clean)}"><span class="needs-ruby" data-text="${esc(clean)}" data-lang="${esc(l)}">${esc(c)}</span></td>`;
      };
      let h = b.title ? `<div class="teach-table-title">${esc(b.title)}</div>` : '';
      h += '<table class="teach-table">';
      if ((b.columns || []).length) h += '<thead><tr>' + b.columns.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead>';
      h += '<tbody>' + (b.rows || []).map(r => '<tr>' + r.map(c => cell(c)).join('') + '</tr>').join('') + '</tbody></table>';
      wrap.innerHTML = h;
      applyRuby(wrap, player && player.vocabGlossary, false, _liveLookupOn());   // teach → hover/tap glosses
      // For logographic languages, detect English label cells (no ruby annotation)
      // and mark them no-audio. For Latin-script languages no ruby exists anywhere,
      // so all cells stay clickable.
      const logographic = wrap.querySelector('ruby') !== null;
      wrap.querySelectorAll('td').forEach(td => {
        if (logographic && !td.querySelector('ruby')) { td.classList.add('no-audio'); return; }
        const t = td.dataset.tts;
        if (t) td.onclick = () => playTTS(t, l);
      });
      return wrap;
    }
    if (b.type === 'examples') {
      const box = document.createElement('div'); box.className = 'tb-examples';
      (b.items || []).forEach(it => {
        const card = document.createElement('div'); card.className = 'tb-ex';
        card.appendChild(speakLine(it.text));
        if (it.lit) { const l = document.createElement('div'); l.className = 'tb-lit'; l.textContent = '(lit. ' + it.lit + ')'; card.appendChild(l); }
        if (it.gloss) { const g = document.createElement('div'); g.className = 'tb-gloss'; g.textContent = it.gloss; card.appendChild(g); }
        box.appendChild(card);
      });
      return box;
    }
    if (b.type === 'quick_check') {
      return renderQuickCheck(b);
    }
    if (b.type === 'contrast') {
      const card = document.createElement('div'); card.className = 'mp-card';
      [b.a, b.b].forEach(s => {
        const wrap = document.createElement('div'); wrap.className = 'mp-line';
        wrap.appendChild(speakLine(s.text));
        if (s.gloss) { const g = document.createElement('div'); g.className = 'mp-gloss'; g.textContent = s.gloss; wrap.appendChild(g); }
        card.appendChild(wrap);
      });
      if (b.label) { const c = document.createElement('div'); c.className = 'mp-contrast';
        c.innerHTML = '↕ ' + renderMarkdown(b.label); card.appendChild(c); }
      return card;
    }
    return null;
  }

  function renderTeach(teach) {
    const blocks = teach.blocks || [];
    // C1 · tap-through teach cards: one block per screen with a dot pager, so a
    // grammar lesson's blocks never render as one wall of text. Foundations and
    // legacy item-list lessons keep the single scroll page (their teach IS the
    // lesson and is laid out as a unit).
    player.teachPaged = !!player && player.theme !== 'foundations'
      && !(teach.items || []).length && blocks.length > 1;
    player.teachBlocks = blocks;
    player.teachItemCount = (teach.items || []).length;   // legacy word-list lessons
    player.teachIntro = teach.intro || '';
    player.teachIdx = 0;
    if (player.teachPaged) { renderTeachCard(); return; }
    updateReportBtn();          // unpaged: offered only for a single authored block
    document.getElementById('teach-dots').style.display = 'none';
    { const tb = document.getElementById('teach-back'); if (tb) tb.style.display = 'none'; }
    setTeachAction(true);
    document.getElementById('teach-intro').textContent = teach.intro || '';
    const wrap = document.getElementById('teach-items');
    wrap.innerHTML = '';
    const lang = player && player.lang;
    // Free-form teach blocks (prose/table/examples/contrast/note) — the unified
    // authored lesson's main teaching content.
    (teach.blocks || []).forEach(b => { const el = renderBlock(b); if (el) wrap.appendChild(el); });
    // Legacy vocab "items" word-list (back-compat with older lessons).
    (teach.items || []).forEach(it => {
      const blocks = it.blocks || [];
      const isGrammar = it.grammar && blocks.length;
      const row = document.createElement('div');
      row.className = 'teach-item' + (isGrammar ? ' teach-grammar' : '');
      // .needs-ruby spans are resolved by applyRuby() below.
      const targetHtml = it.target
        ? `<span class="needs-ruby" data-text="${esc(it.target)}" data-lang="${esc(lang)}">${esc(it.target)}</span>`
        : '';
      row.innerHTML = `<div class="teach-text">
          <div class="teach-target">${targetHtml}${it.grammar ? '<span class="teach-tag">grammar</span>' : ''}</div>
          <div class="teach-gloss">${esc(it.gloss || '')}</div>
          ${it.note ? `<div class="teach-note">${esc(it.note)}</div>` : ''}
        </div>`;
      if (isGrammar) {
        const body = row.querySelector('.teach-text');
        blocks.forEach(b => { const el = renderBlock(b); if (el) body.appendChild(el); });
      } else {
        const speak = it.audio || it.target;
        if (speak) {
          const btn = document.createElement('button');
          btn.className = 'audio-play'; btn.type = 'button'; btn.textContent = '🔊';
          bindAudioBtn(btn, speak, lang);
          row.appendChild(btn);
        }
        if (it.keyword) {
          const kwLine = document.createElement('div');
          kwLine.className = 'teach-keyword';
          kwLine.innerHTML = `<span class="kw-label">Keyword:</span> <span class="kw-word">${esc(it.keyword)}</span>` +
            (it.keyword_roman ? ` <span class="kw-roman">${esc(it.keyword_roman)}</span>` : '');
          const kwBtn = document.createElement('button');
          kwBtn.className = 'audio-play kw-audio'; kwBtn.type = 'button'; kwBtn.textContent = '🔊';
          bindAudioBtn(kwBtn, it.keyword, lang);
          kwLine.appendChild(kwBtn);
          row.querySelector('.teach-text').appendChild(kwLine);
        }
      }
      wrap.appendChild(row);
    });
    // Resolve ruby annotations asynchronously (non-blocking). Teach text gets
    // hover/tap glosses from the lesson's vocab_glossary, with a live AI fallback
    // for words it didn't gloss (hybrid; off for the foundations reading track).
    applyRuby(wrap, player && player.vocabGlossary, false, _liveLookupOn());
  }

  // One teach block per screen (C1). Intro rides on the first card only.
  function renderTeachCard() {
    const dots = document.getElementById('teach-dots');
    dots.style.display = '';
    dots.innerHTML = player.teachBlocks
      .map((_, i) => `<span class="${i === player.teachIdx ? 'on' : ''}"></span>`).join('');
    document.getElementById('teach-intro').textContent =
      player.teachIdx === 0 ? player.teachIntro : '';
    const wrap = document.getElementById('teach-items');
    wrap.innerHTML = '';
    const el = renderBlock(player.teachBlocks[player.teachIdx]);
    if (el) wrap.appendChild(el);
    applyRuby(wrap, player && player.vocabGlossary, false, _liveLookupOn());
    setTeachAction(player.teachIdx >= player.teachBlocks.length - 1);
    updateReportBtn();
    const tb = document.getElementById('teach-back');
    if (tb) tb.style.display = player.teachIdx > 0 ? '' : 'none';
    // Teach cards are counted work, so paging through them moves the step bar.
    updateBar();
    window.scrollTo(0, 0);
  }

  // C1 · quick_check: a one-tap formative check inside the teach flow. Ungraded —
  // it never joins the mistake queue, mastery ledger, or score, and a miss never
  // breaks the combo; a correct tap still bumps combo/XP so it feels rewarding.
  function renderQuickCheck(b) {
    const box = document.createElement('div'); box.className = 'qc';
    box.innerHTML = `<div class="qc-kicker">Quick check</div><div class="qc-q">${esc(b.question || '')}</div>`;
    const lang = player && player.lang;
    let answered = false;
    const btns = [];
    (b.options || []).forEach((o, i) => {
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'qc-opt';
      const inner = (needsRuby(lang) && !_isLatin(o)) ? targetSpan(o, lang) : esc(o);
      btn.innerHTML = `<span>${inner}</span><span class="qc-mark"></span>`;
      btn.onclick = () => {
        if (answered) return;
        answered = true;
        box.classList.add('answered');
        const ok = i === b.answer;
        btn.classList.add(ok ? 'good' : 'bad');
        btn.querySelector('.qc-mark').textContent = ok ? '✓' : '✗';
        if (!ok && btns[b.answer]) {
          btns[b.answer].classList.add('good');
          btns[b.answer].querySelector('.qc-mark').textContent = '✓';
        }
        const why = document.createElement('div'); why.className = 'qc-why';
        why.innerHTML = `${ok ? 'Right!' : 'Not quite.'} ${esc(b.why || '')}` +
          '<small>Ungraded warm-up — it never counts against you.</small>';
        box.appendChild(why);
        if (ok) {
          sfx.correct();
          if (player) {
            player.combo++;
            player.maxCombo = Math.max(player.maxCombo, player.combo);
            const gained = comboXp(player.combo);
            player.xp += gained;
            bumpCombo(gained);
          }
        } else { sfx.wrong(); }
        try { if (navigator.vibrate) navigator.vibrate(ok ? 15 : [0, 30, 30, 30]); } catch {}
      };
      btns.push(btn);
      box.appendChild(btn);
    });
    return box;
  }

  // Live word lookup is enabled for AI-lesson teach text (not foundations, where
  // romanization is the lesson and a "translation" would be meaningless).
  function _liveLookupOn() { return !!player && player.theme !== 'foundations'; }

  // C4 · listening-first variant. Sometimes present a production drill (English
  // prompt → pick the target) as a listening drill (hear the target → pick it),
  // so replays of a lesson stop being identical. Client-only; the options are
  // already target-language, so the answer key is untouched. Skipped for
  // checkpoints/test-out (deterministic quizzes) and foundations/practice.
  function _maybeListeningVariant(ex) {
    if (!ex || ex.type !== 'choice' || ex.prompt_lang !== 'english') return ex;
    if (!player || player.checkpointUnitId || player.testOut
        || player.theme === 'foundations' || player.practiceGame) return ex;
    const opts = ex.options || [];
    const ans = ex.answer;
    if (ans == null || ans < 0 || ans >= opts.length || !opts[ans]) return ex;
    // Don't MAKE ear-only drills when audio is evidently down — a readable drill
    // is better than one that has to be skipped a moment later.
    if (_ttsFailCount > 0 && _ttsOkCount === 0) return ex;
    if (Math.random() > 0.35) return ex;   // ~1 in 3 production drills
    return { ...ex, type: 'listening', instruction: 'What did you hear?',
             audio: opts[ans], prompt: '', prompt_roman: '', audio_roman: '' };
  }

  // 🎤 Speaking inside ordinary lessons. A per-drill coin flip made these almost
  // invisible: a standard-length lesson holds only three or four eligible
  // production drills, so a 1-in-5 roll usually produced NONE and the feature
  // read as missing. Instead each lesson gets a small BUDGET, and each step
  // spends at most one of it on a randomly chosen eligible drill — reliably
  // present, still varying between plays, never dominating a step.
  const SPEAK_PER_LESSON = 2;

  function _canSpeakVariant(ex) {
    // `speechSupported()` only says the API exists. A device whose recogniser
    // has already failed to answer gets no further speaking drills this session
    // — offering one that can only time out is worse than not offering it.
    if (!_speakDrills || _speechDead || !speechSupported()
        || !speechLangFor(player && player.lang)) return false;
    if (!ex || ex.type !== 'choice' || ex.prompt_lang !== 'english' || ex.is_cloze) return false;
    if (!player || player.checkpointUnitId || player.testOut
        || player.theme === 'foundations' || player.practiceGame) return false;
    const opts = ex.options || [];
    const ans = ex.answer;
    return !(ans == null || ans < 0 || ans >= opts.length || !opts[ans]);
  }

  function _toSpeakVariant(ex) {
    // The English prompt stays and the options go: the learner produces the line
    // rather than picking it, which is the whole point of speaking it.
    return { ...ex, type: 'speak', instruction: 'Say this out loud',
             read_aloud: false, target: ex.options[ex.answer],
             target_roman: ex.answer_roman || '',
             accept: [], options: null, answer: null };
  }

  // Spend at most one of the lesson's speaking budget on this step.
  function _addSpeakVariant(exs) {
    if (player.speakBudget == null) player.speakBudget = SPEAK_PER_LESSON;
    if (player.speakBudget <= 0) return exs;
    const eligible = [];
    exs.forEach((e, i) => { if (_canSpeakVariant(e)) eligible.push(i); });
    if (!eligible.length) return exs;
    const pick = eligible[Math.floor(Math.random() * eligible.length)];
    exs[pick] = _toSpeakVariant(exs[pick]);
    player.speakBudget--;
    return exs;
  }

  function startExercises() {   // teach "Start/Continue" button → run current segment's exercises
    const seg = player.segments[player.segIdx];
    // Build the queue once per segment. Returning here via the back button (teach
    // → exercises) must REUSE the existing queue so its `_counted` flags survive —
    // rebuilding fresh copies would let already-answered drills recount.
    if (player._queueSeg !== player.segIdx) {
      // Stamp each drill with WHERE it lives in the stored lesson. The queue is
      // reordered, remapped (listening/speak variants) and re-copied for the
      // mistake lap, so a queue index can't be used to name a stored item — and
      // naming one is exactly what "regenerate this question" needs.
      let exs = (seg.exercises || []).map((e, i) => ({ ...e, _seg: player.segIdx, _ix: i }));
      // drillsOnly: only the LLM-graded construction drills + mini-games (skip recognition/word-bank/etc.)
      if (player.drillsOnly) exs = exs.filter(e => SELF_MANAGED.has(e.type));
      else exs = _addSpeakVariant(exs).map(_maybeListeningVariant);   // vary replays
      // A speaking drill on a browser that can't listen is a dead end — drop it
      // rather than render a mic the learner can never use.
      if (!speechSupported()) {
        const kept = exs.filter(e => e.type !== 'speak');
        _dropFromTotals(exs.length - kept.length);
        exs = kept;
      }
      player.queue = exs;
      player.idx = 0;
      player._queueSeg = player.segIdx;
    }
    if (!player.queue.length) { afterSegment(); return; }
    show('player');
    renderExercise();
  }

  // A drill the learner never got the chance to answer must not count against
  // the score, and must not leave the progress bar stuck short of the end.
  function _dropFromTotals(n) {
    if (!n || !player) return;
    player.total = Math.max(0, player.total - n);
    const st = player.segTotals || [];
    if (st[player.segIdx] != null) st[player.segIdx] = Math.max(0, st[player.segIdx] - n);
  }

  // Pull an unanswerable drill out of the run entirely.
  function _skipExercise(ex, notice) {
    const i = player.queue.indexOf(ex);
    if (i < 0) return;
    player.queue.splice(i, 1);
    if (!ex._review && !ex._counted) _dropFromTotals(1);
    if (notice) learnToast(notice);
    if (i < player.idx) {                 // behind us — the screen is unaffected
      player.idx--;
      updateBar();
      return;
    }
    if (player.idx >= player.queue.length) {
      if (player.reviewStarted) finishLesson();
      else afterSegment();
    } else {
      renderExercise();                   // the next drill slides into this slot
    }
  }

  // Ear-only drills need working audio. Give the clip one retry (edge-tts fails
  // transiently), then drop the drill instead of leaving the learner staring at
  // a dead 🔊 with nothing to go on. Returns true when the drill was dropped.
  function _guardAudioExercise(ex) {
    if (!_audioOnly(ex)) return false;
    // A normal listening choice has a readable fallback. Keep it in the run and
    // let the learner switch to romanization; audio-only mini-games still drop.
    if (ex.type === 'listening' && (ex._usedAudioFallback || ex._audioUnavailable)) return false;
    const lang = player.lang;
    const clips = _exClips(ex);
    if (!clips.length) {
      if (ex.type === 'listening') { ex._audioUnavailable = true; return false; }
      _skipExercise(ex, 'Skipped a listening exercise — no audio.'); return true;
    }
    const allDead = () => clips.every(c => ttsHealth(c, lang) === 'fail');
    const settle = () => {
      if (!player || player.queue[player.idx] !== ex || player.graded
          || ex._usedAudioFallback || ex._audioUnavailable || !allDead()) return;
      if (!ex._audioRetried) {            // one clean retry before giving up on it
        ex._audioRetried = true;
        clips.forEach(c => _retryTTS(c, lang));
        return;
      }
      if (ex.type === 'listening') {
        ex._audioUnavailable = true;
        learnToast('Audio unavailable — showing romanization.');
        renderExercise();
        return;
      }
      _skipExercise(ex, 'Skipped a listening exercise — audio unavailable.');
    };
    const alive = () => !!player && player.queue[player.idx] === ex;
    clips.forEach(c => onTTSHealth(c, lang, alive, settle));
    clips.forEach(c => _prewarmTTS(c, lang));
    settle();
    return player.queue[player.idx] !== ex;
  }

  // Every genuinely ear-only screen has an immediate learner-controlled exit.
  // Listening choices have their richer romanization switch in their renderer;
  // tile translation drills are no longer ear-only because the server supplies
  // an English prompt. The remaining specialist drills either become readable
  // or are skipped without affecting score/mastery.
  function _renderAudioBypass(ex) {
    const wrap = document.getElementById('audio-bypass');
    if (!wrap) return;
    wrap.innerHTML = '';
    wrap.style.display = 'none';
    if (!ex || ex.type === 'listening' || !_audioOnly(ex)) return;

    const btn = document.createElement('button');
    btn.type = 'button';
    if (ex.type === 'block_build' && ex.roman) {
      btn.textContent = 'Can’t listen? Show romanization';
      btn.onclick = () => {
        stopTTS();
        ex.hide_roman = false;
        renderExercise();
      };
    } else if (ex.type === 'memory_match' && ex.audio_mode) {
      btn.textContent = 'Can’t listen? Match with text instead';
      btn.onclick = () => {
        stopTTS();
        ex.audio_mode = false;
        renderExercise();
      };
    } else {
      btn.textContent = 'Can’t listen? Skip this audio exercise';
      btn.onclick = () => {
        stopTTS();
        if (player && player._mgCleanup) {
          player._mgCleanup();
          player._mgCleanup = null;
        }
        _skipExercise(ex, 'Skipped an audio exercise.');
      };
    }
    wrap.appendChild(btn);
    wrap.style.display = '';
  }

  function renderExercise() {
    const ex = player.queue[player.idx];
    // Before the guard: it refuses to yank a drill the learner has answered, and
    // the flag still belongs to the exercise leaving the screen.
    player.graded = false;
    if (_guardAudioExercise(ex)) return;   // dropped: it re-rendered whatever follows
    updateReportBtn();
    const root = document.getElementById('exercise-root');
    const fb = document.getElementById('feedback'); fb.className = 'feedback'; fb.innerHTML = '';
    document.getElementById('review-badge').style.display = ex._review ? '' : 'none';
    document.getElementById('grammar-badge').style.display = ex.grammar ? '' : 'none';
    updateComboChip();
    updateBackBtn();
    const action = document.getElementById('player-action');
    const footer = document.getElementById('player-footer');
    // Construction drill is self-managed (its own submit + turn loop, LLM-graded):
    // hide the standard footer (feedback + Check/Continue) and advance via onDone.
    if (player._mgCleanup) { player._mgCleanup(); player._mgCleanup = null; }
    if (SELF_MANAGED.has(ex.type)) {
      footer.style.display = 'none';
      player.controller = EXERCISE_TYPES[ex.type].render(ex, root, player.lang, () => {
        if (!ex._review && !ex._counted) player.firstPassCorrect++;
        if (!ex._review) ex._counted = true;
        advanceExercise();
      });
      _renderAudioBypass(ex);
      updateBar();
      return;
    }
    footer.style.display = '';
    player.controller = (EXERCISE_TYPES[ex.type] || EXERCISE_TYPES.choice).render(ex, root, player.lang);
    // Hide inline ruby in exercises — romanization goes into the hover/tap .gl
    // tooltip on each word. The exception is a speaking drill, where the reading
    // is the help the learner needs to say the line, not the answer they're being
    // asked for. NEVER pass the vocab glossary here: a recognition prompt would
    // reveal its own English answer on hover (glossing is teach-only).
    if (needsRuby(player.lang)) applyRuby(root, null, ex.type !== 'speak');
    _renderAudioBypass(ex);

    const a = document.getElementById('player-action'); a.textContent = 'Check'; a.disabled = true;
    updateBar(); updateAction();
  }

  // Accepted-but-not-the-model-answer is the interesting case: the learner found
  // a different valid form and should see that it counted.
  function _correctHead(judged) {
    return (judged && !judged.exact) ? 'Correct — that works too!' : 'Correct!';
  }

  // ...and should also see the answer the LESSON had in mind. "That works too"
  // on its own leaves the learner with praise and no idea what the other way of
  // saying it even was — so the two sit side by side, and the judge's note is
  // asked to spell out how they differ.
  function _expectedLine(judged, expectedHtml, dup) {
    if (!judged || judged.exact || !expectedHtml || dup) return '';
    return `<small>Lesson's answer: ${expectedHtml}</small>`;
  }

  async function onAction() {
    if (!player) return;
    const a = document.getElementById('player-action');
    if (!player.graded) {
      if (!player.controller.isReady()) return;
      if (player._grading) return;              // typed drills grade over the network
      const ex = player.queue[player.idx];
      // grade() may be async (typed answers call the server). Await it either
      // way, and hold the button so a double-tap can't submit twice or advance
      // past the verdict.
      player._grading = true;
      a.disabled = true;
      let correct;
      try {
        correct = await player.controller.grade();
      } catch (e) {
        // A throw used to escape onAction entirely: the button stayed disabled,
        // nothing was rendered, and there was no way forward — the lesson was
        // simply over. Treat it as an unreachable grader instead, which the
        // uncheckable path below already knows how to show and move past.
        console.error('grade() failed', e);
        correct = false;
        player._gradeFailed = true;
      } finally {
        player._grading = false;
      }
      // The learner may have quit or gone back while the check was in flight.
      if (!player || player.queue[player.idx] !== ex) return;
      const failed = !!player._gradeFailed;
      player._gradeFailed = false;
      const uncheckable = failed
        || (player.controller.uncheckable ? player.controller.uncheckable() : false);
      try { player.controller.lock(correct); } catch (e) { console.error('lock() failed', e); }
      player.graded = true;
      if (uncheckable) {
        // Grader unreachable. Show the answer and move on WITHOUT touching the
        // score, combo, mistake queue or mastery ledger — the learner was quite
        // possibly right, and guessing either way teaches the wrong thing.
        const fbEl = document.getElementById('feedback');
        let at = '', why = '';
        try {
          at = player.controller.answerText ? player.controller.answerText() : '';
          // Why it couldn't be checked differs by drill (no network for a typed
          // answer, no microphone for a spoken one), so let the drill say.
          why = (player.controller.feedback && (player.controller.feedback() || {}).reason) || '';
        } catch (e) { console.error('uncheckable feedback failed', e); }
        why = why || "Couldn't check this one — no connection to the grader.";
        fbEl.className = 'feedback';
        fbEl.innerHTML = esc(why) + (at ? `<small>Expected: ${at}</small>` : '');
        const lastSeg0 = player.segIdx >= player.segments.length - 1;
        const isLastEx0 = player.idx >= player.queue.length - 1;
        a.textContent = (lastSeg0 && isLastEx0 && !player.mistakes.length) ? 'Finish' : 'Continue';
        a.disabled = false;
        return;
      }
      // First-pass counting happens ONCE per exercise. Going back (goBack) or
      // resuming (_applyResume) can re-render an already-answered exercise; the
      // `_counted` flag stops it inflating the score / combo / mistake queue.
      const firstTime = !ex._review && !ex._counted;
      if (correct) {
        if (firstTime) {
          player.firstPassCorrect++;
          player.combo++;
          player.maxCombo = Math.max(player.maxCombo, player.combo);
          if (ex.type === 'listening' && !ex._usedAudioFallback)
            player.listeningHits = (player.listeningHits || 0) + 1;
          const gained = comboXp(player.combo);
          player.xp += gained;
          bumpCombo(gained);
        }
      } else if (ex._review) {
        player.queue.push({ ...ex });          // retry this mistake again later
      } else if (firstTime) {
        player.mistakes.push(ex);              // review it at the end of the lesson
        player.combo = 0;                      // a first-pass miss breaks the combo
        updateComboChip();
      }
      // Track first-pass outcomes per concept for the mastery ledger (once).
      if (firstTime && ex.concept_key) {
        const m = player.conceptResults[ex.concept_key] || (player.conceptResults[ex.concept_key] = { correct: 0, total: 0 });
        m.total++;
        if (correct) m.correct++;
      }
      if (!ex._review) ex._counted = true;
      const fb = document.getElementById('feedback');
      const tip = ex.tip ? `<small>💡 ${esc(ex.tip)}</small>` : '';
      // Typed drills carry a graded verdict: the learner's own wording was
      // accepted or corrected, so say which rather than just "Correct!".
      const judged = player.controller.feedback ? player.controller.feedback() : null;
      const jNote = judged && judged.note ? `<small>${esc(judged.note)}</small>` : '';
      const jFix = judged && judged.corrected
        ? `<small>Better: ${targetSpan(judged.corrected, player.lang)}`
          + (judged.corrected_roman ? ` <em>${esc(judged.corrected_roman)}</em>` : '')
          + `</small>` : '';
      if (correct) {
        fb.className = 'feedback correct';
        const exp = player.controller.expectedText ? player.controller.expectedText() : '';
        // Don't print the expected answer twice when the "Better:" line is
        // already showing exactly it.
        const dup = !!(judged && judged.corrected
          && _normTyped(judged.corrected) === _normTyped(ex.answer || ''));
        fb.innerHTML = _correctHead(judged) + _expectedLine(judged, exp, dup)
          + jFix + jNote + tip;
        sfx.correct();
      } else {
        const at = player.controller.answerText ? player.controller.answerText() : '';
        fb.className = 'feedback wrong';
        fb.innerHTML = 'Not quite.' + (at ? `<small>Answer: ${at}</small>` : '') + jNote + tip;
        sfx.wrong();
        const root = document.getElementById('exercise-root');
        root.classList.remove('shake'); void root.offsetWidth; root.classList.add('shake');
      }
      // Replay button so the learner can re-hear the audio while reading feedback.
      if (ex.audio) {
        const rp = document.createElement('button');
        rp.className = 'fb-replay'; rp.type = 'button'; rp.title = 'Replay audio';
        rp.textContent = '🔊';
        fb.appendChild(rp);              // append first — bindAudioBtn watches liveness
        bindAudioBtn(rp, ex.audio, player.lang);
      }
      try { if (navigator.vibrate) navigator.vibrate(correct ? 15 : [0, 30, 30, 30]); } catch {}
      const lastSeg = player.segIdx >= player.segments.length - 1;
      const isLastEx = player.idx >= player.queue.length - 1;
      const willReview = (!player.reviewStarted && player.mistakes.length > 0) || (player.reviewStarted && !correct);
      a.textContent = (lastSeg && isLastEx && !willReview) ? 'Finish' : 'Continue';
      a.disabled = false;
    } else {
      advanceExercise();
    }
  }

  function advanceExercise() {
    player.answered++;
    player.segAnswered++;
    player.idx++;
    if (player.idx >= player.queue.length) {
      if (player.reviewStarted) finishLesson();   // review queue exhausted
      else afterSegment();                         // next segment / start review / finish
    } else {
      renderExercise();
    }
  }

  async function finishLesson() {
    if (player.lessonId) _clearResume(player.lessonId);   // completed → no resume
    const total = player.total;
    const score = total ? Math.round(player.firstPassCorrect / total * 100) : 100;
    const perfect = total > 0 && player.firstPassCorrect === total;
    const xpEarned = player.xp + (perfect ? PERFECT_BONUS : 0);
    document.getElementById('combo-chip').classList.remove('on');
    const results = Object.entries(player.conceptResults).map(([k, v]) => ({
      concept_key: k, correct: v.correct, total: v.total,
    }));
    await reportQuestSignals();   // combo / listening quests (before the quest refetch)

    // Default results buttons (the test-out fail path rewires them below).
    const contBtn = document.getElementById('results-continue');
    const retryBtn = document.getElementById('results-retry');
    contBtn.textContent = 'Continue'; contBtn.onclick = backToMap;
    retryBtn.style.display = ''; retryBtn.textContent = 'Practice again'; retryBtn.onclick = retryLesson;

    // B3 · checkpoint: its own completion route + pass/fail results screen.
    if (player.checkpointUnitId) {
      let info = null;
      try {
        const res = await fetch('/api/units/' + player.checkpointUnitId + '/checkpoint', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ score, results }),
        });
        info = await res.json();
      } catch {}
      const passed = info ? info.passed : score >= (player.checkpointPassPct || 80);
      document.getElementById('results-score').textContent = score + '%';
      const emoji = document.getElementById('results-emoji');
      emoji.textContent = passed ? '🛡' : '💪';
      document.getElementById('results-msg').textContent = passed
        ? 'Checkpoint passed — this unit is sealed!'
        : `You need ${(info && info.pass_pct) || 80}% to seal the unit. Review and try again!`;
      const badges = document.getElementById('results-badges');
      badges.innerHTML = passed
        ? `<span class="results-badge perfect">🛡 Checkpoint passed${info && info.xp_awarded ? ` +${info.xp_awarded} XP` : ''}</span>`
        : '';
      renderResultsXp(info && info.xp_awarded ? info.xp_awarded : 0, false, player.maxCombo, 0, false);
      document.getElementById('results-recap').innerHTML = '';
      document.getElementById('results-vocab').innerHTML = '';
      document.getElementById('results-feedback').innerHTML = '';
      show('results');
      renderResultsQuests();
      emoji.classList.remove('bounce-in'); void emoji.offsetWidth; emoji.classList.add('bounce-in');
      if (passed) { sfx.complete(); confetti(); } else { sfx.wrong(); }
      try { if (navigator.vibrate) navigator.vibrate([0, 40, 40, 80]); } catch {}
      loadStreak();
      return;
    }

    // A4 · test-out fail: nothing is recorded — offer the full lesson instead.
    if (player.testOut && player.firstPassCorrect < Math.ceil(total * 0.75)) {
      const lessonId = player.lessonId;
      document.getElementById('results-score').textContent = score + '%';
      const emoji = document.getElementById('results-emoji');
      emoji.textContent = '💪';
      document.getElementById('results-msg').textContent =
        `You need ${Math.ceil(total * 0.75)} of ${total} to test out — the full lesson will get you there.`;
      document.getElementById('results-badges').innerHTML = '';
      renderResultsXp(0, false, player.maxCombo, 0, false);
      document.getElementById('results-recap').innerHTML = '';
      document.getElementById('results-vocab').innerHTML = '';
      document.getElementById('results-feedback').innerHTML = '';
      contBtn.textContent = 'Start the full lesson →';
      contBtn.onclick = () => openLesson(lessonId);
      retryBtn.textContent = 'Back to map'; retryBtn.onclick = backToMap;
      show('results');
      renderResultsQuests();
      emoji.classList.remove('bounce-in'); void emoji.offsetWidth; emoji.classList.add('bounce-in');
      sfx.wrong();
      return;
    }

    let serverInfo = null;
    if (player.lessonId) {
      try {
        const body = { score, results, xp: xpEarned };
        if (player.testOut) { body.xp = Math.round(xpEarned * 0.5); body.tested_out = true; }
        const res = await fetch('/api/lessons/' + player.lessonId + '/complete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        serverInfo = await res.json();
      } catch {}
    }
    const emoji = document.getElementById('results-emoji');
    if (player.practiceGame) {
      // Mini-games (foundations + B4 lightning) score themselves on their own
      // screen; the "1 of 1" lesson tally would be meaningless here.
      document.getElementById('results-score').textContent = 'Done!';
      emoji.textContent = player.lightning ? '⚡' : '🎉';
      document.getElementById('results-msg').textContent = 'Nice practice — keep the combo going!';
    } else {
      document.getElementById('results-score').textContent = total ? score + '%' : 'Done!';
      emoji.textContent = perfect ? '🏆' : (score >= 80 ? '🎉' : (score >= 50 ? '👍' : '💪'));
      document.getElementById('results-msg').textContent =
        total ? `You got ${player.firstPassCorrect} of ${total} correct.` : 'Nice — keep going!';
    }
    renderResultsXp(xpEarned, player.practiceGame ? false : perfect, player.maxCombo,
                    serverInfo && serverInfo.crown_level, serverInfo && serverInfo.crown_leveled_up);
    // Textbook units read like a book: keep going into the next lesson from
    // here rather than sending the learner back to hunt for it on the map. When
    // the next one isn't authored yet the server is already building it.
    const follow = serverInfo && serverInfo.next_lesson;
    if (follow && follow.next) {
      contBtn.textContent = 'Next lesson →';
      contBtn.onclick = () => openLesson(follow.next.id);
      retryBtn.textContent = 'Back to map'; retryBtn.onclick = backToMap;
    } else if (follow && follow.queued_remaining) {
      contBtn.textContent = 'Build the next lesson →';
      contBtn.onclick = () => { backToMap(); generateTextbookLesson(follow.unit_id); };
      retryBtn.textContent = 'Back to map'; retryBtn.onclick = backToMap;
    }
    if (player.testOut) {
      document.getElementById('results-badges').insertAdjacentHTML('beforeend',
        '<span class="results-badge">🎓 Tested out</span>');
    }
    show('results');
    renderResultsRecap();   // C3 · "What you learned" recap card
    renderResultsVocab();   // async fill of the "New words → deck" panel
    renderResultsFeedback();// D4 · 👍/👎 "How was this lesson?"
    renderResultsQuests();  // B1 · quest ticks advancing
    emoji.classList.remove('bounce-in'); void emoji.offsetWidth; emoji.classList.add('bounce-in');
    sfx.complete();
    if (score >= 60) confetti();
    if (perfect) setTimeout(confetti, 350);   // double burst for a flawless run
    try { if (navigator.vibrate) navigator.vibrate([0, 40, 40, 80]); } catch {}
    if (serverInfo && serverInfo.freeze_earned) {   // B5 · earned a streak freeze
      setTimeout(() => learnToast('🛡 You earned a streak freeze!'), 900);
    }
    loadStreak();   // refresh the ⭐ header total + daily-goal ring with the new XP
  }

  // C3 · recap card: the lesson's key table (else first examples block),
  // collapsed under the score so learners can revisit what they just learned.
  function renderResultsRecap() {
    const wrap = document.getElementById('results-recap');
    wrap.innerHTML = '';
    if (!player || player.theme === 'foundations' || player.practiceGame) return;
    let block = null;
    for (const sg of player.segments || []) {
      const blocks = (sg.teach && sg.teach.blocks) || [];
      block = block || blocks.find(b => b.type === 'table');
      if (block) break;
    }
    if (!block) {
      for (const sg of player.segments || []) {
        const blocks = (sg.teach && sg.teach.blocks) || [];
        block = block || blocks.find(b => b.type === 'examples');
        if (block) break;
      }
    }
    if (!block) return;
    const card = document.createElement('div');
    card.className = 'recap ' + scriptClassFor(player.lang);
    card.innerHTML = `<button class="recap-toggle" type="button">
        <span class="r-kicker">What you learned</span><span class="r-chev">›</span>
      </button><div class="recap-body"></div>`;
    card.querySelector('.recap-toggle').onclick = () => card.classList.toggle('open');
    const el = renderBlock(block);
    if (!el) return;
    card.querySelector('.recap-body').appendChild(el);
    wrap.appendChild(card);
    applyRuby(card, player.vocabGlossary);
  }

  // D4 · lightweight 👍/👎 rating. One tap posts to the lesson-feedback ring
  // buffer the planner reads. Only for real AI-lesson completions (not
  // foundations/practice/checkpoints/test-out).
  function renderResultsFeedback() {
    const wrap = document.getElementById('results-feedback');
    wrap.innerHTML = '';
    if (!player || !player.lessonId || player.theme === 'foundations'
        || player.practiceGame || player.checkpointUnitId || player.testOut) return;
    const lessonId = player.lessonId;
    wrap.innerHTML = `<div class="results-fb" id="results-fb">
        <span class="rfb-q">How was this lesson?</span>
        <div class="rfb-btns">
          <button class="rfb-btn" data-rating="up" aria-label="Good">👍</button>
          <button class="rfb-btn" data-rating="down" aria-label="Not great">👎</button>
        </div></div>`;
    wrap.querySelectorAll('.rfb-btn').forEach(btn => {
      btn.onclick = () => {
        const rating = btn.dataset.rating;
        wrap.querySelectorAll('.rfb-btn').forEach(b => {
          b.disabled = true;
          b.classList.toggle('picked', b === btn);
        });
        wrap.querySelector('.rfb-q').textContent = 'Thanks — noted for your next lessons!';
        fetch('/api/lessons/' + lessonId + '/feedback', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rating }),
        }).catch(() => {});
      };
    });
  }

  // B1 · compact quest state on the results screen (fresh fetch shows the
  // just-earned progress; the chest is claimable right here).
  async function renderResultsQuests() {
    const wrap = document.getElementById('results-quests');
    wrap.innerHTML = '';
    try {
      _questData = await fetch('/api/quests').then(r => r.ok ? r.json() : null);
    } catch { _questData = null; }
    renderQuestCard();   // keep the course-page card in sync for the return trip
    if (!_questData || !(_questData.quests || []).length) return;
    wrap.innerHTML = `<div class="quests" style="margin:0">
      <div class="q-head"><span class="q-kicker">Daily quests</span></div>
      ${_questRows(_questData, true)}
      ${_questData.all_done ? _chestHtml(_questData) : ''}
    </div>`;
  }

  const PERFECT_BONUS = 25;
  function renderResultsXp(xp, perfect, maxCombo, crownLevel, crownUp) {
    const box = document.getElementById('results-xp');
    const num = document.getElementById('results-xp-num');
    box.style.display = xp > 0 ? '' : 'none';
    if (xp > 0) {
      const dur = 700, start = performance.now();
      (function tick(t) {
        const p = Math.min(1, (t - start) / dur);
        num.textContent = Math.round(xp * p);
        if (p < 1) requestAnimationFrame(tick); else num.textContent = xp;
      })(start);
    }
    const badges = document.getElementById('results-badges');
    badges.innerHTML = '';
    if (crownLevel >= 1) {
      // Level-up: this run raised the crown. Otherwise it was already maxed.
      const label = crownUp
        ? (crownLevel >= 3 ? 'Crown maxed!' : `Crown level up → ${crownLevel}`)
        : `Crown ${crownLevel}`;
      badges.innerHTML += `<span class="results-badge crown${crownUp ? ' levelup' : ''}">${Array(crownLevel).fill(_ic.crown).join('')} ${label}</span>`;
    }
    if (perfect) badges.innerHTML += `<span class="results-badge perfect">★ Perfect lesson +${PERFECT_BONUS}</span>`;
    if (maxCombo >= 3) badges.innerHTML += `<span class="results-badge">${_ic.flame} Best combo ×${maxCombo}</span>`;
  }

  // ── Results screen: add this lesson's new vocab to the SRS deck ────────────
  async function renderResultsVocab() {
    const wrap = document.getElementById('results-vocab');
    wrap.innerHTML = '';
    if (!player) return;
    const vocab = (player.concepts || []).filter(c =>
      (c.kind || 'vocab') === 'vocab' && (c.label || '').trim() && (c.gloss || '').trim());
    if (!vocab.length) return;

    let statuses = {};
    try {
      const res = await fetch('/api/cards/status', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ words: vocab.map(v => v.label), lang: player.lang }),
      });
      statuses = (await res.json()).statuses || {};
    } catch {}

    const panel = document.createElement('div');
    panel.className = 'rv-panel ' + scriptClassFor(player.lang);
    panel.innerHTML = `<div class="rv-title">Words added to your flashcards</div>`;
    vocab.forEach(v => {
      const row = document.createElement('div');
      row.className = 'rv-row';
      const inDeck = !!statuses[v.label];
      row.innerHTML = `<div class="rv-word">${targetSpan(v.label, player.lang)}</div>
        <div class="rv-gloss">${esc(v.gloss)}</div>`;
      if (inDeck) {
        row.insertAdjacentHTML('beforeend', '<span class="rv-in-deck">✓ In your deck</span>');
      } else {
        row.insertAdjacentHTML('beforeend', '<span class="rv-in-deck rv-adding">Adding…</span>');
      }
      panel.appendChild(row);
    });
    wrap.appendChild(panel);
    applyRuby(panel);   // inline ruby — no spoiler concern on the results screen
  }

  async function addWordToDeck(v, btn) {
    if (btn.disabled) return;
    btn.disabled = true; btn.textContent = '…';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: v.gloss, target_text: v.label,
          target_lang: player.lang, priority: 3,
          notes: 'From lesson: ' + (player.title || ''),
        }),
      });
      if (!res.ok) throw new Error();
      btn.outerHTML = '<span class="rv-in-deck">✓ Added</span>';
    } catch {
      btn.disabled = false; btn.textContent = '＋ Add';
    }
  }

  // ── Back navigation within a step ──────────────────────────────────────────
  // Whether the current segment's teach screen would have been shown (so "back"
  // from the first exercise can return to it). Not for skip-teach/foundations runs.
  function _segTeachVisible(seg) {
    return !!(seg && seg.teach && player && !player.skipTeach
      && ((seg.teach.items || []).length || seg.teach.intro || (seg.teach.blocks || []).length));
  }

  // ── ⚑ Report a bad item → regenerate just that item ─────────────────────────
  // A lesson is authored in one shot, so one wrong drill used to leave the
  // learner replaying a lesson they know is broken (or deleting the whole
  // thing). Here they point at the item, optionally say what's wrong, and the
  // server re-authors THAT item through the same validation as the original.
  let _report = null;   // {kind, seg, ix} while the sheet is open

  // Only stored, AI-authored items can be rewritten: practice runs and
  // checkpoints aren't a lesson, self-managed drills are generated as you play
  // them, and the reading track is built by code rather than a model.
  function _canReport(kind) {
    if (!player || !player.lessonId || player.practiceGame || player.checkpointUnitId) return false;
    if (player.theme === 'foundations') return false;
    if (kind === 'teach') {
      // Paged teach shows one block per screen, so teachIdx names it exactly. An
      // unpaged step is reportable only when it holds a single block (index 0);
      // a legacy word-list teach isn't authored as blocks at all.
      if (player.teachPaged) return true;
      return (player.teachBlocks || []).length === 1 && !player.teachItemCount;
    }
    const ex = player.queue[player.idx];
    return !!ex && ex._seg != null && !SELF_MANAGED.has(ex.type);
  }

  function updateReportBtn() {
    const row = document.getElementById('report-row');
    if (row) row.style.display = _canReport('drill') ? '' : 'none';
    const trow = document.getElementById('teach-report-row');
    if (trow) trow.style.display = _canReport('teach') ? '' : 'none';
  }

  // The sheet is bottom-anchored, so the phone keyboard would sit on top of the
  // note field and the button that submits it. Shrink the overlay to the VISIBLE
  // viewport so its flex-end lands above the keyboard (iOS never resizes the
  // layout viewport). Same trick as the textbook sheet.
  function _syncReportViewport() {
    const vv = window.visualViewport;
    const el = document.getElementById('report-overlay');
    if (!vv || !el || !el.classList.contains('open')) return;
    const overlap = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    el.style.bottom = overlap ? overlap + 'px' : '';
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', _syncReportViewport);
    window.visualViewport.addEventListener('scroll', _syncReportViewport);
  }

  function openReportSheet(kind) {
    if (!_canReport(kind)) return;
    const ex = player.queue[player.idx];
    _report = kind === 'teach'
      ? { kind, seg: player.segIdx, ix: player.teachIdx || 0 }
      : { kind, seg: ex._seg, ix: ex._ix };
    const what = kind === 'teach' ? 'explanation' : 'question';
    document.getElementById('report-sheet').innerHTML = `
      <div class="intro-grab"></div>
      <div class="intro-kicker">Report</div>
      <h2>⚑ Something wrong with this ${what}?</h2>
      <p class="intro-obj">We'll rewrite just this ${what} and keep the rest of the lesson.
        Saying what's wrong helps — but you can leave it blank.</p>
      <textarea class="report-note" id="report-note" rows="3"
        placeholder="e.g. two of the options are correct"></textarea>
      <div class="report-status" id="report-status"></div>
      <div class="intro-actions">
        <button class="cta-btn" id="report-go" onclick="submitRegen()">✨ Rewrite this ${what}</button>
        <div class="row2"><button class="cta-btn secondary" onclick="closeReportSheet()">Cancel</button></div>
      </div>`;
    document.getElementById('report-overlay').classList.add('open');
    _syncReportViewport();
    setTimeout(() => { try { document.getElementById('report-note').focus({ preventScroll: true }); } catch {} }, 80);
  }

  function closeReportSheet() {
    const el = document.getElementById('report-overlay');
    el.classList.remove('open');
    el.style.bottom = '';
    _report = null;
  }

  async function submitRegen() {
    if (!_report || !player) return;
    const btn = document.getElementById('report-go');
    const status = document.getElementById('report-status');
    const note = (document.getElementById('report-note') || {}).value || '';
    btn.disabled = true;
    status.className = 'report-status';
    status.textContent = 'Rewriting…';
    const { kind, seg, ix } = _report;
    const lessonId = player.lessonId;
    try {
      const res = await fetch('/api/lessons/' + lessonId + '/regenerate-item', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ segment: seg, index: ix, kind, note: note.trim().slice(0, 400) }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.item) throw new Error(data.detail || "Couldn't rewrite that one.");
      // The learner may have moved on (or quit) while the rewrite was in flight.
      if (!player || player.lessonId !== lessonId) { closeReportSheet(); return; }
      const segment = (player.segments || [])[seg];
      if (kind === 'teach') {
        const blocks = (segment && segment.teach && segment.teach.blocks) || [];
        if (blocks[ix]) blocks[ix] = data.item;
        if (player.teachBlocks && player.teachBlocks[ix]) player.teachBlocks[ix] = data.item;
        closeReportSheet();
        if (_currentState === 'teach' && player.teachPaged) renderTeachCard();
      } else {
        const exs = (segment && segment.exercises) || [];
        if (exs[ix]) exs[ix] = data.item;
        // Swap it into the live run too — including any later copy of the same
        // drill waiting in the mistake lap, which would otherwise re-ask the
        // broken one. A fresh object also resets `_counted`, which is right: it
        // is a different question now.
        const swap = e => (e && e._seg === seg && e._ix === ix)
          ? { ...data.item, _seg: seg, _ix: ix, _review: e._review } : e;
        player.queue = player.queue.map(swap);
        player.mistakes = (player.mistakes || []).map(swap);
        closeReportSheet();
        if (_currentState === 'player') renderExercise();
      }
      learnToast('Rewritten — thanks for flagging it.');
    } catch (e) {
      if (!btn.isConnected) return;
      btn.disabled = false;
      status.className = 'report-status err';
      status.textContent = e.message || "Couldn't rewrite that one — please try again.";
    }
  }

  function updateBackBtn() {
    const b = document.getElementById('player-back');
    if (!b) return;
    let can = false;
    if (player && !player.reviewStarted) {
      const ex = player.queue[player.idx];
      if (!(ex && SELF_MANAGED.has(ex.type)))
        can = player.idx > 0 || _segTeachVisible(player.segments[player.segIdx]);
    }
    b.style.display = can ? '' : 'none';
  }

  // Step back one exercise (re-rendered fresh — the `_counted` guard keeps the
  // score/combo honest), or from the first exercise back to the step's teach.
  function goBack() {
    if (!player || _currentState !== 'player' || player.reviewStarted) return;
    const ex = player.queue[player.idx];
    if (ex && SELF_MANAGED.has(ex.type)) return;   // self-managed drills own their screen
    if (player._mgCleanup) { player._mgCleanup(); player._mgCleanup = null; }
    if (player.idx > 0) {
      player.idx--;
      player.segAnswered = Math.max(0, player.segAnswered - 1);
      renderExercise();
    } else {
      const seg = player.segments[player.segIdx];
      if (!_segTeachVisible(seg)) return;
      renderTeach(seg.teach);
      if (player.teachPaged) { player.teachIdx = player.teachBlocks.length - 1; renderTeachCard(); }
      show('teach'); updateBar();
      updateBar();
    }
  }

  function goBackTeach() {
    if (player && player.teachPaged && player.teachIdx > 0) {
      player.teachIdx--;
      renderTeachCard();
    }
  }

  // ── Resume: save an in-progress lesson so the learner can stop early ─────────
  function _resumeKey(id) { return 'lessonResume:' + id; }
  function _loadResume(id) {
    try {
      const s = JSON.parse(localStorage.getItem(_resumeKey(id)) || 'null');
      if (!s || s.lessonId !== id || !Array.isArray(s.segments) || !s.segments.length) return null;
      return s;
    } catch { return null; }
  }
  function _clearResume(id) { try { localStorage.removeItem(_resumeKey(id)); } catch {} }

  function _saveResume() {
    // Only ordinary lessons are resumable — not practice games (lessonId 0),
    // quizzes (test-out / checkpoint) or drills-only runs, and only once the
    // learner has actually started.
    if (!player || !player.lessonId || player.testOut || player.checkpointUnitId || player.drillsOnly) return false;
    const started = player.segIdx > 0 || player.segAnswered > 0 || player.firstPassCorrect > 0
      || _currentState === 'player';
    if (!started) return false;
    const snap = {
      v: 1, lessonId: player.lessonId, mode: player.mode || '',
      state: _currentState === 'teach' ? 'teach' : 'player',
      reviewStarted: !!player.reviewStarted,
      segIdx: player.segIdx, idx: player.idx, segAnswered: player.segAnswered, answered: player.answered,
      firstPassCorrect: player.firstPassCorrect, xp: player.xp, maxCombo: player.maxCombo,
      listeningHits: player.listeningHits, mistakes: player.mistakes, conceptResults: player.conceptResults,
      queue: player.queue, segments: player.segments, segTotals: player.segTotals, total: player.total,
      theme: player.theme, lang: player.lang, title: player.title, ts: Date.now(),
    };
    try { localStorage.setItem(_resumeKey(player.lessonId), JSON.stringify(snap)); return true; }
    catch { return false; }
  }

  // Restore a snapshot onto the freshly-built `player` (openLesson has already set
  // lang/theme/vocabGlossary/concepts). Returns false to fall back to a fresh start.
  function _applyResume(s) {
    try {
      if (!s || !player || !Array.isArray(s.segments) || !s.segments.length) return false;
      if (typeof s.segIdx !== 'number' || s.segIdx < 0 || s.segIdx >= s.segments.length) return false;
      player.segments = s.segments;
      player.segTotals = s.segTotals || s.segments.map(sg => (sg.exercises || []).length);
      player.total = s.total || player.segTotals.reduce((a, b) => a + b, 0);
      player.firstPassCorrect = s.firstPassCorrect || 0;
      player.answered = s.answered || 0;
      player.xp = s.xp || 0;
      player.maxCombo = s.maxCombo || 0;
      player.listeningHits = s.listeningHits || 0;
      player.mistakes = Array.isArray(s.mistakes) ? s.mistakes : [];
      player.conceptResults = s.conceptResults || {};
      player.reviewStarted = !!s.reviewStarted;
      player.segIdx = s.segIdx;
      _preloadDrills(s.segments, player.lang);
      if (s.state === 'player' && Array.isArray(s.queue) && s.queue.length) {
        player.queue = s.queue;
        player._queueSeg = s.segIdx;
        player.idx = Math.min(Math.max(0, s.idx || 0), s.queue.length - 1);
        player.segAnswered = Math.min(s.segAnswered != null ? s.segAnswered : player.idx,
          player.segTotals[s.segIdx] || s.queue.length);
        show('player');
        renderExercise();
      } else {
        startSegment(s.segIdx);
      }
      return true;
    } catch { return false; }
  }

  function resumeLesson(id) {
    const s = _loadResume(id);
    if (s) openLesson(id, s.mode || '', s);
    else openLesson(id);
  }

  function quitLesson() {
    const canSave = _canSaveResume();
    const msg = canSave
      ? 'Stop this lesson for now? Your progress is saved — you can pick up where you left off.'
      : 'Quit this lesson? This attempt won\'t be saved.';
    if (confirm(msg)) {
      if (canSave) _saveResume();
      if (_cdDrillCleanup) _cdDrillCleanup();
      // Stop any running mini-game timer — a leaked interval would keep ticking
      // (and crash on a null player) after the lesson is gone.
      if (player && player._mgCleanup) { player._mgCleanup(); player._mgCleanup = null; }
      player = null; refreshAndShowCourse();
    }
  }
  function _canSaveResume() {
    return !!(player && player.lessonId && !player.testOut && !player.checkpointUnitId && !player.drillsOnly
      && (player.segIdx > 0 || player.segAnswered > 0 || player.firstPassCorrect > 0));
  }
  function retryLesson() {
    if (!player) return;
    if (player.lightning) {   // B4 · re-roll a fresh lightning set from the source
      const src = player.lightningSource, lang = player.lang, title = player.lightningTitle;
      show('lesson-loading'); _startLightning(src, lang, title);
    }
    else if (player.practiceGame === 'mistakes' || player.practiceGame === 'speaking')
      openCoursePractice(player.practiceCourseId, player.practiceGame, player.practiceLessonId);
    else if (player.practiceGame) openPracticeGame(player.practiceCourseId, player.practiceGame, player.practiceOpts);
    else if (player.checkpointUnitId) openCheckpoint(player.checkpointUnitId);
    else openLesson(player.lessonId, player.mode || '');
  }
  function backToMap() { player = null; refreshAndShowCourse(); }

  async function refreshAndShowCourse() {
    show('loading');
    loadQuests();
    try {
      const res = await fetch('/api/courses/active');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const { course } = await res.json();
      if (course) { renderCourse(course); show('course'); } else { show('empty'); }
    } catch { show('error'); }
  }

  // ── Viewport-clamped tooltip for .gl[data-gloss] spans ─────────────────────
  // The CSS ::after approach can't clamp to viewport bounds, so we use one shared
  // fixed-position div repositioned on every show().
  (function () {
    const tip = document.getElementById('gl-tip');
    let _visible = false;

    function showTip(el) {
      const gloss = el.dataset.gloss;
      if (!gloss) { hideTip(); return; }
      tip.textContent = gloss;
      tip.style.maxWidth = Math.min(220, window.innerWidth - 16) + 'px';
      tip.style.display = 'block';
      _visible = true;

      const r  = el.getBoundingClientRect();
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      const vw = window.innerWidth;
      const vh = window.innerHeight;

      // Centre horizontally on the word, clamped to 8 px from each edge.
      const left = Math.max(8, Math.min(r.left + r.width / 2 - tw / 2, vw - tw - 8));
      // Prefer below; flip above when there isn't room.
      const top  = (r.bottom + th + 8 > vh && r.top - th - 5 > 0)
        ? r.top - th - 5
        : r.bottom + 5;

      tip.style.left = left + 'px';
      tip.style.top  = top  + 'px';
    }

    function hideTip() { if (_visible) { tip.style.display = 'none'; _visible = false; } }

    // Mouse
    document.addEventListener('mouseover', e => {
      const gl = e.target.closest && e.target.closest('.gl[data-gloss]');
      gl ? showTip(gl) : hideTip();
    });

    // Keyboard / tap focus
    document.addEventListener('focusin', e => {
      const gl = e.target.closest && e.target.closest('.gl[data-gloss]');
      if (gl) showTip(gl);
    });
    document.addEventListener('focusout', hideTip);

    // Dismiss on scroll or touch-outside
    document.addEventListener('scroll', hideTip, { passive: true });
    document.addEventListener('touchstart', e => {
      if (!(e.target.closest && e.target.closest('.gl[data-gloss], .gl-live'))) hideTip();
    }, { passive: true });

    // Hybrid live lookup: teach words the lesson didn't gloss are .gl-live.
    // Tapping one fetches a translation on demand (reusing the reader endpoint),
    // caches it for the session, and reveals it in the tooltip.
    const _liveCache = {};
    async function liveLookup(el) {
      if (el.dataset.gloss) { showTip(el); return; }   // already resolved
      const word = el.dataset.lw;
      if (!word || !player) return;
      const key = player.lang + '\0' + word;
      if (_liveCache[key]) { el.dataset.gloss = _liveCache[key]; showTip(el); return; }
      el.dataset.gloss = '…'; showTip(el);
      try {
        const res = await fetch('/api/reader/translate-word', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ word, target_lang: player.lang }),
        });
        if (!res.ok) throw new Error();
        const data = await res.json();
        const eng = (data.source_text || '').trim() || '(no translation)';
        _liveCache[key] = eng;
        el.dataset.gloss = eng;
      } catch {
        delete el.dataset.gloss;          // allow a retry on next tap
        return;
      }
      showTip(el);
    }
    document.addEventListener('click', e => {
      const gl = e.target.closest && e.target.closest('.gl-live');
      if (gl) liveLookup(gl);
    });
  })();

  init();
  // ── Keyboard shortcuts (desktop QoL): 1–9 pick an option, Enter = Check/Continue ──
  document.addEventListener('keydown', e => {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (_currentState === 'teach') {
      if (e.key === 'Enter') {
        e.preventDefault();
        document.getElementById('teach-action').click();
      } else if (/^[1-9]$/.test(e.key)) {
        // Quick-check options answer to number keys like exercise options do.
        const b = document.querySelectorAll('#teach-items .qc-opt')[+e.key - 1];
        if (b) b.click();
      }
      return;
    }
    if (_currentState !== 'player' || !player) return;
    const ex = player.queue[player.idx];
    if (!ex || SELF_MANAGED.has(ex.type)) return;   // self-managed drills own their input
    if (e.key === 'Enter') {
      // Let a focused non-action button keep its native Enter behaviour (a11y),
      // but make Enter anywhere else act as Check/Continue.
      if (t && t.tagName === 'BUTTON' && t.id !== 'player-action') return;
      e.preventDefault(); onAction(); return;
    }
    if (!player.graded && /^[1-9]$/.test(e.key)) {
      const opts = document.querySelectorAll('#exercise-root .opt');
      const b = opts[+e.key - 1];
      if (b) b.click();
    }
  });

  document.addEventListener('langchange', function () { init(); });
