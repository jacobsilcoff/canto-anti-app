
  // ── SVG icons ─────────────────────────────────────────────────────────────
  const ICONS = {
    pencil:   '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
    trash:    '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    volume:   '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>',
    note:     '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px;vertical-align:-2px"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>',
    bookmark: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>',
  };

  // ── State ─────────────────────────────────────────────────────────────────
  let languages = [];
  let langByCode = {};
  let queue = [];
  let total = 0;
  let idx = 0;
  // Cards still in learning (e.g. just rated "again") are re-inserted this many
  // positions ahead so they come back around in the same session.
  const LEARNING_REQUEUE_GAP = 3;
  const reviewedFaces = new Set();   // unique card-faces graded this session
  let revealed = false;
  let _duePoller = null;   // setInterval handle; non-null only on caughtup/done screens
  let currentAudio = null;
  let isOnline = navigator.onLine;
  let allLabels = [];
  let allCards = [];
  let filterLabelId = null;
  let filterPicker = null;
  let inlinePicker = null;
  let settings = { new_cards_per_day: 20, default_target_lang: 'yue' };
  let sessionStats = { review_count: 0, new_count: 0, daily_new_used: 0, daily_new_limit: 20 };
  let _myDecks = [];
  let _activeDeckId = null;

  // ── Language helpers ──────────────────────────────────────────────────────
  function langInfo(code) {
    return langByCode[code] || { code, name: code, romanization: null, logographic: false };
  }
  function isLogographic(code) {
    return !!langInfo(code).logographic;
  }
  function scriptFamily(code) {
    return langInfo(code).script_family || 'latin';
  }
  // Tag a container so its target-text elements (.is-chinese/.chinese) pick up
  // the right --script-font for the language's writing system.
  function applyScript(el, code) {
    if (!el) return;
    el.classList.remove('script-chinese', 'script-devanagari', 'script-telugu', 'script-hangul', 'script-japanese', 'script-bengali', 'script-arabic', 'script-cyrillic', 'script-latin', 'script-greek', 'script-thai', 'script-hebrew');
    el.classList.add('script-' + scriptFamily(code));
  }
  // Returns { source, target, pronunciation } display labels for the given language
  function faceLabels(code) {
    const info = langInfo(code);
    return {
      source: 'English',
      target: info.name,
      pronunciation: info.romanization
        ? info.romanization.charAt(0).toUpperCase() + info.romanization.slice(1)
        : 'Audio',
    };
  }
  function romanizationFieldLabel(code) {
    const info = langInfo(code);
    if (!info.romanization) return null;
    return info.romanization.charAt(0).toUpperCase() + info.romanization.slice(1);
  }

  // ── IndexedDB (v2 — bumped from v1 to clear cards using old face/field names) ──
  let _db = null;

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open('cantonese-app', 2);
      req.onupgradeneeded = e => {
        const db = e.target.result;
        if (db.objectStoreNames.contains('cards')) db.deleteObjectStore('cards');
        db.createObjectStore('cards', { keyPath: 'id' });
        if (!db.objectStoreNames.contains('pending')) db.createObjectStore('pending', { autoIncrement: true });
      };
      req.onsuccess = e => resolve(e.target.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idb() { if (!_db) _db = await openDB(); return _db; }

  async function idbPut(store, value) {
    const db = await idb();
    return new Promise((res, rej) => {
      const tx = db.transaction(store, 'readwrite');
      tx.objectStore(store).put(value);
      tx.oncomplete = res; tx.onerror = () => rej(tx.error);
    });
  }
  async function idbGetAll(store) {
    const db = await idb();
    return new Promise((res, rej) => {
      const req = db.transaction(store).objectStore(store).getAll();
      req.onsuccess = () => res(req.result); req.onerror = () => rej(req.error);
    });
  }
  async function idbClear(store) {
    const db = await idb();
    return new Promise((res, rej) => {
      const tx = db.transaction(store, 'readwrite');
      tx.objectStore(store).clear();
      tx.oncomplete = res; tx.onerror = () => rej(tx.error);
    });
  }
  async function idbDelete(store, key) {
    const db = await idb();
    return new Promise((res, rej) => {
      const tx = db.transaction(store, 'readwrite');
      tx.objectStore(store).delete(key);
      tx.oncomplete = res; tx.onerror = () => rej(tx.error);
    });
  }
  async function idbGetAllWithKeys(store) {
    const db = await idb();
    return new Promise((res, rej) => {
      const items = [];
      const req = db.transaction(store).objectStore(store).openCursor();
      req.onsuccess = e => {
        const cursor = e.target.result;
        if (cursor) { items.push({ key: cursor.key, value: cursor.value }); cursor.continue(); }
        else res(items);
      };
      req.onerror = () => rej(req.error);
    });
  }

  // ── Local XP tracking (drives the home XP ring instantly, even offline) ───
  // The daily-goal ring on the home page depends on the server's points_today,
  // which needs a round trip (and is unreachable offline). Flashcard reviews
  // are the one XP-earning action that works fully offline, so we mirror the
  // server's award amounts into localStorage the instant a review happens;
  // today.html takes max(serverPointsToday, this) so the ring never lags
  // behind what the learner just did on this device.
  const LOCAL_XP_KEY = 'xpLocalToday';
  // The learner's LOCAL day — the same boundary the server now uses for the XP
  // ring and the streak. (This used to be the UTC date, which rolls over at 5pm
  // in California, so the ring reset itself mid-afternoon.)
  function _localDateStr() {
    const d = new Date();
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }
  function bumpLocalXp(amount) {
    if (!amount) return;
    let cur = null;
    try { cur = JSON.parse(localStorage.getItem(LOCAL_XP_KEY) || 'null'); } catch {}
    const today = _localDateStr();
    if (!cur || cur.date !== today) cur = { date: today, xp: 0 };
    cur.xp += amount;
    try { localStorage.setItem(LOCAL_XP_KEY, JSON.stringify(cur)); } catch {}
  }

  // ── Offline / sync bar ────────────────────────────────────────────────────
  function updateOfflineBar(pendingCount) {
    const bar = document.getElementById('offline-bar');
    const badge = document.getElementById('sync-badge');
    if (!isOnline) {
      bar.classList.add('show');
      if (pendingCount > 0) { badge.textContent = `${pendingCount} pending sync`; badge.style.display = 'inline-block'; }
      else badge.style.display = 'none';
    } else if (pendingCount > 0) {
      bar.classList.add('show');
      bar.firstChild.textContent = 'Back online — syncing… ';
      badge.textContent = `${pendingCount} reviews`; badge.style.display = 'inline-block';
    } else {
      bar.classList.remove('show');
    }
  }

  // A queued review carries the LOCAL date it was answered. Without it the
  // server credits the streak to the day the sync happened, so studying on the
  // train at night and reconnecting next morning silently lost that day.
  async function queueReview(item, quality) {
    await idbPut('pending', {
      card_id: item.card_id, quality, face: item.face, studied_on: _localDateStr(),
    });
    updateOfflineBar((await idbGetAllWithKeys('pending')).length);
  }

  async function syncPending() {
    const entries = await idbGetAllWithKeys('pending');
    if (entries.length === 0) return;
    let synced = 0;
    for (const { key, value } of entries) {
      try {
        const res = await fetch(`/api/cards/${value.card_id}/review`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            quality: value.quality, face: value.face, studied_on: value.studied_on || null,
          }),
        });
        if (res.ok) { await idbDelete('pending', key); synced++; }
      } catch { break; }
    }
    if (synced > 0) {
      const remaining = entries.length - synced;
      if (remaining === 0) {
        showToast(`Synced ${synced} review${synced === 1 ? '' : 's'}.`);
        updateOfflineBar(0);
        document.getElementById('offline-bar').firstChild.textContent = 'Offline — using cached cards ';
      } else { updateOfflineBar(remaining); }
    }
  }

  window.addEventListener('online', () => { isOnline = true; syncPending(); });
  window.addEventListener('offline', () => { isOnline = false; idbGetAllWithKeys('pending').then(e => updateOfflineBar(e.length)); });

  // ── Settings ──────────────────────────────────────────────────────────────
  async function loadSettings() {
    try {
      settings = await fetch('/api/settings').then(r => r.json());
    } catch { settings = { new_cards_per_day: 20, default_target_lang: 'yue' }; }
  }

  // ── Labels ────────────────────────────────────────────────────────────────
  async function loadLabels() {
    try {
      const { labels } = await fetch('/api/labels').then(r => r.json());
      allLabels = labels;
    } catch { allLabels = []; }

    const pickerLabels = _filterPickerLabels();
    if (filterPicker) {
      filterPicker.setLabels(pickerLabels);
    } else {
      filterPicker = new LabelPicker(
        document.getElementById('filter-picker-wrap'),
        {
          allLabels: pickerLabels,
          initialIds: filterLabelId ? [filterLabelId] : [],
          allowCreate: false,
          mode: 'single',
          placeholder: 'Filter by label…',
          onChange: ids => {
            const id = [...ids][0] ?? null;
            if (id !== filterLabelId) {
              filterLabelId = id;
              loadSession('due');
            }
          },
        }
      );
    }
    if (inlinePicker) inlinePicker.setLabels(allLabels);
  }

  function filterQuery() {
    const ids = [];
    const deckLabel = _activeDeckId
      ? (_myDecks.find(d => d.id === _activeDeckId) || {}).import_label_id
      : null;
    if (deckLabel) ids.push(deckLabel);
    if (filterLabelId && filterLabelId !== deckLabel) ids.push(filterLabelId);
    if (!ids.length) return '';
    if (ids.length === 1) return `?label_id=${ids[0]}`;
    return `?label_ids=${ids.join(',')}`;
  }

  // ── Priority dots ─────────────────────────────────────────────────────────
  function renderPriorityDots(priority, interactive, onChange) {
    const wrap = document.createElement('div');
    wrap.className = 'priority-dots' + (interactive ? ' interactive' : '');
    wrap.title = `Priority: ${priority}/5`;
    for (let i = 1; i <= 5; i++) {
      const dot = document.createElement('span');
      dot.className = 'priority-dot' + (i <= priority ? ' filled' : '');
      if (interactive && onChange) {
        const level = i;
        dot.onclick = () => onChange(level);
      }
      wrap.appendChild(dot);
    }
    return wrap;
  }
  function updatePriorityDots(container, priority) {
    container.querySelectorAll('.priority-dot').forEach((dot, i) => {
      dot.classList.toggle('filled', i + 1 <= priority);
    });
    container.title = `Priority: ${priority}/5`;
  }

  // ── Boot ──────────────────────────────────────────────────────────────────
  async function loadLanguages() {
    try {
      const { languages: langs } = await fetch('/api/languages').then(r => r.json());
      languages = langs;
    } catch { languages = [{ code: 'yue', name: 'Cantonese', romanization: 'jyutping', logographic: true }]; }
    langByCode = Object.fromEntries(languages.map(l => [l.code, l]));
  }


  async function loadStreak() {
    try {
      const { streak, points } = await fetch('/api/streak').then(r => r.json());
      if (window.renderHeaderStats) { window.renderHeaderStats(streak || 0, points || 0); return; }
      const parts = [];
      const _flame = `<svg viewBox="0 0 16 20" width="13" height="16" aria-hidden="true"><path fill="#f4702a" d="M8 0C5.5 3.5 3 6.5 3 10.5a5 5 0 0010 0c0-2-.9-3.8-1.8-4.8-.4 1.6-1.1 2.6-2 2.2.4-2.5.2-5.2-1.2-7.9z"/></svg>`;
      const _star  = `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg>`;
      const fmtN = n => n >= 10000 ? Math.round(n/1000)+'k' : n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,'')+'k' : String(n);
      const numSpan = n => { const s=fmtN(n),f=n.toLocaleString(); return s===f?s:`<span style="cursor:pointer" title="${f}" onclick="this.textContent=this.textContent==='${s}'?'${f}':'${s}'">${s}</span>`; };
      const ic = n => `<span style="display:inline-flex;align-items:center;gap:3px">${n}</span>`;
      if (streak > 0) parts.push(ic(`${numSpan(streak)} ${_flame}`));
      if (points > 0) parts.push(ic(`${numSpan(points)} ${_star}`));
      if (parts.length) {
        document.querySelectorAll('.streak-display').forEach(el => {
          el.innerHTML = parts.join('<span style="opacity:0.4;margin:0 4px">·</span>');
          el.style.display = '';
        });
      }
    } catch {}
  }

  async function loadSession(mode = 'due') {
    show('loading');
    if (isOnline) await syncPending();
    const pending = await idbGetAllWithKeys('pending');
    updateOfflineBar(pending.length);

    let cards;
    if (isOnline) {
      try {
        const url = (mode === 'all' ? '/api/cards/all-faces' : '/api/cards/due') + filterQuery();
        const data = await fetch(url).then(r => r.json());
        cards = data.cards;
        if (mode === 'due') {
          sessionStats = {
            review_count: data.review_count || 0,
            new_count: data.new_count || 0,
            daily_new_used: data.daily_new_used || 0,
            daily_new_limit: data.daily_new_limit || settings.new_cards_per_day,
          };
          if (!filterLabelId && !_activeDeckId) {
            await idbClear('cards');
            for (const c of cards) await idbPut('cards', { ...c, id: `${c.card_id}-${c.face}` });
          }
        }
      } catch {
        showToast('Network error — using cached cards.');
        cards = await idbGetAll('cards');
        isOnline = false;
      }
    } else {
      cards = await idbGetAll('cards');
      if (filterLabelId) cards = cards.filter(c => (c.labels || []).some(l => l.id === filterLabelId));
    }

    const emptyTitle   = document.getElementById('empty-title');
    const emptyMsg     = document.getElementById('empty-msg');
    const caughtupMsg  = document.getElementById('caughtup-msg');
    const deckName = _activeDeckId ? (_myDecks.find(d => d.id === _activeDeckId) || {}).name : null;
    const labelName = filterLabelId ? (allLabels.find(l => l.id === filterLabelId) || {}).name : null;
    const filterDesc = [deckName, labelName].filter(Boolean).join(' + ') || null;
    if (filterDesc) {
      emptyTitle.textContent  = `No cards for "${filterDesc}"`;
      emptyMsg.textContent    = 'Try a different filter or translate something new.';
      caughtupMsg.textContent = `No cards due for "${filterDesc}".`;
    } else {
      emptyTitle.textContent  = 'No flashcards yet';
      emptyMsg.textContent    = 'Translate some phrases to build your deck.';
      caughtupMsg.textContent = 'No cards due right now.';
    }

    if (cards.length === 0) {
      if (mode === 'due') {
        if (isOnline) {
          const { cards: all } = await fetch('/api/cards/all-faces' + filterQuery()).then(r => r.json()).catch(() => ({ cards: [] }));
          if (all.length === 0) {
            show('empty');
          } else {
            const capReached = sessionStats.daily_new_used >= sessionStats.daily_new_limit;
            if (capReached && sessionStats.review_count === 0) {
              caughtupMsg.textContent = `Daily new card limit reached (${sessionStats.daily_new_limit}/day). Great work!`;
            }
            show('caughtup');
          }
        } else { show('caughtup'); }
      } else { show('empty'); }
      return;
    }

    queue = shuffle(cards);
    total = queue.length;
    idx = 0;
    reviewedFaces.clear();
    show('review');
    updateSessionStatsLine();
    showCard();
  }

  function updateSessionStatsLine() {
    const el = document.getElementById('session-stats-line');
    if (sessionStats.review_count > 0 || sessionStats.new_count > 0) {
      const parts = [];
      if (sessionStats.review_count > 0) parts.push(`${sessionStats.review_count} review${sessionStats.review_count !== 1 ? 's' : ''}`);
      if (sessionStats.new_count > 0) parts.push(`${sessionStats.new_count} new`);
      el.textContent = parts.join(' · ');
    } else {
      el.textContent = '';
    }
  }

  function shuffle(arr) {
    const a = [...arr];
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  function show(state) {
    ['loading', 'empty', 'caughtup', 'done', 'review'].forEach(s =>
      document.getElementById(`state-${s}`).style.display = s === state ? '' : 'none'
    );
    if (state === 'caughtup' || state === 'done') {
      loadCefrDistribution(state);
      startDuePoller();
    } else {
      stopDuePoller();
    }
  }

  function startDuePoller() {
    stopDuePoller();
    // Poll every 30 s. Learning cards can come due in as little as 1 min, so
    // 30 s means at most a 30-second delay before the session auto-resumes.
    _duePoller = setInterval(async () => {
      if (!isOnline || document.hidden) return;
      try {
        const url = '/api/cards/due-count' + filterQuery();
        const { count } = await fetch(url).then(r => r.json());
        if (count > 0) {
          stopDuePoller();
          loadSession('due');
        }
      } catch { /* ignore network errors, will retry */ }
    }, 30_000);
  }

  function stopDuePoller() {
    if (_duePoller !== null) { clearInterval(_duePoller); _duePoller = null; }
  }

  const CEFR_LEVELS = ['A1', 'A2', 'B1', 'B2', 'C1', 'C2'];
  const CEFR_COLORS = { A1: '#16a34a', A2: '#15803d', B1: '#ca8a04', B2: '#ea580c', C1: '#dc2626', C2: '#7c3aed' };

  async function loadCefrDistribution(state) {
    const containerId = state === 'caughtup' ? 'cefr-dist-caughtup' : 'cefr-dist-done';
    const container = document.getElementById(containerId);
    if (!container) return;
    try {
      const dist = await fetch('/api/cards/cefr-distribution').then(r => r.json());
      const labelled = CEFR_LEVELS.reduce((s, l) => s + (dist[l] || 0), 0);
      if (!labelled) { container.innerHTML = ''; return; }
      const max = Math.max(...CEFR_LEVELS.map(l => dist[l] || 0), 1);
      container.innerHTML = `<div class="cefr-dist-title">Vocab by CEFR level</div>` +
        CEFR_LEVELS.map(l => {
          const count = dist[l] || 0;
          const pct = Math.round((count / max) * 100);
          return `<div class="cefr-dist-row">
            <span class="cefr-dist-label cefr-badge cefr-${l}">${l}</span>
            <div class="cefr-dist-bar-wrap">
              <div class="cefr-dist-bar" style="width:${pct}%;background:${CEFR_COLORS[l]}"></div>
            </div>
            <span class="cefr-dist-count">${count}</span>
          </div>`;
        }).join('');
    } catch { container.innerHTML = ''; }
  }

  // ── Render card ───────────────────────────────────────────────────────────
  function showCard() {
    if (idx >= queue.length) { finishSession(); return; }
    const item = queue[idx];
    revealed = false;
    stopAudio();
    document.getElementById('card-answer').style.display = 'none';
    document.getElementById('pre-reveal').style.display = '';
    document.getElementById('post-reveal').style.display = 'none';
    document.getElementById('card-meta-actions').style.display = 'none';
    document.getElementById('ask-tutor-btn').style.display = '';   // restored after any inline edit

    const done = idx;
    document.getElementById('progress-label').textContent = `${done} / ${total}`;
    document.getElementById('progress-fill').style.width = `${(done / total) * 100}%`;

    const isNew = !item.first_seen_date && item.repetitions === 0;
    document.getElementById('new-badge').style.display = isNew ? '' : 'none';

    const langCode = item.target_lang || 'yue';
    applyScript(document.getElementById('flashcard'), langCode);
    // Cards are already scoped to the user's learning language — no need to label each one.
    document.getElementById('card-lang-tag').style.display = 'none';

    const confWrap = document.getElementById('card-confidence-bar-wrap');
    const confFill = document.getElementById('card-confidence-bar');
    if (item.first_seen_date) {
      const pct = Math.min(Math.round((item.interval_days / 90) * 100), 100);
      confFill.style.width = pct + '%';
      confWrap.style.display = '';
      confWrap.title = `Confidence: ${pct}%`;
    } else {
      confWrap.style.display = 'none';
    }

    const flagBtn = document.getElementById('tutor-flag-btn');
    flagBtn.classList.toggle('active', !!item.tutor_flag);

    const promptEl = document.getElementById('prompt-text');
    promptEl.className = 'card-prompt-text';
    promptEl.innerHTML = '';

    const labels = faceLabels(langCode);
    document.getElementById('prompt-label').textContent = labels[item.face] || item.face;

    if (item.face === 'source') {
      promptEl.classList.add('is-english');
      promptEl.textContent = item.source_text;
    } else if (item.face === 'target') {
      if (isLogographic(langCode)) promptEl.classList.add('is-chinese');
      promptEl.textContent = item.target_text;
    } else {
      // pronunciation face
      promptEl.classList.add('is-cantonese');
      const audioBtn = document.createElement('button');
      audioBtn.className = 'audio-btn';
      audioBtn.innerHTML = `${ICONS.volume} Play`;
      audioBtn.onclick = () => playCardAudio(item.card_id, audioBtn);
      promptEl.appendChild(audioBtn);
      if (isLogographic(langCode) && item.romanization && settings.audio_show_romanization !== false) {
        const rEl = document.createElement('div');
        rEl.className = 'jyutping-display';
        rEl.textContent = item.romanization;
        promptEl.appendChild(rEl);
      }
    }
  }

  // ── Reveal ────────────────────────────────────────────────────────────────
  function revealCard(silent) {
    if (revealed) return;
    revealed = true;
    const item = queue[idx];
    const langCode = item.target_lang || 'yue';
    const labels = faceLabels(langCode);
    const logographic = isLogographic(langCode);
    const answerEl = document.getElementById('card-answer');
    answerEl.innerHTML = '';

    ['source', 'target', 'pronunciation'].filter(f => f !== item.face).forEach(face => {
      const row = document.createElement('div');
      row.className = 'answer-row';
      const label = document.createElement('div');
      label.className = 'answer-label';
      label.textContent = labels[face];
      row.appendChild(label);
      if (face === 'source') {
        const val = document.createElement('div');
        val.className = 'answer-value'; val.textContent = item.source_text;
        row.appendChild(val);
      } else if (face === 'target') {
        const val = document.createElement('div');
        val.className = 'answer-value' + (logographic ? ' chinese' : '');
        val.textContent = item.target_text;
        row.appendChild(val);
        // On a pronunciation-face flip there is no pronunciation row in the answer,
        // so romanization must appear here instead. On source/target flips the
        // pronunciation row already carries it — don't duplicate.
        if (logographic && item.romanization && item.face === 'pronunciation') {
          const rEl = document.createElement('div');
          rEl.className = 'answer-value jyutping'; rEl.textContent = item.romanization;
          row.appendChild(rEl);
        }
      } else {
        // pronunciation
        if (logographic && item.romanization) {
          const rEl = document.createElement('div');
          rEl.className = 'answer-value jyutping'; rEl.textContent = item.romanization;
          row.appendChild(rEl);
        }
        const audioBtn = document.createElement('button');
        audioBtn.className = 'audio-btn'; audioBtn.style.marginTop = '6px';
        audioBtn.innerHTML = `${ICONS.volume} Play`;
        audioBtn.onclick = () => playCardAudio(item.card_id, audioBtn);
        row.appendChild(audioBtn);
      }
      answerEl.appendChild(row);
    });

    if (item.notes && item.notes.trim()) {
      const det = document.createElement('details');
      det.className = 'review-notes';
      const sum = document.createElement('summary'); sum.innerHTML = `${ICONS.note} Notes`;
      const body = document.createElement('div'); body.className = 'review-notes-body';
      body.textContent = item.notes;
      det.appendChild(sum); det.appendChild(body);
      answerEl.appendChild(det);
    }

    if (item.labels && item.labels.length) {
      const activeDeck = _activeDeck();
      const deckLabelName = activeDeck ? `📦 ${activeDeck.name}` : null;
      const visibleLabels = item.labels.filter(lbl => lbl.name !== deckLabelName);
      if (visibleLabels.length) {
        const row = document.createElement('div');
        row.className = 'review-labels';
        visibleLabels.forEach(lbl => {
          const chip = document.createElement('span');
          chip.className = 'label-chip-static'; chip.textContent = lbl.name;
          row.appendChild(chip);
        });
        answerEl.appendChild(row);
      }
    }

    if (item.cefr_level) {
      const cefrRow = document.createElement('div');
      cefrRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:4px;font-size:0.78rem;color:var(--text-muted)';
      cefrRow.innerHTML = `<span style="font-weight:600;text-transform:uppercase;letter-spacing:0.05em">CEFR</span><span class="cefr-badge cefr-${item.cefr_level}">${item.cefr_level}</span>`;
      answerEl.appendChild(cefrRow);
    }

    answerEl.style.display = 'flex';
    document.getElementById('pre-reveal').style.display = 'none';
    document.getElementById('post-reveal').style.display = '';
    document.getElementById('card-meta-actions').style.display = 'flex';
    updateMetaActions();

    if (!silent && item.face !== 'pronunciation') {
      const dummyBtn = { classList: { add: () => {}, remove: () => {} } };
      setTimeout(() => playCardAudio(item.card_id, dummyBtn), 300);
    }
  }

  function _refreshCardDisplay() {
    const wasRevealed = revealed;
    showCard();
    if (wasRevealed) revealCard(true);
  }

  function updateMetaActions() {
    const item = queue[idx];
    if (!item) return;
    const btn = document.getElementById('meta-tutor-btn');
    btn.classList.toggle('active', !!item.tutor_flag);
    btn.innerHTML = `${ICONS.bookmark} ${item.tutor_flag ? 'Flagged for tutor' : 'Flag for tutor'}`;

    const dotsWrap = document.getElementById('review-priority-dots');
    dotsWrap.innerHTML = '';
    dotsWrap.appendChild(renderPriorityDots(item.priority || 3, true, setReviewPriority));

    document.getElementById('meta-suspend-btn').style.display = item.priority === 1 ? '' : 'none';
  }

  async function setReviewPriority(level) {
    const item = queue[idx];
    if (!item) return;
    try {
      const res = await fetch(`/api/cards/${item.card_id}/priority`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ priority: level }),
      });
      if (!res.ok) throw new Error();
      queue.forEach(q => { if (q.card_id === item.card_id) q.priority = level; });
      const headerDots = document.getElementById('card-priority-dots').querySelector('.priority-dots');
      if (headerDots) updatePriorityDots(headerDots, level);
      updateMetaActions();
    } catch { showToast('Failed to update priority.'); }
  }

  // ── Rate ──────────────────────────────────────────────────────────────────
  async function rate(quality) {
    const item = queue[idx];
    let state = null;
    // Mirrors the server's award in main.py review_card (5 good / 7 easy) so
    // offline reviews can show + track XP immediately, without waiting on sync.
    const expectedXp = quality === 'easy' ? 7 : quality === 'good' ? 5 : 0;
    if (isOnline) {
      try {
        const res = await fetch(`/api/cards/${item.card_id}/review`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ quality, face: item.face }),
        });
        if (res.ok) {
          state = await res.json();
          if (state.xp > 0) { showXpPop(state.xp); bumpLocalXp(state.xp); }
        }
      } catch {
        isOnline = false;
        await queueReview(item, quality);
        if (expectedXp > 0) { showXpPop(expectedXp); bumpLocalXp(expectedXp); }
      }
    } else {
      await queueReview(item, quality);
      if (expectedXp > 0) { showXpPop(expectedXp); bumpLocalXp(expectedXp); }
    }
    reviewedFaces.add(`${item.card_id}-${item.face}`);
    if (_activeDeckId) _recordDeckSeen(_activeDeckId, item.card_id);
    idx++;
    // A card still in learning is due again in a minute or two — bring it back
    // around this session instead of waiting for the next one. (Offline, we have
    // no server state, so fall back to requeuing on "again".)
    const stillLearning = state ? state.learning_step !== null && state.learning_step !== undefined
                                : quality === 'again';
    if (stillLearning) {
      const requeued = { ...item };
      if (state) Object.assign(requeued, {
        interval_days: state.interval_days,
        ease_factor: state.ease_factor,
        repetitions: state.repetitions,
        learning_step: state.learning_step,
      });
      const insertAt = Math.min(queue.length, idx + LEARNING_REQUEUE_GAP);
      queue.splice(insertAt, 0, requeued);
      total = queue.length;
    }
    showCard();
    maybePromptDeckRating();
  }

  let _xpPopTimer;
  function showXpPop(xp) {
    const el = document.getElementById('xp-pop');
    if (!el) return;
    el.innerHTML = `+${xp} <svg style="display:inline-block;vertical-align:-0.15em" viewBox="0 0 20 20" width="12" height="12" aria-hidden="true"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg> XP`;
    el.classList.add('show');
    clearTimeout(_xpPopTimer);
    _xpPopTimer = setTimeout(() => el.classList.remove('show'), 1400);
  }

  function finishSession() {
    stopAudio();
    show('done');
    const n = reviewedFaces.size || total;
    document.getElementById('done-msg').textContent =
      `You reviewed ${n} flashcard${n === 1 ? '' : 's'}. Great work!`;
    // Offer a rating when a just-studied community deck is still unrated.
    const d = _activeDeck();
    const rateWrap = document.getElementById('done-rate');
    if (_isRatableDeck(d)) {
      const stars = document.getElementById('done-rate-stars');
      stars.dataset.deckId = d.id;
      stars.innerHTML = _rateStarsHtml(d.id, 0);
      rateWrap.style.display = '';
    } else {
      rateWrap.style.display = 'none';
    }
  }

  async function toggleTutorFlag() {
    const item = queue[idx];
    if (!item) return;
    const newVal = !item.tutor_flag;
    try {
      const res = await fetch(`/api/cards/${item.card_id}/tutor-flag`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ flagged: newVal }),
      });
      if (!res.ok) throw new Error();
      queue.forEach(q => { if (q.card_id === item.card_id) q.tutor_flag = newVal ? 1 : 0; });
      document.getElementById('tutor-flag-btn').classList.toggle('active', newVal);
      updateMetaActions();
      showToast(newVal ? 'Flagged for tutor review.' : 'Flag removed.');
    } catch { showToast('Failed to update flag.'); }
  }

  async function confirmReset() {
    const item = queue[idx];
    if (!item) return;
    if (!confirm('Reset this card to "new" and set priority to lowest? This clears all SRS progress for this card.')) return;
    try {
      const res = await fetch(`/api/cards/${item.card_id}/reset`, { method: 'POST' });
      if (!res.ok) throw new Error();
      const before = queue.length;
      queue = queue.filter((q, i) => !(q.card_id === item.card_id && i >= idx));
      total -= (before - queue.length);
      showToast('Card reset and deprioritized.');
      showCard();
    } catch { showToast('Failed to reset card.'); }
  }

  async function confirmSuspend() {
    const item = queue[idx];
    if (!item) return;
    if (!confirm('Suspend this card? It will no longer appear in study sessions.')) return;
    try {
      const res = await fetch(`/api/cards/${item.card_id}/suspend`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ suspended: true }),
      });
      if (!res.ok) throw new Error();
      const before = queue.length;
      queue = queue.filter((q, i) => !(q.card_id === item.card_id && i >= idx));
      total -= (before - queue.length);
      showToast('Card suspended.');
      showCard();
    } catch { showToast('Failed to suspend card.'); }
  }

  // ── Audio ─────────────────────────────────────────────────────────────────
  function playCardAudio(cardId, btn, isRetry) {
    stopAudio();
    const url = `/api/audio/${cardId}`;
    // Volume boost (Settings). Plays the decoded bytes through the gain graph
    // and leaves the <audio> element alone, so any failure — no boost, sleeping
    // graph, fetch or decode error — falls through to ordinary playback below.
    if (!isRetry) {
      let boosted = null;
      try { boosted = CantoShell.playBoosted(url, () => btn.classList.remove('playing')); } catch {}
      if (boosted) {
        btn.classList.add('playing');
        boosted.catch(() => { btn.classList.remove('playing'); playCardAudio(cardId, btn, true); });
        return;
      }
    }
    const el = new Audio(url);
    currentAudio = el;
    btn.classList.add('playing');
    btn.classList.remove('audio-dead');
    btn.disabled = false;
    el.onended = () => btn.classList.remove('playing');
    el.play().catch(err => {
      // Without this the button stays stuck in its 'playing' state forever
      // (onended never fires) and the failure is invisible.
      btn.classList.remove('playing');
      if (err && err.name === 'NotAllowedError') return;   // autoplay policy
      // A card's audio is synthesised lazily on first play, so the first
      // request can fail transiently upstream. Give it one more go.
      if (!isRetry && currentAudio === el) {
        setTimeout(() => {
          if (currentAudio === el) playCardAudio(cardId, btn, true);
        }, 600);
        return;
      }
      // Out of retries: show the button as dead instead of leaving a live-looking
      // control that silently does nothing every time it's tapped.
      btn.classList.add('audio-dead');
      btn.disabled = true;
      btn.title = "Audio isn't available for this card right now";
    });
  }

  function stopAudio() {
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    try { CantoShell.stopBoosted(); } catch {}
  }

  function renderCardListItem(card, container) {
    const item = document.createElement('div');
    item.className = 'card-list-item' + (card.suspended ? ' suspended' : '');
    item.id = `cli-${card.id}`;
    item._cardLabelIds = (card.labels || []).map(l => l.id);
    item._priority = card.priority || 3;
    item._tutorFlag = !!card.tutor_flag;
    item._suspended = !!card.suspended;
    item._targetLang = card.target_lang || 'yue';
    item._canonicalCardId = card.canonical_card_id || null;

    const langCode = item._targetLang;
    const logographic = isLogographic(langCode);
    const labels = faceLabels(langCode);
    applyScript(item, langCode);

    const labelsHtml = (card.labels || []).map(l =>
      `<span class="label-chip-static">${escHtml(l.name)}</span>`
    ).join('');
    const notesHtml = card.notes
      ? `<details class="card-list-notes"><summary>${ICONS.note} Note</summary><div>${escHtml(card.notes)}</div></details>`
      : '';
    const langTagHtml = languages.length > 1
      ? `<span class="lang-tag" style="margin-bottom:4px">${escHtml(labels.target)}</span>`
      : '';
    const romanRowHtml = (logographic && card.romanization)
      ? `<div class="jyutping">${escHtml(card.romanization)}</div>` : '';
    const romanInputHtml = logographic
      ? `<input class="edit-input" id="edit-roman-${card.id}" value="${escAttr(card.romanization || '')}" placeholder="${escAttr(romanizationFieldLabel(langCode) || 'Romanization')}">`
      : '';

    item.innerHTML = `
      <div class="card-display" id="display-${card.id}">
        ${langTagHtml}
        <div class="${logographic ? 'chinese' : ''}">${escHtml(card.target_text)}</div>
        ${romanRowHtml}
        ${card.classifier ? `<span class="card-classifier">${escHtml(card.classifier)}</span>` : ''}
        ${card.cefr_level ? `<span class="cefr-badge cefr-${card.cefr_level}">${escHtml(card.cefr_level)}</span>` : ''}
        <div class="english">${escHtml(card.source_text)}</div>
        ${card.canonical_card_id ? `<div class="card-canonical-ref" id="canonical-ref-${card.id}">Form of: <span class="canonical-target" id="canonical-target-${card.id}">…</span></div>` : ''}
        <div class="card-list-meta" id="meta-${card.id}"></div>
        ${labelsHtml ? `<div class="card-list-labels">${labelsHtml}</div>` : ''}
        ${notesHtml}
      </div>
      <div class="card-edit-form" id="edit-form-${card.id}" style="display:none">
        <input class="edit-input" id="edit-source-${card.id}" value="${escAttr(card.source_text)}" placeholder="English">
        <input class="edit-input" id="edit-target-${card.id}" value="${escAttr(card.target_text)}" placeholder="${escAttr(labels.target)}">
        ${romanInputHtml}
        <textarea class="edit-input" id="edit-notes-${card.id}" placeholder="Notes (optional)" rows="2">${escHtml(card.notes || '')}</textarea>
        <div class="inline-labels-section">
          <div class="inline-labels-title">Labels</div>
          <div id="edit-labels-${card.id}"></div>
        </div>
        <div class="inline-labels-section">
          <div class="inline-labels-title">Canonical / base form <span style="font-weight:400;font-size:0.8rem;color:var(--text-muted)">(e.g. infinitive for conjugations)</span></div>
          <div class="canonical-search-wrap">
            <input class="edit-input" id="edit-canonical-${card.id}" placeholder="Search cards…" autocomplete="off" oninput="filterCanonicalResults(${card.id})">
            <div class="canonical-results" id="canonical-results-${card.id}" style="display:none"></div>
            <div class="canonical-current" id="canonical-current-${card.id}"></div>
          </div>
        </div>
        <div class="edit-actions">
          <button class="edit-save-btn" onclick="saveCard(${card.id})">Save</button>
          <button class="edit-cancel-btn" onclick="cancelEdit(${card.id})">Cancel</button>
        </div>
      </div>
      <div class="card-actions" id="actions-${card.id}">
        <button class="edit-card-btn" onclick="startEdit(${card.id})" title="Edit" aria-label="Edit">${ICONS.pencil}</button>
        <button class="delete-card-btn" onclick="deleteCard(${card.id})" title="Delete" aria-label="Delete">${ICONS.trash}</button>
      </div>
    `;
    container.appendChild(item);
    renderCardListMeta(card.id);
  }

  function renderCardListMeta(cardId) {
    const item = document.getElementById(`cli-${cardId}`);
    const metaEl = document.getElementById(`meta-${cardId}`);
    if (!item || !metaEl) return;
    metaEl.innerHTML = '';

    const dots = renderPriorityDots(item._priority, true, async (level) => {
      try {
        const res = await fetch(`/api/cards/${cardId}/priority`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ priority: level }),
        });
        if (!res.ok) throw new Error();
        item._priority = level;
        updatePriorityDots(dots, level);
        queue.forEach(q => { if (q.card_id === cardId) q.priority = level; });
        if (queue[idx] && queue[idx].card_id === cardId) {
          updatePriorityDots(document.getElementById('card-priority-dots').querySelector('.priority-dots'), level);
        }
      } catch { showToast('Failed to update priority.'); }
    });
    metaEl.appendChild(dots);

    const flagBtn = document.createElement('button');
    flagBtn.className = 'meta-action-btn' + (item._tutorFlag ? ' active' : '');
    flagBtn.style.padding = '2px 8px';
    flagBtn.style.fontSize = '0.7rem';
    flagBtn.innerHTML = `${ICONS.bookmark} ${item._tutorFlag ? 'Flagged' : 'Tutor flag'}`;
    flagBtn.onclick = async () => {
      const newVal = !item._tutorFlag;
      try {
        const res = await fetch(`/api/cards/${cardId}/tutor-flag`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ flagged: newVal }),
        });
        if (!res.ok) throw new Error();
        item._tutorFlag = newVal;
        flagBtn.classList.toggle('active', newVal);
        flagBtn.innerHTML = `${ICONS.bookmark} ${newVal ? 'Flagged' : 'Tutor flag'}`;
        queue.forEach(q => { if (q.card_id === cardId) q.tutor_flag = newVal ? 1 : 0; });
      } catch { showToast('Failed to update flag.'); }
    };
    metaEl.appendChild(flagBtn);

    const suspendBtn = document.createElement('button');
    suspendBtn.className = 'meta-action-btn' + (item._suspended ? '' : ' danger');
    suspendBtn.style.padding = '2px 8px';
    suspendBtn.style.fontSize = '0.7rem';
    suspendBtn.textContent = item._suspended ? 'Unsuspend' : 'Suspend';
    suspendBtn.onclick = async () => {
      const newVal = !item._suspended;
      if (newVal && !confirm('Suspend this card? It won\'t appear in study sessions.')) return;
      try {
        const res = await fetch(`/api/cards/${cardId}/suspend`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ suspended: newVal }),
        });
        if (!res.ok) throw new Error();
        item._suspended = newVal;
        item.classList.toggle('suspended', newVal);
        suspendBtn.textContent = newVal ? 'Unsuspend' : 'Suspend';
        suspendBtn.classList.toggle('danger', !newVal);
        showToast(newVal ? 'Card suspended.' : 'Card unsuspended.');
      } catch { showToast('Failed to update.'); }
    };
    metaEl.appendChild(suspendBtn);

    const resetBtn = document.createElement('button');
    resetBtn.className = 'meta-action-btn danger';
    resetBtn.style.padding = '2px 8px';
    resetBtn.style.fontSize = '0.7rem';
    resetBtn.textContent = 'Reset';
    resetBtn.title = 'Reset to new & set priority to 1';
    resetBtn.onclick = async () => {
      if (!confirm('Reset this card to "new" and set priority to 1? This clears all SRS progress.')) return;
      try {
        const res = await fetch(`/api/cards/${cardId}/reset`, { method: 'POST' });
        if (!res.ok) throw new Error();
        item._priority = 1;
        renderCardListMeta(cardId);
        showToast('Card reset and deprioritized.');
      } catch { showToast('Failed to reset card.'); }
    };
    metaEl.appendChild(resetBtn);
  }

  function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function escAttr(s) { return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  // ── Canonical card helpers ────────────────────────────────────────────────

  function canonicalCardText(canonicalId) {
    if (!canonicalId) return null;
    const found = allCards.find(c => c.id === canonicalId);
    return found ? (found.target_text + (found.source_text ? ` (${found.source_text})` : '')) : `#${canonicalId}`;
  }

  function renderCanonicalCurrent(cardId, canonicalId) {
    const el = document.getElementById(`canonical-current-${cardId}`);
    if (!el) return;
    if (!canonicalId) { el.innerHTML = ''; return; }
    const label = canonicalCardText(canonicalId);
    el.innerHTML = `<span class="canonical-chip">${escHtml(label)}</span> <button class="canonical-clear-btn" onclick="clearCanonical(${cardId})">✕ Remove</button>`;
  }

  function clearCanonical(cardId) {
    const item = document.getElementById(`cli-${cardId}`);
    if (!item) return;
    item._pendingCanonicalId = null;
    renderCanonicalCurrent(cardId, null);
  }

  function filterCanonicalResults(cardId) {
    const input = document.getElementById(`edit-canonical-${cardId}`);
    const results = document.getElementById(`canonical-results-${cardId}`);
    const query = input.value.trim().toLowerCase();
    if (!query) { results.style.display = 'none'; return; }
    const item = document.getElementById(`cli-${cardId}`);
    const matches = allCards.filter(c =>
      c.id !== cardId &&
      (c.target_text.toLowerCase().includes(query) || c.source_text.toLowerCase().includes(query))
    ).slice(0, 8);
    if (!matches.length) { results.style.display = 'none'; return; }
    results.innerHTML = '';
    matches.forEach(c => {
      const row = document.createElement('div');
      row.className = 'canonical-result-row';
      row.textContent = c.target_text + (c.source_text ? ` — ${c.source_text}` : '');
      row.onclick = () => {
        item._pendingCanonicalId = c.id;
        renderCanonicalCurrent(cardId, c.id);
        input.value = '';
        results.style.display = 'none';
      };
      results.appendChild(row);
    });
    results.style.display = 'block';
  }

  function startEdit(id) {
    document.getElementById(`display-${id}`).style.display = 'none';
    document.getElementById(`actions-${id}`).style.display = 'none';
    document.getElementById(`edit-form-${id}`).style.display = 'block';
    document.getElementById(`edit-source-${id}`).focus();

    const item = document.getElementById(`cli-${id}`);
    const pickerContainer = document.getElementById(`edit-labels-${id}`);
    pickerContainer.innerHTML = '';
    item._editPicker = new LabelPicker(pickerContainer, {
      allLabels,
      initialIds: item._cardLabelIds || [],
      allowCreate: true,
      mode: 'multi',
      placeholder: 'Add label…',
      onLabelsRefreshed: labels => { allLabels = labels; if (filterPicker) filterPicker.setLabels(_filterPickerLabels()); },
    });

    // Populate canonical current state.
    item._pendingCanonicalId = item._canonicalCardId || null;
    renderCanonicalCurrent(id, item._pendingCanonicalId);
  }

  function cancelEdit(id) {
    document.getElementById(`display-${id}`).style.display = '';
    document.getElementById(`actions-${id}`).style.display = '';
    document.getElementById(`edit-form-${id}`).style.display = 'none';
    const item = document.getElementById(`cli-${id}`);
    item._editPicker = null;
  }

  async function saveCard(id) {
    const sourceText = document.getElementById(`edit-source-${id}`).value.trim();
    const targetText = document.getElementById(`edit-target-${id}`).value.trim();
    const romanEl = document.getElementById(`edit-roman-${id}`);
    const romanization = romanEl ? romanEl.value.trim() : '';
    const notes = document.getElementById(`edit-notes-${id}`).value;
    const item = document.getElementById(`cli-${id}`);
    const labelIds = item._editPicker ? [...item._editPicker.getSelected()] : [];
    const langCode = item._targetLang;
    const logographic = isLogographic(langCode);

    if (!sourceText || !targetText) { showToast('Source and target text are required.'); return; }
    if (logographic && !romanization) { showToast('Romanization is required for this language.'); return; }

    const btn = document.querySelector(`#edit-form-${id} .edit-save-btn`);
    btn.disabled = true; btn.textContent = 'Saving…';
    const pendingCanonicalId = item._pendingCanonicalId !== undefined
      ? item._pendingCanonicalId : item._canonicalCardId;

    try {
      const res = await fetch(`/api/cards/${id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: sourceText,
          target_text: targetText,
          romanization: romanization,
          notes,
          label_ids: labelIds,
        }),
      });
      if (!res.ok) throw new Error();

      // Update canonical if it changed.
      if (pendingCanonicalId !== item._canonicalCardId) {
        await fetch(`/api/cards/${id}/canonical`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ canonical_card_id: pendingCanonicalId }),
        });
      }

      const display = document.getElementById(`display-${id}`);
      const targetEl = display.querySelector(logographic ? '.chinese' : 'div:not(.lang-tag):not(.english):not(.jyutping):not(.card-list-meta):not(.card-list-labels)');
      // Easier: re-render the card item entirely from new data.
      const cardData = {
        id,
        source_text: sourceText,
        target_text: targetText,
        romanization,
        notes,
        priority: item._priority,
        tutor_flag: item._tutorFlag ? 1 : 0,
        suspended: item._suspended ? 1 : 0,
        target_lang: langCode,
        labels: allLabels.filter(l => labelIds.includes(l.id)),
        canonical_card_id: pendingCanonicalId,
      };
      const parent = item.parentNode;
      const next = item.nextSibling;
      item.remove();
      const tmp = document.createElement('div');
      renderCardListItem(cardData, tmp);
      const newItem = tmp.firstElementChild;
      parent.insertBefore(newItem, next);

      showToast('Card updated.');
      queue.forEach(q => {
        if (q.card_id === id) {
          q.source_text = sourceText; q.target_text = targetText; q.romanization = romanization;
          q.notes = notes; q.labels = cardData.labels;
        }
      });
      loadLabels();
    } catch { showToast('Failed to save card.'); }
    finally { btn.disabled = false; btn.textContent = 'Save'; }
  }

  async function deleteCard(id) {
    if (!confirm('Delete this card and all its progress?')) return;
    await fetch(`/api/cards/${id}`, { method: 'DELETE' });
    document.getElementById(`cli-${id}`)?.remove();
    const qi = queue.findIndex(c => c.card_id === id);
    if (qi !== -1) {
      queue.splice(qi, 1);
      total = Math.max(0, total - 1);
      if (qi < idx) idx = Math.max(0, idx - 1);
    }
    loadLabels();
  }

  // ── Deck picker ────────────────────────────────────────────────────────────
  async function loadDecks() {
    try {
      const { decks } = await fetch('/api/decks/mine').then(r => r.json());
      _myDecks = decks;
    } catch { _myDecks = []; }
  }

  function toggleDeckPicker() {
    const picker = document.getElementById('deck-picker');
    const btn = document.getElementById('deck-toggle');
    if (picker.style.display === 'none') {
      renderDeckPicker();
      picker.style.display = '';
      btn.classList.add('open');
    } else {
      picker.style.display = 'none';
      btn.classList.remove('open');
    }
  }

  function renderDeckPicker() {
    const list = document.getElementById('deck-picker-list');
    const rows = [`<div class="deck-pick-row${_activeDeckId === null ? ' active' : ''}" data-deck-id="">
      <span class="deck-pick-check"></span>
      <span class="deck-pick-name">All cards</span>
    </div>`];
    _myDecks.forEach(d => {
      if (!d.import_label_id) return;
      const stars = (!d.is_creator && d.import_label_id)
        ? `<span class="deck-rate-stars deck-pick-stars" data-deck-id="${d.id}" onclick="event.stopPropagation()">${_rateStarsHtml(d.id, d.user_rating || 0)}</span>`
        : '';
      rows.push(`<div class="deck-pick-row${_activeDeckId === d.id ? ' active' : ''}" data-deck-id="${d.id}" data-label-id="${d.import_label_id}">
        <span class="deck-pick-check"></span>
        <span class="deck-pick-name">${escHtml(d.name)}</span>
        <span class="deck-pick-meta">${d.card_count} cards</span>
        ${stars}
      </div>`);
    });
    list.innerHTML = rows.join('');
    list.querySelectorAll('.deck-pick-row').forEach(row => {
      row.onclick = () => selectDeck(row);
    });
  }

  document.addEventListener('click', e => {
    const picker = document.getElementById('deck-picker');
    const toggle = document.getElementById('deck-toggle');
    if (picker.style.display !== 'none' &&
        !picker.contains(e.target) && !toggle.contains(e.target)) {
      picker.style.display = 'none';
      toggle.classList.remove('open');
    }
  });

  function _deckLabelId() {
    if (!_activeDeckId) return null;
    return (_myDecks.find(d => d.id === _activeDeckId) || {}).import_label_id || null;
  }

  function _filterPickerLabels() {
    const hide = _deckLabelId();
    return hide ? allLabels.filter(l => l.id !== hide) : allLabels;
  }

  function selectDeck(row) {
    const deckId = row.dataset.deckId ? parseInt(row.dataset.deckId) : null;
    _activeDeckId = deckId;
    filterLabelId = null;
    if (filterPicker) {
      filterPicker.setLabels(_filterPickerLabels());
      filterPicker.setSelected([]);
    }
    document.getElementById('deck-toggle-label').textContent =
      deckId ? row.querySelector('.deck-pick-name').textContent : 'All cards';
    document.getElementById('deck-picker').style.display = 'none';
    document.getElementById('deck-toggle').classList.remove('open');
    loadSession('due');
  }

  // ── Deck rating ──────────────────────────────────────────────────────────
  function _activeDeck() { return _myDecks.find(d => d.id === _activeDeckId) || null; }

  // An imported community deck (not one you created) that you haven't rated yet.
  function _isRatableDeck(d) { return !!(d && d.import_label_id && !d.is_creator && !d.user_rating); }

  function _rateStarsHtml(deckId, current) {
    let h = '';
    for (let i = 1; i <= 5; i++) {
      h += `<span class="star${current && i <= current ? ' filled' : ''}" data-star="${i}" onclick="rateDeckHere(${deckId},${i},event)">★</span>`;
    }
    return h;
  }

  async function rateDeckHere(deckId, rating, ev) {
    if (ev) ev.stopPropagation();
    try {
      await fetch(`/api/decks/${deckId}/rate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rating }),
      });
      const d = _myDecks.find(x => x.id === deckId);
      if (d) d.user_rating = rating;
      localStorage.setItem(`deckRatePrompted:${deckId}`, '1');
      // Reflect the new rating on every visible star group for this deck.
      document.querySelectorAll(`.deck-rate-stars[data-deck-id="${deckId}"]`).forEach(el => {
        el.innerHTML = _rateStarsHtml(deckId, rating);
      });
      showToast('Thanks for rating!');
      if (document.getElementById('deck-rate-overlay').style.display !== 'none') {
        setTimeout(closeDeckRateModal, 700);
      }
      const doneRate = document.getElementById('done-rate');
      if (doneRate && doneRate.querySelector(`[data-deck-id="${deckId}"]`)) {
        setTimeout(() => { doneRate.style.display = 'none'; }, 700);
      }
    } catch { showToast('Rating failed'); }
  }

  // Cumulative distinct cards a user has worked through in a given deck, so the
  // rate prompt survives across study sessions.
  function _deckSeenKey(id) { return `deckSeen:${id}`; }
  function _deckSeenCount(id) {
    try { return new Set(JSON.parse(localStorage.getItem(_deckSeenKey(id)) || '[]')).size; }
    catch { return 0; }
  }
  function _recordDeckSeen(deckId, cardId) {
    try {
      const key = _deckSeenKey(deckId);
      const set = new Set(JSON.parse(localStorage.getItem(key) || '[]'));
      set.add(cardId);
      localStorage.setItem(key, JSON.stringify([...set]));
    } catch {}
  }

  function maybePromptDeckRating() {
    const d = _activeDeck();
    if (!_isRatableDeck(d)) return;
    if (localStorage.getItem(`deckRatePrompted:${d.id}`)) return;
    if (!d.card_count) return;
    // Prompt after 50 cards OR half the deck, whichever comes first.
    const threshold = Math.min(50, Math.max(1, Math.ceil(0.5 * d.card_count)));
    if (_deckSeenCount(d.id) >= threshold) openDeckRateModal(d.id);
  }

  function openDeckRateModal(deckId) {
    const d = _myDecks.find(x => x.id === deckId);
    if (!d) return;
    localStorage.setItem(`deckRatePrompted:${deckId}`, '1');  // one prompt only — don't nag
    document.getElementById('deck-rate-title').textContent = `Enjoying "${d.name}"?`;
    const stars = document.getElementById('deck-rate-modal-stars');
    stars.dataset.deckId = deckId;
    stars.innerHTML = _rateStarsHtml(deckId, d.user_rating || 0);
    document.getElementById('deck-rate-overlay').style.display = '';
  }
  function closeDeckRateModal() {
    document.getElementById('deck-rate-overlay').style.display = 'none';
  }

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg; t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 4000);
  }

  // ── Inline card edit (review) ─────────────────────────────────────────────
  function startInlineEdit() {
    const item = queue[idx];
    const langCode = item.target_lang || 'yue';
    const logographic = isLogographic(langCode);
    const labels = faceLabels(langCode);

    document.getElementById('inline-source').value = item.source_text || '';
    document.getElementById('inline-target').value = item.target_text || '';
    document.getElementById('inline-target').placeholder = labels.target;
    document.getElementById('inline-target').style.fontFamily = logographic
      ? { chinese: 'var(--chinese-font)', devanagari: 'var(--devanagari-font)', telugu: 'var(--telugu-font)', hangul: 'var(--hangul-font)', greek: 'var(--greek-font)', thai: 'var(--thai-font)', hebrew: 'var(--hebrew-font)' }[scriptFamily(langCode)] || 'var(--chinese-font)'
      : 'inherit';
    const romanEl = document.getElementById('inline-roman');
    if (logographic) {
      romanEl.style.display = '';
      romanEl.value = item.romanization || '';
      romanEl.placeholder = romanizationFieldLabel(langCode) || 'Romanization';
    } else {
      romanEl.style.display = 'none';
      romanEl.value = '';
    }
    document.getElementById('inline-notes').value = item.notes || '';

    const pickerContainer = document.getElementById('inline-label-picker');
    pickerContainer.innerHTML = '';
    inlinePicker = new LabelPicker(pickerContainer, {
      allLabels,
      initialIds: (item.labels || []).map(l => l.id),
      allowCreate: true,
      mode: 'multi',
      placeholder: 'Add label…',
      onLabelsRefreshed: lbls => { allLabels = lbls; if (filterPicker) filterPicker.setLabels(_filterPickerLabels()); },
    });

    // The editor (and its Delete button) is reused for every card — reset the
    // Delete button so a prior deletion doesn't leave it stuck on "Deleting…".
    const delBtn = document.querySelector('#card-inline-edit .edit-delete-btn');
    if (delBtn) { delBtn.disabled = false; delBtn.textContent = 'Delete'; }

    document.getElementById('prompt-text').style.display    = 'none';
    document.getElementById('card-answer').style.display    = 'none';
    document.getElementById('pre-reveal').style.display     = 'none';
    document.getElementById('post-reveal').style.display    = 'none';
    document.getElementById('card-inline-edit').style.display = 'flex';
    document.getElementById('ask-tutor-btn').style.display = 'none';   // avoid overlapping Save/Cancel
    document.getElementById('inline-source').focus();
  }

  async function saveInlineEdit() {
    const item = queue[idx];
    const langCode = item.target_lang || 'yue';
    const logographic = isLogographic(langCode);

    const sourceText = document.getElementById('inline-source').value.trim();
    const targetText = document.getElementById('inline-target').value.trim();
    const romanization = logographic ? document.getElementById('inline-roman').value.trim() : '';
    const notes = document.getElementById('inline-notes').value;
    const labelIds = inlinePicker ? [...inlinePicker.getSelected()] : [];

    if (!sourceText || !targetText) { showToast('Source and target text are required.'); return; }
    if (logographic && !romanization) { showToast('Romanization is required.'); return; }

    const btn = document.querySelector('#card-inline-edit .edit-save-btn');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
      const res = await fetch(`/api/cards/${item.card_id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: sourceText,
          target_text: targetText,
          romanization,
          notes,
          label_ids: labelIds,
        }),
      });
      if (!res.ok) throw new Error();
      const selectedLabels = allLabels.filter(l => labelIds.includes(l.id));
      queue.forEach(q => {
        if (q.card_id === item.card_id) {
          q.source_text = sourceText; q.target_text = targetText; q.romanization = romanization;
          q.notes = notes; q.labels = selectedLabels;
        }
      });
      cancelInlineEdit();
      showToast('Card updated.');
      loadLabels();
    } catch { showToast('Failed to save.'); }
    finally { btn.disabled = false; btn.textContent = 'Save'; }
  }

  async function regenerateAiFields() {
    const item = queue[idx];
    if (!item) return;
    const btn = document.getElementById('inline-regen-btn');
    const label = btn.querySelector('span');
    btn.disabled = true; btn.classList.add('spin'); label.textContent = 'Regenerating…';
    try {
      const res = await fetch(`/api/cards/${item.card_id}/regenerate`, { method: 'POST' });
      if (res.status === 402 || res.status === 429) {
        showToast(res.status === 402
          ? "You've hit your AI usage limit — add your own API key in Settings."
          : 'Too many requests — try again shortly.');
        label.textContent = 'Regenerate AI note';
        return;
      }
      if (!res.ok) throw new Error();
      const data = await res.json();
      if (typeof data.notes === 'string') document.getElementById('inline-notes').value = data.notes;
      const romanEl = document.getElementById('inline-roman');
      if (data.romanization && romanEl.style.display !== 'none') romanEl.value = data.romanization;
      label.textContent = '✓ Regenerated · review & Save';
      setTimeout(() => { label.textContent = 'Regenerate AI note'; }, 2600);
    } catch {
      showToast("Couldn't regenerate — try again.");
      label.textContent = 'Regenerate AI note';
    } finally {
      btn.disabled = false; btn.classList.remove('spin');
    }
  }

  async function deleteInlineCard() {
    const item = queue[idx];
    if (!item) return;
    if (!confirm('Delete this card from your deck? This removes all its review faces and cannot be undone.')) return;
    const btn = document.querySelector('#card-inline-edit .edit-delete-btn');
    btn.disabled = true; btn.textContent = 'Deleting…';
    try {
      const res = await fetch(`/api/cards/${item.card_id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error();
      const delId = item.card_id;
      // A card can sit in the queue multiple times (one entry per face / requeue).
      // Drop them all; decrement idx for each removed entry BEFORE the cursor so it
      // lands on the next card (the current entry at idx is removed but not counted,
      // which makes the following card slide into idx).
      let removedBefore = 0;
      for (let i = queue.length - 1; i >= 0; i--) {
        if (queue[i].card_id === delId) {
          queue.splice(i, 1);
          if (i < idx) removedBefore++;
        }
      }
      idx -= removedBefore;
      total = queue.length;
      inlinePicker = null;
      document.getElementById('card-inline-edit').style.display = 'none';
      document.getElementById('prompt-text').style.display = '';
      showToast('Card deleted.');
      loadLabels();
      showCard();
    } catch {
      showToast('Failed to delete.');
      btn.disabled = false; btn.textContent = 'Delete';
    }
  }

  function cancelInlineEdit() {
    inlinePicker = null;
    document.getElementById('card-inline-edit').style.display  = 'none';
    document.getElementById('prompt-text').style.display        = '';
    if (revealed) {
      document.getElementById('card-answer').style.display  = 'flex';
      document.getElementById('post-reveal').style.display  = '';
      document.getElementById('card-meta-actions').style.display = 'flex';
    } else {
      document.getElementById('pre-reveal').style.display = '';
    }
    // Re-render so the visible side reflects updated values.
    showCard();
  }

  document.addEventListener('click', e => {
    if (!e.target.closest('header')) {
      const dd = document.getElementById('nav-dropdown');
      if (dd) dd.classList.remove('open');
    }
  });

  // ── Ask the tutor (contextual study Q&A pop-over) ──────────────────────────
  let _askHistory = [];   // ephemeral, this pop-over only: [{role,text}]
  let _askSending = false;
  let _askCard = null;
  let _askCardId = null;

  function _askStatus(item) {
    if (!item) return '';
    if (!item.first_seen_date && (item.repetitions || 0) === 0) return 'brand new (not learned yet)';
    if (item.learning_step !== null && item.learning_step !== undefined) return 'currently learning it';
    const d = item.interval_days || 0;
    if (d <= 0) return 'in review';
    if (d < 21) return `in review (interval ~${Math.round(d)} days)`;
    return `well known (interval ~${Math.round(d)} days)`;
  }

  function openAskTutor() {
    const item = queue[idx];
    if (!item) return;
    const langCode = item.target_lang || 'yue';
    const sameCard = _askCardId === item.card_id;
    _askCard = {
      target_text: item.target_text || '',
      source_text: item.source_text || '',
      romanization: item.romanization || '',
      notes: item.notes || '',
      status: _askStatus(item),
      card_id: item.card_id,
    };
    _askCard._lang = langCode;
    _askCardId = item.card_id;

    const thread = document.getElementById('ask-thread');
    applyScript(thread, langCode);
    const head = document.getElementById('ask-head-card');
    head.textContent = item.target_text ? `· ${item.target_text}` : '';

    if (!sameCard) {
      _askHistory = [];
      const qs = ['Why this word here?', 'Use it in a sentence', 'Related words?', 'How do I remember this?'];
      thread.innerHTML =
        `<div class="ask-empty">Ask anything about <b>${escHtml(item.target_text || 'this card')}</b>` +
        (item.source_text ? ` (${escHtml(item.source_text)})` : '') + `.` +
        `<div class="ask-suggest">` +
        qs.map(q => `<button class="ask-chip" onclick="_askPreset(this)">${escHtml(q)}</button>`).join('') +
        `</div></div>`;
    }

    document.getElementById('ask-overlay').classList.add('open');
    const inp = document.getElementById('ask-input');
    inp.value = ''; inp.style.height = 'auto';
    document.getElementById('ask-send').disabled = true;
    setTimeout(() => inp.focus(), 50);
  }

  function closeAskTutor() {
    document.getElementById('ask-overlay').classList.remove('open');
  }

  function _askPreset(btn) {
    const inp = document.getElementById('ask-input');
    inp.value = btn.textContent;
    document.getElementById('ask-send').disabled = false;
    sendAskTutor();
  }

  function _askBubble(role, text) {
    const thread = document.getElementById('ask-thread');
    const empty = thread.querySelector('.ask-empty');
    if (empty) empty.remove();
    const b = document.createElement('div');
    b.className = 'ask-bubble ' + role;
    if (role === 'tutor') { b.innerHTML = `<span class="ask-tgt">${escHtml(text)}</span>`; }
    else b.textContent = text;
    thread.appendChild(b);
    thread.scrollTop = thread.scrollHeight;
    return b;
  }

  function _askRenderNewItems(container, items) {
    if (!items || !items.length) return;
    const wrap = document.createElement('div');
    wrap.className = 'ask-newitems';
    items.forEach(it => {
      const row = document.createElement('div');
      row.className = 'ask-ni';
      const main = document.createElement('div');
      main.className = 'ask-ni-main';
      main.innerHTML =
        `<div class="ask-ni-tgt">${escHtml(it.target_text)}` +
        (it.romanization ? ` <span class="ask-ni-rom">${escHtml(it.romanization)}</span>` : '') + `</div>` +
        `<div class="ask-ni-en">${escHtml(it.english)}</div>`;
      const add = document.createElement('button');
      add.className = 'ask-ni-add';
      add.textContent = '＋ Add';
      add.onclick = () => _askAddItem(it, add);
      row.appendChild(main); row.appendChild(add);
      wrap.appendChild(row);
    });
    container.appendChild(wrap);
    document.getElementById('ask-thread').scrollTop = 1e9;
  }

  function _askRenderCardUpdate(container, updates) {
    if (!updates || !Object.keys(updates).length) return;
    const wrap = document.createElement('div');
    wrap.className = 'ask-card-update';
    wrap.innerHTML = '<div class="ask-cu-title">Suggested card update</div>';
    const labels = { notes_append: 'Add to notes', notes: 'Replace notes', romanization: 'Romanization', source_text: 'Translation' };
    for (const [field, val] of Object.entries(updates)) {
      if (!val) continue;
      const row = document.createElement('div');
      row.className = 'ask-cu-field';
      row.innerHTML = `<span class="ask-cu-label">${escHtml(labels[field] || field)}:</span> <span class="ask-cu-val">${escHtml(val)}</span>`;
      wrap.appendChild(row);
    }
    const btn = document.createElement('button');
    btn.className = 'ask-cu-apply';
    btn.textContent = 'Apply to card';
    btn.onclick = () => _askApplyUpdate(updates, btn);
    wrap.appendChild(btn);
    container.appendChild(wrap);
    document.getElementById('ask-thread').scrollTop = 1e9;
  }

  async function _askApplyUpdate(updates, btn) {
    const cardId = _askCard && _askCard.card_id;
    if (!cardId) return;
    btn.disabled = true; btn.textContent = 'Applying…';
    // Notes are additive by default: `notes_append` is concatenated to whatever's
    // already on the card; `notes` (replace) is only sent to fix an actual error.
    const existingNotes = _askCard.notes || '';
    let newNotes = existingNotes;
    if (updates.notes_append) {
      newNotes = existingNotes ? `${existingNotes}\n${updates.notes_append}` : updates.notes_append;
    } else if (updates.notes !== undefined) {
      newNotes = updates.notes;
    }
    const notesChanged = newNotes !== existingNotes;
    const merged = {
      source_text: updates.source_text || _askCard.source_text || '',
      target_text: _askCard.target_text || '',
      romanization: updates.romanization || _askCard.romanization || '',
      notes: newNotes,
    };
    try {
      const res = await fetch(`/api/cards/${cardId}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(merged),
      });
      if (!res.ok) throw new Error('update failed');
      btn.textContent = '✓ Applied';
      if (updates.source_text) _askCard.source_text = updates.source_text;
      if (updates.romanization) _askCard.romanization = updates.romanization;
      if (notesChanged) _askCard.notes = newNotes;
      for (const q of queue) {
        if (q.card_id !== cardId) continue;
        if (updates.source_text) q.source_text = updates.source_text;
        if (updates.romanization) q.romanization = updates.romanization;
        if (notesChanged) q.notes = newNotes;
      }
      _refreshCardDisplay();
    } catch {
      btn.disabled = false; btn.textContent = 'Apply to card';
    }
  }

  async function _askAddItem(it, btn) {
    btn.disabled = true; btn.textContent = '…';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: it.english, target_text: it.target_text,
          romanization: it.romanization || '', notes: it.notes || '',
          target_lang: (_askCard && _askCard._lang) || 'yue',
        }),
      });
      if (!res.ok) throw new Error('add failed');
      btn.textContent = '✓ Added';
    } catch {
      btn.disabled = false; btn.textContent = '＋ Add';
    }
  }

  async function sendAskTutor() {
    const inp = document.getElementById('ask-input');
    const q = inp.value.trim();
    if (!q || _askSending) return;
    _askSending = true;
    document.getElementById('ask-send').disabled = true;
    inp.value = ''; inp.style.height = 'auto';

    _askBubble('user', q);
    const typing = document.createElement('div');
    typing.className = 'ask-bubble tutor';
    typing.innerHTML = '<span class="ask-typing"><span></span><span></span><span></span></span>';
    const thread = document.getElementById('ask-thread');
    thread.appendChild(typing); thread.scrollTop = thread.scrollHeight;

    try {
      const res = await fetch('/api/tutor/ask/stream', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: q, card: _askCard, history: _askHistory,
          lang: (_askCard && _askCard._lang) || undefined,
        }),
      });
      // Errors (limits, auth) come back as a normal status BEFORE the stream body.
      if (res.status === 402 || res.status === 429) {
        typing.remove();
        _askBubble('tutor', res.status === 402
          ? "You've hit your AI usage limit — add your own API key in Settings to keep going."
          : 'Slow down a moment — too many questions in a row. Try again shortly.');
        return;
      }
      if (!res.ok || !res.body) throw new Error('ask failed');

      // Keep the typing indicator until the first delta arrives, then swap to
      // a live bubble with a blinking cursor so the user sees text streaming in.
      let b = null, span = null, cursor = null;
      let replyText = '';
      let finalMsg = null;
      let errored = false;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          let evt;
          try { evt = JSON.parse(line); } catch { continue; }
          if (evt.type === 'delta') {
            if (!b) {
              typing.remove();
              b = _askBubble('tutor', '');
              span = b.querySelector('.ask-tgt');
              cursor = document.createElement('span');
              cursor.className = 'ask-cursor';
              span.after(cursor);
            }
            replyText += evt.text;
            span.textContent = replyText;
            thread.scrollTop = thread.scrollHeight;
          } else if (evt.type === 'final') {
            finalMsg = evt;
          } else if (evt.type === 'error') {
            errored = true;
            if (!b) { typing.remove(); b = _askBubble('tutor', ''); span = b.querySelector('.ask-tgt'); }
            span.textContent = evt.message || "Sorry — I couldn't answer that. Please try again.";
          }
        }
      }
      if (cursor) cursor.remove();
      if (!b) { typing.remove(); b = _askBubble('tutor', ''); span = b.querySelector('.ask-tgt'); }
      if (finalMsg && finalMsg.reply) {
        replyText = finalMsg.reply;
        span.textContent = replyText;
      }
      if (!replyText && !errored) span.textContent = '…';
      if (finalMsg) {
        _askRenderNewItems(b, finalMsg.new_items);
        _askRenderCardUpdate(b, finalMsg.card_updates);
      }
      _askHistory.push({ role: 'user', text: q });
      _askHistory.push({ role: 'tutor', text: replyText });
    } catch {
      if (typing.parentNode) typing.remove();
      _askBubble('tutor', "Sorry — I couldn't answer that. Please try again.");
    } finally {
      _askSending = false;
      document.getElementById('ask-send').disabled = !inp.value.trim();
    }
  }

  (function () {
    const inp = document.getElementById('ask-input');
    if (!inp) return;
    inp.addEventListener('input', () => {
      document.getElementById('ask-send').disabled = !inp.value.trim() || _askSending;
      inp.style.height = 'auto';
      inp.style.height = Math.min(inp.scrollHeight, 96) + 'px';
    });
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendAskTutor(); }
    });
  })();

  document.addEventListener('keydown', e => {
    if (document.getElementById('translate-panel').classList.contains('open')) {
      if (e.key === 'Escape') closeTranslatePanel();
      return;
    }
    if (document.getElementById('community-panel').classList.contains('open')) {
      if (e.key === 'Escape') closeCommunityPanel();
      return;
    }
    if (document.getElementById('ask-overlay').classList.contains('open')) {
      if (e.key === 'Escape') closeAskTutor();
      return;
    }
    if (document.getElementById('state-review').style.display === 'none') return;
    if (document.getElementById('card-inline-edit').style.display !== 'none') return;
    if (!revealed) {
      if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); revealCard(); }
    } else {
      if (e.key === '1') rate('again');
      else if (e.key === '2') rate('hard');
      else if (e.key === '3') rate('good');
      else if (e.key === '4') rate('easy');
    }
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  (async () => {
    await loadLanguages();
    await Promise.all([loadLabels(), loadSettings(), loadStreak(), loadDecks()]);
    const _params = new URLSearchParams(window.location.search);
    if (_params.has('label_id')) {
      const lid = parseInt(_params.get('label_id'), 10);
      if (!isNaN(lid)) {
        const deck = _myDecks.find(d => d.import_label_id === lid);
        if (deck) {
          _activeDeckId = deck.id;
          document.getElementById('deck-toggle-label').textContent = deck.name;
          if (filterPicker) filterPicker.setLabels(_filterPickerLabels());
        } else {
          filterLabelId = lid;
          if (filterPicker) filterPicker.setSelected([lid]);
        }
      }
    }
    loadSession(_params.get('study') === '1' ? 'all' : 'due');
  })();

  document.addEventListener('langchange', function () {
    loadSession('due');
    fetch('/api/settings').then(r => r.json()).then(s => {
      settings = s;
      _trTargetLang = s.default_target_lang || 'yue';
      _trSourceIsTarget = false;
      if (document.getElementById('translate-panel').classList.contains('open')) {
        trResetOutput();
        document.getElementById('tr-input').value = '';
        document.getElementById('tr-char-count').textContent = '0';
        trUpdateDirectionButtons();
      }
      if (document.getElementById('community-panel').classList.contains('open')) {
        _cdLoadDecks();
      }
    }).catch(() => {});
  });
