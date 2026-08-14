
  let LANGS = [];
  let lang = 'yue';
  let langName = 'your language';
  let conversations = [];
  let currentConv = null;       // conversation id or null (created on first send)
  let pointsTotal = 0;
  let sending = false;
  let activeDrill = null;       // { id, skill, panel, body } while a drill is in progress
  let rubyDefault = localStorage.getItem('tutor_ruby_inline') !== '0';   // default ON

  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function scriptClassFor(code) { const l = LANGS.find(x => x.code === code); return 'script-' + ((l && l.script_family) || 'latin'); }

  // ── Ruby helpers ─────────────────────────────────────────────────────────────
  const RUBY_LANGS = new Set(['yue', 'cmn', 'ko', 'hi', 'te', 'ja', 'bn', 'ur', 'ar', 'ru', 'fa', 'uk', 'el', 'th', 'he']);
  function needsRuby(l) { return RUBY_LANGS.has(l); }

  // Token cache (lang+text → [{text, roman, is_word}]) filled by ONE batched
  // request per render — per-bubble GET /api/ruby was N round-trips and made
  // opening a conversation visibly slow.
  const _tokenCache = {};
  function _ck(t) { return lang + '\0' + t; }
  async function fetchTokensBatch(texts) {
    if (!texts.length) return;
    try {
      const res = await fetch('/api/ruby/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ texts, lang }),
      });
      const results = (await res.json()).results || {};
      texts.forEach(t => { _tokenCache[_ck(t)] = results[t] || []; });
    } catch {
      texts.forEach(t => { if (!(_ck(t) in _tokenCache)) _tokenCache[_ck(t)] = []; });
    }
  }

  // Per-token HTML. Inline ruby when the bubble has it on; the tap/hover
  // tooltip shows the English gloss ONLY (romanization already lives in the
  // ruby — no redundant second way to see it).
  function _tokensToHtml(tokens, glossary, showRuby) {
    if (!Array.isArray(tokens) || !tokens.length) return '';
    const hasRoman = tokens.some(t => t.roman);
    return tokens.map(t => {
      const inner = (showRuby && hasRoman && t.is_word && t.roman)
        ? `<ruby>${esc(t.text)}<rt>${esc(t.roman)}</rt></ruby>` : esc(t.text);
      const g = t.is_word ? (glossary || {})[t.text] : null;
      return g ? `<span class="gl" tabindex="0" data-gloss="${esc(g)}">${inner}</span>` : inner;
    }).join('');
  }

  // Paint every .rb span in a bubble from the token cache (sync — cache must
  // already be filled). Re-runs cheaply when the bubble's あ toggle flips.
  function paintRuby(b) {
    b.querySelectorAll('.rb').forEach(s => {
      const tokens = _tokenCache[_ck(s.dataset.text)];
      if (!tokens || !tokens.length) { s.textContent = s.dataset.text; return; }
      const gloss = ('glossed' in s.dataset) ? (b._gloss || {}) : null;
      s.innerHTML = _tokensToHtml(tokens, gloss, b._ruby !== false);
    });
  }

  async function renderRubyAll(bubbles) {
    const needed = new Set();
    bubbles.forEach(b => b.querySelectorAll('.rb').forEach(s => {
      if (!(_ck(s.dataset.text) in _tokenCache)) needed.add(s.dataset.text);
    }));
    await fetchTokensBatch([...needed]);
    bubbles.forEach(paintRuby);
  }

  function rbSpan(text, glossed) {
    if (!text) return '';
    return `<span class="rb" data-text="${esc(text)}"${glossed ? ' data-glossed="1"' : ''}>${esc(text)}</span>`;
  }
  function targetSpan(text) {
    if (!text) return '';
    return needsRuby(lang) ? rbSpan(text) : esc(text);
  }

  function cleanForTTS(text, l) {
    let t = (text || '').replace(/\([^)]*\)/g, '').replace(/\*+/g, '').trim();
    if (RUBY_LANGS.has(l)) {
      const m = t.match(/^([\s\S]*[ऀ-෿ఀ-౿　-鿿가-힯㐀-䶿。！？、，；：“”‘’「」【】〔〕《》·…—～]+)/);
      if (m) t = m[1].trim();
    }
    return t;
  }
  let _audio = null;
  function playTTS(text, l) {
    if (!text) return;
    try { if (_audio) { _audio.pause(); _audio.currentTime = 0; } } catch {}
    let _b = null;
    try { _b = CantoShell.playBoosted('/api/tts?text=' + encodeURIComponent(text) + '&lang=' + encodeURIComponent(l)); } catch {}
    if (_b) { _b.catch(() => {}); return; }
    _audio = new Audio('/api/tts?text=' + encodeURIComponent(text) + '&lang=' + encodeURIComponent(l));
    _audio.play().catch(() => {});
  }

  // ── Init ─────────────────────────────────────────────────────────────────────
  async function init() {
    let latest = null;
    try {
      const [langs, convRes] = await Promise.all([
        fetch('/api/languages').then(r => r.json()).catch(() => ({ languages: [] })),
        fetch('/api/tutor/conversations').then(r => r.json()),
      ]);
      LANGS = langs.languages || [];
      lang = convRes.lang || 'yue';
      const l = LANGS.find(x => x.code === lang);
      langName = l ? l.name : lang;
      conversations = convRes.conversations || [];
      latest = convRes.latest || null;
      setPoints(convRes.points || 0);
    } catch {}
    document.getElementById('thread').className = 'thread ' + scriptClassFor(lang);
    document.querySelector('.composer textarea').classList.add(scriptClassFor(lang));
    updateCurrentTitle();
    if (conversations.length) {
      // The list response already carries the newest conversation's messages —
      // render straight away instead of a second round trip.
      if (latest && latest.id === conversations[0].id) {
        currentConv = latest.id;
        updateCurrentTitle();
        renderMessages(latest.messages || [], latest.active_drill_id);
      } else {
        openConversation(conversations[0].id);
      }
    } else {
      renderWelcome();
    }
    loadStreak();
  }

  function setPoints(n, bump) {
    pointsTotal = n;
    const el = document.getElementById('tutor-points');
    const _s = `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg>`;
    el.innerHTML = `<span style="display:inline-flex;align-items:center;gap:3px">${n} ${_s}</span>`;
    el.style.display = n > 0 ? '' : 'none';
    if (bump) { el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump'); }
  }

  // ── Conversations (header button + drawer) ───────────────────────────────────
  function _relTime(iso) {
    if (!iso) return '';
    // Server sends UTC 'YYYY-MM-DD HH:MM:SS' (SQLite); local bumps use ISO.
    let t = Date.parse(iso);
    if (isNaN(t)) t = Date.parse(iso.replace(' ', 'T') + 'Z');
    if (isNaN(t)) return '';
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    if (s < 604800) return Math.floor(s / 86400) + 'd ago';
    return new Date(t).toLocaleDateString();
  }

  function updateCurrentTitle() {
    const c = conversations.find(x => x.id === currentConv);
    document.getElementById('current-chat-title').textContent =
      (c && c.title) ? c.title : 'New chat';
  }

  function renderChatsList() {
    const list = document.getElementById('chats-list');
    list.innerHTML = '';
    if (!conversations.length) {
      list.innerHTML = '<div class="chats-empty">No conversations yet.<br>Start chatting below!</div>';
      return;
    }
    conversations.forEach(c => {
      const row = document.createElement('div');
      row.className = 'chat-row' + (c.id === currentConv ? ' active' : '');
      row.innerHTML = `<div class="chat-row-main">
          <div class="chat-row-title">${esc(c.title || 'New chat')}</div>
          <div class="chat-row-time">${esc(_relTime(c.updated_at || c.created_at))}</div>
        </div>
        <button class="chat-del" title="Delete">🗑</button>`;
      row.querySelector('.chat-row-main').onclick = () => {
        closeChats();
        if (c.id !== currentConv) openConversation(c.id);
      };
      row.querySelector('.chat-del').onclick = e => { e.stopPropagation(); deleteConversation(c.id); };
      list.appendChild(row);
    });
  }

  function openChats() {
    renderChatsList();
    document.getElementById('chats-drawer').classList.add('open');
  }
  function closeChats() {
    document.getElementById('chats-drawer').classList.remove('open');
  }
  document.getElementById('chats-btn').onclick = openChats;
  document.getElementById('new-chat-btn').onclick = newConversation;
  document.getElementById('chats-close').onclick = closeChats;
  document.getElementById('chats-scrim').onclick = closeChats;
  document.getElementById('chats-new').onclick = newConversation;
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeChats(); });

  async function newConversation() {
    closeChats();
    _openGen++;                    // invalidate any in-flight conversation load
    currentConv = null;
    updateCurrentTitle();
    renderWelcome();
    document.getElementById('composer-input').focus();
  }

  async function deleteConversation(id) {
    if (!confirm('Delete this conversation?')) return;
    await fetch('/api/tutor/conversations/' + id, { method: 'DELETE' }).catch(() => {});
    conversations = conversations.filter(c => c.id !== id);
    if (currentConv === id) { _openGen++; currentConv = null; renderWelcome(); }
    updateCurrentTitle();
    renderChatsList();
  }

  // Generation guard: clicking between chats quickly used to interleave both
  // fetches into the thread (conversations rendered one after the other).
  let _openGen = 0;
  async function openConversation(id) {
    const gen = ++_openGen;
    currentConv = id;
    updateCurrentTitle();
    document.getElementById('thread').innerHTML = '';
    try {
      const conv = await fetch('/api/tutor/conversations/' + id).then(r => r.json());
      if (gen !== _openGen) return;          // user moved on — drop this render
      renderMessages(conv.messages || [], conv.active_drill_id);
    } catch {}
  }

  function renderMessages(messages, activeDrillId = null) {
    const thread = document.getElementById('thread');
    thread.innerHTML = '';
    clearDrillUI();
    const rubyEls = [];
    let lastUser = null;
    let curDrill = null;   // { id, body }
    messages.forEach(m => {
      // Group consecutive messages sharing a drill_id into one collapsible panel.
      if (m.drill_id) {
        if (!curDrill || curDrill.id !== m.drill_id) {
          const active = (m.drill_id === activeDrillId);
          const { panel, body } = createDrillPanel(m.drill_skill, m.drill_id, active);
          curDrill = { id: m.drill_id, body };
          if (active) setDrillActive(m.drill_skill, m.drill_id, panel, body);
        }
      } else {
        curDrill = null;
      }
      const target = curDrill ? curDrill.body : null;
      const b = appendMessage(m, false, target);
      if (m.role === 'user') lastUser = b;
      if (m.role === 'tutor') {
        rubyEls.push(b);
        const cw = renderCorrections(lastUser, m.corrections, false);   // history: collapsed
        if (cw) rubyEls.push(cw);
      }
    });
    scrollThread();
    renderRubyAll(rubyEls).then(scrollThread);
    markInDeck(thread);
  }

  function renderWelcome() {
    const thread = document.getElementById('thread');
    thread.innerHTML = '';
    clearDrillUI();
    const b = document.createElement('div');
    b.className = 'bubble system';
    b.innerHTML = `👋 I'm your ${esc(langName)} tutor. Talk to me in ${esc(langName)} as much as
      you can — I'll keep the conversation going, fix slips gently, and teach you new
      words you can save to your flashcards. Don't worry about mistakes: if you don't
      know a word, talk around it! Earn <svg style="display:inline-block;vertical-align:-0.15em" viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg> by using what you've learned.`;
    thread.appendChild(b);
  }

  // ── Messages ─────────────────────────────────────────────────────────────────
  function scrollThread() {
    const t = document.getElementById('thread');
    t.scrollTop = t.scrollHeight;
  }

  // Collapsible section inside a tutor bubble (corrections / new words).
  // Fresh messages start open; history loads collapsed to keep the thread tidy.
  function collapsible(label, count, body, startOpen) {
    const wrap = document.createElement('div');
    wrap.className = 'xtra' + (startOpen ? ' open' : '');
    const head = document.createElement('button');
    head.className = 'xtra-head';
    head.type = 'button';
    head.innerHTML = `${label} · ${count} <span class="xtra-chev">▾</span>`;
    head.onclick = () => wrap.classList.toggle('open');
    body.classList.add('xtra-body');
    wrap.appendChild(head);
    wrap.appendChild(body);
    return wrap;
  }

  // Corrections live UNDER the learner's message (right-aligned), inserted as a
  // sibling right after the user bubble they critique.
  function renderCorrections(afterBubble, corrections, startOpen) {
    if (!afterBubble || !corrections || !corrections.length) return null;
    const wrap = document.createElement('div');
    wrap.className = 'corr-wrap';
    const body = document.createElement('div');
    corrections.forEach(c => {
      const card = document.createElement('div');
      card.className = 'corr-card';
      card.innerHTML = `${c.quote ? `<div class="corr-quote">${esc(c.quote)}</div>` : ''}
        <div class="corr-fixed"><button class="mini-speak" title="Listen">🔊</button>
          <span>${targetSpan(c.corrected)}</span>
          ${c.corrected_roman && !needsRuby(lang) ? `<span class="ni-rom">${esc(c.corrected_roman)}</span>` : ''}</div>
        ${c.construction ? `<div class="corr-form">📐 ${esc(c.construction)}</div>` : ''}
        ${c.explanation ? `<div class="corr-why">${esc(c.explanation)}</div>` : ''}`;
      card.querySelector('.mini-speak').onclick = () => playTTS(cleanForTTS(c.corrected, lang), lang);
      body.appendChild(card);
    });
    wrap.appendChild(collapsible('✏️ Correction' + (corrections.length > 1 ? 's' : ''),
                                 corrections.length, body, startOpen));
    afterBubble.insertAdjacentElement('afterend', wrap);
    return wrap;
  }

  function appendMessage(m, animate = true, target = null) {
    const parent = target || document.getElementById('thread');
    const b = document.createElement('div');
    if (m.role === 'user') {
      b.className = 'bubble user';
      b.textContent = m.text || '';
      parent.appendChild(b);
      scrollThread();
      return b;
    }
    if (m.role === 'system') {
      b.className = 'bubble system';
      b.textContent = m.text || '';
      parent.appendChild(b);
      scrollThread();
      return b;
    }
    // Tutor bubble: reply + per-line tools + collapsible corrections/new items.
    b.className = 'bubble tutor';
    b._gloss = m.gloss || {};
    b._ruby = rubyDefault;
    const reply = m.reply || '';
    const replyEn = (m.reply_en || '').trim();
    // A blank reply (a failed/empty model response, incl. older persisted drills)
    // would otherwise render as an empty bubble — show a clear placeholder instead.
    const replyHtml = reply.trim()
      ? rbSpan(reply, true)
      : '<span class="bubble-empty">⚠️ This didn’t load — please try again.</span>';
    b.innerHTML = `<div class="bubble-reply">${replyHtml}</div>
      ${replyEn ? `<div class="bubble-en" style="display:none">${esc(replyEn)}</div>` : ''}`;

    const items = m.new_items || [];
    const drillSkill = (m.drill || '').trim();

    const tools = document.createElement('div');
    tools.className = 'bubble-tools';
    tools.innerHTML = `<button class="tool-btn speak" title="Listen">🔊</button>
      ${replyEn ? `<button class="tool-btn en" title="Show English">Aa</button>` : ''}
      ${needsRuby(lang) ? `<button class="tool-btn rb-toggle${b._ruby ? '' : ' rom-toggle-off'}" title="Toggle romanization">あ</button>` : ''}
      ${drillSkill ? `<button class="drill-btn" title="Practice this pattern">🎯 Drill: ${esc(drillSkill)}</button>` : ''}`;
    b.appendChild(tools);

    tools.querySelector('.speak').onclick = () => playTTS(cleanForTTS(reply, lang), lang);
    const enToggle = tools.querySelector('.en');
    if (enToggle) {
      enToggle.onclick = () => {
        const enDiv = b.querySelector('.bubble-en');
        const showing = enDiv.style.display !== 'none';
        enDiv.style.display = showing ? 'none' : '';
        enToggle.classList.toggle('on', !showing);
      };
    }
    const rbToggle = tools.querySelector('.rb-toggle');
    if (rbToggle) {
      rbToggle.onclick = () => {
        b._ruby = !b._ruby;
        rubyDefault = b._ruby;                       // remember for new bubbles
        localStorage.setItem('tutor_ruby_inline', b._ruby ? '1' : '0');
        rbToggle.classList.toggle('rom-toggle-off', !b._ruby);
        paintRuby(b);
      };
    }
    const drill = tools.querySelector('.drill-btn');
    if (drill) drill.onclick = () => { if (!sending) startDrill(drillSkill, drill); };

    if (items.length) {
      const list = document.createElement('div');
      list.className = 'ni-list';
      items.forEach(it => list.appendChild(newItemChip(it)));
      b.appendChild(collapsible('💡 New words', items.length, list, animate));
    }

    parent.appendChild(b);
    scrollThread();
    return b;
  }

  function newItemChip(it) {
    const chip = document.createElement('div');
    chip.className = 'ni-chip';
    chip.dataset.word = it.target_text;
    chip.innerHTML = `<button class="mini-speak" title="Listen">🔊</button>
      <div style="flex:1;min-width:0">
        <span class="ni-word">${targetSpan(it.target_text)}</span>
        ${it.romanization && !needsRuby(lang) ? ` <span class="ni-rom">${esc(it.romanization)}</span>` : ''}
        <div class="ni-gloss">${esc(it.english)}</div>
        ${it.notes ? `<div class="ni-notes">${esc(it.notes)}</div>` : ''}
      </div>`;
    chip.querySelector('.mini-speak').onclick = () => playTTS(it.target_text, lang);
    const btn = document.createElement('button');
    btn.className = 'ni-add'; btn.type = 'button'; btn.textContent = '＋ Add';
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = '…';
      try {
        const res = await fetch('/api/cards', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source_text: it.english, target_text: it.target_text,
            romanization: it.romanization || '', target_lang: lang,
            notes: it.notes || 'From tutor chat', priority: 3,
          }),
        });
        if (!res.ok) throw new Error();
        btn.outerHTML = '<span class="ni-in-deck">✓ Added</span>';
      } catch { btn.disabled = false; btn.textContent = '＋ Add'; }
    };
    chip.appendChild(btn);
    const x = document.createElement('button');
    x.className = 'ni-x'; x.type = 'button'; x.title = 'Dismiss'; x.textContent = '✕';
    x.onclick = () => {
      const group = chip.closest('.xtra');
      chip.remove();
      if (group && !group.querySelector('.ni-chip')) group.remove();
    };
    chip.appendChild(x);
    return chip;
  }

  // Replace Add buttons with "In deck" for words already saved — ONE status
  // call per render (was one per message).
  async function markInDeck(container) {
    const chips = [...container.querySelectorAll('.ni-chip')].filter(c => c.querySelector('.ni-add'));
    if (!chips.length) return;
    const words = [...new Set(chips.map(c => c.dataset.word))];
    try {
      const res = await fetch('/api/cards/status', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ words, lang }),
      });
      const statuses = (await res.json()).statuses || {};
      chips.forEach(chip => {
        if (statuses[chip.dataset.word]) {
          const btn = chip.querySelector('.ni-add');
          if (btn) btn.outerHTML = '<span class="ni-in-deck">✓ In deck</span>';
        }
      });
    } catch {}
  }

  function showPointsToast(awards) {
    if (!awards || !awards.length) return;
    const total = awards.reduce((n, p) => n + (p.points || 0), 0);
    const what = awards.map(p => p.concept).filter(Boolean).join(', ');
    const toast = document.getElementById('pt-toast');
    const _ts = `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true" style="vertical-align:-0.1em"><path fill="#f0b429" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg>`;
    toast.innerHTML = `+${total} ${_ts}${what ? ' — ' + what : ''}`;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2600);
  }

  // ── Sending ──────────────────────────────────────────────────────────────────
  const input = document.getElementById('composer-input');
  const sendBtn = document.getElementById('composer-send');

  // Cap the textarea relative to the *visible* viewport so it shrinks when the
  // keyboard is up (otherwise a tall box eats the whole area above the keyboard).
  function composerMax() {
    const vh = (window.visualViewport && window.visualViewport.height) || window.innerHeight || 600;
    return Math.max(54, Math.min(110, Math.round(vh * 0.22)));
  }
  function growInput() {
    const max = composerMax();
    // Once the box is capped and overflowing, leave the height alone — resetting
    // to `auto` each keystroke momentarily expands the textarea, reflows the
    // composer and resets scrollTop, which reads as the text jumping up/down.
    // We only need to re-measure while the content can still grow the box.
    if (parseFloat(input.style.height) >= max && input.scrollHeight > max) return;
    const prevTop = input.scrollTop;
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, max) + 'px';
    input.scrollTop = prevTop;
  }
  input.addEventListener('input', () => {
    sendBtn.disabled = !input.value.trim() || sending;
    growInput();
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  sendBtn.onclick = () => send();

  async function send() {
    const text = input.value.trim();
    if (!text || sending) return;
    sending = true;
    sendBtn.disabled = true;
    input.value = ''; input.style.height = 'auto';

    // First message of a fresh chat: clear the welcome bubble, create the conv.
    if (!currentConv) document.getElementById('thread').innerHTML = '';
    // If a drill is in progress, the message is an answer that lives in the panel.
    const drillBody = activeDrill ? activeDrill.body : null;
    const userBubble = appendMessage({ role: 'user', text }, true, drillBody);
    const typing = document.createElement('div');
    typing.className = 'bubble tutor';
    typing.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    (drillBody || document.getElementById('thread')).appendChild(typing);
    scrollThread();

    try {
      if (!currentConv) {
        const res = await fetch('/api/tutor/conversations', { method: 'POST' });
        if (!res.ok) throw new Error('Could not start a conversation.');
        const conv = await res.json();
        currentConv = conv.id;
        conversations.unshift({ id: conv.id, title: text.slice(0, 60) });
      }
      const convAtSend = currentConv;
      const res = await fetch(`/api/tutor/conversations/${convAtSend}/messages`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        const msg = (await res.json().catch(() => ({}))).detail
          || (res.status === 402 ? 'You\'ve hit your monthly AI limit — add your own Gemini key in Settings.'
            : res.status === 429 ? 'Slow down a little — try again in a minute.'
              : 'The tutor couldn\'t reply — please try again.');
        throw new Error(msg);
      }
      const data = await res.json();
      typing.remove();
      // Only render into the thread if the user hasn't switched chats meanwhile.
      if (currentConv === convAtSend) {
        const b = appendMessage(data.message, true, drillBody);
        const cw = renderCorrections(userBubble, data.message.corrections, true);
        renderRubyAll(cw ? [b, cw] : [b]).then(scrollThread);
        markInDeck(b);
      }
      const gained = (data.message.points || []).reduce((n, p) => n + (p.points || 0), 0);
      if (gained > 0) { showPointsToast(data.message.points); }
      setPoints(data.points_total || 0, gained > 0);
      // Keep the local list in sync: first message becomes the title, and this
      // conversation moves to the top (most recent) with a fresh timestamp.
      const c = conversations.find(x => x.id === convAtSend);
      if (c) {
        if (!c.title) c.title = text.slice(0, 60);
        c.updated_at = new Date().toISOString();
        conversations = [c, ...conversations.filter(x => x.id !== c.id)];
      }
      updateCurrentTitle();
    } catch (e) {
      typing.remove();
      appendMessage({ role: 'system', text: e.message || 'Something went wrong — please try again.' });
    } finally {
      sending = false;
      sendBtn.disabled = !input.value.trim();
      input.focus();
    }
  }

  // ── Drill sub-sessions ─────────────────────────────────────────────────────
  // A drill is a self-contained, collapsible panel. The learner answers in the
  // normal composer (the server routes answers to drill mode while it's active),
  // and ends it with the panel's "End drill" button — after which it collapses.
  function createDrillPanel(skill, drillId, active) {
    const thread = document.getElementById('thread');
    const panel = document.createElement('div');
    panel.className = 'drill-panel' + (active ? '' : ' collapsed');
    panel.dataset.drillId = drillId;
    const head = document.createElement('div');
    head.className = 'drill-head';
    head.innerHTML = `<span class="drill-head-title">🎯 Drill: ${esc(skill || 'practice')}</span>`;
    const endBtn = document.createElement('button');
    endBtn.className = 'drill-end'; endBtn.type = 'button'; endBtn.textContent = 'End drill';
    endBtn.style.display = active ? '' : 'none';
    endBtn.onclick = (e) => { e.stopPropagation(); endDrill(); };
    const chev = document.createElement('span');
    chev.className = 'drill-chev'; chev.textContent = '▾';
    head.appendChild(endBtn); head.appendChild(chev);
    head.onclick = () => panel.classList.toggle('collapsed');
    const body = document.createElement('div');
    body.className = 'drill-body';
    panel.appendChild(head); panel.appendChild(body);
    thread.appendChild(panel);
    return { panel, body, endBtn };
  }

  function setDrillActive(skill, drillId, panel, body) {
    activeDrill = { id: drillId, skill, panel, body };
    input.placeholder = 'Type your translation…';
  }

  function clearDrillUI() {
    activeDrill = null;
    input.placeholder = 'Say something…';
  }

  async function endDrill() {
    if (!activeDrill || !currentConv) return;
    const conv = currentConv, panel = activeDrill.panel;
    const endBtn = panel && panel.querySelector('.drill-end');
    if (endBtn) endBtn.disabled = true;
    try { await fetch(`/api/tutor/conversations/${conv}/drill/end`, { method: 'POST' }); } catch {}
    if (panel) {
      panel.classList.add('collapsed');
      if (endBtn) endBtn.style.display = 'none';
    }
    clearDrillUI();
    input.focus();
  }

  // The tutor offered a generalizable pattern — begin a drill panel. The opening
  // question is the first bubble inside the panel; the learner just answers it.
  async function startDrill(skill, btn) {
    if (!skill || !currentConv || sending || activeDrill) return;
    sending = true;
    sendBtn.disabled = true;
    if (btn) { btn.disabled = true; btn.textContent = '🎯 …'; }
    const typing = document.createElement('div');
    typing.className = 'bubble tutor';
    typing.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    document.getElementById('thread').appendChild(typing);
    scrollThread();
    const convAtSend = currentConv;
    try {
      const res = await fetch(`/api/tutor/conversations/${convAtSend}/drill`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ skill }),
      });
      if (!res.ok) {
        const msg = (await res.json().catch(() => ({}))).detail
          || (res.status === 402 ? 'You\'ve hit your monthly AI limit — add your own Gemini key in Settings.'
            : res.status === 429 ? 'Slow down a little — try again in a minute.'
              : 'Couldn\'t start the drill — please try again.');
        throw new Error(msg);
      }
      const data = await res.json();
      // Defensive: a blank opener would render as an empty, confusing panel — treat
      // it as a failure (the backend already guards this, but never show a void).
      if (!((data.message && data.message.reply) || '').trim()) {
        throw new Error('The tutor couldn’t start the drill — please try again.');
      }
      typing.remove();
      if (btn) btn.remove();          // consumed — one drill per offer
      if (currentConv === convAtSend) {
        const { panel, body } = createDrillPanel(data.skill || skill, data.drill_id, true);
        setDrillActive(data.skill || skill, data.drill_id, panel, body);
        const b = appendMessage(data.message, true, body);
        renderRubyAll([b]).then(scrollThread);
        input.focus();
      }
    } catch (e) {
      typing.remove();
      if (btn) { btn.disabled = false; btn.textContent = '🎯 Drill: ' + skill; }
      appendMessage({ role: 'system', text: e.message || 'Something went wrong — please try again.' });
    } finally {
      sending = false;
      sendBtn.disabled = !input.value.trim();
    }
  }

  // ── Keyboard / viewport fit (mobile) ─────────────────────────────────────────
  // iOS doesn't shrink the layout viewport when the keyboard opens, so the chat
  // column would stay full-height and the composer would sit BELOW the keyboard.
  // The body is position:fixed (see CSS); we size it to the *visible* viewport so
  // the whole chat compresses above the keyboard. Crucially we also follow
  // visualViewport.offsetTop: when iOS scrolls the layout viewport to reveal the
  // focused input, a fixed body pinned at top:0 would get dragged off-screen (the
  // composer "shoots to the top"). Tracking offsetTop moves the body WITH the
  // visible area — following iOS rather than scrollTo-fighting it (no flash). This
  // is the MDN visualViewport pattern, using `top` (not transform) so position:
  // fixed descendants — the drawer, tooltip, toast — keep their viewport anchoring.
  (function () {
    const vv = window.visualViewport;
    if (!vv) return;
    const fit = () => {
      if (vv.scale && Math.abs(vv.scale - 1) > 0.05) return;   // ignore pinch-zoom
      document.body.style.height = Math.round(vv.height) + 'px';
      document.body.style.top = Math.round(vv.offsetTop) + 'px';
      // Keyboard up ⇒ the visible viewport is meaningfully shorter than the
      // layout viewport. Drop the home-indicator safe-area padding then.
      const kbOpen = (window.innerHeight - vv.height) > 100;
      document.body.classList.toggle('kb-open', kbOpen);
      growInput();
    };
    vv.addEventListener('resize', fit);
    vv.addEventListener('scroll', fit);
    // When the box is focused the keyboard animates in; re-fit after it settles
    // and keep the latest text in view.
    input.addEventListener('focus', () => {
      setTimeout(() => { fit(); scrollThread(); }, 120);
    });
    fit();

    // Stop iOS's document rubber-band: dragging on the composer (or any
    // non-scroller) otherwise bounces the whole fixed page and reveals empty
    // space below the textbox. overscroll-behavior alone doesn't cover this on
    // iOS Safari — we must preventDefault touchmove everywhere EXCEPT real
    // scrollers (the thread / chats list) and the composer when it actually
    // overflows (so a long draft can still be scrolled).
    document.addEventListener('touchmove', e => {
      const t = e.target;
      if (t.closest && t.closest('.thread, .chats-list')) return;
      const ce = t.closest && t.closest('#composer-input');
      if (ce && ce.scrollHeight > ce.clientHeight + 1) return;
      e.preventDefault();
    }, { passive: false });
  })();

  // ── Gloss tooltip (shared mechanism with learn.html) ─────────────────────────
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
      const r = el.getBoundingClientRect();
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      const left = Math.max(8, Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - 8));
      const top = (r.bottom + th + 8 > window.innerHeight && r.top - th - 5 > 0) ? r.top - th - 5 : r.bottom + 5;
      tip.style.left = left + 'px'; tip.style.top = top + 'px';
    }
    function hideTip() { if (_visible) { tip.style.display = 'none'; _visible = false; } }
    document.addEventListener('mouseover', e => {
      const gl = e.target.closest && e.target.closest('.gl[data-gloss]');
      gl ? showTip(gl) : hideTip();
    });
    document.addEventListener('focusin', e => {
      const gl = e.target.closest && e.target.closest('.gl[data-gloss]');
      if (gl) showTip(gl);
    });
    document.addEventListener('focusout', hideTip);
    document.addEventListener('scroll', hideTip, { passive: true, capture: true });
  })();

  async function loadStreak() {
    try {
      const { streak, points } = await fetch('/api/streak').then(r => r.json());
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
  init();
  document.addEventListener('langchange', function () { init(); });
