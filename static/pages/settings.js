
  // ── Tab navigation ──
  let _currentPane = 'account';
  function switchPane(pane) {
    _currentPane = pane;
    document.querySelectorAll('.settings-tab').forEach(t =>
      t.classList.toggle('active', t.dataset.pane === pane));
    document.querySelectorAll('.settings-pane').forEach(p =>
      p.classList.toggle('active', p.id === 'pane-' + pane));
    localStorage.setItem('settings_pane', pane);
  }

  async function init() {
    await Promise.all([loadLanguages(), loadSettings(), loadProfile(), loadBilling()]);
    checkAdmin();
    loadStreak();
    loadMastery();
    if (new URLSearchParams(location.search).get('upgraded')) {
      showToast('Welcome to Pro! Your subscription is active.');
      history.replaceState(null, '', '/settings');
    }
    const saved = localStorage.getItem('settings_pane');
    if (saved && document.getElementById('pane-' + saved)) switchPane(saved);
  }

  async function loadBilling() {
    let data;
    try {
      data = await fetch('/api/billing/status').then(r => r.json());
    } catch { return; }
    document.getElementById('plan-section').style.display = '';
    const upgradeBtn0 = document.getElementById('plan-upgrade-btn');
    const manageBtn0 = document.getElementById('plan-manage-btn');
    if (data.unlimited) {
      document.getElementById('plan-name').textContent = 'Unlimited';
      document.getElementById('plan-usage-text').textContent =
        'No monthly limit on AI translations.';
      document.getElementById('plan-usage-bar-wrap').style.display = 'none';
      document.getElementById('plan-note').textContent =
        'You have unlimited access (admin or your own Gemini key).';
      upgradeBtn0.style.display = 'none';
      manageBtn0.style.display = 'none';
      return;
    }
    const isPro = data.plan === 'pro';
    document.getElementById('plan-name').textContent = isPro ? 'Pro' : 'Free';

    const used = data.used, limit = data.limit;
    const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
    document.getElementById('plan-usage-text').textContent =
      `${used} / ${limit} AI translations this month`;
    const bar = document.getElementById('plan-usage-bar');
    bar.style.width = pct + '%';
    bar.style.background = pct >= 100 ? 'var(--again)' : (pct >= 80 ? 'var(--warn-accent)' : 'var(--primary)');

    const note = document.getElementById('plan-note');
    const cancelNote = document.getElementById('plan-cancel-note');
    const upgradeBtn = document.getElementById('plan-upgrade-btn');
    const manageBtn = document.getElementById('plan-manage-btn');
    const cancelBtn = document.getElementById('plan-cancel-btn');
    const resumeBtn = document.getElementById('plan-resume-btn');

    for (const b of [upgradeBtn, manageBtn, cancelBtn, resumeBtn]) {
      b.style.display = 'none';
      b.disabled = false;
    }
    cancelNote.style.display = 'none';
    cancelBtn.dataset.periodEnd = data.subscription_period_end || '';

    if (isPro) {
      note.textContent = 'Resets on the 1st of each month. Need more? Add your own Gemini key above for unlimited use.';
      if (data.has_subscription && data.cancel_at_period_end) {
        resumeBtn.style.display = '';
        let cancelMsg = 'Your subscription is set to cancel at the end of your billing period. You\'ll keep Pro until then.';
        if (data.subscription_period_end) {
          const d = new Date(data.subscription_period_end);
          const fmt = d.toLocaleDateString(undefined, { year:'numeric', month:'long', day:'numeric' });
          cancelMsg = `Your Pro access expires on ${fmt}. You can resume your subscription before then.`;
        }
        cancelNote.textContent = cancelMsg;
        cancelNote.style.display = '';
      } else if (data.has_subscription) {
        manageBtn.style.display = '';
        cancelBtn.style.display = '';
      }
    } else {
      note.textContent = data.billing_enabled
        ? `Upgrade for ${data.pro_limit} translations/month, or add your own Gemini key above for unlimited use.`
        : 'Add your own Gemini key above for unlimited use.';
      upgradeBtn.style.display = data.billing_enabled ? '' : 'none';
      manageBtn.style.display = data.has_subscription ? '' : 'none';
    }
  }

  async function startCheckout() {
    const btn = document.getElementById('plan-upgrade-btn');
    btn.disabled = true;
    try {
      const res = await fetch('/api/billing/checkout', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (res.ok && body.url) { location.href = body.url; return; }
      showToast(body.detail || 'Could not start checkout.');
    } catch { showToast('Network error.'); }
    btn.disabled = false;
  }

  async function openPortal() {
    const btn = document.getElementById('plan-manage-btn');
    btn.disabled = true;
    try {
      const res = await fetch('/api/billing/portal', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (res.ok && body.url) { location.href = body.url; return; }
      showToast(body.detail || 'Could not open billing portal.');
    } catch { showToast('Network error.'); }
    btn.disabled = false;
  }

  function showCancelModal() {
    const periodEnd = document.getElementById('plan-cancel-btn').dataset.periodEnd;
    let bodyText = 'Your Pro access will continue until the end of your billing period. After that your account reverts to Free.';
    if (periodEnd) {
      const d = new Date(periodEnd);
      const fmt = d.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
      bodyText = `Your Pro access will continue until ${fmt}. After that your account reverts to Free.`;
    }
    document.getElementById('cancel-modal-body').textContent = bodyText;
    const confirmBtn = document.getElementById('cancel-confirm-btn');
    confirmBtn.disabled = false;
    confirmBtn.textContent = 'Yes, cancel';
    document.getElementById('cancel-modal').style.display = 'flex';
  }

  function closeCancelModal() {
    document.getElementById('cancel-modal').style.display = 'none';
  }

  async function confirmCancel() {
    const btn = document.getElementById('cancel-confirm-btn');
    btn.disabled = true;
    btn.textContent = 'Canceling…';
    try {
      const res = await fetch('/api/billing/cancel', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        closeCancelModal();
        await loadBilling();
        let fmt = 'the end of your billing period';
        if (body.period_end) {
          const d = new Date(body.period_end);
          fmt = d.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
        }
        showToast(`Subscription canceled. Pro access continues until ${fmt}.`);
        return;
      }
      showToast(body.detail || 'Could not cancel subscription.');
    } catch { showToast('Network error.'); }
    btn.disabled = false;
    btn.textContent = 'Yes, cancel';
  }

  async function resumeSubscription() {
    const btn = document.getElementById('plan-resume-btn');
    btn.disabled = true;
    try {
      const res = await fetch('/api/billing/resume', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        showToast('Subscription resumed. Your plan will renew as normal.');
        await loadBilling();
        return;
      }
      showToast(body.detail || 'Could not resume subscription.');
    } catch { showToast('Network error.'); }
    btn.disabled = false;
  }

  async function loadProfile() {
    try {
      const data = await fetch('/api/profile').then(r => r.json());
      document.getElementById('profile-display-name').value = data.display_name || '';
      document.getElementById('profile-username').value = data.username || '';
      document.getElementById('profile-email').value = data.email || '';
      renderAvatar(data.avatar_url, data.display_name || data.username || '');
      if (data.email && !data.email_verified) {
        document.getElementById('profile-email-desc').innerHTML =
          'Email not yet verified. <a href="#" onclick="resendVerification(event)">Resend link</a>';
      }
      renderEvenToken(data.has_even_hub_token);
    } catch {}
  }

  function renderEvenToken(hasToken) {
    document.getElementById('even-token-status').textContent = hasToken ? ' · Connected ✓' : '';
    document.getElementById('even-token-generate').textContent =
      hasToken ? 'Regenerate token' : 'Generate token';
    document.getElementById('even-token-revoke').style.display = hasToken ? '' : 'none';
  }

  async function generateEvenToken() {
    try {
      const res = await fetch('/api/profile/even-hub-token', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(body.detail || 'Could not generate token.'); return; }
      document.getElementById('even-token-value').value = body.token;
      document.getElementById('even-token-reveal').style.display = 'flex';
      renderEvenToken(true);
    } catch { showToast('Could not generate token.'); }
  }

  function copyEvenToken() {
    const el = document.getElementById('even-token-value');
    el.select();
    const copy = window.__copyToClipboard
      ? window.__copyToClipboard(el.value)
      : navigator.clipboard?.writeText(el.value);
    Promise.resolve(copy).then(
      () => showToast('Token copied.'),
      () => showToast('Select the token and copy it manually.'),
    );
  }

  async function revokeEvenToken() {
    if (!confirm('Revoke the current glasses token? The plugin will stop syncing until you generate a new one.')) return;
    try {
      const res = await fetch('/api/profile/even-hub-token', { method: 'DELETE' });
      if (!res.ok) { showToast('Could not revoke token.'); return; }
      document.getElementById('even-token-reveal').style.display = 'none';
      document.getElementById('even-token-value').value = '';
      renderEvenToken(false);
      showToast('Glasses token revoked.');
    } catch { showToast('Could not revoke token.'); }
  }

  function renderAvatar(url, name) {
    const el = document.getElementById('avatar-preview');
    const removeBtn = document.getElementById('avatar-remove-btn');
    if (url) {
      el.innerHTML = `<img src="${url}" alt="Profile picture">`;
      removeBtn.style.display = '';
    } else {
      el.textContent = (name || '?').slice(0, 1).toUpperCase();
      removeBtn.style.display = 'none';
    }
  }

  async function onAvatarSelect(input) {
    const file = input.files && input.files[0];
    input.value = '';  // allow re-selecting the same file later
    if (!file) return;
    if (!file.type.startsWith('image/')) { showToast('Please choose an image file.'); return; }
    const fd = new FormData();
    fd.append('file', file);
    showToast('Uploading…');
    try {
      const res = await fetch('/api/profile/avatar', { method: 'POST', body: fd });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        renderAvatar(body.avatar_url, '');
        showToast('Profile picture updated.');
      } else {
        showToast(body.detail || 'Upload failed.');
      }
    } catch { showToast('Network error.'); }
  }

  async function removeAvatar() {
    try {
      const res = await fetch('/api/profile/avatar', { method: 'DELETE' });
      if (res.ok) {
        const name = document.getElementById('profile-display-name').value
          || document.getElementById('profile-username').value || '';
        renderAvatar(null, name);
        showToast('Profile picture removed.');
      } else { showToast('Could not remove picture.'); }
    } catch { showToast('Network error.'); }
  }

  async function saveProfile() {
    const displayName = document.getElementById('profile-display-name').value.trim();
    const username = document.getElementById('profile-username').value.trim();
    const email = document.getElementById('profile-email').value.trim();
    const msg = document.getElementById('profile-msg');
    msg.style.color = '';
    msg.textContent = 'Saving…';
    try {
      const res = await fetch('/api/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: displayName, username, email }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        msg.style.color = 'var(--good)';
        msg.textContent = body.email_changed
          ? 'Saved! Check your email to verify the new address.'
          : 'Profile saved.';
      } else {
        msg.style.color = 'var(--again)';
        msg.textContent = body.detail || 'Failed to save.';
      }
    } catch {
      msg.style.color = 'var(--again)';
      msg.textContent = 'Network error.';
    }
  }

  async function resendVerification(e) {
    e.preventDefault();
    const email = document.getElementById('profile-email').value.trim();
    if (!email) return;
    await fetch('/api/resend-verification', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });
    document.getElementById('profile-email-desc').textContent = 'Verification email sent — check your inbox.';
  }

  async function checkAdmin() {
    try {
      const me = await fetch('/api/me').then(r => r.json());
      if (me.is_admin) {
        document.getElementById('admin-tab').style.display = '';
        await loadUsers();
      }
    } catch {}
  }

  function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  let _allUsers = [];
  let _usersPage = 0;
  const _USERS_PER_PAGE = 10;

  async function loadUsers() {
    const list = document.getElementById('user-list');
    list.innerHTML = '<p class="settings-desc" style="padding:12px 18px">Loading…</p>';
    document.getElementById('users-pagination').style.display = 'none';
    try {
      const { users } = await fetch('/api/admin/users').then(r => r.json());
      _allUsers = users;
      _usersPage = 0;
      if (!users.length) {
        list.innerHTML = '<p class="settings-desc" style="padding:12px 18px">No users.</p>';
        return;
      }
      renderUsersPage();
    } catch {
      list.innerHTML = '<p class="settings-desc" style="padding:12px 18px;color:var(--again)">Failed to load users.</p>';
    }
  }

  function renderUsersPage() {
    const list = document.getElementById('user-list');
    const total = _allUsers.length;
    const pages = Math.ceil(total / _USERS_PER_PAGE);
    const start = _usersPage * _USERS_PER_PAGE;
    const slice = _allUsers.slice(start, start + _USERS_PER_PAGE);
    list.innerHTML = '';
    slice.forEach(u => {
      const row = document.createElement('div');
      row.className = 'user-row';
      const uname = escHtml(u.username).replace(/'/g, "\\'");
      const isPro = u.plan === 'pro';
      const hasStripe = !!u.stripe_customer_id;
      const planBtn = u.is_admin
        ? '<span class="settings-desc" style="white-space:nowrap">Unlimited</span>'
        : hasStripe
            ? `<span class="settings-desc" style="white-space:nowrap">Pro (Stripe)</span>`
            : (isPro
                ? `<button class="admin-action-btn" onclick="setPlan(${u.id}, 'free')">Plan: Pro</button>`
                : `<button class="admin-action-btn" onclick="setPlan(${u.id}, 'pro')">Plan: Free</button>`);
      const verifiedTag = u.email_verified === 0
        ? '<span style="color:var(--again);font-size:0.75rem;margin-left:6px">unverified</span>'
        : '';
      const emailLine = u.email
        ? `<div class="settings-desc">${escHtml(u.email)}${verifiedTag}</div>`
        : '';
      const nameLine = u.display_name
        ? `<div class="settings-desc" style="color:var(--text)">${escHtml(u.display_name)}</div>`
        : '';
      row.innerHTML = `
        <div>
          <div class="user-name">${escHtml(u.username)}${u.is_admin ? '<span class="admin-tag">Admin</span>' : ''}</div>
          ${nameLine}${emailLine}
          <div class="settings-desc">Created ${escHtml(u.created_at || '')}</div>
        </div>
        ${planBtn}
        <button class="admin-action-btn" onclick="resetPassword(${u.id}, '${uname}')">Reset pw</button>
        <button class="admin-action-btn danger" onclick="deleteUser(${u.id}, '${uname}')">Delete</button>
      `;
      list.appendChild(row);
    });
    const pag = document.getElementById('users-pagination');
    if (pages > 1) {
      pag.style.display = 'flex';
      pag.innerHTML = `<button onclick="_usersPage--;renderUsersPage()" ${_usersPage===0?'disabled':''}>&#8592; Prev</button>
        <span class="page-info">${_usersPage+1} / ${pages}</span>
        <button onclick="_usersPage++;renderUsersPage()" ${_usersPage>=pages-1?'disabled':''}>Next &#8594;</button>`;
    } else {
      pag.style.display = 'none';
    }
  }

  async function createUser(e) {
    e.preventDefault();
    const username = document.getElementById('new-username').value.trim();
    const password = document.getElementById('new-password').value;
    const isAdmin = document.getElementById('new-admin').checked;
    if (!username || !password) { showToast('Username and password required.'); return; }
    if (password.length < 6) { showToast('Password must be at least 6 characters.'); return; }
    try {
      const res = await fetch('/api/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, is_admin: isAdmin }),
      });
      if (!res.ok) { showToast((await res.json()).detail || 'Failed to create user.'); return; }
      document.getElementById('new-username').value = '';
      document.getElementById('new-password').value = '';
      document.getElementById('new-admin').checked = false;
      showToast('User created.');
      loadUsers();
    } catch { showToast('Network error.'); }
  }

  async function setPlan(userId, plan) {
    try {
      const res = await fetch(`/api/admin/users/${userId}/plan`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan }),
      });
      if (!res.ok) { showToast((await res.json()).detail || 'Failed to change plan.'); return; }
      showToast(plan === 'pro' ? 'Upgraded to Pro.' : 'Reverted to Free.');
      loadUsers();
    } catch { showToast('Failed to change plan.'); }
  }

  async function resetPassword(userId, username) {
    const pw = prompt(`New password for ${username} (min 6 characters):`);
    if (!pw) return;
    if (pw.length < 6) { showToast('Password must be at least 6 characters.'); return; }
    try {
      const res = await fetch(`/api/admin/users/${userId}/password`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw }),
      });
      if (!res.ok) throw new Error();
      showToast('Password reset.');
    } catch { showToast('Failed to reset password.'); }
  }

  async function deleteUser(userId, username) {
    if (!confirm(`Delete user "${username}" and ALL their cards, labels, and settings? This cannot be undone.`)) return;
    try {
      const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
      if (!res.ok) { showToast((await res.json()).detail || 'Failed to delete user.'); return; }
      showToast('User deleted.');
      loadUsers();
    } catch { showToast('Network error.'); }
  }

  let languages = [];
  async function loadLanguages() {
    try {
      const res = await fetch('/api/languages').then(r => r.json());
      languages = res.languages;
    } catch {
      languages = [{ code: 'yue', name: 'Cantonese', flag: '🇭🇰' }];
    }
    const msel = document.getElementById('mastery-lang-select');
    msel.innerHTML = '';
    languages.forEach(l => {
      const opt = document.createElement('option');
      opt.value = l.code;
      opt.textContent = (l.flag ? l.flag + ' ' : '') + l.name;
      msel.appendChild(opt);
    });
    msel.value = (settings && settings.default_target_lang) || 'yue';
  }

  let settings = {};
  async function loadSettings() {
    try {
      settings = await fetch('/api/settings').then(r => r.json());
    } catch {
      settings = { new_cards_per_day: 20, default_target_lang: 'yue' };
    }
    document.getElementById('settings-new-per-day').value = settings.new_cards_per_day;
    document.getElementById('settings-lesson-buffer').value = settings.lesson_buffer !== undefined ? settings.lesson_buffer : 3;
    document.getElementById('settings-audio-romanization').checked = settings.audio_show_romanization !== false;
    document.getElementById('settings-auto-add-vocab').checked = !!settings.auto_add_reader_vocab;
    document.getElementById('settings-compact-phrases').checked = !!settings.chat_compact_phrases;
    document.getElementById('settings-reader-difficulty').value = settings.default_reader_difficulty || 'B1';
    document.getElementById('settings-lesson-premium').checked = !!settings.lesson_premium;
    if (settings.lesson_premium_model) {
      document.getElementById('lesson-premium-desc').textContent =
        `Use ${settings.lesson_premium_model} for the planner + lesson authoring (slower, costs more)`;
    }
    const lmSel = document.getElementById('settings-lesson-model');
    if (lmSel && Array.isArray(settings.lesson_model_options)) {
      lmSel.innerHTML = '<option value="">Default (follow toggle above)</option>' +
        settings.lesson_model_options.map(m => `<option value="${m}">${m}</option>`).join('');
      lmSel.value = settings.lesson_model || '';
    }
    document.getElementById('settings-learner-profile').value = settings.learner_profile || '';
    const goalSel = document.getElementById('settings-daily-goal');
    const goal = settings.daily_xp_goal || 50;
    goalSel.value = [10, 30, 50, 100].includes(goal) ? String(goal) : '50';
    // Read-only: the app shell reports the device's zone automatically. Shown
    // so "why did my streak roll over then?" is answerable at a glance.
    const tzEl = document.getElementById('settings-timezone');
    if (tzEl) tzEl.textContent = (settings.timezone || 'UTC').replace(/_/g, ' ');
    document.getElementById('settings-lesson-length').value = settings.lesson_length || 'standard';
    document.getElementById('settings-ai-speak').checked = settings.lesson_ai_speak !== false;
    document.getElementById('settings-speaking').checked = settings.speaking_drills !== false;
    document.getElementById('settings-audio-mix').checked = settings.audio_mix !== false;
    _setVolumeSlider(settings.audio_volume);
    document.getElementById('settings-warmup').checked = settings.lesson_warmup !== false;
    document.getElementById('settings-course-focus').value = settings.course_focus || 'balanced';
    const pScore = settings.populate_min_score !== undefined ? settings.populate_min_score : 0.55;
    document.getElementById('settings-populate-score').value = Math.round(pScore * 100);
    updateScoreLabel(Math.round(pScore * 100));

    const sel = document.getElementById('settings-default-lang');
    sel.innerHTML = '';
    languages.forEach(l => {
      const opt = document.createElement('option');
      opt.value = l.code;
      opt.textContent = (l.flag ? l.flag + ' ' : '') + l.name;
      sel.appendChild(opt);
    });
    sel.value = settings.default_target_lang || 'yue';
    renderLangDetail();
    renderAiSection();
  }

  function renderLangDetail() {
    const code = document.getElementById('settings-default-lang').value;
    const l = languages.find(x => x.code === code);
    const panel = document.getElementById('lang-detail');
    if (!l) { panel.style.display = 'none'; return; }
    const rows = [];
    if (l.full_name && l.full_name !== l.name) rows.push(`<strong>Variant:</strong> ${l.full_name}`);
    rows.push(`<strong>Script:</strong> ${l.script}`);
    if (l.romanization) rows.push(`<strong>Romanization:</strong> ${l.romanization}`);
    rows.push(`<strong>Code:</strong> ${l.code}`);
    panel.innerHTML = rows.join('<span style="margin:0 8px;color:var(--border)">·</span>');
    panel.style.display = '';
  }

  function modelLabel(m) {
    return m
      .replace(/^gemini-/, 'Gemini ')
      .replace('flash-lite', 'Flash Lite')
      .replace(/flash$/, 'Flash')
      .replace(/pro$/, 'Pro');
  }

  function renderAiSection() {
    const models = settings.available_models || [];
    const mt = document.getElementById('settings-model-translate');
    const mr = document.getElementById('settings-model-reader');
    [mt, mr].forEach(sel => {
      sel.innerHTML = '';
      models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m; opt.textContent = modelLabel(m);
        sel.appendChild(opt);
      });
    });
    mt.value = settings.model_translate || settings.default_model;
    mr.value = settings.model_reader || settings.default_model;

    const canChoose = !!settings.can_choose_models;
    mt.disabled = !canChoose;
    mr.disabled = !canChoose;

    const status = document.getElementById('api-key-status');
    status.textContent = settings.has_api_key ? ' Key is set.' : ' No key set.';
    status.style.color = settings.has_api_key ? 'var(--primary,#1a73e8)' : 'var(--text-muted,#888)';

    const note = document.getElementById('model-note');
    if (settings.has_api_key) {
      note.textContent = 'Models apply to AI calls billed to your key.';
    } else if (settings.is_admin) {
      note.textContent = 'These models apply to your own AI calls on the shared key.';
    } else {
      note.textContent = 'Add your own Gemini key to choose models. Otherwise translations use the default model.';
    }
  }

  async function saveApiKey() {
    const input = document.getElementById('settings-api-key');
    const val = input.value.trim();
    if (!val) { showToast('Enter a key, or use Clear to remove.'); return; }
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gemini_api_key: val }),
      });
      if (!res.ok) throw new Error();
      input.value = '';
      settings.has_api_key = true;
      settings.can_choose_models = true;
      renderAiSection();
      showToast('API key saved.');
    } catch {
      showToast('Failed to save API key.');
    }
  }

  async function clearApiKey() {
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gemini_api_key: '' }),
      });
      if (!res.ok) throw new Error();
      document.getElementById('settings-api-key').value = '';
      settings.has_api_key = false;
      settings.can_choose_models = false;
      settings = await fetch('/api/settings').then(r => r.json());
      renderAiSection();
      showToast('API key removed.');
    } catch {
      showToast('Failed to remove API key.');
    }
  }

  async function saveModels() {
    const mt = document.getElementById('settings-model-translate').value;
    const mr = document.getElementById('settings-model-reader').value;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_translate: mt, model_reader: mr }),
      });
      if (!res.ok) throw new Error();
      settings.model_translate = mt;
      settings.model_reader = mr;
      showToast('Models saved.');
    } catch {
      showToast('Failed to save models.');
    }
  }

  let _saveStudyTimer;
  function updateScoreLabel(v) {
    document.getElementById('populate-score-label').textContent = (v / 100).toFixed(2);
  }

  function debounceSaveStudy() {
    clearTimeout(_saveStudyTimer);
    _saveStudyTimer = setTimeout(saveSettings, 600);
  }

  async function saveSettings() {
    const val = parseInt(document.getElementById('settings-new-per-day').value, 10);
    const lang = document.getElementById('settings-default-lang').value;
    const buf = parseInt(document.getElementById('settings-lesson-buffer').value, 10);
    const popScore = parseInt(document.getElementById('settings-populate-score').value, 10) / 100;
    const dailyGoal = parseInt(document.getElementById('settings-daily-goal').value, 10) || 50;
    const lessonLength = document.getElementById('settings-lesson-length').value;
    const courseFocus = document.getElementById('settings-course-focus').value;
    if (!val || val < 1 || val > 500) { showToast('Must be between 1 and 500.'); return; }
    const lessonBuf = isNaN(buf) ? 3 : Math.max(0, Math.min(10, buf));
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_cards_per_day: val, default_target_lang: lang, lesson_buffer: lessonBuf, populate_min_score: popScore, daily_xp_goal: dailyGoal, lesson_length: lessonLength, course_focus: courseFocus }),
      });
      if (!res.ok) throw new Error();
      settings.new_cards_per_day = val;
      settings.default_target_lang = lang;
      settings.lesson_buffer = lessonBuf;
      settings.populate_min_score = popScore;
      settings.daily_xp_goal = dailyGoal;
      settings.lesson_length = lessonLength;
      settings.course_focus = courseFocus;
      showToast('Saved.');
    } catch {
      showToast('Failed to save settings.');
    }
  }

  async function saveAudioRomanization() {
    const checked = document.getElementById('settings-audio-romanization').checked;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio_show_romanization: checked }),
      });
      if (!res.ok) throw new Error();
      settings.audio_show_romanization = checked;
      showToast(checked ? 'Romanization shown on audio cards.' : 'Romanization hidden on audio cards.');
    } catch {
      document.getElementById('settings-audio-romanization').checked = !checked;
      showToast('Failed to save setting.');
    }
  }

  async function saveAudioMix() {
    const checked = document.getElementById('settings-audio-mix').checked;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio_mix: checked }),
      });
      if (!res.ok) throw new Error();
      settings.audio_mix = checked;
      // Write through to the cached shell snapshot AND re-apply now, so the
      // change takes effect on this page instead of after a reload.
      if (window.CantoShell) {
        window.CantoShell.patch('settings', { audio_mix: checked });
        window.CantoShell.applyAudioSession({ audio_mix: checked });
      }
      showToast(checked ? 'Sound will play over your music.' : 'Sound will play on its own.');
    } catch {
      document.getElementById('settings-audio-mix').checked = !checked;
      showToast('Failed to save setting.');
    }
  }

  async function saveSpeakingDrills() {
    const checked = document.getElementById('settings-speaking').checked;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speaking_drills: checked }),
      });
      if (!res.ok) throw new Error();
      settings.speaking_drills = checked;
      showToast(checked ? 'Speaking drills on.' : 'Speaking drills off.');
    } catch {
      document.getElementById('settings-speaking').checked = !checked;
      showToast('Failed to save setting.');
    }
  }

  // ── Audio volume ──────────────────────────────────────────────────────────
  // How loud our own clips play against whatever else is going. 100% is the clip
  // as recorded; above that the app-shell routes playback through a gain node
  // with a limiter (there is no web API to duck the user's music harder, so the
  // only lever is our own level).
  function _setVolumeSlider(vol) {
    const pct = Math.round((Number(vol) || 1) * 100);
    document.getElementById('settings-audio-volume').value = String(Math.min(300, Math.max(50, pct)));
    _renderVolumeLabel(pct);
  }
  function _renderVolumeLabel(pct) {
    document.getElementById('audio-volume-label').textContent = pct + '%';
  }
  // Live, before anything is saved — the point of the Test button is comparing
  // against the music that's already playing.
  function previewAudioVolume(pct) {
    _renderVolumeLabel(pct);
    if (window.CantoShell) CantoShell.setAudioGain(Number(pct) / 100);
  }

  let _volumeTestAudio = null;
  function testAudioVolume() {
    const lang = settings.default_target_lang || 'yue';
    const btn = document.getElementById('settings-volume-test');
    try { if (_volumeTestAudio) { _volumeTestAudio.pause(); } } catch (e) {}
    try { CantoShell.stopBoosted(); } catch (e) {}
    // A fresh element each time: one already routed through the gain graph is
    // fine to reuse, but a failed one is permanently dead (see learn.js).
    const url = '/api/tts?text=' + encodeURIComponent(_VOLUME_TEST_TEXT[lang] || 'Hello')
              + '&lang=' + encodeURIComponent(lang);
    btn.disabled = true;
    const done = () => { btn.disabled = false; };
    // The boosted path is what a lesson will actually use, so test THAT.
    let boosted = null;
    try { boosted = CantoShell.playBoosted(url, done); } catch {}
    if (boosted) { boosted.catch(() => { done(); showToast('Audio unavailable right now.'); }); return; }
    _volumeTestAudio = new Audio(url);
    _volumeTestAudio.onended = done;
    _volumeTestAudio.play().then(done).catch(() => { done(); showToast('Audio unavailable right now.'); });
  }

  // A short, real phrase in the language being learned — testing the level on
  // the voice you actually hear beats a beep.
  const _VOLUME_TEST_TEXT = {
    yue: '你好，聽唔聽到我講嘢？', cmn: '你好，听得到吗？', ja: 'こんにちは', ko: '안녕하세요',
    fr: 'Bonjour, tu m\u2019entends ?', es: 'Hola, ¿me oyes?', de: 'Hallo, hörst du mich?',
    it: 'Ciao, mi senti?', pt: 'Olá, está a ouvir?', th: 'สวัสดีค่ะ', hi: 'नमस्ते',
  };

  async function saveAudioVolume() {
    const pct = Number(document.getElementById('settings-audio-volume').value);
    const vol = Math.round(pct) / 100;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio_volume: vol }),
      });
      if (!res.ok) throw new Error();
      settings.audio_volume = vol;
      // Write through to the shell snapshot, or the next page reads the old
      // value from sessionStorage and the boost silently reverts.
      if (window.CantoShell) CantoShell.patch('settings', { audio_volume: vol });
      showToast(pct === 100 ? 'Audio volume reset.' : 'Audio volume set to ' + pct + '%.');
    } catch {
      showToast('Failed to save setting.');
    }
  }

  async function saveAiSpeak() {
    const checked = document.getElementById('settings-ai-speak').checked;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lesson_ai_speak: checked }),
      });
      if (!res.ok) throw new Error();
      settings.lesson_ai_speak = checked;
      showToast(checked ? 'AI Speak practice on.' : 'AI Speak practice off.');
    } catch {
      document.getElementById('settings-ai-speak').checked = !checked;
      showToast('Failed to save setting.');
    }
  }

  async function saveWarmup() {
    const checked = document.getElementById('settings-warmup').checked;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lesson_warmup: checked }),
      });
      if (!res.ok) throw new Error();
      settings.lesson_warmup = checked;
      showToast(checked ? 'Lesson warm-ups on.' : 'Lesson warm-ups off.');
    } catch {
      document.getElementById('settings-warmup').checked = !checked;
      showToast('Failed to save setting.');
    }
  }

  async function saveCompactPhrases() {
    const checked = document.getElementById('settings-compact-phrases').checked;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_compact_phrases: checked }),
      });
      if (!res.ok) throw new Error();
      settings.chat_compact_phrases = checked;
      showToast(checked ? 'Photo button: icon only.' : 'Photo button: icon + text.');
    } catch {
      document.getElementById('settings-compact-phrases').checked = !checked;
      showToast('Failed to save setting.');
    }
  }

  async function saveLessonPremium() {
    const checked = document.getElementById('settings-lesson-premium').checked;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lesson_premium: checked }),
      });
      if (!res.ok) throw new Error();
      settings.lesson_premium = checked;
      showToast('Lesson model preference saved.');
    } catch {
      document.getElementById('settings-lesson-premium').checked = !!settings.lesson_premium;
      showToast('Failed to save setting.');
    }
  }

  async function saveLessonModel() {
    const val = document.getElementById('settings-lesson-model').value;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lesson_model: val }),
      });
      if (!res.ok) throw new Error();
      settings.lesson_model = val;
      showToast(val ? `Lessons will use ${val}.` : 'Lesson model reset to default.');
    } catch {
      document.getElementById('settings-lesson-model').value = settings.lesson_model || '';
      showToast('Failed to save setting.');
    }
  }

  async function saveLearnerProfile() {
    const val = (document.getElementById('settings-learner-profile').value || '').slice(0, 2000);
    const msg = document.getElementById('learner-profile-msg');
    msg.style.color = ''; msg.textContent = 'Saving…';
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ learner_profile: val }),
      });
      if (!res.ok) throw new Error();
      msg.style.color = 'var(--good)'; msg.textContent = 'Saved.';
      setTimeout(() => { msg.textContent = ''; }, 2000);
    } catch {
      msg.style.color = 'var(--again)'; msg.textContent = 'Failed to save.';
    }
  }

  let _masteryConcepts = [];
  let _masteryPage = 0;
  const _MASTERY_PER_PAGE = 10;

  async function loadMastery() {
    const sel = document.getElementById('mastery-lang-select');
    const lang = sel.value || (settings && settings.default_target_lang) || 'yue';
    const list = document.getElementById('mastery-list');
    list.innerHTML = '<span class="settings-desc">Loading…</span>';
    document.getElementById('mastery-pagination').style.display = 'none';
    try {
      const { concepts } = await fetch('/api/mastery?lang=' + encodeURIComponent(lang)).then(r => r.json());
      _masteryConcepts = concepts;
      _masteryPage = 0;
      if (!concepts.length) {
        list.innerHTML = '<span class="settings-desc">No data yet — complete some lessons to see mastery stats.</span>';
        return;
      }
      renderMasteryPage();
      document.getElementById('mastery-section').style.display = '';
    } catch {
      list.innerHTML = '<span class="settings-desc" style="color:var(--again)">Failed to load mastery data.</span>';
    }
  }

  function renderMasteryPage() {
    const list = document.getElementById('mastery-list');
    const total = _masteryConcepts.length;
    const pages = Math.ceil(total / _MASTERY_PER_PAGE);
    const start = _masteryPage * _MASTERY_PER_PAGE;
    const slice = _masteryConcepts.slice(start, start + _MASTERY_PER_PAGE);
    list.innerHTML = '';
    slice.forEach(c => {
      const pct = c.total ? Math.round(c.correct / c.total * 100) : 0;
      const color = pct >= 80 ? 'var(--good)' : pct >= 50 ? 'var(--warn-accent)' : 'var(--again)';
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid var(--border)';
      row.innerHTML = `
        <div style="flex:1;min-width:0">
          <span style="font-size:0.88rem;color:var(--text)">${escHtml(c.concept_key.replace(/_/g,' '))}</span>
        </div>
        <div style="width:80px;height:6px;background:var(--border);border-radius:999px;overflow:hidden;flex-shrink:0">
          <div style="height:100%;width:${pct}%;background:${color};transition:width .3s"></div>
        </div>
        <div style="font-size:0.82rem;color:${color};width:44px;text-align:right;flex-shrink:0">${pct}% <span style="color:var(--text-muted)">${c.total}×</span></div>`;
      list.appendChild(row);
    });
    const pag = document.getElementById('mastery-pagination');
    if (pages > 1) {
      pag.style.display = 'flex';
      pag.innerHTML = `<button onclick="_masteryPage--;renderMasteryPage()" ${_masteryPage===0?'disabled':''}>&#8592; Prev</button>
        <span class="page-info">${_masteryPage+1} / ${pages}</span>
        <button onclick="_masteryPage++;renderMasteryPage()" ${_masteryPage>=pages-1?'disabled':''}>Next &#8594;</button>`;
    } else {
      pag.style.display = 'none';
    }
  }

  async function saveReaderDifficulty() {
    const val = document.getElementById('settings-reader-difficulty').value;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ default_reader_difficulty: val }),
      });
      if (!res.ok) throw new Error();
      settings.default_reader_difficulty = val;
      showToast('Default difficulty saved.');
    } catch {
      document.getElementById('settings-reader-difficulty').value = settings.default_reader_difficulty || 'B1';
      showToast('Failed to save setting.');
    }
  }

  async function saveAutoAdd() {
    const checked = document.getElementById('settings-auto-add-vocab').checked;
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_add_reader_vocab: checked }),
      });
      if (!res.ok) throw new Error();
      settings.auto_add_reader_vocab = checked;
      showToast(checked ? 'Auto-add enabled.' : 'Auto-add disabled.');
    } catch {
      document.getElementById('settings-auto-add-vocab').checked = !checked;
      showToast('Failed to save setting.');
    }
  }

  // ── Appearance (theme) — localStorage-only, applied by /static/theme.js ────
  (function () {
    const seg = document.getElementById('theme-seg');
    if (!seg) return;
    function sync() {
      const pref = localStorage.getItem('theme') || 'auto';
      seg.querySelectorAll('button').forEach(b =>
        b.classList.toggle('active', b.dataset.pref === pref));
    }
    seg.addEventListener('click', e => {
      const b = e.target.closest('button[data-pref]');
      if (!b) return;
      if (window.setThemePref) setThemePref(b.dataset.pref);
      sync();
    });
    sync();
  })();

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
          el.innerHTML = parts.join('<span style="opacity:0.4;margin:0 4px">·</span>'); el.style.display = '';
        });
      }
    } catch {}
  }

  async function runAtomize() {
    const lang = settings.default_target_lang || 'yue';
    const btn = document.getElementById('atomize-btn');
    btn.disabled = true;
    btn.textContent = 'Running…';
    try {
      const res = await fetch('/api/cards/atomize', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Error');
      const msg = data.total_phrase_cards === 0
        ? 'No phrase cards found'
        : data.queued > 0
          ? `Queued ${data.queued} new word card${data.queued !== 1 ? 's' : ''} (${data.total_phrase_cards} phrase cards total)`
          : 'All phrase card words already in deck';
      showToast(msg + (data.remaining > 0 ? ` — ${data.remaining} more phrase cards, run again` : ''));
    } catch (e) { showToast(e.message || 'Failed'); }
    btn.disabled = false;
    btn.textContent = 'Run';
  }

  async function runBackfillClassifiers() {
    const lang = settings.default_target_lang || 'yue';
    const btn = document.getElementById('backfill-classifier-btn');
    btn.disabled = true;
    btn.textContent = 'Running…';
    try {
      const res = await fetch('/api/cards/backfill-classifiers', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Error');
      showToast(data.message || 'Done');
    } catch (e) { showToast(e.message || 'Failed'); }
    btn.disabled = false;
    btn.textContent = 'Run';
  }

  let _toastTimer;
  function showToast(msg) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
  }

  init();
  document.addEventListener('langchange', function (e) {
    var sel = document.getElementById('settings-default-lang');
    if (sel && e.detail && e.detail.code) { sel.value = e.detail.code; renderLangDetail(); }
  });
