
  // ── State ─────────────────────────────────────────────────────────────────
  let _convs = [];
  let _activeConvId = null;
  let _activeConvType = null;  // 'inapp' | 'messenger'
  let _targetLang = 'yue';
  let _pollTimer = null;
  let _eventStream = null;
  let _lastMsgId = 0;
  const _wordStatusCache = {};  // word → 'known'|'weak' (populated by preload; absent = unknown)
  const _msgVocab = {};  // msgId → [{target_text, english, romanization}] for "Add all"

  // ── Ruby helpers (same pipeline as tutor.html) ────────────────────────────
  const RUBY_LANGS = new Set(['yue', 'cmn', 'ko', 'hi', 'te', 'ja', 'bn', 'ur', 'ar', 'ru', 'fa', 'uk', 'el', 'th', 'he']);
  function needsRuby(l) { return RUBY_LANGS.has(l); }
  let rubyDefault = localStorage.getItem('msg_ruby_inline') !== '0';

  const _tokenCache = {};
  function _ck(t) { return _targetLang + '\0' + t; }
  const _playIcon = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';

  // Merge server-provided per-message tokens (from the messages/send response)
  // into the local cache. The server now romanizes inline, so this is the normal
  // path — no separate /api/ruby round trip, always consistent.
  function _ingestTokens(msgs) {
    msgs.forEach(m => {
      if (m.tokens) Object.entries(m.tokens).forEach(([text, toks]) => {
        if (Array.isArray(toks)) _tokenCache[_ck(text)] = toks;
      });
    });
  }

  // Fallback fetch for any text the server didn't tokenize (rare). Crucially,
  // does NOT cache failures — a transient error must not permanently blank a
  // message's romanization (which also broke the あ toggle: nothing to toggle).
  async function fetchTokensBatch(texts) {
    if (!texts.length) return;
    try {
      const res = await fetch('/api/ruby/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ texts, lang: _targetLang }),
      });
      if (!res.ok) return;
      const results = (await res.json()).results || {};
      texts.forEach(t => { if (t in results) _tokenCache[_ck(t)] = results[t]; });
    } catch { /* leave uncached so a later render/toggle can retry */ }
  }

  // Best-effort, non-blocking: prime the deck-status cache so the word tooltip
  // can show a Known/Weak badge instantly. Never blocks rendering.
  async function _preloadWordStatuses(words) {
    if (!words.length) return;
    const toFetch = words.filter(w => !(w in _wordStatusCache)).slice(0, 100);
    if (!toFetch.length) { _applyWordHighlights(); return; }
    try {
      const { statuses } = await fetch('/api/cards/status', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ words: toFetch, lang: _targetLang }),
      }).then(r => r.json());
      Object.assign(_wordStatusCache, statuses || {});
    } catch {}
    _applyWordHighlights();
  }

  function _applyWordHighlights() {
    document.querySelectorAll('.msg-bubble-wrap.theirs .msg-bubble .gl[data-word]').forEach(el => {
      const w = el.dataset.word;
      const st = _wordStatusCache[w];
      el.classList.remove('gl-known', 'gl-weak', 'gl-new');
      if (st === 'known') el.classList.add('gl-known');
      else if (st === 'weak') el.classList.add('gl-weak');
      else el.classList.add('gl-new');
    });
    _updateNewWordPills();
  }

  function _updateNewWordPills() {
    document.querySelectorAll('.msg-bubble-wrap.theirs').forEach(bwrap => {
      const head = bwrap.querySelector('.nw-xtra-head');
      if (!head) return;
      const newCount = bwrap.querySelectorAll('.msg-bubble .gl.gl-new').length;
      if (newCount > 0) {
        head.style.display = '';
        head.querySelector('.nw-count').textContent = newCount;
      } else {
        head.style.display = 'none';
        const xtra = bwrap.querySelector('.nw-xtra');
        if (xtra) xtra.classList.remove('open');
      }
    });
  }

  function _wordsFromMessages(msgs) {
    const s = new Set();
    msgs.forEach(m => {
      if (m.analysis?.type === 'image') return;
      if (needsRuby(_targetLang)) {
        const collect = txt => (_tokenCache[_ck(txt)] || []).forEach(
          tok => { if (tok.is_word && tok.text) s.add(tok.text); });
        if (m.display_text) collect(m.display_text);
        if (m.is_mine) (m.analysis?.corrections || []).forEach(c => { if (c.corrected) collect(c.corrected); });
      } else if (m.display_text && !m.is_mine) {
        m.display_text.split(/\s+/).forEach(w => { if (w) s.add(w); });
      }
    });
    return s;
  }

  // Defensive background fill: the server normally supplies all tokens inline,
  // but if any text is missing (transient hiccup), fetch it and repaint so
  // romanization + the あ toggle always end up working.
  async function _backfillTokens(msgs) {
    if (!needsRuby(_targetLang)) return;
    const missing = new Set();
    msgs.forEach(m => {
      if (m.display_text && !(_ck(m.display_text) in _tokenCache)) missing.add(m.display_text);
      (m.analysis?.corrections || []).forEach(c => {
        if (c.corrected && !(_ck(c.corrected) in _tokenCache)) missing.add(c.corrected);
      });
      (m.analysis?.vocab || []).forEach(v => {
        if (v.target_text && !(_ck(v.target_text) in _tokenCache)) missing.add(v.target_text);
      });
    });
    if (!missing.size) return;
    await fetchTokensBatch([...missing]);
    repaintAllRuby();
  }

  function _tokensToHtml(tokens, showRuby) {
    if (!Array.isArray(tokens) || !tokens.length) return '';
    const hasRoman = tokens.some(t => t.roman);
    return tokens.map(t => {
      const inner = (showRuby && hasRoman && t.is_word && t.roman)
        ? `<ruby>${esc(t.text)}<rt>${esc(t.roman)}</rt></ruby>` : esc(t.text);
      return t.is_word
        ? `<span class="gl" tabindex="0" data-word="${esc(t.text)}">${inner}</span>`
        : esc(t.text);
    }).join('');
  }

  function paintRuby(bwrap) {
    const showRuby = rubyDefault;
    bwrap.querySelectorAll('.rb').forEach(s => {
      const tokens = _tokenCache[_ck(s.dataset.text)];
      if (!tokens || !tokens.length) { s.textContent = s.dataset.text; return; }
      s.innerHTML = _tokensToHtml(tokens, showRuby);
    });
    const btn = bwrap.querySelector('.rb-toggle');
    if (btn) btn.classList.toggle('rom-toggle-off', !showRuby);
  }

  // The あ toggle is a single global preference — flip it and repaint every
  // bubble so romanization shows/hides consistently across the whole thread.
  function repaintAllRuby() {
    document.querySelectorAll('#msg-thread .msg-bubble-wrap').forEach(paintRuby);
  }

  function rbSpan(text) {
    if (!text) return '';
    return `<span class="rb" data-text="${esc(text)}">${esc(text)}</span>`;
  }

  // Detect whether the user typed in their target language or English.
  // Non-Latin scripts are unambiguous; accented Latin chars suggest target.
  // CJK langs where the user might genuinely type English to get a translation.
  // For all other langs (fr, es, de...) plain Latin input is almost certainly
  // the target language -- grammar-check it rather than treating it as English.
  const _CJK_LANGS = new Set(['yue', 'cmn']);

  function detectInputMode(text) {
    if (!text) return 'native';
    if (/[一-鿿぀-ヿ豈-﫿]/.test(text)) return 'target'; // CJK script
    if (/[가-힯ᄀ-ᇿ]/.test(text)) return 'target'; // Korean
    if (/[ऀ-ॿ]/.test(text)) return 'target'; // Devanagari (Hindi)
    if (/[ఀ-౿]/.test(text)) return 'target'; // Telugu
    if (/[àáâãäåæçèéêëìíîïðñòóôõöùúûüýþÿœ]/i.test(text)) return 'target'; // accented Latin
    // Plain Latin with no accents: only treat as English if the target lang
    // is CJK (user might want auto-translation). For Latin-script target langs
    // (French, Spanish, etc.) keep as target so grammar-check applies.
    return _CJK_LANGS.has(_targetLang) ? 'native' : 'target';
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  let _compactPhrases = false;
  async function init() {
    try {
      const s = await fetch('/api/settings').then(r => r.json());
      _targetLang = s.default_target_lang || 'yue';
      _compactPhrases = !!s.chat_compact_phrases;
    } catch {}
    await loadConvs();
    loadFriends();
    loadMessengerStatus();
    startPolling();

    // Facebook OAuth callback — deferred until Meta App Review
    // const fb = new URLSearchParams(location.search).get('fb');
    // if (fb) {
    //   history.replaceState(null, '', location.pathname);
    //   if (fb === 'connected') { showToast('Facebook connected'); openFriendsModal(); }
    //   else if (fb === 'error') { showToast('Facebook connection failed'); }
    // }
  }

  // ── Conversations list ────────────────────────────────────────────────────
  async function loadConvs() {
    try {
      const { conversations } = await fetch('/api/conversations').then(r => r.json());
      _convs = conversations || [];
      renderConvList();
    } catch {}
  }

  // The AI tutor is pinned above friend conversations as a special contact —
  // one "Chat" destination covers both practice (tutor) and social messaging.
  const TUTOR_ROW = `<div class="msg-conv-row msg-tutor-row" onclick="location.href='/tutor'">
      <div class="msg-conv-avatar tutor-avatar">
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1 2.5 2.5 6 2.5s6-1.5 6-2.5v-5"/></svg>
      </div>
      <div class="msg-conv-info">
        <div class="msg-conv-name">Language Tutor <span class="tutor-ai-badge">AI</span></div>
        <div class="msg-conv-preview">Practice conversation — corrections &amp; new words</div>
      </div>
      <div class="msg-conv-meta"><span class="msg-tutor-arrow">›</span></div>
    </div>`;

  function renderConvList() {
    const el = document.getElementById('msg-convs');
    if (!_convs.length) {
      el.innerHTML = TUTOR_ROW +
        '<div class="msg-conv-empty">No conversations yet.<br>Add friends to start chatting.</div>';
      return;
    }
    el.innerHTML = TUTOR_ROW + _convs.map(c => {
      const name = c.name || c.last_sender_name || '(unknown)';
      const initials = name.slice(0, 2).toUpperCase();
      const isActive = c.id === _activeConvId;
      const messenger = c.type === 'messenger';
      let _previewText = c.last_text || '';
      if (c.last_translations) {
        try {
          const _lt = JSON.parse(c.last_translations);
          _previewText = _lt[_targetLang] || _previewText;
        } catch {}
      }
      const preview = _previewText ? _previewText.slice(0, 40) + (_previewText.length > 40 ? '…' : '') : 'No messages yet';
      const badge = c.unread > 0 ? `<div class="msg-unread-badge">${c.unread}</div>` : '';
      const time = c.last_message_at ? relTime(c.last_message_at) : '';
      const avInner = messenger ? '📘' : (c.avatar_url ? `<img src="${esc(c.avatar_url)}" alt="" loading="lazy" decoding="async">` : initials);
      return `<div class="msg-conv-row${isActive ? ' active' : ''}" onclick="openConv(${c.id})">
        <div class="msg-conv-avatar${messenger ? ' messenger' : ''}">${avInner}</div>
        <div class="msg-conv-info">
          <div class="msg-conv-name">${esc(name)}</div>
          <div class="msg-conv-preview">${esc(preview)}</div>
        </div>
        <div class="msg-conv-meta">
          <div class="msg-conv-time">${time}</div>
          ${badge}
        </div>
      </div>`;
    }).join('');
  }

  function relTime(iso) {
    const d = new Date(iso + (iso.includes('Z') || iso.includes('+') ? '' : 'Z'));
    const diff = (Date.now() - d) / 1000;
    if (diff < 60) return 'now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h';
    return Math.floor(diff / 86400) + 'd';
  }

  // ── Open a conversation ───────────────────────────────────────────────────
  async function openConv(convId) {
    _activeConvId = convId;
    _lastMsgId = 0;
    const conv = _convs.find(c => c.id === convId);
    _activeConvType = conv ? conv.type : 'inapp';
    _activeConvSeenAt = conv ? conv.last_message_at : null;

    // Update active state in sidebar
    renderConvList();
    hideSidebar();
    showChat();

    // Set header
    const name = conv ? (conv.name || conv.last_sender_name || '(unknown)') : '…';
    document.getElementById('chat-name').textContent = name;
    const chatAv = document.getElementById('chat-avatar');
    if (conv && conv.avatar_url) {
      chatAv.innerHTML = `<img src="${esc(conv.avatar_url)}" alt="">`;
    } else {
      chatAv.textContent = name.slice(0, 2).toUpperCase();
    }
    document.getElementById('chat-sub').textContent =
      conv?.type === 'messenger' ? '📘 Messenger' : 'In-app chat';

    // Hide composer for one-sided Messenger view (for now, allow reply)
    document.getElementById('msg-composer').style.display = '';

    document.getElementById('msg-thread').innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:20px;font-size:0.9rem">Loading…</div>';
    await loadMessages();
    await fetch(`/api/conversations/${convId}/read`, { method: 'POST' });
    // Refresh unread count
    if (conv) conv.unread = 0;
    renderConvList();
    if (window._refreshNotifCounts) window._refreshNotifCounts();
  }

  async function openConvWithFriend(friendUserId) {
    try {
      const { id } = await fetch(`/api/conversations/start/${friendUserId}`).then(r => r.json());
      if (id) {
        await loadConvs();
        await openConv(id);
      }
    } catch {}
  }

  // ── Load messages ─────────────────────────────────────────────────────────
  async function loadMessages(append = false) {
    if (!_activeConvId) return;
    const convId = _activeConvId;
    try {
      const url = `/api/conversations/${convId}/messages` +
        (_lastMsgId ? `?before_id=${_lastMsgId}` : '');
      const { messages } = await fetch(url).then(r => r.json());
      if (!messages || _activeConvId !== convId) return;  // conv switched mid-fetch
      const thread = document.getElementById('msg-thread');
      if (!messages.length && !append) {
        thread.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:20px;font-size:0.9rem">No messages yet. Say hello! 👋</div>';
        return;
      }
      // Server already includes romanization tokens — just ingest + render. One
      // round trip, romanization always present (no flash, no inconsistency).
      _ingestTokens(messages);

      const wasAtBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 50;
      const html = messages.map(renderBubble).join('');
      if (append) {
        thread.insertAdjacentHTML('beforeend', html);
        messages.map(m => document.getElementById(`bwrap-${m.id}`)).filter(Boolean)
          .forEach(b => { _initBwrap(b); paintRuby(b); });
      } else {
        thread.innerHTML = html;
        thread.querySelectorAll('.msg-bubble-wrap').forEach(b => { _initBwrap(b); paintRuby(b); });
      }
      if (wasAtBottom || !append) thread.scrollTop = thread.scrollHeight;
      if (messages.length) _lastMsgId = Math.max(...messages.map(m => m.id));
      // Background: fill any missing tokens + prime deck-status badges. Neither
      // blocks the render — the thread is already on screen.
      _backfillTokens(messages);
      _preloadWordStatuses([..._wordsFromMessages(messages)]);
    } catch {}
  }

  const _trashSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';

  function renderBubble(m) {
    const mine = m.is_mine;
    const cls = mine ? 'mine' : 'theirs';
    const isSending = !!m._sending;
    const analysis = m.analysis || {};
    const reactions = m.reactions || {};

    // ── Deleted message ─────────────────────────────────────────────────────
    if (analysis.deleted) {
      return `<div class="msg-bubble-wrap ${cls}" id="bwrap-${m.id}">
        <div class="msg-deleted">${mine ? 'You unsent a message' : 'Message deleted'}</div>
      </div>`;
    }

    // ── Image bubble ─────────────────────────────────────────────────────────
    if (analysis.type === 'image') {
      const imgSrc = analysis.url || m.original_text;
      const description = analysis.description || '';
      const infoId = `imginfo-${m.id}`;
      const nameHtml = !mine ? `<div class="msg-bubble-sender">${esc(m.sender_name)}</div>` : '';
      const rxEntries = Object.entries(reactions).sort((a, b) => b[1].count - a[1].count);
      const rxHtml = rxEntries.length
        ? `<div class="msg-reactions" id="rx-${m.id}">${rxEntries.map(([emoji, {count, mine: rxMine}]) =>
            `<button class="reaction-pill${rxMine ? ' mine' : ''}" data-emoji="${emoji}" data-msg-id="${m.id}">
              ${emoji}<span class="rx-count">${count > 1 ? ' ' + count : ''}</span>
            </button>`).join('')}</div>`
        : `<div class="msg-reactions" id="rx-${m.id}"></div>`;
      // Pick sender vs receiver perspective; handle legacy flat-array format
      const normPhrase = p => typeof p === 'string' ? {text: p, en: ''} : {text: p.text || '', en: p.en || ''};
      const _sugg = (analysis.suggestions || {})[_targetLang];
      let myPhrases = [];
      if (_sugg) {
        const perspective = mine ? 'sender' : 'receiver';
        const raw = Array.isArray(_sugg) ? _sugg : (_sugg[perspective] || _sugg.sender || []);
        myPhrases = raw.map(normPhrase).filter(p => p.text);
      }
      const hasInfo = !!(description || myPhrases.length);
      const phraseRows = myPhrases.map(p => `
        <div class="img-phrase-row">
          <div class="img-phrase-content">
            <div class="img-phrase-target">${needsRuby(_targetLang) ? rbSpan(p.text) : esc(p.text)}</div>
            ${p.en ? `<div class="img-phrase-en">${esc(p.en)}</div>` : ''}
          </div>
          <div class="img-phrase-actions">
            <button class="img-phrase-use" onclick="pastePhrase(${escAttr(JSON.stringify(p.text))})" title="Use in message">↪ Use</button>
            <button class="img-phrase-add" onclick="addPhraseCard(this,${escAttr(JSON.stringify(p.text))},${escAttr(JSON.stringify(p.en))})" title="Save to flashcard deck">Add card</button>
          </div>
        </div>`).join('');
      // Description in viewer's target language if available, else English
      const descLang = (analysis.descriptions || {})[_targetLang] || description;
      const descHtml = descLang
        ? (needsRuby(_targetLang) ? rbSpan(descLang) : esc(descLang))
        : '';
      const infoPanel = hasInfo ? `<div class="img-info-panel" id="${infoId}" style="display:none">
        <div class="img-info-header">
          <div class="img-info-desc">${descHtml}</div>
          <button class="img-info-close" onclick="toggleImgInfo('${infoId}')" title="Close">✕</button>
        </div>
        ${myPhrases.length ? `<div class="img-phrases-hint">Suggested phrases</div>` : ''}
        ${phraseRows}
      </div>` : '';
      const infoBtn = hasInfo
        ? `<button class="img-info-btn${_compactPhrases ? ' compact' : ''}" onclick="toggleImgInfo('${infoId}')" title="Phrases &amp; info"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 1 4 12.7V17a1 1 0 0 1-1 1h-6a1 1 0 0 1-1-1v-2.3A7 7 0 0 1 12 2z"/></svg>${_compactPhrases ? '' : ' Phrases'}</button>`
        : '';
      const imgInner = `${nameHtml}
        <div class="msg-bubble msg-photo-bubble">
          <img src="${esc(imgSrc)}" class="msg-photo" loading="lazy"
               onclick="openPhoto('${esc(imgSrc)}')" alt="photo">
          ${infoBtn}
        </div>
        ${infoPanel}
        ${isSending ? '' : rxHtml}
        <div class="msg-bubble-time">${isSending ? '<span class="sending-indicator">⟳ Analyzing…</span>' : relTime(m.created_at)}</div>`;
      if (mine && !isSending) {
        return `<div class="msg-bubble-wrap ${cls}" id="bwrap-${m.id}">
          <div class="msg-swipe-row">
            <div class="msg-swipe-content">${imgInner}</div>
            <button class="msg-delete-action" data-msg-id="${m.id}" title="Unsend">${_trashSvg}</button>
          </div>
        </div>`;
      }
      return `<div class="msg-bubble-wrap ${cls}" id="bwrap-${m.id}">${imgInner}</div>`;
    }

    // ── Text bubble ──────────────────────────────────────────────────────────
    const corrections = analysis.corrections || [];
    const nuanceNote = analysis.nuance_note || '';
    const replyEn = analysis.reply_en || (m.original_lang === 'en' ? m.original_text : '');
    const hasDiff = m.original_text && m.display_text && m.original_text !== m.display_text;
    const hasEn = replyEn && replyEn !== m.display_text;
    const hasAa = hasEn || hasDiff;

    const nameHtml = !mine ? `<div class="msg-bubble-sender">${esc(m.sender_name)}</div>` : '';
    const textHtml = needsRuby(_targetLang)
      ? rbSpan(m.display_text || '')
      : glossedText(m.display_text || '');

    const willHaveTransPanel = mine && m.original_lang === 'en' && (hasDiff || !!(analysis.translation_failed));
    const showAa = hasAa && !willHaveTransPanel;
    const tools = isSending ? '' : `<div class="bubble-tools">
      <button class="tool-btn" data-speak="${esc(m.display_text || '')}" title="Listen">${_playIcon}</button>
      ${needsRuby(_targetLang) ? `<button class="tool-btn rb-toggle" title="Toggle romanization">あ</button>` : ''}
      ${showAa ? `<button class="tool-btn aa-btn" data-id="${m.id}" title="Translation">Aa</button>` : ''}
      <button class="tool-btn rx-open-btn" data-msg-id="${m.id}" title="Add reaction"><svg width="14" height="14" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="11" r="7"/><path d="M6 13.5s1 1.5 3 1.5 3-1.5 3-1.5"/><circle cx="7" cy="9.5" r="0.8" fill="currentColor" stroke="none"/><circle cx="11" cy="9.5" r="0.8" fill="currentColor" stroke="none"/><line x1="17" y1="4" x2="21" y2="4"/><line x1="19" y1="2" x2="19" y2="6"/></svg></button>
    </div>`;

    const corrHtml = mine && !isSending && corrections.length ? `
      <div class="corr-wrap">
        <div class="corr-xtra open" id="cxtra-${m.id}">
          <button class="corr-xtra-head" data-id="${m.id}">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg> Correction${corrections.length > 1 ? 's' : ''} · ${corrections.length} <span>▾</span>
          </button>
          <div class="corr-xtra-body">
            ${corrections.map(c => `<div class="corr-card">
              ${c.original ? `<div class="corr-quote">${esc(c.original)}</div>` : ''}
              <div class="corr-fixed">
                <button class="corr-speak" data-speak="${esc(c.corrected)}" title="Listen">${_playIcon}</button>
                ${needsRuby(_targetLang) ? rbSpan(c.corrected) : esc(c.corrected)}
              </div>
              ${c.construction ? `<div class="corr-form">📐 ${esc(c.construction)}</div>` : ''}
              ${c.explanation ? `<div class="corr-why">${esc(c.explanation)}</div>` : ''}
            </div>`).join('')}
          </div>
        </div>
      </div>` : '';

    // Translation breakdown panel: when user typed English and it was auto-translated
    const vocab = analysis.vocab || [];
    if (vocab.length) _msgVocab[m.id] = vocab;
    const explanation = analysis.explanation || '';
    const translationFailed = !!(analysis.translation_failed);
    const isTranslated = mine && !isSending && hasDiff && m.original_lang === 'en';
    const showTransPanel = mine && !isSending && m.original_lang === 'en' && (isTranslated || translationFailed);
    let transHtml = '';
    if (showTransPanel && translationFailed) {
      transHtml = `<div class="trans-panel" id="tpanel-${m.id}">
        <div class="trans-failed-toggle">
          ⚠ Translation failed
          <button class="trans-retry-btn" data-msg-id="${m.id}">Retry</button>
        </div>
      </div>`;
    } else if (showTransPanel) {
      transHtml = `<div class="trans-panel open" id="tpanel-${m.id}">
        <button class="trans-toggle" data-panel="${m.id}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 1 4 12.7V17a1 1 0 0 1-1 1h-6a1 1 0 0 1-1-1v-2.3A7 7 0 0 1 12 2z"/></svg>
          Translation · ${vocab.length ? vocab.length + ' words' : 'details'} <span>▾</span>
        </button>
        <div class="trans-body">
          <div class="trans-orig">"${esc(m.original_text)}"</div>
          ${explanation ? `<div class="trans-explain">${esc(explanation)}${nuanceNote ? ` <em>${esc(nuanceNote)}</em>` : ''}</div>` : ''}
          ${vocab.length ? `<div class="trans-vocab-title">Words to learn <button class="trans-vocab-addall" data-msg-id="${m.id}">＋ Add all</button></div>
            ${vocab.map(v => `<div class="trans-vocab-item">
              <button class="trans-vocab-speak" data-speak="${esc(v.target_text)}" title="Listen">${_playIcon}</button>
              <span class="trans-vocab-word">${needsRuby(_targetLang) ? rbSpan(v.target_text) : esc(v.target_text)}</span>
              <span class="trans-vocab-gloss">${esc(v.english)}</span>
              <button class="trans-vocab-add" data-target="${escAttr(v.target_text)}" data-english="${escAttr(v.english)}" data-roman="${escAttr(v.romanization || '')}">＋ Add</button>
            </div>`).join('')}` : ''}
        </div>
      </div>`;
    }

    const showOriginal = hasDiff && !isTranslated && m.original_lang !== 'en';
    const aaInner = [
      hasEn && !isTranslated ? `<div>${esc(replyEn)}</div>` : '',
      showOriginal ? `<div class="msg-aa-orig">Original: ${esc(m.original_text)}${nuanceNote ? `<div class="msg-aa-nuance">💡 ${esc(nuanceNote)}</div>` : ''}</div>` : '',
    ].join('');
    const hasAaContent = aaInner.replace(/<[^>]*>/g, '').trim();
    const aaHtml = hasAaContent ? `<div class="msg-aa" id="aa-${m.id}" style="display:none">${aaInner}</div>` : '';

    const rxEntries = Object.entries(reactions).sort((a, b) => b[1].count - a[1].count);
    const rxHtml = rxEntries.length ? `<div class="msg-reactions" id="rx-${m.id}">
      ${rxEntries.map(([emoji, {count, mine: rxMine}]) =>
        `<button class="reaction-pill${rxMine ? ' mine' : ''}" data-emoji="${emoji}" data-msg-id="${m.id}">
          ${emoji}<span class="rx-count">${count > 1 ? ' ' + count : ''}</span>
        </button>`).join('')}
    </div>` : `<div class="msg-reactions" id="rx-${m.id}"></div>`;

    const nwHtml = !mine && !isSending ? `<div class="nw-wrap">
      <div class="nw-xtra" id="nwxtra-${m.id}">
        <button class="nw-xtra-head" data-msg-id="${m.id}" style="display:none">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg> New words · <span class="nw-count">0</span> <span>▾</span>
        </button>
        <div class="nw-xtra-body" id="nwbody-${m.id}"></div>
      </div>
    </div>` : '';

    const txtInner = `${nameHtml}
      <div class="msg-bubble">${textHtml}</div>
      ${tools}
      ${corrHtml}
      ${transHtml}
      ${nwHtml}
      ${aaHtml}
      ${isSending ? '' : rxHtml}
      <div class="msg-bubble-time">${isSending ? '<span class="sending-indicator">⟳ Processing…</span>' : relTime(m.created_at)}</div>`;
    if (mine && !isSending) {
      return `<div class="msg-bubble-wrap ${cls}" id="bwrap-${m.id}" data-msgtext="${esc(m.display_text || '')}">
        <div class="msg-swipe-row">
          <div class="msg-swipe-content">${txtInner}</div>
          <button class="msg-delete-action" data-msg-id="${m.id}" title="Unsend">${_trashSvg}</button>
        </div>
      </div>`;
    }
    return `<div class="msg-bubble-wrap ${cls}" id="bwrap-${m.id}" data-msgtext="${esc(m.display_text || '')}">${txtInner}</div>`;
  }

  function toggleAa(msgId) {
    const el = document.getElementById(`aa-${msgId}`);
    if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
  }

  function _initBwrap(el) {
    const btn = el.querySelector('.rb-toggle');
    if (btn) btn.onclick = () => {
      rubyDefault = !rubyDefault;
      localStorage.setItem('msg_ruby_inline', rubyDefault ? '1' : '0');
      repaintAllRuby();
    };
  }

  // ── Audio ─────────────────────────────────────────────────────────────────
  let _audio = null;
  function speakMsg(text) {
    if (!text) return;
    try { if (_audio) { _audio.pause(); _audio.currentTime = 0; } } catch {}
    _audio = new Audio('/api/tts?text=' + encodeURIComponent(text.slice(0, 200)) + '&lang=' + encodeURIComponent(_targetLang));
    try { CantoShell.prepareAudio(_audio); } catch {}
    _audio.play().catch(() => {});
  }

  // ── Word-tap glossing ─────────────────────────────────────────────────────
  function glossedText(text) {
    if (!text) return '';
    return text.split(/(\s+)/).map(w => {
      if (/^\s+$/.test(w) || !w) return w;
      return `<span class="gl" tabindex="0" data-word="${esc(w)}">${esc(w)}</span>`;
    }).join('');
  }

  // ── Rich word tooltip (reader-style) ─────────────────────────────────────
  let tooltipWordData = null;
  let _activeWordEl = null;

  async function onWordTap(el, word) {
    if (!word || word.trim().length < 1) return;
    if (_activeWordEl) _activeWordEl.classList.remove('tapped');
    _activeWordEl = el;
    el.classList.add('tapped');

    // Set loading content first so positionTooltip measures correct (min) height
    // Show cached deck status badge immediately while waiting for full lookup
    const _cs = _wordStatusCache[word];
    const _csBadge = _cs
      ? `<span class="tooltip-status-badge ${_cs}" style="margin-right:6px">${_cs === 'known' ? 'Known' : 'Weak'}</span>`
      : '';
    document.getElementById('tooltip-content').innerHTML =
      `<div class="tooltip-loading" style="display:flex;align-items:center;gap:6px">${_csBadge}<span style="display:inline-block;width:10px;height:10px;border:2px solid var(--border);border-top-color:var(--primary);border-radius:50%;animation:spin 0.7s linear infinite;flex-shrink:0"></span>Looking up…</div>`;
    positionTooltip(el);  // sets display:block and positions; leaves display as block

    const bwrap = el.closest('.msg-bubble-wrap');
    const context = bwrap ? (bwrap.dataset.msgtext || '') : '';

    try {
      const res = await fetch('/api/reader/translate-word', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word, context, target_lang: _targetLang }),
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      tooltipWordData = data;
      renderTooltip(data);
      // Reposition now that the tooltip has its full height
      if (_activeWordEl) positionTooltip(_activeWordEl);
    } catch {
      document.getElementById('tooltip-content').innerHTML =
        `<div class="tooltip-error">Lookup failed — try again.</div>`;
    }
  }

  function positionTooltip(el) {
    const tooltip = document.getElementById('word-tooltip');
    tooltip.style.display = 'block';  // show (overrides CSS display:none)
    const rect = el.getBoundingClientRect();
    const tw = tooltip.offsetWidth || 240;
    const th = tooltip.offsetHeight || 80;
    const margin = 10;
    // Use visualViewport height on iOS (window.innerHeight doesn't shrink for keyboard)
    const vph = (window.visualViewport ? window.visualViewport.height : window.innerHeight);
    let left = rect.left;
    let top  = rect.bottom + margin;
    if (left + tw > window.innerWidth - margin) left = window.innerWidth - tw - margin;
    if (left < margin) left = margin;
    if (top + th > vph - margin) top = rect.top - th - margin;
    if (top < margin) top = margin;
    tooltip.style.left = left + 'px';
    tooltip.style.top  = top  + 'px';
    // display stays 'block' — closeTooltip() resets it to 'none'
  }

  function renderTooltip(data) {
    const isInDeck = data.source === 'deck';
    const statusLabel = { known: 'Known', weak: 'Weak', new: 'New to deck' }[data.status] || 'New';
    document.getElementById('tooltip-content').innerHTML = `
      <div class="tooltip-word">${esc(data.target_text)}</div>
      ${data.romanization ? `<div class="tooltip-rom">${esc(data.romanization)}</div>` : ''}
      <div class="tooltip-eng">${esc(data.source_text)}</div>
      <span class="tooltip-status-badge ${data.status || 'new'}">${statusLabel}</span>
      ${data.notes ? `<div class="tooltip-notes">${esc(data.notes)}</div>` : ''}
      <div class="tooltip-actions">
        <button class="play-btn" id="tooltip-play-btn" title="Play">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        </button>
        <button class="tooltip-add-btn ${isInDeck ? 'added' : ''}" id="tooltip-add-btn"
          ${isInDeck ? 'disabled' : ''}>
          ${isInDeck ? 'In deck ✓' : 'Add to deck'}
        </button>
      </div>`;
    // Wire up buttons via JS (safe — no inline onclick escaping issues)
    const playBtn = document.getElementById('tooltip-play-btn');
    if (playBtn) playBtn.onclick = () => speakMsg(tooltipWordData?.target_text || '');
    const addBtn = document.getElementById('tooltip-add-btn');
    if (addBtn && !isInDeck) addBtn.onclick = addWordToDeck;
  }

  async function addWordToDeck() {
    if (!tooltipWordData || tooltipWordData.source === 'deck') return;
    const btn = document.getElementById('tooltip-add-btn');
    btn.disabled = true; btn.textContent = 'Adding…';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text:  tooltipWordData.source_text,
          target_text:  tooltipWordData.target_text,
          romanization: tooltipWordData.romanization || '',
          target_lang:  _targetLang,
          notes:        tooltipWordData.notes || '',
          priority:     tooltipWordData.priority || 3,
        }),
      });
      if (!res.ok && res.status !== 409) throw new Error();
      tooltipWordData = { ...tooltipWordData, source: 'deck', status: 'weak' };
      btn.textContent = 'In deck ✓'; btn.classList.add('added');
      if (_activeWordEl) { _activeWordEl.classList.add('deck'); }
      showToast(res.status === 409 ? 'Already in deck' : 'Added to deck ✓');
    } catch { btn.disabled = false; btn.textContent = 'Add to deck'; showToast('Could not add'); }
  }

  function closeTooltip() {
    const tooltip = document.getElementById('word-tooltip');
    tooltip.style.display = 'none';
    if (_activeWordEl) { _activeWordEl.classList.remove('tapped'); _activeWordEl = null; }
    tooltipWordData = null;
  }

  // Single delegated click handler for the thread — all dynamic buttons
  document.getElementById('msg-thread').addEventListener('click', e => {
    // Word tap
    const gl = e.target.closest('.gl[data-word]');
    if (gl) { e.stopPropagation(); onWordTap(gl, gl.dataset.word); return; }
    // 🔊 speak (bubble toolbar or correction card)
    const speak = e.target.closest('[data-speak]');
    if (speak) { speakMsg(speak.dataset.speak); return; }
    // Aa translation panel
    const aa = e.target.closest('.aa-btn[data-id]');
    if (aa) { toggleAa(aa.dataset.id); return; }
    // New words toggle (received messages)
    const nwHead = e.target.closest('.nw-xtra-head[data-msg-id]');
    if (nwHead) { toggleNewWords(nwHead.dataset.msgId); return; }
    // New words "Add all" button
    const nwAddAll = e.target.closest('.nw-add-all[data-msg-id]');
    if (nwAddAll && !nwAddAll.classList.contains('added')) { addAllNewWords(nwAddAll); return; }
    // New word individual add
    const nwAdd = e.target.closest('.nw-vocab-add[data-target]');
    if (nwAdd && !nwAdd.classList.contains('added')) { addNewWordItem(nwAdd); return; }
    // Corrections toggle
    const corr = e.target.closest('.corr-xtra-head[data-id]');
    if (corr) { document.getElementById(`cxtra-${corr.dataset.id}`)?.classList.toggle('open'); return; }
    // Translation panel toggle
    const transToggle = e.target.closest('.trans-toggle[data-panel]');
    if (transToggle) { document.getElementById(`tpanel-${transToggle.dataset.panel}`)?.classList.toggle('open'); return; }
    // Translation retry button
    const retryBtn = e.target.closest('.trans-retry-btn[data-msg-id]');
    if (retryBtn && !retryBtn.disabled) { retryTranslation(retryBtn); return; }
    // Translation vocab "Add all" button
    const vocabAddAll = e.target.closest('.trans-vocab-addall[data-msg-id]');
    if (vocabAddAll && !vocabAddAll.classList.contains('added')) { addAllTransVocab(vocabAddAll); return; }
    // Translation vocab "Add" button
    const vocabAdd = e.target.closest('.trans-vocab-add[data-target]');
    if (vocabAdd && !vocabAdd.classList.contains('added')) { addTransVocab(vocabAdd); return; }
    // Reaction pill (toggle existing)
    const rxPill = e.target.closest('.reaction-pill[data-msg-id]');
    if (rxPill) { e.stopPropagation(); toggleReaction(rxPill.dataset.msgId, rxPill.dataset.emoji); return; }
    // Open reaction picker
    const rxOpen = e.target.closest('.rx-open-btn[data-msg-id]');
    if (rxOpen) { e.stopPropagation(); openReactionPicker(rxOpen, rxOpen.dataset.msgId); return; }
  });

  async function addTransVocab(btn) {
    const target = btn.dataset.target;
    const english = btn.dataset.english;
    const roman = btn.dataset.roman || '';
    btn.disabled = true;
    btn.textContent = '…';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: english, target_text: target,
          romanization: roman, target_lang: _targetLang,
          notes: 'From chat translation', priority: 3,
        }),
      });
      if (!res.ok && res.status !== 409) throw new Error();
      btn.textContent = res.status === 409 ? 'In deck' : '✓ Added';
      btn.classList.add('added');
      showToast(res.status === 409 ? 'Already in deck' : 'Added to deck ✓');
    } catch {
      btn.disabled = false;
      btn.textContent = '＋ Add';
      showToast('Could not add');
    }
  }

  async function addAllTransVocab(btn) {
    const msgId = btn.dataset.msgId;
    const vocab = _msgVocab[msgId];
    if (!vocab || !vocab.length) return;
    btn.disabled = true;
    btn.textContent = 'Adding…';
    const panel = document.getElementById(`tpanel-${msgId}`);
    const itemBtns = panel ? panel.querySelectorAll('.trans-vocab-add') : [];
    let added = 0, dupes = 0, failed = 0;
    for (let i = 0; i < vocab.length; i++) {
      const v = vocab[i];
      const itemBtn = itemBtns[i];
      if (itemBtn?.classList.contains('added')) { dupes++; continue; }
      try {
        const res = await fetch('/api/cards', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source_text: v.english, target_text: v.target_text,
            romanization: v.romanization || '', target_lang: _targetLang,
            notes: 'From chat translation', priority: 3,
          }),
        });
        if (!res.ok && res.status !== 409) { failed++; continue; }
        if (itemBtn) {
          itemBtn.textContent = res.status === 409 ? 'In deck' : '✓ Added';
          itemBtn.classList.add('added');
        }
        if (res.status === 409) dupes++; else added++;
      } catch { failed++; }
    }
    btn.textContent = '✓ All added';
    btn.classList.add('added');
    const parts = [];
    if (added) parts.push(`${added} added`);
    if (dupes) parts.push(`${dupes} already in deck`);
    if (failed) parts.push(`${failed} failed`);
    showToast(parts.join(', '));
  }

  const _nwLoaded = {};
  const _nwPanelItems = {};
  const _nwSpinner = '<span style="display:inline-block;width:10px;height:10px;border:2px solid var(--border);border-top-color:var(--primary);border-radius:50%;animation:spin 0.7s linear infinite;flex-shrink:0"></span>';

  function toggleNewWords(msgId) {
    const xtra = document.getElementById(`nwxtra-${msgId}`);
    if (!xtra) return;
    if (xtra.classList.contains('open')) {
      xtra.classList.remove('open');
      return;
    }
    xtra.classList.add('open');
    if (_nwLoaded[msgId]) return;

    const body = document.getElementById(`nwbody-${msgId}`);
    const bwrap = document.getElementById(`bwrap-${msgId}`);
    if (!body || !bwrap) return;

    const glEls = bwrap.querySelectorAll('.msg-bubble .gl.gl-new');
    const newWords = [...new Set([...glEls].map(el => el.dataset.word))].filter(Boolean);
    if (!newWords.length) {
      body.innerHTML = '<div style="color:var(--text-muted);font-size:0.82rem;padding:4px 0">All words already in deck</div>';
      _nwLoaded[msgId] = true;
      return;
    }

    const words = newWords.slice(0, 20);
    _nwPanelItems[msgId] = words.map(w => ({ target_text: w }));
    body.innerHTML = `
      <div class="nw-vocab-title">Words to learn
        <button class="trans-vocab-addall nw-add-all" data-msg-id="${msgId}" disabled>＋ Add all</button>
      </div>
      ${words.map((w, i) => `<div class="nw-vocab-item" id="nwi-${msgId}-${i}">
        <button class="trans-vocab-speak" data-speak="${escAttr(w)}" title="Listen">${_playIcon}</button>
        <span class="nw-vocab-word"><span class="gl" tabindex="0" data-word="${escAttr(w)}">${esc(w)}</span></span>
        <span class="nw-vocab-gloss" id="nwg-${msgId}-${i}">${_nwSpinner}</span>
        <button class="nw-vocab-add" data-target="${escAttr(w)}" data-english="" data-roman="" disabled>＋ Add</button>
      </div>`).join('')}`;

    const context = bwrap.dataset.msgtext || '';
    let resolved = 0;
    words.forEach((w, i) => {
      fetch('/api/reader/translate-word', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word: w, context, target_lang: _targetLang }),
      }).then(r => r.ok ? r.json() : null).catch(() => null).then(d => {
        resolved++;
        const gloss = document.getElementById(`nwg-${msgId}-${i}`);
        const row = document.getElementById(`nwi-${msgId}-${i}`);
        if (!row) return;
        const addBtn = row.querySelector('.nw-vocab-add');
        if (d) {
          _nwPanelItems[msgId][i] = d;
          if (gloss) gloss.textContent = d.source_text;
          if (addBtn) {
            addBtn.dataset.english = d.source_text;
            addBtn.dataset.roman = d.romanization || '';
            addBtn.disabled = false;
          }
          if (needsRuby(_targetLang) && d.romanization) {
            const wordSpan = row.querySelector('.nw-vocab-word .gl');
            if (wordSpan) wordSpan.innerHTML = `<ruby>${esc(d.target_text)}<rt>${esc(d.romanization)}</rt></ruby>`;
          }
        } else {
          if (gloss) gloss.innerHTML = '<span style="color:var(--text-muted);font-style:italic">—</span>';
        }
        if (resolved === words.length) {
          _nwLoaded[msgId] = true;
          const addAll = body.querySelector('.nw-add-all');
          if (addAll && _nwPanelItems[msgId].some(it => it.source_text)) addAll.disabled = false;
        }
      });
    });
  }

  async function addNewWordItem(btn) {
    const target = btn.dataset.target;
    const english = btn.dataset.english;
    const roman = btn.dataset.roman || '';
    btn.disabled = true;
    btn.textContent = '…';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: english, target_text: target,
          romanization: roman, target_lang: _targetLang,
          notes: 'From message', priority: 3,
        }),
      });
      if (!res.ok && res.status !== 409) throw new Error();
      _wordStatusCache[target] = 'weak';
      btn.textContent = res.status === 409 ? 'In deck' : '✓ Added';
      btn.classList.add('added');
      showToast(res.status === 409 ? 'Already in deck' : 'Added to deck ✓');
      _applyWordHighlights();
    } catch {
      btn.disabled = false;
      btn.textContent = '＋ Add';
      showToast('Could not add');
    }
  }

  async function addAllNewWords(btn) {
    const msgId = btn.dataset.msgId;
    const items = _nwPanelItems[msgId];
    if (!items || !items.length) return;
    btn.disabled = true;
    btn.textContent = 'Adding…';
    const body = document.getElementById(`nwbody-${msgId}`);
    const itemBtns = body ? body.querySelectorAll('.nw-vocab-add') : [];
    let added = 0, dupes = 0, failed = 0;
    for (let i = 0; i < items.length; i++) {
      const d = items[i];
      if (!d.source_text) continue;
      const itemBtn = itemBtns[i];
      if (itemBtn?.classList.contains('added')) { dupes++; continue; }
      try {
        const res = await fetch('/api/cards', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source_text: d.source_text, target_text: d.target_text,
            romanization: d.romanization || '', target_lang: _targetLang,
            notes: 'From message', priority: 3,
          }),
        });
        if (!res.ok && res.status !== 409) { failed++; continue; }
        _wordStatusCache[d.target_text] = 'weak';
        if (itemBtn) {
          itemBtn.textContent = res.status === 409 ? 'In deck' : '✓ Added';
          itemBtn.classList.add('added');
        }
        if (res.status === 409) dupes++; else added++;
      } catch { failed++; }
    }
    btn.textContent = '✓ All added';
    btn.classList.add('added');
    const parts = [];
    if (added) parts.push(`${added} added`);
    if (dupes) parts.push(`${dupes} already in deck`);
    if (failed) parts.push(`${failed} failed`);
    showToast(parts.join(', '));
    _applyWordHighlights();
  }

  async function retryTranslation(btn) {
    const msgId = btn.dataset.msgId;
    btn.disabled = true;
    btn.textContent = 'Retrying…';
    try {
      const res = await fetch(`/api/messages/${msgId}/retry-translate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed');
      const data = await res.json();
      const bwrap = document.getElementById(`bwrap-${msgId}`);
      if (!bwrap) return;
      const bubble = bwrap.querySelector('.msg-bubble');
      if (bubble) {
        bubble.innerHTML = needsRuby(_targetLang) ? rbSpan(data.display_text) : glossedText(data.display_text);
      }
      bwrap.dataset.msgtext = data.display_text;
      if (data.tokens) _ingestTokens([{ tokens: data.tokens }]);
      const panel = document.getElementById(`tpanel-${msgId}`);
      if (panel) {
        const vocab = data.analysis?.vocab || [];
        if (vocab.length) _msgVocab[msgId] = vocab;
        const explanation = data.analysis?.explanation || '';
        const nuanceNote = data.analysis?.nuance_note || '';
        const origText = bwrap.querySelector('.msg-bubble')?.textContent || '';
        panel.className = 'trans-panel open';
        panel.innerHTML = `
          <button class="trans-toggle" data-panel="${msgId}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 1 4 12.7V17a1 1 0 0 1-1 1h-6a1 1 0 0 1-1-1v-2.3A7 7 0 0 1 12 2z"/></svg>
            Translation · ${vocab.length ? vocab.length + ' words' : 'details'} <span>▾</span>
          </button>
          <div class="trans-body">
            ${explanation ? `<div class="trans-explain">${esc(explanation)}${nuanceNote ? ` <em>${esc(nuanceNote)}</em>` : ''}</div>` : ''}
            ${vocab.length ? `<div class="trans-vocab-title">Words to learn <button class="trans-vocab-addall" data-msg-id="${msgId}">＋ Add all</button></div>
              ${vocab.map(v => `<div class="trans-vocab-item">
                <button class="trans-vocab-speak" data-speak="${esc(v.target_text)}" title="Listen">${_playIcon}</button>
                <span class="trans-vocab-word">${needsRuby(_targetLang) ? rbSpan(v.target_text) : esc(v.target_text)}</span>
                <span class="trans-vocab-gloss">${esc(v.english)}</span>
                <button class="trans-vocab-add" data-target="${escAttr(v.target_text)}" data-english="${escAttr(v.english)}" data-roman="${escAttr(v.romanization || '')}">＋ Add</button>
              </div>`).join('')}` : ''}
          </div>`;
      }
      paintRuby(bwrap);
      showToast('Translated ✓');
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Retry';
      showToast(e.message || 'Translation failed — try again');
    }
  }

  // ── Reaction picker ───────────────────────────────────────────────────────
  let _rxMsgId = null;

  function openReactionPicker(anchorEl, msgId) {
    closeReactionPicker();
    _rxMsgId = msgId;
    const picker = document.getElementById('reaction-picker');
    picker.classList.add('open');
    // Position above the anchor button
    const rect = anchorEl.getBoundingClientRect();
    const pw = picker.offsetWidth || 280;
    let left = rect.left - pw / 2 + rect.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    const top = rect.top - (picker.offsetHeight || 60) - 8;
    picker.style.left = left + 'px';
    picker.style.top = Math.max(8, top) + 'px';
  }

  function closeReactionPicker() {
    document.getElementById('reaction-picker').classList.remove('open');
    _rxMsgId = null;
  }

  document.getElementById('reaction-picker').addEventListener('click', e => {
    const btn = e.target.closest('.rx-option[data-emoji]');
    if (btn && _rxMsgId) { toggleReaction(_rxMsgId, btn.dataset.emoji); closeReactionPicker(); }
  });

  async function toggleReaction(msgId, emoji) {
    try {
      const res = await fetch(`/api/messages/${msgId}/reactions/${encodeURIComponent(emoji)}`,
        { method: 'POST' });
      if (!res.ok) return;
      const { reactions } = await res.json();
      updateReactionPills(msgId, reactions);
    } catch {}
  }

  function updateReactionPills(msgId, reactions) {
    const el = document.getElementById(`rx-${msgId}`);
    if (!el) return;
    const rxEntries = Object.entries(reactions).sort((a, b) => b[1].count - a[1].count);
    el.innerHTML = rxEntries.map(([emoji, {count, mine}]) =>
      `<button class="reaction-pill${mine ? ' mine' : ''}" data-emoji="${emoji}" data-msg-id="${msgId}">
        ${emoji}<span class="rx-count">${count > 1 ? ' ' + count : ''}</span>
      </button>`
    ).join('');
  }

  // ── Swipe-to-delete on own messages ────────────────────────────────────────
  {
    const _SWIPE_THRESHOLD = 30;
    let _swipeRow = null, _startX = 0, _dx = 0, _tracking = false;

    const thread = document.getElementById('msg-thread');

    function _closeAllSwipes(except) {
      document.querySelectorAll('.msg-delete-action.visible').forEach(btn => {
        if (except && btn.closest('.msg-swipe-row') === except) return;
        btn.classList.remove('visible');
      });
    }

    thread.addEventListener('touchstart', e => {
      _tracking = false;
      const row = e.target.closest('.msg-swipe-row');
      if (!row) { _closeAllSwipes(); return; }
      _swipeRow = row;
      _startX = e.touches[0].clientX;
      _dx = 0;
      _tracking = true;
    }, { passive: true });

    thread.addEventListener('touchmove', e => {
      if (!_tracking || !_swipeRow) return;
      _dx = e.touches[0].clientX - _startX;
    }, { passive: true });

    thread.addEventListener('touchend', () => {
      if (!_tracking || !_swipeRow) return;
      const btn = _swipeRow.querySelector('.msg-delete-action');
      if (!btn) { _swipeRow = null; return; }
      const wasOpen = btn.classList.contains('visible');
      if (_dx < -_SWIPE_THRESHOLD && !wasOpen) {
        _closeAllSwipes(_swipeRow);
        btn.classList.add('visible');
      } else if (_dx > _SWIPE_THRESHOLD && wasOpen) {
        btn.classList.remove('visible');
      }
      _swipeRow = null;
      _tracking = false;
    }, { passive: true });

    thread.addEventListener('touchcancel', () => { _swipeRow = null; _tracking = false; }, { passive: true });

    thread.addEventListener('click', async e => {
      const btn = e.target.closest('.msg-delete-action[data-msg-id]');
      if (!btn) {
        if (!e.target.closest('.msg-swipe-row')) _closeAllSwipes();
        return;
      }
      const msgId = btn.dataset.msgId;
      if (!confirm('Unsend this message? It will be deleted for everyone.')) return;
      try {
        const res = await fetch(`/api/messages/${msgId}`, { method: 'DELETE' });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          showToast(data.detail || 'Could not delete message', 2500);
          return;
        }
        const bwrap = document.getElementById(`bwrap-${msgId}`);
        if (bwrap) {
          bwrap.innerHTML = '<div class="msg-deleted">You unsent a message</div>';
          bwrap.className = 'msg-bubble-wrap mine';
        }
      } catch {
        showToast('Could not delete message', 2500);
      }
    });
  }

  // Close tooltip and reaction picker when clicking outside
  document.addEventListener('click', e => {
    const tooltip = document.getElementById('word-tooltip');
    if (tooltip.style.display !== 'none' &&
        !tooltip.contains(e.target) &&
        !e.target.closest('.gl[data-word]')) {
      closeTooltip();
    }
    const picker = document.getElementById('reaction-picker');
    if (picker.classList.contains('open') &&
        !picker.contains(e.target) &&
        !e.target.closest('.rx-open-btn')) {
      closeReactionPicker();
    }
  });

  function showToast(msg, ms) {
    let t = document.getElementById('msg-toast');
    if (!t) { t = document.createElement('div'); t.id = 'msg-toast'; t.style.cssText =
      'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--tooltip-bg,#222);color:#fff;border-radius:999px;padding:7px 16px;font-size:0.85rem;font-weight:600;z-index:9999;transition:opacity 0.3s'; document.body.appendChild(t); }
    t.textContent = msg; t.style.opacity = '1';
    t.style.maxWidth = '90vw'; t.style.textAlign = 'center';
    clearTimeout(t._timer); t._timer = setTimeout(() => { t.style.opacity = '0'; }, ms || 2000);
  }

  // ── Image upload ──────────────────────────────────────────────────────────
  async function sendImage(input) {
    const file = input.files[0];
    input.value = '';  // reset so same file can be picked again
    if (!file || !_activeConvId) return;
    const btn = document.getElementById('msg-img-btn');
    if (btn) btn.disabled = true;

    const objUrl = URL.createObjectURL(file);
    const tempId = 'tmp-img-' + Date.now();
    const thread = document.getElementById('msg-thread');
    if (!thread.querySelector('.msg-bubble-wrap')) thread.innerHTML = '';
    thread.insertAdjacentHTML('beforeend', `
      <div class="msg-bubble-wrap mine" id="${tempId}">
        <div class="msg-bubble msg-photo-bubble" style="opacity:0.55">
          <img src="${objUrl}" class="msg-photo" alt="uploading…">
        </div>
        <div class="msg-bubble-time"><span class="sending-indicator">⟳ Analyzing…</span></div>
      </div>`);
    thread.scrollTop = thread.scrollHeight;

    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await fetch(`/api/conversations/${_activeConvId}/image`, { method: 'POST', body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Upload failed');
      document.getElementById(tempId)?.remove();
      const realBubble = {
        id: data.msg_id, is_mine: true,
        display_text: data.url, original_text: `📷 ${data.description || 'Photo'}`,
        sender_name: 'Me', created_at: new Date().toISOString(),
        analysis: { type: 'image', url: data.url, description: data.description || '',
                    descriptions: data.descriptions || {}, suggestions: data.suggestions || {} },
        reactions: {},
      };
      thread.insertAdjacentHTML('beforeend', renderBubble(realBubble));
      thread.scrollTop = thread.scrollHeight;
      if (data.msg_id) _lastMsgId = Math.max(_lastMsgId, data.msg_id);
      await loadConvs();
      const c = _convs.find(c => c.id === _activeConvId);
      if (c) _activeConvSeenAt = c.last_message_at;
    } catch (e) {
      document.getElementById(tempId)?.remove();
      showToast(e.message || 'Failed to send photo');
    }
    URL.revokeObjectURL(objUrl);
    if (btn) btn.disabled = false;
  }

  function openPhoto(src) {
    const lb = document.createElement('div');
    lb.className = 'photo-lightbox';
    lb.innerHTML = `<img src="${src}" alt="photo">`;
    lb.onclick = () => lb.remove();
    document.body.appendChild(lb);
  }

  async function toggleImgInfo(id) {
    const panel = document.getElementById(id);
    if (!panel) return;
    const isHidden = panel.style.display === 'none';
    panel.style.display = isHidden ? '' : 'none';
    const btn = panel.previousElementSibling?.querySelector('.img-info-btn');
    if (btn) btn.classList.toggle('on', isHidden);
    // On open: fetch & paint ruby for the description + all phrase texts
    if (isHidden && needsRuby(_targetLang)) {
      const rbs = [...panel.querySelectorAll('.rb[data-text]')];
      const missing = rbs.map(el => el.dataset.text).filter(t => t && !(_ck(t) in _tokenCache));
      if (missing.length) await fetchTokensBatch(missing);
      rbs.forEach(el => {
        const tokens = _tokenCache[_ck(el.dataset.text)];
        if (tokens) el.innerHTML = _tokensToHtml(tokens, rubyDefault);
      });
    }
  }

  async function addPhraseCard(btn, text, en) {
    if (btn.classList.contains('added')) return;
    btn.disabled = true; btn.textContent = '…';
    try {
      const res = await fetch('/api/cards', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_text: en || text, target_text: text,
          romanization: '', target_lang: _targetLang, notes: '', priority: 3,
        }),
      });
      if (res.ok || res.status === 409) {
        btn.textContent = res.status === 409 ? 'In deck' : '✓ Saved';
        btn.classList.add('added');
        showToast(res.status === 409 ? 'Already in deck' : 'Added to flashcard deck ✓');
      } else { throw new Error(); }
    } catch {
      btn.disabled = false; btn.textContent = 'Add card'; showToast('Could not add');
    }
  }

  function pastePhrase(phrase) {
    const input = document.getElementById('msg-input');
    if (!input) return;
    input.value = phrase;
    input.focus();
    input.dispatchEvent(new Event('input'));
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    // Brief highlight so the user sees where the text went
    input.style.borderColor = 'var(--primary)';
    input.style.boxShadow = '0 0 0 2px var(--primary-soft)';
    setTimeout(() => { input.style.borderColor = ''; input.style.boxShadow = ''; }, 700);
  }

  // ── Send message ──────────────────────────────────────────────────────────
  async function sendMessage() {
    if (!_activeConvId) return;
    const input = document.getElementById('msg-input');
    const text = input.value.trim();
    if (!text) return;
    const btn = document.getElementById('msg-send-btn');
    btn.disabled = true;
    input.value = '';
    resizeInput();

    const thread = document.getElementById('msg-thread');
    // Clear empty-state placeholder if present
    if (!thread.querySelector('.msg-bubble-wrap')) thread.innerHTML = '';

    // Render optimistic bubble immediately with typed text
    const tempId = `temp-${Date.now()}`;
    const tempBubble = {
      id: tempId, is_mine: true, _sending: true,
      display_text: text, original_text: text,
      sender_name: 'Me', created_at: new Date().toISOString(),
    };
    thread.insertAdjacentHTML('beforeend', renderBubble(tempBubble));
    const tempBwrap = document.getElementById(`bwrap-${tempId}`);
    if (tempBwrap) { _initBwrap(tempBwrap); paintRuby(tempBwrap); }
    thread.scrollTop = thread.scrollHeight;

    const endpoint = _activeConvType === 'messenger'
      ? `/api/conversations/${_activeConvId}/messenger-reply`
      : `/api/conversations/${_activeConvId}/messages`;

    try {
      const res = await fetch(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, mode: detectInputMode(text) }),
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      const { display_text, msg_id } = data;
      if (msg_id) _lastMsgId = msg_id;

      // Replace optimistic bubble with real one (includes analysis + reactions).
      // Server returns inline tokens — ingest them so ruby paints immediately.
      const mode = detectInputMode(text);
      const realBubble = {
        id: msg_id || Date.now(), is_mine: true,
        display_text, original_text: text,
        original_lang: mode === 'native' ? 'en' : _targetLang,
        sender_name: 'Me', created_at: new Date().toISOString(),
        analysis: data.analysis || {}, reactions: {}, tokens: data.tokens || {},
      };
      _ingestTokens([realBubble]);
      if (tempBwrap) tempBwrap.remove();
      thread.insertAdjacentHTML('beforeend', renderBubble(realBubble));
      const realBwrap = document.getElementById(`bwrap-${realBubble.id}`);
      if (realBwrap) { _initBwrap(realBwrap); paintRuby(realBwrap); }
      thread.scrollTop = thread.scrollHeight;
      await loadConvs();
      // We've already rendered our own message — don't let the next poll re-fetch.
      const c = _convs.find(c => c.id === _activeConvId);
      if (c) _activeConvSeenAt = c.last_message_at;
    } catch {
      if (tempBwrap) tempBwrap.remove();
      input.value = text;
    } finally {
      btn.disabled = false;
      input.focus();
    }
  }

  // ── Live updates ──────────────────────────────────────────────────────────
  // An SSE signal makes the conversation list the cheap change detector; only
  // re-fetch the active thread when its last_message_at moves.
  let _activeConvSeenAt = null;
  function startPolling() {
    async function refreshFromSignal() {
      if (document.hidden) return;   // don't burn radio/battery in a background tab
      await loadConvs();
      if (!_activeConvId) return;
      const conv = _convs.find(c => c.id === _activeConvId);
      const at = conv && conv.last_message_at;
      if (!at || at !== _activeConvSeenAt) {
        _activeConvSeenAt = at;
        await checkNewMessages();
      }
    }

    // Prefer one server-sent event connection. A slow fallback remains for
    // proxies or browsers that do not support EventSource.
    if ('EventSource' in window) {
      _eventStream = new EventSource('/api/events/messages');
      _eventStream.onmessage = refreshFromSignal;
      _eventStream.onopen = () => {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
      };
      _eventStream.onerror = () => {
        if (!_pollTimer) _pollTimer = setInterval(refreshFromSignal, 30000);
      };
    } else {
      _pollTimer = setInterval(refreshFromSignal, 30000);
    }
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refreshFromSignal();
    });
    window.addEventListener('pagehide', () => {
      if (_eventStream) _eventStream.close();
      if (_pollTimer) clearInterval(_pollTimer);
    }, { once: true });
  }

  async function checkNewMessages() {
    try {
      const { messages } = await fetch(
        `/api/conversations/${_activeConvId}/messages`
      ).then(r => r.json());
      if (!messages || !messages.length) return;
      const latest = messages[messages.length - 1];
      if (latest.id > _lastMsgId) {
        const prevId = _lastMsgId;
        _lastMsgId = latest.id;
        const thread = document.getElementById('msg-thread');
        const atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60;
        const newMsgs = messages.filter(m => m.id > prevId);
        // Clear empty-state placeholder if thread has no bubbles yet
        if (newMsgs.length && !thread.querySelector('.msg-bubble-wrap')) thread.innerHTML = '';
        _ingestTokens(newMsgs);
        newMsgs.forEach(m => thread.insertAdjacentHTML('beforeend', renderBubble(m)));
        newMsgs.forEach(m => {
          const bwrap = document.getElementById(`bwrap-${m.id}`);
          if (bwrap) { _initBwrap(bwrap); paintRuby(bwrap); }
        });
        if (atBottom) thread.scrollTop = thread.scrollHeight;
        _backfillTokens(newMsgs);
        _preloadWordStatuses([..._wordsFromMessages(newMsgs)]);
        await fetch(`/api/conversations/${_activeConvId}/read`, { method: 'POST' });
        if (window._refreshNotifCounts) window._refreshNotifCounts();
      }
    } catch {}
  }

  // ── Friends modal ─────────────────────────────────────────────────────────
  let _activeFriendsTab = 'friends';

  function switchFriendsTab(tab) {
    _activeFriendsTab = tab;
    document.getElementById('tab-friends').classList.toggle('active', tab === 'friends');
    document.getElementById('tab-leaderboard').classList.toggle('active', tab === 'leaderboard');
    document.getElementById('friends-tab-body').style.display = tab === 'friends' ? '' : 'none';
    document.getElementById('leaderboard-tab-body').style.display = tab === 'leaderboard' ? '' : 'none';
    if (tab === 'leaderboard') loadLeaderboard();
  }

  function openFriendsModal() {
    document.getElementById('friends-modal').style.display = '';
    // Always switch back to friends tab on open
    switchFriendsTab('friends');
    loadFriends();
    loadMessengerStatus();
    // loadFacebookStatus(); // deferred until Meta App Review
  }

  // ── Find friends via Facebook — deferred until Meta App Review ────────────
  // async function loadFacebookStatus() {
  //   try {
  //     const data = await fetch('/api/social/facebook/status').then(r => r.json());
  //     const section = document.getElementById('fb-friends-section');
  //     if (!data.configured) { section.style.display = 'none'; return; }
  //     section.style.display = '';
  //     document.getElementById('fb-connect-view').style.display = data.connected ? 'none' : '';
  //     document.getElementById('fb-connected-view').style.display = data.connected ? '' : 'none';
  //     if (data.connected) document.getElementById('fb-display-name').textContent = data.display_name || 'Connected';
  //   } catch {}
  // }
  //
  // async function connectFacebook() {
  //   try {
  //     const { url } = await fetch('/api/social/facebook/login').then(r => r.json());
  //     if (url) window.location.href = url;
  //   } catch { showToast('Could not start Facebook connect'); }
  // }
  //
  // async function disconnectFacebook() {
  //   if (!confirm('Disconnect Facebook?')) return;
  //   await fetch('/api/social/facebook/disconnect', { method: 'DELETE' });
  //   loadFacebookStatus();
  // }
  //
  // async function loadFacebookSuggestions() {
  //   const el = document.getElementById('fb-suggestions');
  //   el.innerHTML = '<div class="modal-empty">Looking for friends…</div>';
  //   try {
  //     const data = await fetch('/api/social/facebook/friend-suggestions').then(async r => {
  //       if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Failed');
  //       return r.json();
  //     });
  //     if (!data.suggestions || !data.suggestions.length) {
  //       el.innerHTML = `<div class="modal-empty">No Facebook friends on the app yet. Invite them!</div>`;
  //       return;
  //     }
  //     el.innerHTML = data.suggestions.map(s => `
  //       <div class="friend-result">
  //         <span class="friend-result-name">${esc(s.username)}</span>
  //         <button class="friend-action-btn" onclick="sendReq('${esc(s.username)}', this)">Add friend</button>
  //       </div>`).join('');
  //   } catch (e) {
  //     el.innerHTML = `<div class="modal-empty">${esc(e.message || 'Could not load suggestions.')}</div>`;
  //   }
  // }
  function closeFriendsModal() {
    document.getElementById('friends-modal').style.display = 'none';
    if (window._refreshNotifCounts) window._refreshNotifCounts();
  }
  function closeFriendsModalIfBg(e) { if (e.target === document.getElementById('friends-modal').firstElementChild) closeFriendsModal(); }

  async function loadFriends() {
    try {
      const { friends, sent, received } = await fetch('/api/friends').then(r => r.json());
      // Pending received
      const pendingEl = document.getElementById('pending-section');
      const pendingList = document.getElementById('pending-list');
      if (received && received.length) {
        pendingEl.style.display = '';
        pendingList.innerHTML = received.map(f => `
          <div class="friend-row">
            ${miniAvatarHtml(f.avatar_url, f.username)}
            <span class="friend-row-name">${esc(f.username)} wants to be friends</span>
            <div class="friend-row-actions">
              <button class="friend-action-btn accept" onclick="acceptReq(${f.id})">Accept</button>
              <button class="friend-action-btn danger" onclick="rejectReq(${f.id})">Reject</button>
            </div>
          </div>`).join('');
      } else {
        pendingEl.style.display = 'none';
      }
      // Friends list
      const friendsEl = document.getElementById('friends-list');
      if (!friends || !friends.length) {
        friendsEl.innerHTML = '<div class="modal-empty">No friends yet — search by username above.</div>';
      } else {
        friendsEl.innerHTML = friends.map(f => `
          <div class="friend-row">
            ${miniAvatarHtml(f.avatar_url, f.username)}
            <span class="friend-row-name">${esc(f.username)}</span>
            <div class="friend-row-actions">
              <button class="friend-action-btn" onclick="openConvWithFriend(${f.user_id}); closeFriendsModal()">Message</button>
              <button class="friend-action-btn danger" onclick="removeFriend(${f.user_id})">Remove</button>
            </div>
          </div>`).join('');
      }
    } catch {}
  }

  async function loadLeaderboard() {
    const el = document.getElementById('leaderboard-list');
    el.innerHTML = '<div class="lb-loading">Loading…</div>';
    try {
      const { leaderboard } = await fetch('/api/friends/leaderboard').then(r => r.json());
      if (!leaderboard || !leaderboard.length) {
        el.innerHTML = '<div class="lb-loading">Add friends to see the leaderboard!</div>';
        return;
      }
      const medals = ['🥇', '🥈', '🥉'];
      el.innerHTML = leaderboard.map((entry, i) => {
        const rankClass = i === 0 ? 'gold' : i === 1 ? 'silver' : i === 2 ? 'bronze' : '';
        const rankLabel = medals[i] || `${i + 1}`;
        return `<div class="lb-row">
          <div class="lb-rank ${rankClass}">${rankLabel}</div>
          ${miniAvatarHtml(entry.avatar_url, entry.username)}
          <div class="lb-name${entry.is_me ? ' lb-me' : ''}">
            ${esc(entry.username)}${entry.is_me ? '<span class="lb-me-tag">you</span>' : ''}
          </div>
          <div class="lb-stats">
            <span>🔥 ${entry.streak}</span>
            <span class="lb-xp">⭐ ${entry.xp.toLocaleString()}</span>
          </div>
        </div>`;
      }).join('');
    } catch {
      el.innerHTML = '<div class="lb-loading">Could not load leaderboard.</div>';
    }
  }

  async function searchFriend() {
    const q = document.getElementById('friend-search-input').value.trim();
    if (!q) return;
    const el = document.getElementById('friend-search-result');
    el.innerHTML = 'Searching…';
    try {
      const { users } = await fetch(`/api/friends/search?q=${encodeURIComponent(q)}`).then(r => r.json());
      if (!users || !users.length) {
        el.innerHTML = '<div class="modal-empty">No user found with that username.</div>';
        return;
      }
      el.innerHTML = users.map(u => `
        <div class="friend-result">
          ${miniAvatarHtml(u.avatar_url, u.username)}
          <span class="friend-result-name">${esc(u.username)}</span>
          <button class="friend-action-btn" onclick="sendReq('${esc(u.username)}', this)">Add friend</button>
        </div>`).join('');
    } catch { el.innerHTML = '<div class="modal-empty">Error searching.</div>'; }
  }

  async function sendReq(username, btn) {
    btn.disabled = true; btn.textContent = '…';
    try {
      const res = await fetch('/api/friends/request', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username }),
      });
      if (res.ok) { btn.textContent = 'Sent ✓'; }
      else { btn.textContent = 'Error'; btn.disabled = false; }
    } catch { btn.textContent = 'Error'; btn.disabled = false; }
  }

  async function acceptReq(friendshipId) {
    await fetch(`/api/friends/${friendshipId}/accept`, { method: 'POST' });
    loadFriends();
  }
  async function rejectReq(friendshipId) {
    await fetch(`/api/friends/${friendshipId}/reject`, { method: 'POST' });
    loadFriends();
  }
  async function removeFriend(userId) {
    if (!confirm('Remove this friend?')) return;
    await fetch(`/api/friends/${userId}`, { method: 'DELETE' });
    loadFriends();
  }

  // ── Messenger connect ─────────────────────────────────────────────────────
  async function loadMessengerStatus() {
    try {
      const data = await fetch('/api/messenger/status').then(r => r.json());
      document.getElementById('messenger-connected-view').style.display = data.connected ? '' : 'none';
      document.getElementById('messenger-connect-view').style.display = data.connected ? 'none' : '';
      if (data.connected) {
        document.getElementById('messenger-page-name').textContent = data.page_name || data.page_id;
      }
    } catch {}
  }

  async function connectMessenger() {
    const page_id = document.getElementById('messenger-page-id').value.trim();
    const page_access_token = document.getElementById('messenger-token').value.trim();
    const page_name = document.getElementById('messenger-page-name-input').value.trim();
    if (!page_id || !page_access_token) { alert('Page ID and token are required.'); return; }
    const res = await fetch('/api/messenger/connect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page_id, page_access_token, page_name }),
    });
    if (res.ok) { loadMessengerStatus(); }
    else { alert('Could not connect — check your Page ID and token.'); }
  }

  async function disconnectMessenger() {
    if (!confirm('Disconnect Messenger?')) return;
    await fetch('/api/messenger/disconnect', { method: 'DELETE' });
    loadMessengerStatus();
  }

  // ── UI helpers ────────────────────────────────────────────────────────────
  function showChat() {
    document.getElementById('msg-placeholder').style.display = 'none';
    const chat = document.getElementById('msg-chat');
    chat.style.display = 'flex';
    // Phone layout: an open thread is a full-focus view — reclaim the tab bar's
    // space so the composer sits at the true bottom (back button returns to list).
    if (matchMedia('(max-width: 600px)').matches) document.body.classList.add('hide-tabbar');
  }

  function showSidebar() {
    document.getElementById('msg-sidebar').classList.remove('hidden');
    document.getElementById('msg-placeholder').style.display = '';
    document.getElementById('msg-chat').style.display = 'none';
    _activeConvId = null;
    document.body.classList.remove('hide-tabbar');
  }
  function hideSidebar() {
    document.getElementById('msg-sidebar').classList.add('hidden');
  }

  function resizeInput() {
    const ta = document.getElementById('msg-input');
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
  }

  // Escapes &<> AND quotes, so it's safe both in text nodes and in (double- or
  // single-) quoted HTML attributes. NOTE: still NOT safe inside a JS context
  // such as onclick="fn('...')" — for those, JSON.stringify the value first.
  function esc(s) {
    const d = document.createElement('div'); d.textContent = s ?? '';
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  // Retained for callers; esc() already covers attributes now.
  function escAttr(s) { return esc(s); }

  // Small circular avatar (friends/leaderboard rows): a real photo when set,
  // else initials — same fallback pattern as the conversation-list avatars.
  function miniAvatarHtml(url, name) {
    if (url) return `<div class="mini-avatar"><img src="${esc(url)}" alt="" loading="lazy" decoding="async"></div>`;
    return `<div class="mini-avatar">${esc((name || '?').slice(0, 2).toUpperCase())}</div>`;
  }

  // ── Input auto-grow + keyboard ────────────────────────────────────────────
  document.getElementById('msg-input').addEventListener('input', resizeInput);
  document.getElementById('msg-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  document.getElementById('friend-search-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') searchFriend();
  });

  // visualViewport for iOS keyboard
  (function () {
    const vv = window.visualViewport;
    if (!vv) return;
    const fit = () => {
      if (vv.scale && Math.abs(vv.scale - 1) > 0.05) return;
      document.body.style.height = Math.round(vv.height) + 'px';
      document.body.style.top = Math.round(vv.offsetTop) + 'px';
      document.body.classList.toggle('kb-open', (window.innerHeight - vv.height) > 100);
    };
    vv.addEventListener('resize', fit);
    vv.addEventListener('scroll', fit);
    document.getElementById('msg-input').addEventListener('focus', () => {
      setTimeout(() => {
        const thread = document.getElementById('msg-thread');
        thread.scrollTop = thread.scrollHeight;
        fit();
      }, 120);
    });
    fit();

    // Stop iOS rubber-band: preventDefault touchmove everywhere except real
    // scrollers (thread, conversations, modal/menu bodies) and the composer when
    // it overflows (long draft). overscroll-behavior alone doesn't stop
    // document bounce on iOS Safari when body is position:fixed.
    document.addEventListener('touchmove', e => {
      const t = e.target;
      if (t.closest && t.closest('.msg-thread, .msg-convs, .modal-body, .shell-more-sheet, .shell-lang-menu')) return;
      const inp = t.closest && t.closest('#msg-input');
      if (inp && inp.scrollHeight > inp.clientHeight + 1) return;
      e.preventDefault();
    }, { passive: false });
  })();

  // Start
  init();
