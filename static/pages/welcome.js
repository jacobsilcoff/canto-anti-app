
  let selectedLang = null;
  let billingEnabled = false;

  (async function () {
    try {
      const [langRes, status] = await Promise.all([
        fetch('/api/languages').then(r => r.json()),
        fetch('/api/billing/status').then(r => r.json()),
      ]);

      // Already fully onboarded — skip this page.
      if (status.onboarded) { window.location.replace('/'); return; }

      billingEnabled = !!status.billing_enabled && !status.unlimited && status.plan === 'free';
      if (billingEnabled) {
        document.getElementById('step-indicator').style.display = 'flex';
        if (status.limit) document.getElementById('free-limit').textContent = status.limit;
        if (status.pro_limit) document.getElementById('pro-limit').textContent = status.pro_limit;
      }

      const currentLang = status.default_target_lang || 'yue';
      const langs = langRes.languages || [];
      const container = document.getElementById('lang-cards');

      // Pre-select current lang if it was already set
      selectedLang = currentLang;

      langs.forEach(l => {
        const card = document.createElement('div');
        card.className = 'lang-card' + (l.code === currentLang ? ' selected' : '');
        card.dataset.code = l.code;

        const scriptSamples = {
          yue: '你好', cmn: '你好', fr: 'Bonjour', es: 'Hola', de: 'Hallo',
          it: 'Ciao', pt: 'Olá', tl: 'Kumusta', ms: 'Helo', id: 'Halo',
          ko: '안녕하세요', hi: 'नमस्ते', te: 'నమస్తే',
          ja: 'こんにちは', bn: 'নমস্কার', ur: 'السلام علیکم', ar: 'مرحبا',
          sw: 'Habari', ru: 'Привет', vi: 'Xin chào', fa: 'سلام',
          tr: 'Merhaba', nl: 'Hallo', pl: 'Cześć', sv: 'Hej', nb: 'Hei', ro: 'Bună',
          uk: 'Привіт', el: 'Γεια σου', th: 'สวัสดี', he: 'שלום',
        };
        const scriptFonts = {
          hangul: 'var(--hangul-font)', devanagari: 'var(--devanagari-font)', telugu: 'var(--telugu-font)', chinese: 'var(--chinese-font)',
          japanese: 'var(--japanese-font)', bengali: 'var(--bengali-font)', arabic: 'var(--arabic-font)', cyrillic: 'var(--cyrillic-font)',
          greek: 'var(--greek-font)', thai: 'var(--thai-font)', hebrew: 'var(--hebrew-font)',
        };
        const sampleFont = scriptFonts[l.script_family] || 'inherit';
        card.innerHTML = `
          <div class="lang-flag">${l.flag || '🌐'}</div>
          <div class="lang-name">${l.name}</div>
          <div class="lang-script" style="font-family:${sampleFont}">${scriptSamples[l.code] || ''}</div>
        `;
        card.onclick = () => {
          document.querySelectorAll('.lang-card').forEach(c => c.classList.remove('selected'));
          card.classList.add('selected');
          selectedLang = l.code;
          document.getElementById('lang-next-btn').disabled = false;
        };
        container.appendChild(card);
      });

      // Enable the button since we pre-selected
      document.getElementById('lang-next-btn').disabled = false;

    } catch (e) {
      console.error(e);
    }
  })();

  let _suggestedDeck = null;

  function _showStep(id) {
    document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
    document.getElementById(id).classList.add('active');
  }

  // After picking a language, offer the official "Top 100" deck for it (if one
  // has been generated). Skips straight to the plan/finish step if none exists.
  async function goToDeckStep() {
    const btn = document.getElementById('lang-next-btn');
    btn.disabled = true;
    try {
      const res = await fetch('/api/decks/featured?lang=' + encodeURIComponent(selectedLang)).then(r => r.json());
      _suggestedDeck = (res.decks || [])[0] || null;
    } catch { _suggestedDeck = null; }
    btn.disabled = false;
    if (!_suggestedDeck) { goToStep2(); return; }
    document.getElementById('deck-suggest-card').innerHTML = `
      <div class="deck-suggest-title">${esc(_suggestedDeck.name)}</div>
      ${_suggestedDeck.description ? `<div class="deck-suggest-desc">${esc(_suggestedDeck.description)}</div>` : ''}
      <div class="deck-suggest-meta">${_suggestedDeck.card_count} cards · free to add</div>`;
    document.getElementById('deck-add-btn').disabled = false;
    document.getElementById('deck-add-btn').textContent = `Add these ${_suggestedDeck.card_count} words`;
    _showStep('step-deck');
  }

  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  async function addSuggestedDeck() {
    if (!_suggestedDeck) { goToStep2(); return; }
    const btn = document.getElementById('deck-add-btn');
    btn.disabled = true;
    btn.textContent = 'Adding…';
    try {
      await fetch(`/api/decks/${_suggestedDeck.id}/import`, { method: 'POST' });
    } catch {}
    goToStep2();
  }

  function skipDeck() { goToStep2(); }

  function goToStep2() {
    if (!billingEnabled) {
      finish(false);
      return;
    }
    _showStep('step-plan');
    document.getElementById('dot-0').classList.remove('active');
    document.getElementById('dot-1').classList.add('active');
  }

  async function onboard() {
    try {
      await fetch('/api/onboard', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang: selectedLang }),
      });
    } catch {}
  }

  async function finish(goPro) {
    document.getElementById('free-btn').disabled = true;
    document.getElementById('pro-btn').disabled = true;
    document.getElementById('lang-next-btn').disabled = true;
    await onboard();
    if (goPro) {
      try {
        const res = await fetch('/api/billing/checkout', { method: 'POST' });
        const body = await res.json().catch(() => ({}));
        if (res.ok && body.url) { location.href = body.url; return; }
      } catch {}
    }
    window.location.replace('/');
  }
