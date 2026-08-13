
  // ── State ────────────────────────────────────────────────────────────────
  let languages = [];
  let targetLang = 'yue';
  let sourceIsTarget = false;          // false: en → target, true: target → en
  let currentCardId = null;
  let audioEl = null;
  let allLabels = [];
  let stickyPicker = null;
  let savedNotes = '';
  let aiNotes = '';                    // notes returned by AI, automatically saved
  let lastTranslation = null;          // last translate result (with candidates)

  function langByCode(code) {
    return languages.find(l => l.code === code) || null;
  }

  // Tag the page with the target language's writing system so target-text
  // elements (.output-chinese, .candidate-target) use the right font.
  function applyScript(code) {
    const l = langByCode(code);
    const fam = (l && l.script_family) || 'latin';
    document.body.classList.remove('script-chinese', 'script-devanagari', 'script-telugu', 'script-hangul', 'script-japanese', 'script-bengali', 'script-arabic', 'script-cyrillic', 'script-latin', 'script-greek', 'script-thai', 'script-hebrew');
    document.body.classList.add('script-' + fam);
  }

  // ── Language ─────────────────────────────────────────────────────────────
  async function loadLanguages() {
    try {
      const res = await fetch('/api/languages').then(r => r.json());
      languages = res.languages;
    } catch {
      languages = [{ code: 'yue', name: 'Cantonese', flag: '🇭🇰', romanization: 'jyutping', logographic: true }];
    }
    updateDirectionButtons();
  }

  function setDirection(isTarget) {
    sourceIsTarget = isTarget;
    updateDirectionButtons();
    resetOutput();
  }

  function swapLang() {
    setDirection(!sourceIsTarget);
  }

  function updateDirectionButtons() {
    applyScript(targetLang);
    const lang = langByCode(targetLang);
    const langName = lang ? lang.name : targetLang;
    const langLabel = (lang && lang.flag ? lang.flag + ' ' : '') + langName;
    const src = document.getElementById('btn-source');
    const tgt = document.getElementById('btn-target');
    if (src) src.classList.toggle('active', !sourceIsTarget);
    if (tgt) { tgt.classList.toggle('active', sourceIsTarget); tgt.textContent = langLabel; }
    document.getElementById('input').placeholder = sourceIsTarget
      ? `Enter ${langName} text…`
      : 'Enter English text…';
  }

  // ── Input ────────────────────────────────────────────────────────────────
  function onInput() {
    document.getElementById('char-count').textContent = document.getElementById('input').value.length;
  }

  function onContextInput() {
    const has = document.getElementById('context-input').value.trim().length > 0;
    document.getElementById('context-summary-set').style.display = has ? '' : 'none';
    document.querySelector('#context-details .context-summary-label').textContent =
      has ? 'Context for translator' : '+ Add context for translator';
  }

  function onNotesInput() {
    document.getElementById('notes-save-btn').disabled =
      (document.getElementById('notes-input').value === savedNotes);
    document.getElementById('notes-saved-msg').textContent = '';
  }

  function clearInput() {
    document.getElementById('input').value = '';
    document.getElementById('char-count').textContent = '0';
    resetOutput();
  }

  function resetOutput() {
    document.getElementById('candidate-panel').style.display = 'none';
    document.getElementById('output-panel').style.display = 'none';
    document.getElementById('saved-indicator').classList.remove('show');
    document.getElementById('notes-input').value = '';
    document.getElementById('notes-save-btn').disabled = true;
    document.getElementById('notes-saved-msg').textContent = '';
    document.getElementById('output-notes-details').open = false;
    document.getElementById('notes-summary-empty').style.display = '';
    document.getElementById('notes-summary-full').style.display = 'none';
    document.getElementById('ai-notes-block').style.display = 'none';
    savedNotes = '';
    aiNotes = '';
    currentCardId = null;
    lastTranslation = null;
    if (audioEl) { audioEl.pause(); audioEl = null; }
    document.getElementById('audio-btn').classList.remove('playing');
  }

  // ── Labels ───────────────────────────────────────────────────────────────
  async function loadLabels() {
    try {
      const { labels } = await fetch('/api/labels').then(r => r.json());
      allLabels = labels;
    } catch {
      allLabels = [];
    }

    if (stickyPicker) {
      stickyPicker.setLabels(allLabels);
    } else {
      stickyPicker = new LabelPicker(
        document.getElementById('sticky-label-picker'),
        {
          allLabels,
          initialIds: [],
          allowCreate: true,
          mode: 'multi',
          placeholder: 'Search or create a label…',
          onChange: ids => {
            if (currentCardId) syncCurrentCardLabels();
          },
          onLabelsRefreshed: labels => { allLabels = labels; },
        }
      );
    }
  }

  function renderOutputLabels(autoLabels) {
    const el = document.getElementById('output-labels');
    el.innerHTML = '';
    const parts = [];
    if (stickyPicker) {
      const sel = stickyPicker.getSelected();
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

  async function syncCurrentCardLabels() {
    if (!currentCardId || !stickyPicker) return;
    await persistCard();
    renderOutputLabels();
  }

  // classifier display in output — stored on lastTranslation

  // ── Notes ────────────────────────────────────────────────────────────────
  async function saveNotes() {
    if (!currentCardId) return;
    const notes = document.getElementById('notes-input').value;
    const btn = document.getElementById('notes-save-btn');
    btn.disabled = true;
    try {
      savedNotes = notes;
      await persistCard();
      document.getElementById('notes-saved-msg').textContent = 'Saved';
      setTimeout(() => { document.getElementById('notes-saved-msg').textContent = ''; }, 2000);
      updateNotesSummary();
    } catch {
      showToast('Could not save note.');
      btn.disabled = false;
    }
  }

  function updateNotesSummary() {
    const has = savedNotes.trim().length > 0;
    document.getElementById('notes-summary-empty').style.display = has ? 'none' : '';
    document.getElementById('notes-summary-full').style.display = has ? '' : 'none';
  }

  function combinedNotes() {
    const parts = [];
    if (aiNotes.trim()) parts.push(aiNotes.trim());
    if (savedNotes.trim()) parts.push(savedNotes.trim());
    return parts.join('\n\n');
  }

  // ── Persist (update card on backend) ─────────────────────────────────────
  async function persistCard() {
    if (!currentCardId) return;
    const target = document.getElementById('out-target').textContent;
    const roman  = document.getElementById('out-romanization').textContent;
    const english = sourceIsTarget
      ? document.getElementById('out-english').textContent
      : document.getElementById('input').value.trim();
    const res = await fetch(`/api/cards/${currentCardId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_text: english,
        target_text: target,
        romanization: roman,
        notes: combinedNotes(),
        label_ids: stickyPicker ? [...stickyPicker.getSelected()] : [],
      }),
    });
    if (!res.ok) throw new Error('persist failed');
  }

  // ── Translate ────────────────────────────────────────────────────────────
  async function doTranslate() {
    const text = document.getElementById('input').value.trim();
    if (!text) return;

    const context = document.getElementById('context-input').value.trim();
    const btn = document.getElementById('translate-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    resetOutput();

    try {
      const res = await fetch('/api/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          target_lang: targetLang,
          source_is_target: sourceIsTarget,
          context: context || null,
        }),
      });
      if (!res.ok) {
        let msg = 'Translation failed — please try again.';
        try { msg = (await res.json()).detail || msg; } catch {}
        throw new Error(msg);
      }
      lastTranslation = await res.json();

      if ((lastTranslation.candidates || []).length > 1) {
        renderCandidates(lastTranslation.candidates);
      } else if (lastTranslation.candidates && lastTranslation.candidates.length === 1) {
        await chooseCandidate(0);
      } else {
        showToast('No translation returned.');
      }
    } catch (e) {
      showToast(e.message || 'Translation failed — please try again.');
      console.error(e);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Translate';
    }
  }

  function renderCandidates(candidates) {
    const panel = document.getElementById('candidate-panel');
    const list = document.getElementById('candidate-list');
    list.innerHTML = '';
    const lang = langByCode(targetLang);
    candidates.forEach((c, i) => {
      const row = document.createElement('div');
      row.className = 'candidate-option';
      row.onclick = () => chooseCandidate(i);
      const inner = document.createElement('div');
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

  async function chooseCandidate(index) {
    if (!lastTranslation) return;
    const c = lastTranslation.candidates[index];
    aiNotes = c.notes || '';
    savedNotes = '';

    document.getElementById('candidate-panel').style.display = 'none';
    document.getElementById('translate-btn').disabled = true;
    document.getElementById('translate-btn').innerHTML = '<span class="spinner"></span>';

    try {
      const res = await fetch('/api/cards', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: c.english,
          target_text: c.target_text,
          romanization: c.romanization || '',
          target_lang: targetLang,
          notes: aiNotes || null,
          priority: lastTranslation.priority || 3,
          label_ids: stickyPicker ? [...stickyPicker.getSelected()] : [],
          suggested_labels: lastTranslation.suggested_labels || [],
          classifier: lastTranslation.classifier || '',
          cefr_level: lastTranslation.cefr_level || null,
        }),
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      currentCardId = data.card_id;
      renderResult(c, lastTranslation.suggested_labels || []);
    } catch {
      showToast('Could not save card — try again.');
    } finally {
      document.getElementById('translate-btn').disabled = false;
      document.getElementById('translate-btn').textContent = 'Translate';
    }
  }

  function renderResult(c, autoLabels) {
    const lang = langByCode(targetLang);
    document.getElementById('out-target').textContent = c.target_text;
    document.getElementById('out-romanization').textContent =
      (lang && lang.logographic) ? (c.romanization || '') : '';
    document.getElementById('out-romanization').style.display =
      (lang && lang.logographic && c.romanization) ? '' : 'none';

    const classifierEl = document.getElementById('out-classifier');
    const classifier = lastTranslation ? (lastTranslation.classifier || '') : '';
    if (classifier) {
      classifierEl.textContent = classifier;
      classifierEl.style.display = '';
    } else {
      classifierEl.style.display = 'none';
    }

    const englishEl = document.getElementById('out-english');
    if (sourceIsTarget) {
      englishEl.textContent = c.english;
      englishEl.style.display = 'block';
    } else {
      englishEl.textContent = '';
      englishEl.style.display = 'none';
    }

    // AI notes
    if (aiNotes && aiNotes.trim()) {
      document.getElementById('ai-notes-text').textContent = aiNotes.trim();
      document.getElementById('ai-notes-block').style.display = '';
    } else {
      document.getElementById('ai-notes-block').style.display = 'none';
    }

    renderOutputLabels(autoLabels || []);
    document.getElementById('output-panel').style.display = 'block';

    const ind = document.getElementById('saved-indicator');
    ind.classList.add('show');
    setTimeout(() => ind.classList.remove('show'), 3000);

    if (audioEl) { audioEl.pause(); audioEl = null; }
    document.getElementById('audio-btn').classList.remove('playing');
  }

  // ── Audio ────────────────────────────────────────────────────────────────
  function playAudio() {
    if (!currentCardId) return;
    if (audioEl && !audioEl.paused) {
      audioEl.pause();
      document.getElementById('audio-btn').classList.remove('playing');
      return;
    }
    audioEl = new Audio(`/api/audio/${currentCardId}`);
    document.getElementById('audio-btn').classList.add('playing');
    audioEl.onended = () => document.getElementById('audio-btn').classList.remove('playing');
    // Without a catch the button sticks in 'playing' forever and the failure
    // is invisible — audio is synthesised lazily, so it can fail transiently.
    try { CantoShell.prepareAudio(audioEl); } catch {}
    audioEl.play().catch(() => {
      document.getElementById('audio-btn').classList.remove('playing');
    });
  }

  // ── Misc ─────────────────────────────────────────────────────────────────
  async function loadDueCount() {
    try {
      const { count } = await fetch('/api/cards/due-count').then(r => r.json());
      if (count > 0) {
        document.querySelectorAll('.due-badge').forEach(b => {
          b.textContent = count; b.classList.add('visible');
        });
      }
    } catch {}
  }


  async function loadStreak() {
    try {
      const { streak, points } = await fetch('/api/streak').then(r => r.json());
      if (window.renderHeaderStats) { window.renderHeaderStats(streak || 0, points || 0); return; }
      const _flame = `<svg viewBox="0 0 16 20" width="13" height="16" aria-hidden="true"><path fill="#f4702a" d="M8 0C5.5 3.5 3 6.5 3 10.5a5 5 0 0010 0c0-2-.9-3.8-1.8-4.8-.4 1.6-1.1 2.6-2 2.2.4-2.5.2-5.2-1.2-7.9z"/></svg>`;
      const _star  = `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg>`;
      const fmtN = n => n >= 10000 ? Math.round(n/1000)+'k' : n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,'')+'k' : String(n);
      const numSpan = n => { const s=fmtN(n),f=n.toLocaleString(); return s===f?s:`<span style="cursor:pointer" title="${f}" onclick="this.textContent=this.textContent==='${s}'?'${f}':'${s}'">${s}</span>`; };
      const ic = n => `<span style="display:inline-flex;align-items:center;gap:3px">${n}</span>`;
      const parts = [];
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

  async function loadSettings() {
    try {
      const s = await fetch('/api/settings').then(r => r.json());
      if (s.default_target_lang) targetLang = s.default_target_lang;
      updateDirectionButtons();
    } catch {}
  }



  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 4000);
  }

  document.addEventListener('click', e => {
    if (!e.target.closest('header')) {
      const dd = document.getElementById('nav-dropdown');
      if (dd) dd.classList.remove('open');
    }
  });

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      doTranslate();
    }
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  (async () => {
    await loadLanguages();
    await Promise.all([loadSettings(), loadDueCount(), loadLabels(), loadStreak()]);
  })();

  document.addEventListener('langchange', function () {
    loadSettings(); loadDueCount();
  });
