
  // ── State ──────────────────────────────────────────────────────────────────
  // Preload (translate + TTS) the current page plus this many ahead, so long
  // imports stream in lazily instead of firing one LLM/TTS call per sentence on
  // open. The rest stays on-demand (tap a sentence/word → single live call).
  const PRELOAD_AHEAD_PAGES = 1;
  const PRELOAD_SENT_WINDOW = 10;
  let _preloadPromises = {};   // window-start sentence idx → in-flight/settled preload
  let _canPreload = false;     // false for community stories (not owned → no /preload)

  let pageBreaks = [];         // [0, n1, n2, …] — sentence index where each page starts

  let selectedDifficulty = 'B1';

  let languages = [];
  let selectedLang = 'yue';
  let currentTextId = null;
  let currentTargetLang = 'yue';
  let currentTitle = '';
  let sentences = [];         // [{tokens, text}]
  let currentPage = 0;
  let currentSentenceIdx = null;
  let activeWordEl = null;
  let tooltipWordData = null;
  let panelTranslationVisible = true;
  let romanizationOn = false;
  let romanizationMap = null;  // word → romanization, null = not yet loaded

  // ── Init ───────────────────────────────────────────────────────────────────
  async function init() {
    await Promise.all([loadLanguages(), loadDefaultLang()]);
    populateCommLangFilter();
    loadSavedTexts();
    loadStreak();
    // Deep link from Home's "New story": open the generate form directly.
    if (new URLSearchParams(location.search).get('new') === '1') {
      history.replaceState(null, '', '/reader');
      toggleGenForm(true);
      document.getElementById('gen-prompt').focus();
    }
  }

  async function loadDefaultLang() {
    try {
      const s = await fetch('/api/settings').then(r => r.json());
      if (s.default_target_lang) selectedLang = s.default_target_lang;
      if (s.default_reader_difficulty) {
        selectedDifficulty = s.default_reader_difficulty;
        const sel = document.getElementById('gen-difficulty');
        if (sel) sel.value = selectedDifficulty;
      }
    } catch {}
  }

  async function saveDifficultyPref() {
    const val = document.getElementById('gen-difficulty').value;
    selectedDifficulty = val;
    try {
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ default_reader_difficulty: val }),
      });
    } catch {}
  }

  async function loadLanguages() {
    try {
      const res = await fetch('/api/languages').then(r => r.json());
      languages = res.languages;
    } catch {
      languages = [{ code: 'yue', name: 'Cantonese', flag: '🇭🇰' }];
    }
  }

  function populateCommLangFilter() {
    const sel = document.getElementById('comm-lang');
    if (!sel) return;
    let opts = '<option value="">All languages</option>';
    languages.forEach(l => {
      opts += `<option value="${l.code}"${l.code === selectedLang ? ' selected' : ''}>${l.flag || ''} ${l.name}</option>`;
    });
    sel.innerHTML = opts;
  }

  function scriptFamilyOf(code) {
    const l = languages.find(x => x.code === code);
    return (l && l.script_family) || 'latin';
  }
  function applyReaderScript(el, code) {
    if (!el) return;
    el.classList.remove('script-chinese', 'script-devanagari', 'script-telugu', 'script-hangul', 'script-japanese', 'script-bengali', 'script-arabic', 'script-cyrillic', 'script-latin', 'script-greek', 'script-thai', 'script-hebrew');
    el.classList.add('script-' + scriptFamilyOf(code));
  }



  // ── Saved texts ────────────────────────────────────────────────────────────
  function toggleGenForm(force) {
    const form = document.getElementById('gen-form');
    const card = document.getElementById('new-reading-card');
    const open = force !== undefined ? force : !form.classList.contains('open');
    form.classList.toggle('open', open);
    card.classList.toggle('open', open);
  }

  async function loadSavedTexts() {
    const res = await fetch('/api/reader/texts').then(r => r.json());
    const texts = res.texts || [];
    const section = document.getElementById('saved-section');
    if (!texts.length) { section.style.display = 'none'; toggleGenForm(true); return; }
    section.style.display = '';
    const list = document.getElementById('saved-list');
    list.innerHTML = '';
    texts.forEach(t => {
      const langInfo = languages.find(l => l.code === t.target_lang);
      const flag = langInfo?.flag || '';
      const date = new Date(t.created_at + 'Z').toLocaleDateString();
      const vis = t.visibility || 'private';
      const item = document.createElement('div');
      item.className = 'saved-item';
      const grads = ['linear-gradient(160deg,#146b5c,#3fa98f)', 'linear-gradient(160deg,#e4572e,#dfa32e)',
                     'linear-gradient(160deg,#4f46a5,#9a92e8)', 'linear-gradient(160deg,#2c7fb8,#7cc1e8)'];
      const coverCh = (t.title || '?').trim().charAt(0) || '?';
      item.innerHTML = `
        <div class="saved-cover" style="background:${grads[t.id % 4]}">${esc(coverCh)}</div>
        <div class="saved-item-info">
          <div class="saved-item-title">${esc(t.title)}</div>
          <div class="saved-chips">
            <span class="schip">${flag} ${langInfo?.name || t.target_lang}</span>
            ${t.difficulty ? `<span class="schip schip-cefr">${esc(t.difficulty)}</span>` : ''}
            <span class="schip">${date}</span>
          </div>
        </div>
        <div class="saved-item-actions">
          <select class="settings-select saved-item-share${vis !== 'private' ? ' shared' : ''}" title="Sharing"
                  onclick="event.stopPropagation()" onchange="shareSavedText(event, ${t.id})">
            <option value="private"${vis === 'private' ? ' selected' : ''}>🔒 Private</option>
            <option value="friends"${vis === 'friends' ? ' selected' : ''}>👥 Friends</option>
            <option value="public"${vis === 'public' ? ' selected' : ''}>🌐 Public</option>
          </select>
          <button class="saved-item-delete" title="Delete" onclick="deleteText(event,${t.id})">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
          </button>
        </div>`;
      item.querySelector('.saved-item-info').onclick = () => openSavedText(t.id);
      item.querySelector('.saved-cover').onclick = () => openSavedText(t.id);
      list.appendChild(item);
    });
  }

  async function deleteText(e, id) {
    e.stopPropagation();
    if (!confirm('Delete this text?')) return;
    await fetch(`/api/reader/texts/${id}`, { method: 'DELETE' });
    loadSavedTexts();
  }

  async function openSavedText(id) {
    showLoadingView();
    setStep('generate', 'done');
    try {
      const data = await fetch(`/api/reader/texts/${id}`).then(r => r.json());
      if (data.preload_complete) {
        setStep('preload', 'done');
        showReaderView(data);
      } else {
        // Only preload the first window; the rest streams in as the reader pages.
        setStep('preload', 'active');
        const preloaded = await fetch(`/api/reader/texts/${id}/preload?start=0&count=${PRELOAD_SENT_WINDOW}`, { method: 'POST' }).then(r => r.json());
        setStep('preload', 'done');
        showReaderView({ ...data, sentences: preloaded.sentences });
      }
    } catch (err) {
      showToast('Failed to load text');
      showHome();
    }
  }

  // ── Image upload helpers ────────────────────────────────────────────────────
  function onImageSelect(input) {
    const file = input.files?.[0];
    const preview = document.getElementById('gen-image-preview');
    const name = document.getElementById('gen-image-name');
    const clear = document.getElementById('gen-image-clear');
    if (file) {
      preview.src = URL.createObjectURL(file);
      preview.style.display = 'block';
      name.textContent = file.name;
      clear.style.display = '';
    }
  }

  function clearImage() {
    document.getElementById('gen-image-input').value = '';
    document.getElementById('gen-image-preview').style.display = 'none';
    document.getElementById('gen-image-name').textContent = '';
    document.getElementById('gen-image-clear').style.display = 'none';
  }

  // ── Generate mode (AI Story / From URL / From PDF) ───────────────────────────
  let genMode = 'ai';
  function setGenMode(m) {
    genMode = m;
    document.querySelectorAll('.gen-mode-tab').forEach(t => t.classList.toggle('active', t.dataset.mode === m));
    document.getElementById('gen-pane-ai').style.display  = m === 'ai'  ? '' : 'none';
    document.getElementById('gen-pane-url').style.display = m === 'url' ? '' : 'none';
    document.getElementById('gen-pane-pdf').style.display = m === 'pdf' ? '' : 'none';
  }

  function onPdfSelect(input) {
    const file = input.files?.[0];
    document.getElementById('gen-pdf-name').textContent = file ? file.name : 'No file chosen';
    document.getElementById('gen-pdf-clear').style.display = file ? '' : 'none';
  }

  function clearPdf() {
    document.getElementById('gen-pdf-input').value = '';
    document.getElementById('gen-pdf-name').textContent = 'No file chosen';
    document.getElementById('gen-pdf-clear').style.display = 'none';
  }

  // ── Generate ───────────────────────────────────────────────────────────────
  // Read an error response safely — a 500 may send plain text (not JSON), and
  // res.json() on that throws a cryptic DOMException ("string didn't match the
  // expected pattern") that would otherwise mask the real error.
  async function errDetail(res, fallback) {
    try {
      const t = await res.text();
      try { return JSON.parse(t).detail || t || fallback; }
      catch { return t || fallback; }
    } catch { return fallback; }
  }

  async function generate() {
    const imageFile = document.getElementById('gen-image-input').files?.[0];
    const pdfFile = document.getElementById('gen-pdf-input').files?.[0];
    const urlVal = document.getElementById('gen-url-input').value.trim();
    // Validate the active mode before showing the loading view.
    if (genMode === 'url' && !urlVal) { showToast('Paste an article URL first.'); return; }
    if (genMode === 'pdf' && !pdfFile) { showToast('Choose a PDF first.'); return; }

    const btn = document.getElementById('gen-btn');
    btn.disabled = true;

    showLoadingView();
    document.querySelector('#step-generate span').textContent =
      genMode === 'ai' ? 'Generating text' : 'Extracting text';
    setStep('generate', 'active');

    try {
      let res;
      if (genMode === 'pdf') {
        const fd = new FormData();
        fd.append('file', pdfFile);
        fd.append('target_lang', selectedLang);
        fd.append('difficulty', document.getElementById('gen-pdf-difficulty').value);
        fd.append('in_target_language', document.getElementById('gen-pdf-intarget').checked ? 'true' : 'false');
        res = await fetch('/api/reader/generate-from-pdf', { method: 'POST', body: fd });
      } else if (genMode === 'url') {
        res = await fetch('/api/reader/generate-from-url', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url: urlVal,
            target_lang: selectedLang,
            difficulty: document.getElementById('gen-url-difficulty').value,
            in_target_language: document.getElementById('gen-url-intarget').checked,
          }),
        });
      } else if (imageFile) {
        const fd = new FormData();
        fd.append('file', imageFile);
        fd.append('target_lang', selectedLang);
        fd.append('difficulty', selectedDifficulty);
        fd.append('num_paragraphs', document.getElementById('gen-paragraphs').value || '4');
        fd.append('prompt', document.getElementById('gen-prompt').value.trim());
        res = await fetch('/api/reader/generate-from-image', { method: 'POST', body: fd });
      } else {
        res = await fetch('/api/reader/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt: document.getElementById('gen-prompt').value.trim(),
            target_lang: selectedLang,
            difficulty: selectedDifficulty,
            num_paragraphs: parseInt(document.getElementById('gen-paragraphs').value, 10) || 4,
          }),
        });
      }
      if (!res.ok) { throw new Error(await errDetail(res, 'Generation failed')); }
      const data = await res.json();
      setStep('generate', 'done');
      clearImage();
      clearPdf();
      document.getElementById('gen-url-input').value = '';
      document.getElementById('gen-url-intarget').checked = false;
      document.getElementById('gen-pdf-intarget').checked = false;
      loadSavedTexts();

      setStep('preload', 'active');
      const preloaded = await fetch(`/api/reader/texts/${data.id}/preload?start=0&count=${PRELOAD_SENT_WINDOW}`, { method: 'POST' }).then(r => r.json());
      setStep('preload', 'done');

      showReaderView({ ...data, sentences: preloaded.sentences });
    } catch (err) {
      showToast(err.message || 'Generation failed');
      showHome();
    } finally {
      btn.disabled = false;
    }
  }

  // ── Loading view helpers ───────────────────────────────────────────────────
  function showLoadingView() {
    document.getElementById('home-view').style.display = 'none';
    document.getElementById('reader-view').style.display = 'none';
    document.getElementById('loading-view').style.display = 'block';
    setStep('generate', 'pending');
    setStep('preload', 'pending');
  }

  function setStep(id, state) {
    const el = document.getElementById('step-' + id);
    el.className = 'loading-step' + (state === 'active' ? ' active' : state === 'done' ? ' done' : '');
    const icon = el.querySelector('.step-icon');
    if (state === 'active') icon.innerHTML = '<div class="step-spinner"></div>';
    else if (state === 'done') icon.innerHTML = '<span class="step-check">✓</span>';
    else icon.innerHTML = '<div class="step-dot"></div>';
  }

  // ── Reader view ────────────────────────────────────────────────────────────
  // sentenceCache: map of sentence_idx → {translation, has_audio}
  let sentenceCache = {};

  function showReaderView(data) {
    document.body.classList.add('hide-tabbar');   // open story = full-focus view
    currentTextId = data.id;
    _canPreload = !_isCommunityStory;
    currentTargetLang = data.target_lang;
    currentTitle = data.title || '';
    currentSentenceIdx = null;
    currentPage = 0;
    sentenceCache = {};
    (data.sentences || []).forEach(s => { sentenceCache[s.sentence_idx] = s; });
    _preloadPromises = (data.sentences && data.sentences.length) ? { 0: Promise.resolve() } : {};
    document.getElementById('reader-title').textContent = data.title;
    const imgBanner = document.getElementById('reader-image');
    if (data.image_url) {
      imgBanner.src = data.image_url;
      imgBanner.style.display = '';
      imgBanner.onload = () => { computePageBreaks(); renderPage(currentPage); };
    } else {
      imgBanner.style.display = 'none';
    }
    const isLatin = scriptFamilyOf(data.target_lang) === 'latin';
    document.getElementById('reader-text').classList.toggle('latin', isLatin);
    applyReaderScript(document.getElementById('reader-text'), data.target_lang);
    applyReaderScript(document.getElementById('word-tooltip'), data.target_lang);
    if (data.sentence_groups) {
      sentences = data.sentence_groups.map(toks => ({
        tokens: toks,
        text: toks.map(t => t.text).join('')
      }));
    } else {
      sentences = buildSentences(data.tokens, data.target_lang);
    }
    romanizationOn = false;
    romanizationMap = data.romanization && Object.keys(data.romanization).length ? data.romanization : null;

    // Make the reader view visible BEFORE computing page breaks (needs layout).
    document.getElementById('loading-view').style.display = 'none';
    document.getElementById('home-view').style.display = 'none';
    document.getElementById('reader-view').style.display = 'block';
    document.getElementById('reader-tabs').style.display = 'none';
    document.getElementById('community-view').style.display = 'none';
    if (!_isCommunityStory) {
      document.getElementById('story-author').style.display = 'none';
      document.getElementById('publish-row').style.display = 'none';
    }

    document.getElementById('reader-vocab-btn').style.display = '';
    document.getElementById('reader-add-all-btn').style.display = data.all_vocab_added ? 'none' : '';
    document.getElementById('reader-quiz-btn').style.display = '';
    const romBtn = document.getElementById('rom-toggle-btn');
    romBtn.style.display = isLatin ? 'none' : '';
    romBtn.classList.remove('active');
    romBtn.classList.add('rom-toggle-off');
    document.getElementById('reader-text').classList.remove('show-rom');

    computePageBreaks();
    renderPage(0);
    resetPanel();
    _isCommunityStory = false;
  }

  async function addAllVocab() {
    const btn = document.getElementById('reader-add-all-btn');
    btn.disabled = true;
    btn.textContent = 'Adding…';
    try {
      const res = await fetch(`/api/reader/texts/${currentTextId}/add-all-vocab`, { method: 'POST' });
      if (!res.ok) throw new Error();
      const { added, skipped, total_new } = await res.json();
      if (total_new === 0) {
        showToast('All words already in your deck!');
        btn.style.display = 'none';
      } else {
        showToast(`Added ${added} card${added !== 1 ? 's' : ''}${skipped ? `, skipped ${skipped}` : ''}`);
        btn.style.display = 'none';
      }
      // Refresh token colours.
      const textRes = await fetch(`/api/reader/texts/${currentTextId}`);
      if (textRes.ok) {
        const data = await textRes.json();
        if (data.sentence_groups) {
          sentences = data.sentence_groups.map(toks => ({
            tokens: toks, text: toks.map(t => t.text).join('')
          }));
        } else {
          sentences = buildSentences(data.tokens, data.target_lang);
        }
        computePageBreaks();
        renderPage(currentPage);
      }
    } catch {
      showToast('Failed to add vocab — try again.');
      btn.disabled = false;
      btn.textContent = '+ Add all vocab';
    }
  }

  // ── Comprehension quiz ────────────────────────────────────────────────────
  function openQuiz() {
    document.getElementById('quiz-modal').style.display = 'flex';
    loadQuiz();
  }

  function closeQuiz() {
    document.getElementById('quiz-modal').style.display = 'none';
  }

  async function loadQuiz() {
    document.getElementById('quiz-loading').style.display = '';
    document.getElementById('quiz-body').style.display = 'none';
    document.getElementById('quiz-feedback').style.display = 'none';
    document.getElementById('quiz-actions').style.display = 'none';
    const text = sentences.map(s => s.text).join(' ');
    try {
      const res = await fetch('/api/reader/comprehension', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, lang: currentTargetLang, title: currentTitle }),
      });
      if (!res.ok) throw new Error(res.status === 402 ? 'quota' : 'error');
      const data = await res.json();
      showQuizQuestion(data);
    } catch (e) {
      document.getElementById('quiz-loading').style.display = 'none';
      document.getElementById('quiz-body').style.display = '';
      document.getElementById('quiz-question').textContent =
        e.message === 'quota' ? 'AI quota reached — add your own Gemini key in Settings.' : 'Could not generate a question. Try again.';
      document.getElementById('quiz-opts').innerHTML = '';
      document.getElementById('quiz-actions').style.display = '';
    }
  }

  function showQuizQuestion(data) {
    document.getElementById('quiz-loading').style.display = 'none';
    document.getElementById('quiz-body').style.display = '';
    document.getElementById('quiz-question').textContent = data.question;
    document.getElementById('quiz-feedback').style.display = 'none';
    document.getElementById('quiz-actions').style.display = 'none';
    const optsEl = document.getElementById('quiz-opts');
    optsEl.innerHTML = '';
    data.options.forEach((opt, i) => {
      const btn = document.createElement('button');
      btn.className = 'quiz-opt';
      btn.textContent = opt;
      btn.onclick = () => answerQuiz(i, data.correct, data.options, optsEl, data.claim_token);
      optsEl.appendChild(btn);
    });
  }

  async function answerQuiz(chosen, correct, options, optsEl, claimToken) {
    const btns = optsEl.querySelectorAll('.quiz-opt');
    btns.forEach(b => { b.disabled = true; });
    btns[correct].classList.add('correct');
    const isCorrect = chosen === correct;
    if (!isCorrect) btns[chosen].classList.add('wrong');
    const fb = document.getElementById('quiz-feedback');
    if (isCorrect) {
      const star = `<svg style="display:inline-block;vertical-align:-0.15em" viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg>`;
      fb.className = 'quiz-feedback correct';
      fb.innerHTML = `Correct! +10 ${star} XP`;
      try {
        await fetch('/api/reader/comprehension/xp', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ lang: currentTargetLang, claim_token: claimToken }),
        });
        loadStreak();
      } catch {}
    } else {
      fb.className = 'quiz-feedback wrong';
      fb.textContent = `Not quite — the answer was: ${options[correct]}`;
    }
    fb.style.display = '';
    document.getElementById('quiz-actions').style.display = '';
  }

  async function reviewVocab() {
    try {
      const res = await fetch(`/api/reader/texts/${currentTextId}/vocab-label`);
      if (!res.ok) return;
      const { id } = await res.json();
      window.location.href = `/cards?study=1&label_id=${id}`;
    } catch {}
  }

  function showHome() {
    document.body.classList.remove('hide-tabbar');
    if (audioMode) audioStop();
    document.getElementById('reader-view').style.display = 'none';
    document.getElementById('loading-view').style.display = 'none';
    const activeTab = document.querySelector('.reader-tab.active');
    const tab = activeTab ? activeTab.textContent.trim() : 'My Stories';
    document.getElementById('home-view').style.display = tab === 'My Stories' ? 'block' : 'none';
    document.getElementById('community-view').style.display = tab === 'Community' ? 'block' : 'none';
    document.getElementById('reader-tabs').style.display = '';
    closeTooltip();
    currentSentenceIdx = null;
  }

  // ── Tabs ─────────────────────────────────────────────────────────────────
  let _currentTab = 'mine';
  let _isCommunityStory = false;

  function switchTab(tab) {
    _currentTab = tab;
    document.querySelectorAll('.reader-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.reader-tab')[tab === 'mine' ? 0 : 1].classList.add('active');
    document.getElementById('home-view').style.display = tab === 'mine' ? 'block' : 'none';
    document.getElementById('community-view').style.display = tab === 'community' ? 'block' : 'none';
    document.getElementById('reader-view').style.display = 'none';
    document.body.classList.remove('hide-tabbar');
    if (tab === 'community') loadCommunity();
  }

  // ── Community stories ──────────────────────────────────────────────────
  let _commDebounce = null;
  function loadCommunity() {
    clearTimeout(_commDebounce);
    _commDebounce = setTimeout(_loadCommunity, 200);
  }

  async function _loadCommunity() {
    const params = new URLSearchParams();
    const s = document.getElementById('comm-search').value.trim();
    const lang = document.getElementById('comm-lang').value;
    const d = document.getElementById('comm-difficulty').value;
    const l = document.getElementById('comm-length').value;
    const o = document.getElementById('comm-sort').value;
    if (s) params.set('search', s);
    if (lang) params.set('lang', lang);
    if (d) params.set('difficulty', d);
    if (l) params.set('length', l);
    if (o) params.set('sort', o);
    const list = document.getElementById('community-list');
    try {
      const res = await fetch('/api/reader/community?' + params).then(r => r.json());
      const stories = res.stories || [];
      if (!stories.length) {
        list.innerHTML = '<div class="community-empty">No stories found. Be the first to publish one!</div>';
        return;
      }
      list.innerHTML = '';
      stories.forEach(st => {
        const langInfo = languages.find(x => x.code === st.target_lang);
        const flag = langInfo?.flag || '';
        const date = new Date(st.created_at + 'Z').toLocaleDateString();
        const stars = st.avg_rating > 0
          ? _starsHtml(Math.round(st.avg_rating)) + ` <span style="color:var(--text-muted)">(${st.rating_count})</span>`
          : '';
        const lenLabel = st.content_length < 300 ? 'Short' : st.content_length < 1000 ? 'Medium' : 'Long';
        const card = document.createElement('div');
        card.className = 'community-card';
        card.innerHTML = `
          <div class="community-card-title">${esc(st.title)}</div>
          <div class="community-card-meta">
            <span>${flag} ${st.difficulty || ''}</span>
            <span>${lenLabel}</span>
            <span>by ${esc(st.author)}</span>
            <span>${stars}</span>
            <span>${date}</span>
          </div>`;
        card.onclick = () => openCommunityStory(st.id);
        list.appendChild(card);
      });
    } catch {
      list.innerHTML = '<div class="community-empty">Could not load stories.</div>';
    }
  }

  function _starsHtml(n) {
    let h = '';
    for (let i = 1; i <= 5; i++) h += `<span class="star${i <= n ? ' filled' : ''}">★</span>`;
    return h;
  }

  async function openCommunityStory(id) {
    _isCommunityStory = true;
    document.getElementById('reader-tabs').style.display = 'none';
    document.getElementById('community-view').style.display = 'none';
    document.getElementById('loading-view').style.display = 'block';
    setStep('generate', 'done');
    setStep('preload', 'active');
    try {
      const data = await fetch(`/api/reader/community/${id}`).then(r => r.json());
      setStep('preload', 'done');
      _showCommunityReader(data);
    } catch {
      showToast('Failed to load story');
      showHome();
    }
  }

  function _showCommunityReader(data) {
    showReaderView(data);
    document.getElementById('reader-vocab-btn').style.display = 'none';
    document.getElementById('reader-add-all-btn').style.display = 'none';
    document.getElementById('reader-quiz-btn').style.display = 'none';
    const authorEl = document.getElementById('story-author');
    authorEl.textContent = `By ${data.author || 'unknown'} · ${data.difficulty || ''}`;
    authorEl.style.display = '';
    document.getElementById('publish-row').style.display = '';
    _showRating(data.avg_rating || 0, data.rating_count || 0, data.user_rating, data.id);
  }

  // ── Publishing ─────────────────────────────────────────────────────────
  // Share/publish directly from the story list (no need to open the story).
  async function shareSavedText(e, id) {
    e.stopPropagation();
    const sel = e.target;
    const vis = sel.value;
    try {
      await fetch(`/api/reader/texts/${id}/publish`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ visibility: vis }),
      });
      sel.classList.toggle('shared', vis !== 'private');
      showToast(vis === 'private' ? 'Story set to private'
                : vis === 'friends' ? 'Shared with friends' : 'Published publicly');
    } catch { showToast('Failed to update sharing'); }
  }

  // ── Rating ─────────────────────────────────────────────────────────────
  function _showRating(avg, count, userRating, textId) {
    const section = document.getElementById('rating-section');
    section.style.display = '';
    const starsEl = document.getElementById('rating-stars');
    starsEl.innerHTML = '';
    for (let i = 1; i <= 5; i++) {
      const s = document.createElement('span');
      s.className = 'star' + (i <= (userRating || 0) ? ' filled' : '');
      s.textContent = '★';
      s.onclick = async (e) => {
        e.stopPropagation();
        try {
          await fetch(`/api/reader/texts/${textId}/rate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rating: i }),
          });
          starsEl.querySelectorAll('.star').forEach((st, idx) => {
            st.classList.toggle('filled', idx < i);
          });
          showToast('Rating saved');
        } catch { showToast('Failed to rate'); }
      };
      starsEl.appendChild(s);
    }
    const info = document.getElementById('rating-info');
    info.textContent = count > 0 ? `${avg.toFixed(1)} (${count})` : 'No ratings yet';
  }

  // Split flat token list into sentence groups, split on sentence-ending punctuation.
  // Thai writes no sentence punctuation and uses spaces as boundaries; Greek uses
  // ';' (and U+037E) where other languages use '?'. Mirror tokenizer.split_sentences.
  function buildSentences(tokens, lang) {
    const result = [];
    let buf = [];
    let quoteDepth = 0;
    const openQuotes = new Set(['“', '«', '「', '『']);
    const closeQuotes = new Set(['”', '»', '」', '』']);
    const sentenceEnders = lang === 'el' ? /[。！？.!?।॥;;\n]/ : /[。！？.!?।॥\n]/;
    const splitOnSpace = lang === 'th';

    // Mirror tokenizer.split_sentences EXACTLY: it strips each sentence and
    // drops whitespace-only ones, so a '\n' paragraph break is NOT a sentence.
    // The translation cache is keyed by the server's sentence_idx; if the
    // client kept these empty groups, every sentence after a paragraph break
    // would show a later sentence's translation (off-by-N). Trim edge-
    // whitespace tokens and drop empty groups to stay index-aligned.
    const flush = () => {
      let a = 0, b = buf.length;
      while (a < b && !buf[a].is_word && buf[a].text.trim() === '') a++;
      while (b > a && !buf[b - 1].is_word && buf[b - 1].text.trim() === '') b--;
      const trimmed = buf.slice(a, b);
      if (trimmed.length) result.push(trimmed);
      buf = [];
    };

    tokens.forEach((tok, i) => {
      if (!tok.is_word) {
        for (const ch of tok.text) {
          if (openQuotes.has(ch)) quoteDepth++;
          else if (closeQuotes.has(ch)) quoteDepth = Math.max(0, quoteDepth - 1);
        }
      }
      buf.push(tok);
      let isEnder = !tok.is_word && quoteDepth === 0 && sentenceEnders.test(tok.text);
      if (splitOnSpace && !tok.is_word && quoteDepth === 0 && tok.text.trim() === '') isEnder = true;
      if (isEnder) {
        // Skip numeric periods like "1.5"
        if (tok.text.includes('.') && !tok.text.includes('。') && /\d\.\d/.test(tok.text)) return;
        flush();
      }
    });
    flush();
    return result.map(toks => ({
      tokens: toks,
      text: toks.map(t => t.text).join(''),
    }));
  }

  function _headingLevel(sent) {
    const m = (sent.text || '').match(/^(#{1,6})\s/);
    return m ? m[1].length : 0;
  }

  function _buildSentenceEl(sent, si) {
    const hlevel = _headingLevel(sent);
    const sentEl = document.createElement(hlevel ? 'div' : 'span');
    sentEl.className = 'sentence' + (hlevel ? ' reader-heading reader-h' + hlevel : '');
    sentEl.dataset.si = si;
    let tokens = sent.tokens;
    if (hlevel) {
      // Strip leading "## " marker tokens from display
      let skip = 0;
      while (skip < tokens.length && /^[#\s]+$/.test(tokens[skip].text) && !tokens[skip].is_word) skip++;
      tokens = tokens.slice(skip);
    }
    tokens.forEach(tok => {
      let el;
      if (tok.is_word) {
        el = document.createElement('ruby');
        el.appendChild(document.createTextNode(tok.text));
        const rt = document.createElement('rt');
        rt.textContent = (romanizationMap && romanizationMap[tok.text]) || '';
        el.appendChild(rt);
      } else {
        el = document.createElement('span');
        el.textContent = tok.text;
      }
      el.className = 'token' + (tok.is_word ? ' word ' + (tok.status || 'new') : '');
      if (tok.is_word) el.dataset.word = tok.text;
      sentEl.appendChild(el);
    });
    return sentEl;
  }

  function computePageBreaks() {
    if (!sentences.length) { pageBreaks = [0]; return; }
    const container = document.getElementById('reader-text');
    const nav = document.getElementById('page-nav');
    nav.style.display = '';
    const navH = nav.offsetHeight + 20;
    const containerTop = container.getBoundingClientRect().top;
    const availH = window.innerHeight - containerTop - navH - 8;

    // Simulate actual page rendering: each page starts with an empty container
    // and adds sentences until the next one would overflow. This matches what
    // renderReader does (only the page's sentences in the container), so inline
    // wrapping is measured accurately.
    container.style.visibility = 'hidden';
    pageBreaks = [0];
    let i = 0;
    while (i < sentences.length) {
      container.innerHTML = '';
      let pageH = 0;
      let added = 0;
      while (i + added < sentences.length) {
        const el = _buildSentenceEl(sentences[i + added], i + added);
        container.appendChild(el);
        if (!el.classList.contains('reader-heading')) {
          container.appendChild(document.createTextNode(' '));
        }
        pageH = container.scrollHeight;
        if (pageH > availH && added > 0) {
          container.removeChild(el);
          // also remove the trailing space node we just added
          if (container.lastChild && container.lastChild.nodeType === 3) container.removeChild(container.lastChild);
          break;
        }
        added++;
      }
      i += added;
      if (i < sentences.length) pageBreaks.push(i);
    }
    container.innerHTML = '';
    container.style.visibility = '';
  }

  function _totalPages() { return pageBreaks.length; }
  function _pageStart(p) { return pageBreaks[p] || 0; }
  function _pageEnd(p) { return p + 1 < pageBreaks.length ? pageBreaks[p + 1] : sentences.length; }
  function _pageForSentence(si) {
    for (let p = pageBreaks.length - 1; p >= 0; p--) {
      if (si >= pageBreaks[p]) return p;
    }
    return 0;
  }

  function renderPage(pageIdx) {
    if (pageIdx < 0) pageIdx = 0;
    if (pageIdx >= _totalPages()) pageIdx = _totalPages() - 1;
    currentPage = pageIdx;
    const tp = _totalPages();
    const start = _pageStart(pageIdx);
    const end = _pageEnd(pageIdx);
    const pageSentences = sentences.slice(start, end);

    // Update paging controls
    const nav = document.getElementById('page-nav');
    nav.style.display = '';
    document.getElementById('page-prev').disabled = pageIdx === 0;
    document.getElementById('page-prev').style.visibility = tp > 1 ? '' : 'hidden';
    document.getElementById('page-next').disabled = pageIdx >= tp - 1;
    document.getElementById('page-next').style.visibility = tp > 1 ? '' : 'hidden';
    document.getElementById('page-indicator').textContent = tp > 1 ? `${pageIdx + 1} / ${tp}` : '';

    // Reset panel and tooltip when changing page
    closeTooltip();
    resetPanel();
    currentSentenceIdx = null;

    renderReader(pageSentences, start);
    // Render-ahead: make sure this page + the next are translated/voiced.
    preloadAround(pageIdx);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // Preload a window of sentences (translate + TTS) around the given page so the
  // current and next page are ready, without processing the whole text. Cached
  // sentences are skipped server-side; community stories aren't owned so skip.
  function preloadAround(pageIdx) {
    if (!currentTextId || !_canPreload) return Promise.resolve();
    const start = _pageStart(pageIdx);
    if (start >= sentences.length) return Promise.resolve();
    if (_preloadPromises[start]) return _preloadPromises[start];
    const count = Math.max(PRELOAD_SENT_WINDOW, _pageEnd(Math.min(pageIdx + PRELOAD_AHEAD_PAGES, _totalPages() - 1)) - start);
    const url = `/api/reader/texts/${currentTextId}/preload?start=${start}&count=${count}`;
    const p = (async () => {
      try {
        const res = await fetch(url, { method: 'POST' });
        if (!res.ok) return;
        const d = await res.json();
        (d.sentences || []).forEach(s => { sentenceCache[s.sentence_idx] = s; });
        // If the selected sentence just gained a translation, fill the panel.
        if (currentSentenceIdx !== null) {
          const c = sentenceCache[currentSentenceIdx];
          const el = document.getElementById('panel-translation');
          if (c && c.translation && el && !el.textContent.trim()) el.textContent = c.translation;
        }
      } catch {}
    })();
    _preloadPromises[start] = p;
    return p;
  }

  function goPage(delta) {
    const totalPages = _totalPages();
    const next = Math.max(0, Math.min(totalPages - 1, currentPage + delta));
    if (next !== currentPage) renderPage(next);
  }

  function renderReader(pageSentences, globalOffset) {
    const container = document.getElementById('reader-text');
    container.innerHTML = '';
    pageSentences.forEach((sent, localIdx) => {
      const si = globalOffset + localIdx;
      const sentEl = _buildSentenceEl(sent, si);
      sentEl.addEventListener('click', e => {
        if (!e.target.classList.contains('word')) selectSentence(si, sentEl);
      });
      sentEl.querySelectorAll('.word').forEach(el => {
        const word = el.dataset.word || el.textContent;
        const status = el.classList.contains('known') ? 'known' : el.classList.contains('weak') ? 'weak' : 'new';
        el.dataset.si = si;
        el.addEventListener('click', e => {
          e.stopPropagation();
          onWordTap(el, word, status, si, sentEl);
        });
        let pressTimer;
        el.addEventListener('pointerdown', () => {
          pressTimer = setTimeout(() => onWordTap(el, word, status, si, sentEl), 300);
        });
        el.addEventListener('pointerup',    () => clearTimeout(pressTimer));
        el.addEventListener('pointerleave', () => clearTimeout(pressTimer));
      });
      container.appendChild(sentEl);
      if (!sentEl.classList.contains('reader-heading')) {
        container.appendChild(document.createTextNode(' '));
      }
    });
  }

  // ── Sentence panel ─────────────────────────────────────────────────────────
  function resetPanel() {
    document.getElementById('panel-hint').style.display = '';
    document.getElementById('panel-body').style.display = 'none';
    panelTranslationVisible = false;
  }

  function onPanelClick() {
    if (currentSentenceIdx === null) return;
    panelTranslationVisible = !panelTranslationVisible;
    document.getElementById('panel-content').style.display = panelTranslationVisible ? '' : 'none';
    document.getElementById('panel-reveal-hint').style.display = panelTranslationVisible ? 'none' : '';
  }

  async function selectSentence(si, sentEl) {
    const alreadySelected = currentSentenceIdx === si;

    document.querySelectorAll('.sentence.active').forEach(el => el.classList.remove('active'));
    if (!sentEl) sentEl = document.querySelector(`.sentence[data-si="${si}"]`);
    if (sentEl) sentEl.classList.add('active');
    currentSentenceIdx = si;

    // Keep audio mode in sync so playback resumes from the clicked sentence
    if (audioMode && audioSentenceIdx !== si) {
      audioSentenceIdx = si;
      if (_currentAudio) { _currentAudio.pause(); _currentAudio = null; }
    }

    document.getElementById('panel-hint').style.display = 'none';
    document.getElementById('panel-body').style.display = '';

    document.getElementById('panel-content').style.display = panelTranslationVisible ? '' : 'none';
    document.getElementById('panel-reveal-hint').style.display = panelTranslationVisible ? 'none' : '';

    const cached = sentenceCache[si];
    if (cached?.translation) {
      document.getElementById('panel-translation').textContent = cached.translation;
      return;
    }

    // Fallback: live Gemini call using the sentence translation endpoint
    document.getElementById('panel-translation').innerHTML =
      '<span class="sentence-panel-spinner"></span><span class="sentence-panel-loading">Translating…</span>';
    try {
      const res = await fetch('/api/reader/translate-sentence', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: sentences[si].text, target_lang: currentTargetLang, text_id: currentTextId, sentence_idx: si }),
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      document.getElementById('panel-translation').textContent = data.english || '';
      if (!sentenceCache[si]) sentenceCache[si] = {};
      sentenceCache[si].translation = data.english || '';
      sentenceCache[si].romanization = data.romanization || null;
    } catch {
      document.getElementById('panel-translation').textContent = 'Translation failed.';
    }
  }

  // ── Word tooltip ───────────────────────────────────────────────────────────
  async function onWordTap(el, word, status, si, sentEl) {
    // Select sentence
    selectSentence(si, sentEl);

    // Mark the word as active
    if (activeWordEl) activeWordEl.classList.remove('tapped');
    activeWordEl = el;
    el.classList.add('tapped');

    // Show tooltip with loading state
    positionTooltip(el);
    document.getElementById('tooltip-content').innerHTML =
      `<div class="tooltip-loading"><span class="sentence-panel-spinner"></span> Looking up…</div>`;
    document.getElementById('word-tooltip').style.display = '';

    try {
      const fullText = sentences[si].text;
      const res = await fetch('/api/reader/translate-word', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word, context: fullText, target_lang: currentTargetLang }),
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      tooltipWordData = data;
      renderTooltip(data);
    } catch {
      document.getElementById('tooltip-content').innerHTML =
        `<div class="tooltip-error">Lookup failed — try again.</div>`;
    }
  }

  function positionTooltip(el) {
    const tooltip = document.getElementById('word-tooltip');
    tooltip.style.display = 'block';
    const rect = el.getBoundingClientRect();
    const tw = tooltip.offsetWidth || 240;
    const th = tooltip.offsetHeight || 160;
    const margin = 8;
    let left = rect.left;
    let top  = rect.bottom + margin;
    if (left + tw > window.innerWidth - margin) left = window.innerWidth - tw - margin;
    if (left < margin) left = margin;
    if (top + th > window.innerHeight - margin) top = rect.top - th - margin;
    tooltip.style.left = left + 'px';
    tooltip.style.top  = top  + 'px';
  }

  function renderTooltip(data) {
    const isInDeck = data.source === 'deck';
    const statusLabel = { known: 'Known', weak: 'Weak', new: 'New to deck' }[data.status] || 'New';
    const hasAltMeaning = isInDeck && data.context_source_text;
    document.getElementById('tooltip-content').innerHTML = `
      <div class="tooltip-word">${esc(data.target_text)}</div>
      ${data.romanization ? `<div class="tooltip-rom">${esc(data.romanization)}</div>` : ''}
      <div class="tooltip-eng">${esc(data.source_text)}</div>
      <span class="tooltip-status-badge ${data.status}">${statusLabel}</span>
      ${data.notes ? `<div class="tooltip-notes">${esc(data.notes)}</div>` : ''}
      ${hasAltMeaning ? `
        <div class="tooltip-alt-meaning">
          <span class="tooltip-alt-label">In context:</span> ${esc(data.context_source_text)}
          ${data.context_notes ? `<div class="tooltip-notes">${esc(data.context_notes)}</div>` : ''}
          <button class="tooltip-update-meaning-btn" id="tooltip-update-meaning-btn" onclick="updateCardMeaning()">Update card meaning</button>
        </div>` : ''}
      <div class="tooltip-actions">
        <button class="play-btn" onclick="playText(tooltipWordData.target_text)" title="Play pronunciation">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        </button>
        <button class="tooltip-add-btn ${isInDeck ? 'added' : ''}" id="tooltip-add-btn"
          ${isInDeck ? 'disabled' : ''} onclick="addWordToDeck()">
          ${isInDeck ? 'In deck ✓' : 'Add to deck'}
        </button>
        ${!isInDeck ? `<button class="tooltip-flag-btn" onclick="showToast('Flagging will be available in a future update')">Flag</button>` : ''}
      </div>
    `;
  }

  async function updateCardMeaning() {
    if (!tooltipWordData || !tooltipWordData.card_id || !tooltipWordData.context_source_text) return;
    const btn = document.getElementById('tooltip-update-meaning-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Updating…'; }
    try {
      const res = await fetch(`/api/cards/${tooltipWordData.card_id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text:  tooltipWordData.context_source_text,
          target_text:  tooltipWordData.target_text,
          romanization: tooltipWordData.context_romanization || tooltipWordData.romanization || '',
          notes:        tooltipWordData.context_notes || tooltipWordData.notes || '',
        }),
      });
      if (!res.ok) throw new Error();
      tooltipWordData = { ...tooltipWordData, source_text: tooltipWordData.context_source_text, context_source_text: null };
      document.querySelector('.tooltip-alt-meaning')?.remove();
      document.querySelector('.tooltip-eng').textContent = tooltipWordData.source_text;
      showToast('Card meaning updated');
    } catch {
      if (btn) { btn.disabled = false; btn.textContent = 'Update card meaning'; }
      showToast('Failed to update card');
    }
  }

  async function addWordToDeck() {
    if (!tooltipWordData || tooltipWordData.source === 'deck') return;
    const btn = document.getElementById('tooltip-add-btn');
    btn.disabled = true;
    btn.textContent = 'Adding…';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text:     tooltipWordData.source_text,
          target_text:     tooltipWordData.target_text,
          romanization:    tooltipWordData.romanization || '',
          target_lang:     currentTargetLang,
          notes:           tooltipWordData.notes || '',
          priority:        tooltipWordData.priority || 3,
          label_ids:       [],
          suggested_labels: tooltipWordData.suggested_labels || [],
          classifier:      tooltipWordData.classifier || '',
          reader_text_id:  currentTextId,
        }),
      });
      if (!res.ok) throw new Error();
      tooltipWordData = { ...tooltipWordData, source: 'deck', status: 'weak' };
      btn.textContent = 'In deck ✓';
      btn.classList.add('added');
      // Recolour token
      if (activeWordEl) {
        activeWordEl.classList.remove('new', 'weak', 'known');
        activeWordEl.classList.add('weak');
      }
      showToast('Added to deck');
    } catch {
      btn.disabled = false;
      btn.textContent = 'Add to deck';
      showToast('Failed to add card');
    }
  }

  function closeTooltip() {
    document.getElementById('word-tooltip').style.display = 'none';
    if (activeWordEl) { activeWordEl.classList.remove('tapped'); activeWordEl = null; }
    tooltipWordData = null;
  }

  // Close tooltip when clicking outside it
  document.addEventListener('click', e => {
    const tooltip = document.getElementById('word-tooltip');
    if (tooltip.style.display !== 'none' && !tooltip.contains(e.target) && !e.target.classList.contains('word')) {
      closeTooltip();
    }
  });

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
  document.addEventListener('click', e => {
    const dd = document.getElementById('nav-dropdown');
    if (dd.classList.contains('open') && !e.target.closest('header')) dd.classList.remove('open');
  });
  // ── TTS ────────────────────────────────────────────────────────────────────
  let _currentAudio = null;

  async function playText(text) {
    if (_currentAudio) { _currentAudio.pause(); _currentAudio = null; }
    try {
      const res = await fetch('/api/reader/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, target_lang: currentTargetLang }),
      });
      if (!res.ok) throw new Error();
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      _currentAudio = new Audio(url);
      _currentAudio.onended = () => { URL.revokeObjectURL(url); _currentAudio = null; };
      // Awaited so a rejected play() reaches the catch below — play() returns a
      // promise, so without this the failure escaped and the reader just went
      // quiet with no toast. It resolves when playback STARTS, not when it ends.
      await _currentAudio.play();
    } catch {
      showToast('Audio unavailable');
    }
  }

  async function playUrl(url) {
    if (_currentAudio) { _currentAudio.pause(); _currentAudio = null; }
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error();
      const blob = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      _currentAudio = new Audio(objUrl);
      _currentAudio.onended = () => { URL.revokeObjectURL(objUrl); _currentAudio = null; };
      await _currentAudio.play();   // see playText: unawaited rejections escaped
    } catch {
      showToast('Audio unavailable');
    }
  }

  // ── Toast ──────────────────────────────────────────────────────────────────
  let _toastTimer;
  function showToast(msg) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
  }

  function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Romanization toggle ────────────────────────────────────────────────────
  function toggleRomanization() {
    romanizationOn = !romanizationOn;
    const btn = document.getElementById('rom-toggle-btn');
    btn.classList.toggle('active', romanizationOn);
    btn.classList.toggle('rom-toggle-off', !romanizationOn);
    document.getElementById('reader-text').classList.toggle('show-rom', romanizationOn);
  }

  // ── Audio Mode ─────────────────────────────────────────────────────────────
  let audioMode = false;
  let audioPlaying = false;
  let audioSentenceIdx = 0;
  let _audioModeAbort = false;

  function audioPlayPause() {
    if (!audioMode) {
      // Start audio mode from current page
      audioMode = true;
      audioSentenceIdx = _pageStart(currentPage);
      _audioModeAbort = false;
      audioPlaying = true;
      setAudioPlayPauseUI(true);
      runAudioMode();
    } else if (audioPlaying) {
      audioPlaying = false;
      if (_currentAudio) _currentAudio.pause();
      setAudioPlayPauseUI(false);
    } else {
      audioPlaying = true;
      setAudioPlayPauseUI(true);
      if (_currentAudio) _currentAudio.play().catch(() => showToast('Audio unavailable'));
      else runAudioMode();
    }
  }

  function audioStop() {
    _audioModeAbort = true;
    audioMode = false;
    audioPlaying = false;
    if (_currentAudio) { _currentAudio.pause(); _currentAudio = null; }
    setAudioPlayPauseUI(false);
  }

  function setAudioPlayPauseUI(playing) {
    // Toggle visibility of two pre-rendered SVGs rather than rewriting an
    // <svg>'s innerHTML — Safari/iOS creates innerHTML-injected SVG children in
    // the HTML namespace, so they don't paint until a later reflow (the icon
    // appeared "stuck" until you clicked something else).
    document.getElementById('audio-play-icon').style.display = playing ? 'none' : '';
    document.getElementById('audio-pause-icon').style.display = playing ? '' : 'none';
    document.getElementById('audio-mode-btn').classList.toggle('active', playing);
  }

  async function runAudioMode() {
    while (audioMode && !_audioModeAbort && audioSentenceIdx < sentences.length) {
      if (!audioPlaying) {
        // Paused — wait
        await new Promise(r => setTimeout(r, 100));
        continue;
      }

      const si = audioSentenceIdx;

      // Make sure the right page is showing
      const targetPage = _pageForSentence(si);
      if (targetPage !== currentPage) {
        renderPage(targetPage);
        await new Promise(r => setTimeout(r, 50));
      }

      // Scroll active sentence into view and update the panel
      const sentEl = document.querySelector(`.sentence[data-si="${si}"]`);
      if (sentEl) sentEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      await selectSentence(si, sentEl);

      // Play audio
      await playAudioModeStep(si);

      if (_audioModeAbort) break;

      // Brief pause between sentences
      await new Promise(r => setTimeout(r, 400));

      audioSentenceIdx++;
    }

    if (!_audioModeAbort && audioSentenceIdx >= sentences.length) {
      // Reached the end
        showToast('Audio playback complete');
      audioStop();
    }
  }

  function playAudioModeStep(si) {
    return new Promise(resolve => {
      if (_audioModeAbort) { resolve(); return; }

      const cached = sentenceCache[si];
      let fetchPromise;

      if (cached?.has_audio) {
        fetchPromise = fetch(`/api/reader/texts/${currentTextId}/sentences/${si}/audio`)
          .then(r => { if (!r.ok) throw new Error(); return r.blob(); });
      } else {
        fetchPromise = fetch('/api/reader/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: sentences[si].text, target_lang: currentTargetLang, text_id: currentTextId, sentence_idx: si }),
        }).then(r => { if (!r.ok) throw new Error(); return r.blob(); });
      }

      fetchPromise.then(blob => {
        if (_audioModeAbort) { resolve(); return; }
        const url = URL.createObjectURL(blob);
        _currentAudio = new Audio(url);

        _currentAudio.onended = () => {
          URL.revokeObjectURL(url);
          _currentAudio = null;
          resolve();
        };
        _currentAudio.onerror = () => { URL.revokeObjectURL(url); _currentAudio = null; resolve(); };

        // If paused while fetching, don't auto-play — wait for resume
        if (audioPlaying && !_audioModeAbort) {
          _currentAudio.play().catch(() => resolve());
        } else {
          // Paused state: wait for play to be called
          const checkInterval = setInterval(() => {
            if (_audioModeAbort) { clearInterval(checkInterval); _currentAudio = null; resolve(); return; }
            if (audioPlaying && _currentAudio) {
              clearInterval(checkInterval);
              _currentAudio.play().catch(() => resolve());
            }
          }, 100);
        }
      }).catch(() => resolve());
    });
  }

  init();
  document.addEventListener('langchange', function () { init(); });

  let _resizeTimer;
  window.addEventListener('resize', () => {
    if (document.getElementById('reader-view').style.display === 'none') return;
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      const si = currentSentenceIdx ?? _pageStart(currentPage);
      computePageBreaks();
      renderPage(_pageForSentence(si));
    }, 200);
  });
