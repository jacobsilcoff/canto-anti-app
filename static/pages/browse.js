
  let _allCards = [];
  let _allLabels = [];
  let allLabels = [];          // alias used by the ported card-browser helpers
  let _selectedCards = new Set();
  let _currentTab = 'cards';
  let _defaultLang = '';
  let _populateMinScore = 0.55;
  let _langMap = {};
  let languages = [];
  let langByCode = {};
  let _cardsOffset = 0;
  let _cardsTotal = 0;
  let _cardsHasMore = false;
  let _cardsLoading = false;
  let _cardsRequestId = 0;
  let _cardsDebounce = null;
  const CARDS_PAGE_SIZE = 60;

  // SVG icons (subset used by the card browser).
  const ICONS = {
    pencil: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
    trash: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    note: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px;vertical-align:-2px"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>',
    bookmark: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>',
  };

  const STRENGTH_ORDER = { 'new': 0, learning: 1, familiar: 2, strong: 3 };
  function cardStrength(card) {
    if (!card.first_seen_date) return 'new';
    if (card.learning_step != null) return 'learning';
    if ((card.interval_days || 1) >= 21 && (card.ease_factor || 2.5) >= 2.0) return 'strong';
    return 'familiar';
  }
  function strengthBadge(card) {
    const s = cardStrength(card);
    const labels = { 'new': 'New', learning: 'Learning', familiar: 'Familiar', strong: 'Strong' };
    return `<span class="strength-badge strength-${s}">${labels[s]}</span>`;
  }

  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function escAttr(s) { return esc(s); }

  function langBadge(code) {
    const l = _langMap[code];
    if (!l) return code ? esc(code) : '';
    return `${l.flag || ''} ${esc(l.name || code)}`.trim();
  }

  // ── Language helpers (ported from the flashcards page) ──
  function langInfo(code) {
    return langByCode[code] || { code, name: code, romanization: null, logographic: false };
  }
  function isLogographic(code) { return !!langInfo(code).logographic; }
  function scriptFamily(code) { return langInfo(code).script_family || 'latin'; }
  function applyScript(el, code) {
    if (!el) return;
    el.classList.remove('script-chinese', 'script-devanagari', 'script-telugu', 'script-hangul', 'script-japanese', 'script-bengali', 'script-arabic', 'script-cyrillic', 'script-latin', 'script-greek', 'script-thai', 'script-hebrew');
    el.classList.add('script-' + scriptFamily(code));
  }
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

  // ── Priority dots ──
  function renderPriorityDots(priority, interactive, onChange) {
    const wrap = document.createElement('div');
    wrap.className = 'priority-dots' + (interactive ? ' interactive' : '');
    wrap.title = `Priority: ${priority}/5`;
    for (let i = 1; i <= 5; i++) {
      const dot = document.createElement('span');
      dot.className = 'priority-dot' + (i <= priority ? ' filled' : '');
      if (interactive && onChange) { const level = i; dot.onclick = () => onChange(level); }
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

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 4000);
  }

  // ── Init ──
  async function init() {
    const [settingsRes, langs] = await Promise.all([
      fetch('/api/settings').then(r => r.json()).catch(() => ({})),
      fetch('/api/languages').then(r => r.json()).catch(() => ({})),
    ]);
    _defaultLang = settingsRes.default_target_lang || '';
    _populateMinScore = settingsRes.populate_min_score !== undefined ? settingsRes.populate_min_score : 0.55;
    languages = langs.languages || langs || [];
    languages.forEach(l => { if (l && l.code) _langMap[l.code] = l; });
    langByCode = Object.fromEntries(languages.map(l => [l.code, l]));
    populateCardsLangFilter();
    populateCommDeckLangFilter();
    await loadLabels();
    const urlTab = new URLSearchParams(location.search).get('tab');
    if (urlTab && _tabViews[urlTab]) switchTab(urlTab);
    else await loadMyCards(true);
  }

  async function loadLabels() {
    try {
      const res = await fetch('/api/labels').then(r => r.json());
      _allLabels = allLabels = res.labels || [];
      const opts = _allLabels.map(l => `<option value="${l.id}">${esc(l.name)}</option>`).join('');
      document.getElementById('cards-label-filter').innerHTML = '<option value="">All labels</option>' + opts;
    } catch {}
  }

  // ── Labels tab ──
  let _dismissedMergeSuggestions = new Set();

  async function loadLabelsTab() {
    await loadLabels();
    renderLabelsList();
    loadMergeSuggestions();
  }

  function filterLabels() {
    const q = (document.getElementById('label-search')?.value || '').toLowerCase().trim();
    document.querySelectorAll('#labels-list .lbl-row').forEach(row => {
      const name = row.querySelector('.lbl-name')?.value || '';
      row.style.display = name.toLowerCase().includes(q) ? '' : 'none';
    });
  }

  function renderLabelsList() {
    const list = document.getElementById('labels-list');
    list.innerHTML = '';
    if (!allLabels.length) {
      list.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:24px">No labels yet. Create one above.</p>';
      return;
    }
    const mergeBar = document.createElement('div');
    mergeBar.id = 'labels-merge-bar';
    mergeBar.className = 'merge-bar';
    mergeBar.style.display = 'none';
    mergeBar.innerHTML = `
      <span style="font-size:0.85rem;color:var(--text-muted)">Merge selected into:</span>
      <select id="labels-merge-target" class="settings-select" style="flex:1;min-width:120px"></select>
      <button class="lbl-action" onclick="mergeSelectedLabels()">Merge</button>
      <button class="lbl-action danger" onclick="clearMergeSelection()" title="Cancel">✕</button>
    `;
    list.appendChild(mergeBar);

    allLabels.forEach(lbl => {
      const row = document.createElement('div');
      row.className = 'lbl-row';
      row.dataset.labelId = lbl.id;
      row.innerHTML = `
        <input type="checkbox" class="lbl-merge-cb" title="Select for merge">
        <input class="lbl-name" value="${escAttr(lbl.name)}" data-orig="${escAttr(lbl.name)}" maxlength="50">
        <span class="lbl-count">${lbl.card_count} card${lbl.card_count !== 1 ? 's' : ''}</span>
        <button class="lbl-confirm hidden" title="Confirm rename">✓</button>
        <button class="lbl-action" title="Generate vocab for this category">Populate</button>
        <button class="lbl-action danger" title="Delete label">${ICONS.trash}</button>
      `;
      const nameInput = row.querySelector('.lbl-name');
      const confirmBtn = row.querySelector('.lbl-confirm');
      nameInput.addEventListener('input', () => {
        const changed = nameInput.value.trim() !== nameInput.dataset.orig;
        confirmBtn.classList.toggle('hidden', !changed);
      });
      confirmBtn.onclick = () => renameLabelBrowse(lbl.id, nameInput.value, nameInput);
      const btns = row.querySelectorAll('.lbl-action');
      btns[0].onclick = () => populateLabel(lbl.id, lbl.name, row);
      btns[1].onclick = () => deleteLabelBrowse(lbl.id, lbl.name);
      row.querySelector('.lbl-merge-cb').onchange = updateMergeBar;
      list.appendChild(row);
    });
  }

  function updateMergeBar() {
    const checks = [...document.querySelectorAll('#labels-list .lbl-merge-cb:checked')];
    const bar = document.getElementById('labels-merge-bar');
    if (!bar) return;
    if (checks.length < 2) { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    const checkedIds = new Set(checks.map(c => parseInt(c.closest('.lbl-row').dataset.labelId)));
    const sel = document.getElementById('labels-merge-target');
    sel.innerHTML = allLabels
      .filter(l => checkedIds.has(l.id))
      .map(l => `<option value="${l.id}">${escHtml(l.name)}</option>`)
      .join('');
  }

  function clearMergeSelection() {
    document.querySelectorAll('#labels-list .lbl-merge-cb').forEach(c => c.checked = false);
    const bar = document.getElementById('labels-merge-bar');
    if (bar) bar.style.display = 'none';
  }

  async function mergeSelectedLabels() {
    const checks = [...document.querySelectorAll('#labels-list .lbl-merge-cb:checked')];
    if (checks.length < 2) return;
    const targetId = parseInt(document.getElementById('labels-merge-target').value);
    const sourceIds = checks.map(c => parseInt(c.closest('.lbl-row').dataset.labelId)).filter(id => id !== targetId);
    if (!sourceIds.length) { showToast('Select at least two different labels.'); return; }
    const targetName = allLabels.find(l => l.id === targetId)?.name || '';
    if (!confirm(`Merge ${sourceIds.length} label(s) into "${targetName}"? This cannot be undone.`)) return;
    try {
      const res = await fetch('/api/labels/merge', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_ids: sourceIds, target_id: targetId }),
      });
      if (!res.ok) throw new Error();
      await loadLabelsTab();
      showToast('Labels merged.');
    } catch { showToast('Could not merge labels.'); }
  }

  async function loadMergeSuggestions() {
    const container = document.getElementById('merge-suggestions');
    container.innerHTML = '';

    // Build the collapsible wrap immediately with a loading state inside
    const wrap = document.createElement('div');
    wrap.className = 'merge-sug-wrap';
    const collapsed = localStorage.getItem('merge_sug_collapsed') === '1';
    if (collapsed) wrap.classList.add('collapsed');
    wrap.innerHTML = `
      <div class="merge-sug-header">
        <span class="merge-sug-header-title">Suggested merges</span>
        <span class="merge-sug-header-count">…</span>
        <span class="sug-chevron">›</span>
      </div>
      <div class="merge-sug-body">
        <div class="merge-sug-loading" style="padding:12px 14px;color:var(--text-muted);font-size:0.82rem">Finding similar labels…</div>
      </div>
    `;
    wrap.querySelector('.merge-sug-header').onclick = () => {
      wrap.classList.toggle('collapsed');
      localStorage.setItem('merge_sug_collapsed', wrap.classList.contains('collapsed') ? '1' : '');
    };
    container.appendChild(wrap);
    const body = wrap.querySelector('.merge-sug-body');

    let groups;
    try {
      const res = await fetch('/api/labels/suggest-merges');
      ({ groups } = await res.json());
    } catch {
      wrap.remove();
      return;
    }

    // Remove loading indicator
    const loadingEl = body.querySelector('.merge-sug-loading');

    if (!groups || !groups.length) { wrap.remove(); return; }
    const filtered = groups.filter(g => {
      const key = g.labels.map(l => l.id).sort().join(',');
      return !_dismissedMergeSuggestions.has(key);
    });
    if (!filtered.length) { wrap.remove(); return; }

    // Show groups immediately (unreviewed), then fire off LLM reviews
    if (loadingEl) loadingEl.textContent = 'Reviewing with AI…';
    let reviewedCount = 0;

    for (const group of filtered) {
      const key = group.labels.map(l => l.id).sort().join(',');
      const card = document.createElement('div');
      card.className = 'merge-sug';
      card._labels = [...group.labels];
      card._dismissKey = key;
      _renderMergeCard(card, group.labels[0].name, '', wrap);
      body.appendChild(card);
    }
    _updateSuggestionsCount(wrap);

    // Progressive LLM review per group (cached server-side, fast on repeat loads)
    for (const card of [...body.querySelectorAll('.merge-sug')]) {
      const names = card._labels.map(l => l.name);
      try {
        const r = await fetch('/api/labels/review-merge', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ names }),
        });
        const review = await r.json();
        if (review.verdict === 'reject') {
          card.remove();
          _updateSuggestionsCount(wrap);
        } else {
          const nameInput = card.querySelector('.merge-sug-name');
          if (nameInput && review.suggested_name) nameInput.value = review.suggested_name;
          if (review.reason) {
            const existing = card.querySelector('.merge-sug-reason');
            if (existing) { existing.textContent = review.reason; }
            else {
              const r = document.createElement('div');
              r.className = 'merge-sug-reason';
              r.textContent = review.reason;
              card.querySelector('.merge-sug-labels').after(r);
            }
          }
        }
      } catch {}
      reviewedCount++;
    }
    if (loadingEl) loadingEl.remove();
    if (!body.querySelector('.merge-sug')) wrap.remove();
  }

  function _renderMergeCard(card, sugName, reason, wrap) {
    const labels = card._labels;
    const chips = labels.map(l => {
      const removeBtn = labels.length > 2
        ? `<button class="chip-remove" data-id="${l.id}" title="Remove from group">&times;</button>`
        : '';
      return `<span class="merge-sug-chip">${esc(l.name)} <span style="color:var(--text-muted)">(${l.card_count})</span>${removeBtn}</span>`;
    }).join('');
    const reasonHtml = reason ? `<div class="merge-sug-reason">${esc(reason)}</div>` : '';
    card.innerHTML = `
      <div class="merge-sug-labels">${chips}</div>
      ${reasonHtml}
      <div class="merge-sug-actions">
        <span style="font-size:0.78rem;color:var(--text-muted)">Name:</span>
        <input type="text" class="merge-sug-name" value="${esc(sugName)}" style="border:1px solid var(--border);border-radius:8px;padding:4px 8px;font-size:0.82rem;min-width:100px;flex:1;max-width:200px;background:var(--bg);color:var(--text);font-family:inherit">
        <button class="merge-sug-btn">Merge</button>
        <button class="merge-sug-dismiss">Dismiss</button>
      </div>
    `;
    card.querySelector('.merge-sug-btn').onclick = () => acceptMergeSuggestion(card, wrap);
    card.querySelector('.merge-sug-dismiss').onclick = () => {
      if (card._dismissKey) _dismissedMergeSuggestions.add(card._dismissKey);
      card.remove();
      _updateSuggestionsCount(wrap);
    };
    card.querySelectorAll('.chip-remove').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const removeId = parseInt(btn.dataset.id);
        card._labels = card._labels.filter(l => l.id !== removeId);
        if (card._labels.length < 2) {
          card.remove();
          _updateSuggestionsCount(wrap);
        } else {
          _renderMergeCard(card, card.querySelector('.merge-sug-name').value, '', wrap);
        }
      };
    });
  }

  function _updateSuggestionsCount(wrap) {
    const remaining = wrap.querySelectorAll('.merge-sug').length;
    if (remaining === 0) { wrap.remove(); return; }
    const countEl = wrap.querySelector('.merge-sug-header-count');
    if (countEl) countEl.textContent = `${remaining} group${remaining !== 1 ? 's' : ''}`;
  }

  async function acceptMergeSuggestion(cardEl, wrap) {
    const labels = cardEl._labels;
    if (!labels || labels.length < 2) return;
    const desiredName = (cardEl.querySelector('.merge-sug-name')?.value || '').trim();
    const targetId = labels.reduce((a, b) => a.card_count >= b.card_count ? a : b).id;
    const sourceIds = labels.map(l => l.id).filter(id => id !== targetId);
    const targetLabel = labels.find(l => l.id === targetId);
    if (!confirm(`Merge ${sourceIds.length} label(s) into "${desiredName || targetLabel.name}"?`)) return;
    try {
      if (desiredName && desiredName !== targetLabel.name) {
        await fetch(`/api/labels/${targetId}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: desiredName }),
        });
      }
      const res = await fetch('/api/labels/merge', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_ids: sourceIds, target_id: targetId }),
      });
      if (!res.ok) throw new Error();
      cardEl.remove();
      _updateSuggestionsCount(wrap);
      await loadLabels();
      renderLabelsList();
      showToast('Labels merged.');
    } catch { showToast('Could not merge labels.'); }
  }

  async function createNewLabel() {
    const input = document.getElementById('new-label-name');
    const name = input.value.trim();
    if (!name) return;
    try {
      const res = await fetch('/api/labels', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) throw new Error();
      input.value = '';
      await loadLabelsTab();
      showToast('Label created.');
    } catch { showToast('Could not create label.'); }
  }

  async function renameLabelBrowse(id, name, inputEl) {
    name = name.trim();
    if (!name) return;
    try {
      const res = await fetch(`/api/labels/${id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (res.status === 409) {
        const existing = allLabels.find(l => l.name.toLowerCase() === name.toLowerCase() && l.id !== id);
        if (existing && confirm(`A label "${existing.name}" already exists. Merge into it?`)) {
          await fetch('/api/labels/merge', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_ids: [id], target_id: existing.id }),
          });
          await loadLabelsTab();
          showToast('Labels merged.');
        }
        return;
      }
      if (!res.ok) { showToast('Could not rename label.'); return; }
      if (inputEl) { inputEl.dataset.orig = name; inputEl.closest('.lbl-row').querySelector('.lbl-confirm').classList.add('hidden'); }
      await loadLabelsTab();
      showToast('Label renamed.');
    } catch { showToast('Could not rename label.'); }
  }

  async function deleteLabelBrowse(id, name) {
    if (!confirm(`Delete label "${name}"? Cards keep their other labels.`)) return;
    try {
      await fetch(`/api/labels/${id}`, { method: 'DELETE' });
      await loadLabelsTab();
      showToast('Label deleted.');
    } catch { showToast('Could not delete label.'); }
  }

  async function populateLabel(labelId, labelName, rowEl) {
    rowEl.parentNode.querySelectorAll('.lbl-suggest-panel').forEach(el => el.remove());
    const panel = document.createElement('div');
    panel.className = 'lbl-suggest-panel';
    rowEl.after(panel);

    const countEl = rowEl.querySelector('.lbl-count');
    function bumpCount() {
      if (!countEl) return;
      const n = parseInt(countEl.textContent) + 1;
      countEl.textContent = n + ' card' + (n !== 1 ? 's' : '');
    }

    // Header with a Done button to dismiss the suggestions when finished
    const head = document.createElement('div');
    head.className = 'lbl-suggest-head';
    head.innerHTML = `<span style="font-size:0.78rem;font-weight:600;color:var(--text)">Populate "${escHtml(labelName)}"</span><button class="lbl-suggest-done">Done</button>`;
    head.querySelector('.lbl-suggest-done').onclick = () => panel.remove();
    panel.appendChild(head);

    // Two sections, each with its own indeterminate loading bar, so each fills in
    // as its source resolves — deck search and LLM generation run in parallel.
    function makeSection(title) {
      const sec = document.createElement('div');
      sec.className = 'lbl-suggest-section';
      sec.innerHTML =
        `<div class="lbl-suggest-sectitle">${title}</div>` +
        `<div class="lbl-loadbar"></div>` +
        `<div class="lbl-suggest-cards"></div>`;
      return sec;
    }
    const deckSec = makeSection('Already in your deck — tap to tag');
    const newSec = makeSection('New ideas — tap to add');
    panel.appendChild(deckSec);
    panel.appendChild(newSec);
    const deckWrap = deckSec.querySelector('.lbl-suggest-cards');
    const newWrap = newSec.querySelector('.lbl-suggest-cards');
    const shownCardIds = new Set();

    function finishSection(sec, wrap, emptyMsg) {
      sec.querySelector('.lbl-loadbar')?.remove();
      if (!wrap.children.length) {
        const e = document.createElement('div');
        e.className = 'lbl-section-empty';
        e.textContent = emptyMsg;
        wrap.appendChild(e);
      }
    }

    // A deck card already in the user's collection → one tap tags it with this label.
    function addDeckChip(c) {
      // c: {id, target, source}
      if (c.id == null || shownCardIds.has(c.id)) return;
      shownCardIds.add(c.id);
      // Clear an earlier "none found" placeholder if a late match arrives.
      deckWrap.querySelector('.lbl-section-empty')?.remove();
      const chip = document.createElement('button');
      chip.className = 'lbl-suggest-card';
      chip.textContent = c.target + (c.source ? ' — ' + c.source : '');
      chip.onclick = async () => {
        chip.disabled = true; chip.style.opacity = '0.6';
        try {
          const rr = await fetch(`/api/labels/${labelId}/cards`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ card_id: c.id }),
          });
          if (!rr.ok) throw new Error();
          chip.textContent = '✓ ' + chip.textContent; chip.style.opacity = '1'; bumpCount();
        } catch { chip.disabled = false; chip.style.opacity = '1'; showToast('Failed to tag card.'); }
      };
      deckWrap.appendChild(chip);
    }

    // 1) Deck search (embedding similarity) — independent of the LLM, runs first.
    const deckPromise = (async () => {
      try {
        const url = `/api/labels/suggest-cards?name=${encodeURIComponent(labelName)}` +
          `&label_id=${labelId}&limit=15&min_score=${_populateMinScore}`;
        const res = await fetch(url).then(r => r.json());
        (res.cards || []).forEach(c => addDeckChip({
          id: c.id, target: c.target_text, source: c.source_text,
        }));
      } catch {}
    })();

    // 2) LLM vocab generation — the slow part. Its exact-matched deck cards merge
    //    into the deck section; its brand-new words fill the New ideas section.
    const llmPromise = (async () => {
      let data;
      try {
        const r = await fetch('/api/labels/populate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label_name: labelName, label_id: labelId }),
        });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Failed');
        data = await r.json();
      } catch (err) {
        finishSection(newSec, newWrap, `Could not generate ideas: ${err.message || 'unknown error'}`);
        return;
      }

      // Exact-matched deck cards → fold into the deck section (deduped by id).
      (data.existing || []).forEach(c => addDeckChip(c));

      (data.new || []).forEach(s => {
        const chip = document.createElement('button');
        chip.className = 'lbl-suggest-card';
        const rom = s.romanization ? ` (${s.romanization})` : '';
        chip.textContent = s.target + rom + ' — ' + s.source;
        chip.onclick = async () => {
          chip.disabled = true; chip.style.opacity = '0.6';
          try {
            const rr = await fetch('/api/cards', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source_text: s.source, target_text: s.target,
                romanization: s.romanization || '', target_lang: data.lang, label_ids: [labelId] }),
            });
            if (!rr.ok) throw new Error();
            chip.textContent = '✓ ' + chip.textContent; chip.style.opacity = '1'; bumpCount();
          } catch { chip.disabled = false; chip.style.opacity = '1'; showToast('Failed to add card.'); }
        };
        newWrap.appendChild(chip);
      });
      finishSection(newSec, newWrap, 'No new ideas — this label looks well covered.');
    })();

    // The deck section can keep growing until BOTH sources are in (embedding +
    // the LLM's exact matches), so only settle its loader once both resolve.
    Promise.all([deckPromise, llmPromise]).then(() => {
      finishSection(deckSec, deckWrap, 'No matching cards in your deck yet.');
    });
  }

  function populateCardsLangFilter() {
    const sel = document.getElementById('cards-lang-filter');
    if (!sel) return;
    let opts = '<option value="">All languages</option>';
    languages.forEach(lang => {
      opts += `<option value="${lang.code}"${lang.code === _defaultLang ? ' selected' : ''}>${langBadge(lang.code)}</option>`;
    });
    sel.innerHTML = opts;
    if (_defaultLang && languages.some(lang => lang.code === _defaultLang)) sel.value = _defaultLang;
  }

  // ── Tabs ──
  const _tabViews = { cards: 'cards-view', decks: 'my-decks-view', community: 'community-decks-view', labels: 'labels-view' };

  function switchTab(tab) {
    _currentTab = tab;
    document.querySelectorAll('.browse-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tab);
    });
    Object.entries(_tabViews).forEach(([k, id]) => {
      document.getElementById(id).style.display = k === tab ? '' : 'none';
    });
    document.getElementById('deck-detail-view').style.display = 'none';
    document.getElementById('browse-tabs').style.display = '';
    if (tab === 'community') loadCommunityDecks();
    if (tab === 'decks') loadMyDecks();
    if (tab === 'cards') filterCards();
    if (tab === 'labels') loadLabelsTab();
    document.getElementById('selection-bar').classList.toggle('hidden', tab !== 'cards' || !_selectedCards.size);
  }

  // ── My Cards (browse all) ──
  function cardPageParams(offset) {
    const params = new URLSearchParams({ offset: String(offset), limit: String(CARDS_PAGE_SIZE) });
    const search = document.getElementById('cards-search').value.trim();
    const lang = document.getElementById('cards-lang-filter').value;
    const labelId = document.getElementById('cards-label-filter').value;
    const cefr = getActiveChips('cefr-chips');
    const strength = getActiveChips('strength-chips');
    const status = getActiveChips('status-chips');
    const sort = document.getElementById('cards-sort').value;
    if (search) params.set('search', search);
    if (lang) params.set('lang', lang);
    if (labelId) params.set('label_id', labelId);
    if (cefr.length) params.set('cefr', cefr.join(','));
    if (strength.length) params.set('strength', strength.join(','));
    if (status.length) params.set('status', status.join(','));
    if (sort) params.set('sort', sort);
    return params;
  }

  async function loadMyCards(reset = true) {
    if (_cardsLoading && !reset) return;
    const requestId = ++_cardsRequestId;
    _cardsLoading = true;
    const button = document.getElementById('cards-load-more');
    if (button) { button.disabled = true; button.textContent = 'Loading…'; }
    try {
      const nextOffset = reset ? 0 : _cardsOffset;
      const data = await fetch('/api/cards/page?' + cardPageParams(nextOffset)).then(r => r.json());
      if (requestId !== _cardsRequestId) return;
      const cards = data.cards || [];
      _cardsTotal = data.total || 0;
      _cardsHasMore = !!data.has_more;
      _cardsOffset = nextOffset + cards.length;
      if (reset) _allCards = cards;
      else _allCards.push(...cards);
      renderCardsList(cards, _cardsTotal, !reset);
    } catch {
      if (requestId === _cardsRequestId && reset) document.getElementById('cards-list').innerHTML = '<div class="deck-empty">Could not load cards.</div>';
    } finally {
      if (requestId !== _cardsRequestId) return;
      _cardsLoading = false;
      if (button) {
        button.disabled = false;
        button.textContent = 'Load more';
        button.classList.toggle('visible', _cardsHasMore);
      }
    }
  }

  function loadMoreCards() { loadMyCards(false); }

  function getActiveChips(containerId) {
    return [...document.querySelectorAll(`#${containerId} .filter-chip.active`)].map(c => c.dataset.val);
  }
  function toggleChip(el) {
    el.classList.toggle('active');
    filterCards();
  }

  function filterCards() {
    clearTimeout(_cardsDebounce);
    _cardsDebounce = setTimeout(() => loadMyCards(true), 250);
  }

  function clearFilters() {
    document.getElementById('cards-search').value = '';
    document.getElementById('cards-lang-filter').value = _defaultLang || '';
    document.getElementById('cards-label-filter').value = '';
    document.querySelectorAll('.cards-filters .filter-chip.active').forEach(c => c.classList.remove('active'));
    document.getElementById('cards-sort').value = 'newest';
    filterCards();
  }

  function filterByLabel(labelId) {
    document.getElementById('cards-label-filter').value = String(labelId);
    filterCards();
  }

  function renderCardsList(cards, total = cards.length, append = false) {
    _lastFilteredCards = append ? _lastFilteredCards.concat(cards) : cards.slice();
    const el = document.getElementById('cards-count');
    const labelId = parseInt(document.getElementById('cards-label-filter').value) || 0;
    const studyHref = labelId ? `/cards?label_id=${labelId}&study=1` : '/cards?study=1';
    el.innerHTML = `<span>${total} card${total !== 1 ? 's' : ''}</span>` +
      (total ? `<a class="study-link" href="${studyHref}">Study${labelId ? ' this label' : ''} →</a>` : '');
    const list = document.getElementById('cards-list');
    if (!total) {
      list.innerHTML = '<div class="deck-empty">No cards found.</div>';
      updateSelectionBar();
      return;
    }
    if (!append) list.innerHTML = '';
    cards.forEach(card => renderCardListItem(card, list));
    cards.forEach(card => {
      if (card.canonical_card_id) {
        const elc = document.getElementById(`canonical-target-${card.id}`);
        if (elc) elc.textContent = canonicalCardText(card.canonical_card_id) || `#${card.canonical_card_id}`;
      }
    });
    updateSelectionBar();
  }

  // ── Card list item (ported from flashcards page) ──
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
      `<span class="label-chip-static" onclick="filterByLabel(${l.id})" title="Filter by this label">${escHtml(l.name)}</span>`
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
      <input type="checkbox" class="card-select-cb" id="sel-${card.id}" ${_selectedCards.has(card.id) ? 'checked' : ''}
        onchange="toggleCardSelect(${card.id}, this.checked)">
      <div class="card-display" id="display-${card.id}">
        ${langTagHtml}
        <div class="${logographic ? 'chinese' : ''}">${escHtml(card.target_text)}</div>
        ${romanRowHtml}
        ${card.classifier ? `<span class="card-classifier">${escHtml(card.classifier)}</span>` : ''}
        ${card.cefr_level ? `<span class="cefr-badge cefr-${card.cefr_level}">${escHtml(card.cefr_level)}</span>` : ''}
        ${strengthBadge(card)}
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

  // ── Canonical card helpers ──
  function canonicalCardText(canonicalId) {
    if (!canonicalId) return null;
    const found = _allCards.find(c => c.id === canonicalId);
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
    const query = input.value.trim();
    if (!query) { results.style.display = 'none'; return; }
    clearTimeout(input._canonicalTimer);
    input._canonicalTimer = setTimeout(async () => {
      try {
        const data = await fetch('/api/cards/page?limit=20&search=' + encodeURIComponent(query)).then(r => r.json());
        if (input.value.trim() !== query) return;
        const matches = (data.cards || []).filter(card => card.id !== cardId).slice(0, 8);
        if (!matches.length) { results.style.display = 'none'; return; }
        const item = document.getElementById(`cli-${cardId}`);
        matches.forEach(card => {
          if (!_allCards.some(existing => existing.id === card.id)) _allCards.push(card);
        });
        results.innerHTML = '';
        matches.forEach(card => {
          const row = document.createElement('div');
          row.className = 'canonical-result-row';
          row.textContent = card.target_text + (card.source_text ? ` — ${card.source_text}` : '');
          row.onclick = () => {
            item._pendingCanonicalId = card.id;
            renderCanonicalCurrent(cardId, card.id);
            input.value = '';
            results.style.display = 'none';
          };
          results.appendChild(row);
        });
        results.style.display = '';
      } catch { results.style.display = 'none'; }
    }, 200);
  }

  // ── Edit / delete ──
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
      onLabelsRefreshed: labels => { allLabels = _allLabels = labels; },
    });

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
          source_text: sourceText, target_text: targetText,
          romanization, notes, label_ids: labelIds,
        }),
      });
      if (!res.ok) throw new Error();

      if (pendingCanonicalId !== item._canonicalCardId) {
        await fetch(`/api/cards/${id}/canonical`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ canonical_card_id: pendingCanonicalId }),
        });
      }

      const cardData = {
        id, source_text: sourceText, target_text: targetText, romanization, notes,
        priority: item._priority,
        tutor_flag: item._tutorFlag ? 1 : 0,
        suspended: item._suspended ? 1 : 0,
        target_lang: langCode,
        labels: allLabels.filter(l => labelIds.includes(l.id)),
        canonical_card_id: pendingCanonicalId,
      };
      // Keep the in-memory copy in sync so search/canonical reflect the edit.
      const ci = _allCards.findIndex(c => c.id === id);
      if (ci !== -1) _allCards[ci] = { ..._allCards[ci], ...cardData };

      const parent = item.parentNode;
      const next = item.nextSibling;
      item.remove();
      const tmp = document.createElement('div');
      renderCardListItem(cardData, tmp);
      const newItem = tmp.firstElementChild;
      parent.insertBefore(newItem, next);
      if (pendingCanonicalId) {
        const elc = document.getElementById(`canonical-target-${id}`);
        if (elc) elc.textContent = canonicalCardText(pendingCanonicalId) || `#${pendingCanonicalId}`;
      }

      showToast('Card updated.');
      loadLabels();
    } catch { showToast('Failed to save card.'); }
    finally { btn.disabled = false; btn.textContent = 'Save'; }
  }

  async function deleteCard(id) {
    if (!confirm('Delete this card and all its progress?')) return;
    try {
      await fetch(`/api/cards/${id}`, { method: 'DELETE' });
      _allCards = _allCards.filter(c => c.id !== id);
      filterCards();   // re-renders the list + count
      loadLabels();
    } catch { showToast('Failed to delete card.'); }
  }

  // ── Card selection (for deck creation) ──
  let _lastFilteredCards = [];

  function toggleCardSelect(id, checked) {
    if (checked) _selectedCards.add(id);
    else _selectedCards.delete(id);
    updateSelectionBar();
  }

  function selectAllFiltered() {
    _lastFilteredCards.forEach(c => _selectedCards.add(c.id));
    filterCards();
  }

  function clearSelection() {
    _selectedCards.clear();
    filterCards();
  }

  function updateSelectionBar() {
    const n = _selectedCards.size;
    const bar = document.getElementById('selection-bar');
    bar.classList.toggle('hidden', n === 0);
    document.getElementById('sel-count').textContent = `${n} selected`;
  }

  function showCreateDeckModal() {
    if (!_selectedCards.size) return;
    const langs = new Set();
    _allCards.forEach(c => { if (_selectedCards.has(c.id)) langs.add(c.target_lang); });
    if (langs.size > 1) {
      showToast('A deck can only contain one language. Filter by language first, then select cards.');
      return;
    }
    document.getElementById('deck-modal').style.display = 'flex';
    document.getElementById('deck-name').value = '';
    document.getElementById('deck-desc').value = '';
    document.getElementById('deck-name').focus();
  }

  function closeDeckModal() {
    document.getElementById('deck-modal').style.display = 'none';
  }

  // ── Create deck ──
  async function createDeck() {
    const name = document.getElementById('deck-name').value.trim();
    if (!name || !_selectedCards.size) return;
    const btn = document.getElementById('create-btn');
    btn.disabled = true;
    btn.textContent = 'Creating…';
    try {
      const resp = await fetch('/api/decks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          description: document.getElementById('deck-desc').value.trim(),
          visibility: document.getElementById('deck-visibility').value,
          card_ids: [..._selectedCards],
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        showToast(err.detail || 'Failed to create deck');
        btn.disabled = false; btn.textContent = 'Create deck';
        return;
      }
      closeDeckModal();
      showToast('Deck created! These cards now carry a "📦 ' + name + '" label you can study.');
      _selectedCards.clear();
      await loadMyCards();
      populateCardsLangFilter();
      loadLabels();
      filterCards();
    } catch {
      showToast('Failed to create deck');
    }
    btn.disabled = false;
    btn.textContent = 'Create deck';
  }

  // ── My decks ──
  async function loadMyDecks() {
    try {
      const res = await fetch('/api/decks/mine').then(r => r.json());
      const list = document.getElementById('my-deck-list');
      const decks = res.decks || [];
      if (!decks.length) {
        list.innerHTML = '<div class="deck-empty">No decks yet. Select cards in My Cards to create one.</div>';
        return;
      }
      list.innerHTML = '';
      decks.forEach(d => {
        const card = document.createElement('div');
        card.className = 'deck-card';
        const isCreator = d.is_creator;
        card.innerHTML = `
          <div class="deck-card-title">${esc(d.name)}${!isCreator ? ' <span style="font-size:0.72rem;color:var(--text-muted);font-weight:400">(imported)</span>' : ''}</div>
          ${d.description ? `<div class="deck-card-desc">${esc(d.description)}</div>` : ''}
          <div class="deck-card-meta">
            ${d.target_lang ? `<span>${langBadge(d.target_lang)}</span>` : ''}
            <span>${d.card_count} cards</span>
            ${!isCreator && d.creator ? `<span>by ${esc(d.creator)}</span>` : ''}
            <span>${d.visibility}</span>
            ${isCreator ? `<span>${d.import_count} import${d.import_count !== 1 ? 's' : ''}</span>` : ''}
            ${_deckStarsHtml(d.avg_rating, d.rating_count)}
          </div>`;
        card.onclick = () => openDeckDetail(d.id, isCreator);
        list.appendChild(card);
      });
    } catch {}
  }

  // ── Community decks ──
  let _searchDebounce = null;
  function searchCommunity() {
    clearTimeout(_searchDebounce);
    _searchDebounce = setTimeout(loadCommunityDecks, 200);
  }

  function _deckStarsHtml(avg, count) {
    if (!count) return '';
    let h = '<span class="deck-card-stars">';
    const rounded = Math.round(avg);
    for (let i = 1; i <= 5; i++) h += `<span class="${i <= rounded ? 'filled' : ''}">★</span>`;
    h += ` <span style="color:var(--text-muted)">(${count})</span></span>`;
    return h;
  }

  function _interactiveStars(currentRating, deckId) {
    let h = '';
    for (let i = 1; i <= 5; i++) {
      h += `<span class="star${i <= currentRating ? ' filled' : ''}" onclick="rateDeck(${deckId},${i})" data-star="${i}">★</span>`;
    }
    return h;
  }

  async function rateDeck(deckId, rating) {
    try {
      const res = await fetch(`/api/decks/${deckId}/rate`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({rating})
      }).then(r => r.json());
      const starsEl = document.getElementById('deck-rate-stars');
      if (starsEl) starsEl.innerHTML = _interactiveStars(rating, deckId);
      const infoEl = document.getElementById('deck-rate-info');
      if (infoEl) infoEl.textContent = `${Number(res.avg_rating).toFixed(1)} avg (${res.rating_count})`;
    } catch {
      showToast('Rating failed');
    }
  }

  function populateCommDeckLangFilter() {
    const sel = document.getElementById('comm-deck-lang');
    sel.innerHTML = '<option value="">All languages</option>';
    languages.forEach(l => {
      const opt = document.createElement('option');
      opt.value = l.code;
      opt.textContent = `${l.flag} ${l.name}`;
      if (l.code === _defaultLang) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  async function loadCommunityDecks() {
    const params = new URLSearchParams();
    const q = document.getElementById('deck-search').value.trim();
    const lang = document.getElementById('comm-deck-lang').value;
    const sort = document.getElementById('comm-deck-sort').value;
    if (q) params.set('search', q);
    if (lang) params.set('lang', lang);
    if (sort) params.set('sort', sort);
    const qs = params.toString();
    const list = document.getElementById('community-deck-list');
    try {
      const res = await fetch('/api/decks/community' + (qs ? '?' + qs : '')).then(r => r.json());
      const decks = res.decks || [];
      if (!decks.length) {
        list.innerHTML = '<div class="deck-empty">No shared decks found.</div>';
        return;
      }
      list.innerHTML = '';
      decks.forEach(d => {
        const card = document.createElement('div');
        card.className = 'deck-card';
        card.innerHTML = `
          <div class="deck-card-title">${esc(d.name)}</div>
          ${d.description ? `<div class="deck-card-desc">${esc(d.description)}</div>` : ''}
          <div class="deck-card-meta">
            ${d.target_lang ? `<span>${langBadge(d.target_lang)}</span>` : ''}
            <span>${d.card_count} cards</span>
            <span>by ${esc(d.creator)}</span>
            <span>${d.import_count} import${d.import_count !== 1 ? 's' : ''}</span>
            ${_deckStarsHtml(d.avg_rating, d.rating_count)}
            ${d.imported ? '<span style="color:var(--primary);font-weight:600">Imported</span>' : ''}
          </div>`;
        card.onclick = () => openDeckDetail(d.id, false);
        list.appendChild(card);
      });
    } catch {
      list.innerHTML = '<div class="deck-empty">Could not load decks.</div>';
    }
  }

  // ── Deck detail ──
  async function openDeckDetail(deckId, isMine) {
    document.getElementById('browse-tabs').style.display = 'none';
    Object.values(_tabViews).forEach(id => document.getElementById(id).style.display = 'none');
    document.getElementById('deck-detail-view').style.display = '';
    const container = document.getElementById('deck-detail');
    container.innerHTML = '<div class="deck-empty">Loading…</div>';
    try {
      const d = await fetch(`/api/decks/${deckId}`).then(r => r.json());
      const items = d.items || [];
      let actionsHtml = '';
      if (isMine) {
        const studyLink = d.import_label_id
          ? `<a class="deck-import-btn" href="/cards?label_id=${d.import_label_id}&study=1" style="text-decoration:none;display:inline-block">Study this deck</a>`
          : '';
        actionsHtml = `<div class="deck-actions-row">
          ${studyLink}
          <button class="deck-delete-btn" onclick="deleteDeck(${d.id})">Delete deck</button>
        </div>`;
      } else if (d.imported) {
        const studyLink = d.import_label_id
          ? `<a class="deck-import-btn" href="/cards?label_id=${d.import_label_id}&study=1" style="text-decoration:none;display:inline-block">Study this deck</a>`
          : '';
        actionsHtml = `<div class="deck-actions-row">${studyLink}<button class="deck-import-btn" disabled style="background:var(--neutral-soft);color:var(--text-muted)">Imported</button></div>`;
      } else {
        actionsHtml = `<button class="deck-import-btn" id="import-btn" onclick="importDeck(${d.id}, '${d.target_lang || ''}')">Import ${items.length} cards to my deck</button>`;
      }
      const ratingHtml = isMine ? '' : `
        <div class="deck-rating-row">
          <span>Rate this deck:</span>
          <span class="deck-stars deck-stars-interactive" id="deck-rate-stars">${_interactiveStars(d.user_rating || 0, deckId)}</span>
          <span id="deck-rate-info">${d.rating_count ? `${Number(d.avg_rating).toFixed(1)} avg (${d.rating_count})` : 'No ratings yet'}</span>
        </div>`;
      container.innerHTML = `
        <button class="deck-back-btn" onclick="closeDeckDetail()">← Back</button>
        <h2>${esc(d.name)}</h2>
        ${d.description ? `<div class="deck-detail-desc">${esc(d.description)}</div>` : ''}
        <div class="deck-detail-meta">
          <span>${items.length} cards</span>
          ${d.creator ? `<span>by ${esc(d.creator)}</span>` : ''}
          <span>${d.import_count || 0} import${(d.import_count||0) !== 1 ? 's' : ''}</span>
          <span>${d.visibility}</span>
        </div>
        ${ratingHtml}
        ${actionsHtml}
        <div class="deck-detail-items">
          ${items.map(it => `
            <div class="deck-detail-item">
              <span class="deck-detail-target">${esc(it.target_text)}</span>
              <span class="deck-detail-source">${esc(it.source_text)}</span>
            </div>
          `).join('')}
        </div>`;
    } catch {
      container.innerHTML = '<div class="deck-empty">Could not load deck.</div>';
    }
  }

  function closeDeckDetail() {
    document.getElementById('deck-detail-view').style.display = 'none';
    document.getElementById('browse-tabs').style.display = '';
    Object.entries(_tabViews).forEach(([k, id]) => {
      document.getElementById(id).style.display = k === _currentTab ? '' : 'none';
    });
  }

  // Switch the app's learning language if the deck is in a different one (live,
  // via the shared nav helper; falls back to a settings PUT). Returns true if
  // it switched. Keeps _defaultLang in sync.
  async function _maybeSwitchLang(lang) {
    if (!lang || lang === _defaultLang) return false;
    try {
      if (window.setAppLanguage) {
        if (!(await window.setAppLanguage(lang))) return false;
      } else {
        await fetch('/api/settings', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ default_target_lang: lang }),
        });
      }
    } catch { return false; }
    _defaultLang = lang;
    return true;
  }

  async function importDeck(deckId, lang) {
    const btn = document.getElementById('import-btn');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = 'Importing…';
    try {
      const resp = await fetch(`/api/decks/${deckId}/import`, { method: 'POST' });
      const res = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(res.detail || 'Import failed');
      const switched = await _maybeSwitchLang(lang);
      showToast(`Imported ${res.created} new cards (${res.total} total)` + (switched ? ` · switched language` : ''));
      if (res.label_id) {
        btn.outerHTML = `<a class="deck-import-btn" href="/cards?label_id=${res.label_id}&study=1" style="text-decoration:none;display:inline-block">Study now</a>`;
      } else {
        btn.textContent = 'Imported!';
      }
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Import failed — try again';
      showToast(e.message || 'Import failed');
    }
  }

  async function deleteDeck(deckId) {
    if (!confirm('Delete this deck? This cannot be undone.')) return;
    try {
      await fetch(`/api/decks/${deckId}`, { method: 'DELETE' });
      showToast('Deck deleted');
      closeDeckDetail();
      loadMyDecks();
    } catch {
      showToast('Failed to delete');
    }
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeDeckModal();
  });

  init();
  document.addEventListener('langchange', function () { init(); });
