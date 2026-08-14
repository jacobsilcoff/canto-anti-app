
  // ── FAB menu ──
  function toggleAddMenu() {
    document.getElementById('add-fab-menu').classList.toggle('open');
  }
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.add-fab') && !e.target.closest('.add-fab-menu'))
      document.getElementById('add-fab-menu').classList.remove('open');
  });

  // ── Translate panel (full port of the translate page) ──
  let _trTargetLang = null;
  let _trSourceIsTarget = false;   // false: en → target, true: target → en
  let _trLastResult = null;
  let _trCardId = null;
  let _trAudioEl = null;
  let _trStickyPicker = null;
  let _trSavedNotes = '';
  let _trAiNotes = '';

  function _trEsc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
  function _trLi() { return langByCode[_trTargetLang] || null; }

  // Keep the panel below the sticky header so the menu bar stays visible.
  function _trPositionPanels() {
    const h = document.querySelector('header');
    const top = h ? Math.max(0, Math.round(h.getBoundingClientRect().bottom)) : 0;
    document.getElementById('translate-panel').style.top = top + 'px';
    document.getElementById('community-panel').style.top = top + 'px';
  }
  window.addEventListener('resize', _trPositionPanels);

  function openTranslatePanel() {
    document.getElementById('add-fab-menu').classList.remove('open');
    document.getElementById('add-fab').style.display = 'none';
    if (!_trTargetLang) _trTargetLang = (settings && settings.default_target_lang) || 'yue';
    _trEnsureLabelPicker();
    if (_trStickyPicker) _trStickyPicker.setLabels(allLabels);
    trUpdateDirectionButtons();
    _trPositionPanels();
    document.getElementById('translate-panel').classList.add('open');
    document.getElementById('tr-input').focus();
  }

  function closeTranslatePanel() {
    document.getElementById('translate-panel').classList.remove('open');
    document.getElementById('add-fab').style.display = '';
    document.getElementById('tr-input').value = '';
    document.getElementById('tr-char-count').textContent = '0';
    trResetOutput();
  }

  // ── Direction ──
  function trSetDirection(isTarget) {
    _trSourceIsTarget = isTarget;
    trUpdateDirectionButtons();
    trResetOutput();
  }
  function trSwapLang() { trSetDirection(!_trSourceIsTarget); }

  function trUpdateDirectionButtons() {
    applyScript(document.getElementById('translate-panel'), _trTargetLang);
    const lang = _trLi();
    const langName = lang ? lang.name : _trTargetLang;
    const langLabel = (lang && lang.flag ? lang.flag + ' ' : '') + langName;
    const src = document.getElementById('tr-btn-source');
    const tgt = document.getElementById('tr-btn-target');
    if (src) src.classList.toggle('active', !_trSourceIsTarget);
    if (tgt) { tgt.classList.toggle('active', _trSourceIsTarget); tgt.textContent = langLabel; }
    document.getElementById('tr-input').placeholder = _trSourceIsTarget
      ? `Enter ${langName} text…` : 'Enter English text…';
  }

  // ── Input ──
  function trOnInput() {
    document.getElementById('tr-char-count').textContent = document.getElementById('tr-input').value.length;
  }
  function trOnContextInput() {
    const has = document.getElementById('tr-context-input').value.trim().length > 0;
    document.getElementById('tr-context-summary-set').style.display = has ? '' : 'none';
    document.querySelector('#tr-context-details .context-summary-label').textContent =
      has ? 'Context for translator' : '+ Add context for translator';
  }
  function trOnNotesInput() {
    document.getElementById('tr-notes-save-btn').disabled =
      (document.getElementById('tr-notes-input').value === _trSavedNotes);
    document.getElementById('tr-notes-saved-msg').textContent = '';
  }
  function trClearInput() {
    document.getElementById('tr-input').value = '';
    document.getElementById('tr-char-count').textContent = '0';
    trResetOutput();
  }

  function trResetOutput() {
    document.getElementById('tr-candidate-panel').style.display = 'none';
    document.getElementById('tr-output-panel').style.display = 'none';
    document.getElementById('tr-saved-indicator').classList.remove('show');
    document.getElementById('tr-notes-input').value = '';
    document.getElementById('tr-notes-save-btn').disabled = true;
    document.getElementById('tr-notes-saved-msg').textContent = '';
    document.getElementById('tr-output-notes-details').open = false;
    document.getElementById('tr-notes-summary-empty').style.display = '';
    document.getElementById('tr-notes-summary-full').style.display = 'none';
    document.getElementById('tr-ai-notes-block').style.display = 'none';
    _trSavedNotes = ''; _trAiNotes = ''; _trCardId = null; _trLastResult = null;
    if (_trAudioEl) { _trAudioEl.pause(); _trAudioEl = null; }
    document.getElementById('tr-audio-btn').classList.remove('playing');
  }

  // ── Labels ──
  function _trEnsureLabelPicker() {
    if (_trStickyPicker) { _trStickyPicker.setLabels(allLabels); return; }
    _trStickyPicker = new LabelPicker(
      document.getElementById('tr-label-picker'),
      {
        allLabels, initialIds: [], allowCreate: true, mode: 'multi',
        placeholder: 'Search or create a label…',
        onChange: () => { if (_trCardId) trSyncCardLabels(); },
        onLabelsRefreshed: labels => {
          allLabels = labels;
          if (filterPicker) filterPicker.setLabels(_filterPickerLabels());
        },
      }
    );
  }

  function trRenderOutputLabels(autoLabels) {
    const el = document.getElementById('tr-output-labels');
    el.innerHTML = '';
    const parts = [];
    if (_trStickyPicker) {
      const sel = _trStickyPicker.getSelected();
      allLabels.filter(l => sel.has(l.id)).forEach(lbl => parts.push(lbl.name));
    }
    (autoLabels || []).forEach(name => { if (!parts.includes(name)) parts.push(name); });
    if (parts.length === 0) { el.style.display = 'none'; return; }
    el.style.display = 'flex';
    parts.forEach(name => {
      const chip = document.createElement('span');
      chip.className = 'label-chip-static';
      chip.textContent = name;
      el.appendChild(chip);
    });
  }

  async function trSyncCardLabels() {
    if (!_trCardId || !_trStickyPicker) return;
    await trPersistCard();
    trRenderOutputLabels();
  }

  // ── Notes ──
  async function trSaveNotes() {
    if (!_trCardId) return;
    const notes = document.getElementById('tr-notes-input').value;
    const btn = document.getElementById('tr-notes-save-btn');
    btn.disabled = true;
    try {
      _trSavedNotes = notes;
      await trPersistCard();
      document.getElementById('tr-notes-saved-msg').textContent = 'Saved';
      setTimeout(() => { document.getElementById('tr-notes-saved-msg').textContent = ''; }, 2000);
      trUpdateNotesSummary();
    } catch {
      showToast('Could not save note.');
      btn.disabled = false;
    }
  }
  function trUpdateNotesSummary() {
    const has = _trSavedNotes.trim().length > 0;
    document.getElementById('tr-notes-summary-empty').style.display = has ? 'none' : '';
    document.getElementById('tr-notes-summary-full').style.display = has ? '' : 'none';
  }
  function trCombinedNotes() {
    const parts = [];
    if (_trAiNotes.trim()) parts.push(_trAiNotes.trim());
    if (_trSavedNotes.trim()) parts.push(_trSavedNotes.trim());
    return parts.join('\n\n');
  }

  // ── Persist (update card on backend) ──
  async function trPersistCard() {
    if (!_trCardId) return;
    const target = document.getElementById('tr-out-target').textContent;
    const roman  = document.getElementById('tr-out-romanization').textContent;
    const english = _trSourceIsTarget
      ? document.getElementById('tr-out-english').textContent
      : document.getElementById('tr-input').value.trim();
    const res = await fetch(`/api/cards/${_trCardId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_text: english, target_text: target, romanization: roman,
        notes: trCombinedNotes(),
        label_ids: _trStickyPicker ? [..._trStickyPicker.getSelected()] : [],
      }),
    });
    if (!res.ok) throw new Error('persist failed');
  }

  // ── Translate ──
  async function trDoTranslate() {
    const text = document.getElementById('tr-input').value.trim();
    if (!text) return;
    const context = document.getElementById('tr-context-input').value.trim();
    const btn = document.getElementById('tr-translate-btn');
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
    trResetOutput();
    try {
      const res = await fetch('/api/translate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text, target_lang: _trTargetLang,
          source_is_target: _trSourceIsTarget, context: context || null,
        }),
      });
      if (!res.ok) {
        let msg = 'Translation failed — please try again.';
        try { msg = (await res.json()).detail || msg; } catch {}
        throw new Error(msg);
      }
      _trLastResult = await res.json();
      const cands = _trLastResult.candidates || [];
      if (cands.length > 1) trRenderCandidates(cands);
      else if (cands.length === 1) await trChoose(0);
      else showToast('No translation returned.');
    } catch (e) {
      showToast(e.message || 'Translation failed — please try again.');
    } finally {
      btn.disabled = false; btn.textContent = 'Translate';
    }
  }

  function trRenderCandidates(candidates) {
    const panel = document.getElementById('tr-candidate-panel');
    const list = document.getElementById('tr-candidate-list');
    const lang = _trLi();
    list.innerHTML = '';
    candidates.forEach((c, i) => {
      const row = document.createElement('div');
      row.className = 'candidate-option';
      row.onclick = () => trChoose(i);
      if (c.label) {
        const lbl = document.createElement('div');
        lbl.className = 'candidate-label';
        lbl.textContent = c.label;
        row.appendChild(lbl);
      }
      const tgt = document.createElement('div');
      tgt.className = 'candidate-target';
      tgt.textContent = c.target_text;
      row.appendChild(tgt);
      if (c.romanization && lang && lang.logographic) {
        const r = document.createElement('div');
        r.className = 'candidate-roman';
        r.textContent = c.romanization;
        row.appendChild(r);
      }
      const en = document.createElement('div');
      en.className = 'candidate-english';
      en.textContent = c.english;
      row.appendChild(en);
      list.appendChild(row);
    });
    panel.style.display = 'block';
  }

  async function trChoose(index) {
    if (!_trLastResult) return;
    const c = _trLastResult.candidates[index];
    _trAiNotes = c.notes || '';
    _trSavedNotes = '';
    document.getElementById('tr-candidate-panel').style.display = 'none';
    const btn = document.getElementById('tr-translate-btn');
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: c.english, target_text: c.target_text,
          romanization: c.romanization || '', target_lang: _trTargetLang,
          notes: _trAiNotes || null, priority: _trLastResult.priority || 3,
          label_ids: _trStickyPicker ? [..._trStickyPicker.getSelected()] : [],
          suggested_labels: _trLastResult.suggested_labels || [],
          classifier: _trLastResult.classifier || '',
          cefr_level: _trLastResult.cefr_level || null,
        }),
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      _trCardId = data.card_id;
      trRenderResult(c, _trLastResult.suggested_labels || []);
    } catch {
      showToast('Could not save card — try again.');
    } finally {
      btn.disabled = false; btn.textContent = 'Translate';
    }
  }

  function trRenderResult(c, autoLabels) {
    const lang = _trLi();
    document.getElementById('tr-out-target').textContent = c.target_text;
    const romanEl = document.getElementById('tr-out-romanization');
    romanEl.textContent = (lang && lang.logographic) ? (c.romanization || '') : '';
    romanEl.style.display = (lang && lang.logographic && c.romanization) ? '' : 'none';

    const classifierEl = document.getElementById('tr-out-classifier');
    const classifier = _trLastResult ? (_trLastResult.classifier || '') : '';
    if (classifier) { classifierEl.textContent = classifier; classifierEl.style.display = ''; }
    else classifierEl.style.display = 'none';

    const englishEl = document.getElementById('tr-out-english');
    if (_trSourceIsTarget) { englishEl.textContent = c.english; englishEl.style.display = 'block'; }
    else { englishEl.textContent = ''; englishEl.style.display = 'none'; }

    if (_trAiNotes && _trAiNotes.trim()) {
      document.getElementById('tr-ai-notes-text').textContent = _trAiNotes.trim();
      document.getElementById('tr-ai-notes-block').style.display = '';
    } else {
      document.getElementById('tr-ai-notes-block').style.display = 'none';
    }

    trRenderOutputLabels(autoLabels || []);
    document.getElementById('tr-output-panel').style.display = 'block';

    const ind = document.getElementById('tr-saved-indicator');
    ind.classList.add('show');
    setTimeout(() => ind.classList.remove('show'), 3000);

    if (_trAudioEl) { _trAudioEl.pause(); _trAudioEl = null; }
    document.getElementById('tr-audio-btn').classList.remove('playing');
  }

  function trPlayAudio() {
    if (!_trCardId) return;
    if (_trAudioEl && !_trAudioEl.paused) {
      _trAudioEl.pause();
      document.getElementById('tr-audio-btn').classList.remove('playing');
      return;
    }
    const _clear = () => document.getElementById('tr-audio-btn').classList.remove('playing');
    let _b = null;
    try { _b = CantoShell.playBoosted('/api/audio/' + _trCardId, _clear); } catch {}
    if (_b) {
      document.getElementById('tr-audio-btn').classList.add('playing');
      _b.catch(_clear);
      return;
    }
    _trAudioEl = new Audio('/api/audio/' + _trCardId);
    document.getElementById('tr-audio-btn').classList.add('playing');
    _trAudioEl.onended = () => document.getElementById('tr-audio-btn').classList.remove('playing');
    // Without a catch the button sticks in 'playing' forever and the failure
    // is invisible — audio is synthesised lazily, so it can fail transiently.
    _trAudioEl.play().catch(() => {
      document.getElementById('tr-audio-btn').classList.remove('playing');
    });
  }

  document.getElementById('tr-input').addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); trDoTranslate(); }
  });

  // ── Community decks panel ──
  let _cdDebounce = null;
  let _cdImported = new Set();

  function openCommunityPanel() {
    document.getElementById('add-fab-menu').classList.remove('open');
    document.getElementById('add-fab').style.display = 'none';
    _trPositionPanels();
    document.getElementById('community-panel').classList.add('open');
    _cdLoadDecks();
  }

  function closeCommunityPanel() {
    document.getElementById('community-panel').classList.remove('open');
    document.getElementById('add-fab').style.display = '';
  }

  function cdSearch() {
    clearTimeout(_cdDebounce);
    _cdDebounce = setTimeout(_cdLoadDecks, 300);
  }

  async function _cdLoadDecks() {
    const q = (document.getElementById('cd-search').value || '').trim();
    const list = document.getElementById('cd-list');
    list.innerHTML = '<div class="cd-empty">Loading…</div>';
    try {
      const lang = (settings && settings.default_target_lang) || '';
      const params = new URLSearchParams();
      if (q) params.set('search', q);
      if (lang) params.set('lang', lang);
      const res = await fetch('/api/decks/community' + (params.toString() ? '?' + params : ''));
      const data = await res.json();
      const decks = data.decks || [];
      if (decks.length === 0) { list.innerHTML = '<div class="cd-empty">No community decks found.</div>'; return; }
      list.innerHTML = '';
      decks.forEach(d => {
        const el = document.createElement('div');
        el.className = 'cd-deck';
        let meta = (d.creator || 'Unknown') + ' · ' + (d.card_count || 0) + ' cards';
        if (d.target_lang) {
          const li = langByCode[d.target_lang];
          if (li) meta += ' · ' + (li.flag || '') + ' ' + li.name;
        }
        let rating = '';
        if (d.avg_rating) rating = `<span class="cd-rating">${'★'.repeat(Math.round(d.avg_rating))}${'☆'.repeat(5 - Math.round(d.avg_rating))}</span>`;
        el.innerHTML = `<div class="cd-deck-name">${_trEsc(d.name)}${rating}</div>
          <div class="cd-deck-meta">${_trEsc(meta)}</div>
          ${d.description ? '<div class="cd-deck-desc">' + _trEsc(d.description) + '</div>' : ''}`;
        const actions = document.createElement('div');
        actions.className = 'cd-actions';
        el.appendChild(actions);
        _cdRenderActions(actions, d, (d.imported || _cdImported.has(d.id)));
        list.appendChild(el);
      });
    } catch {
      list.innerHTML = '<div class="cd-empty">Failed to load decks.</div>';
    }
  }

  // Render the import / imported+study+remove controls for one deck row.
  function _cdRenderActions(actions, d, isImported) {
    actions.innerHTML = '';
    if (isImported) {
      const tag = document.createElement('span');
      tag.className = 'cd-imported';
      tag.textContent = '✓ Imported';
      actions.appendChild(tag);

      const study = document.createElement('button');
      study.className = 'cd-study-btn';
      study.textContent = 'Study';
      study.onclick = () => _cdStudyDeck(d);
      actions.appendChild(study);

      const remove = document.createElement('button');
      remove.className = 'cd-remove-btn';
      remove.textContent = 'Remove';
      remove.onclick = async () => {
        if (!confirm(`Remove "${d.name}" from your decks? Cards added only by this deck will be deleted.`)) return;
        remove.disabled = true; remove.textContent = 'Removing…';
        try {
          const r = await fetch('/api/decks/' + d.id + '/import', { method: 'DELETE' });
          if (!r.ok) throw new Error();
          const out = await r.json().catch(() => ({}));
          _cdImported.delete(d.id);
          d.imported = false;
          _cdRenderActions(actions, d, false);
          showToast(out.removed ? `Removed deck — ${out.removed} card${out.removed === 1 ? '' : 's'} deleted` : 'Deck removed');
        } catch { remove.disabled = false; remove.textContent = 'Remove'; showToast('Could not remove deck.'); }
      };
      actions.appendChild(remove);
    } else {
      const btn = document.createElement('button');
      btn.className = 'cd-import-btn';
      btn.textContent = 'Import Deck';
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = 'Importing…';
        try {
          const r = await fetch('/api/decks/' + d.id + '/import', { method: 'POST' });
          const out = await r.json().catch(() => ({}));
          // Surface the server's actual error (matches /browse) instead of a
          // generic message, so import failures are diagnosable.
          if (!r.ok) throw new Error(out.detail || 'Could not import deck.');
          _cdImported.add(d.id);
          d.imported = true;
          d.import_label_id = out.label_id || null;
          _cdRenderActions(actions, d, true);
          const n = out.created || 0;
          const switched = await _maybeSwitchLang(d.target_lang);
          const langMsg = switched ? ` · switched to ${langInfo(d.target_lang).name || d.target_lang}` : '';
          showToast((n ? `Imported — ${n} new card${n === 1 ? '' : 's'} added to your deck` : 'Imported — cards already in your deck') + langMsg);
        } catch (e) { btn.disabled = false; btn.textContent = 'Import Deck'; showToast(e.message || 'Could not import deck.'); }
      };
      actions.appendChild(btn);
    }
  }

  // Switch the app's learning language to `lang` if it differs from the current
  // one (live, via the shared nav helper; falls back to a settings PUT). Returns
  // true if it actually switched. Keeps the local `settings` copy in sync.
  async function _maybeSwitchLang(lang) {
    const cur = (settings && settings.default_target_lang) || '';
    if (!lang || lang === cur) return false;
    try {
      if (window.setAppLanguage) {
        const ok = await window.setAppLanguage(lang);
        if (!ok) return false;
      } else {
        await fetch('/api/settings', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ default_target_lang: lang }),
        });
      }
    } catch { return false; }
    if (settings) settings.default_target_lang = lang;
    return true;
  }

  // Close the panel and filter the flashcards to the imported deck's label.
  async function _cdStudyDeck(d) {
    await _maybeSwitchLang(d.target_lang);
    let labelId = d.import_label_id;
    if (!labelId) {
      // The community list doesn't carry the label id — fetch the deck detail.
      try {
        const deck = await fetch('/api/decks/' + d.id).then(r => r.json());
        labelId = deck && deck.import_label_id;
      } catch {}
    }
    if (labelId) {
      closeCommunityPanel();
      window.location.href = '/?study=1&label_id=' + labelId;
    } else {
      window.location.href = '/browse?tab=decks';
    }
  }
