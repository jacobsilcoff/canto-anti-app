(function () {
  'use strict';

  const CACHE_KEY = 'canto:bootstrap';
  const CACHE_TTL = 60 * 1000;
  const nativeFetch = window.fetch.bind(window);
  const endpointMap = new Map([
    ['/api/me', ['me', value => value]],
    ['/api/settings', ['settings', value => value]],
    ['/api/languages', ['languages', value => ({ languages: value || [] })]],
    ['/api/streak', ['streak', value => value]],
    ['/api/cards/due-count', ['due', value => value]],
    ['/api/billing/status', ['billing', value => value]],
    ['/api/notifications/counts', ['notifications', value => value]],
  ]);
  const dirty = new Set();
  let snapshot = null;
  let refreshPromise = null;

  try {
    const cached = JSON.parse(sessionStorage.getItem(CACHE_KEY) || 'null');
    if (cached && cached.data) snapshot = cached;
  } catch (_) {}

  function store(data) {
    snapshot = { at: Date.now(), data };
    try { sessionStorage.setItem(CACHE_KEY, JSON.stringify(snapshot)); } catch (_) {}
    document.dispatchEvent(new CustomEvent('canto:bootstrap', { detail: data }));
    return data;
  }

  function refresh(force) {
    if (refreshPromise && !force) return refreshPromise;
    refreshPromise = nativeFetch('/api/bootstrap', { headers: { Accept: 'application/json' } })
      .then(response => {
        if (!response.ok) throw new Error('bootstrap failed');
        return response.json();
      })
      .then(data => {
        dirty.clear();
        return store(data);
      })
      .finally(() => { refreshPromise = null; });
    return refreshPromise;
  }

  const initialData = snapshot && snapshot.data;
  const initialFresh = snapshot && Date.now() - snapshot.at < CACHE_TTL;
  const ready = initialData ? Promise.resolve(initialData) : refresh();
  if (initialData && !initialFresh) refresh().catch(() => {});

  function responseFor(value) {
    return new Response(JSON.stringify(value), {
      status: 200,
      headers: { 'Content-Type': 'application/json', 'X-Canto-Cache': 'bootstrap' },
    });
  }

  function updateSection(key, value) {
    if (!snapshot || !snapshot.data) return;
    snapshot.data[key] = value;
    snapshot.at = Date.now();
    try { sessionStorage.setItem(CACHE_KEY, JSON.stringify(snapshot)); } catch (_) {}
    document.dispatchEvent(new CustomEvent('canto:bootstrap', { detail: snapshot.data }));
  }

  function markDirtyForMutation(path) {
    if (path === '/api/settings') {
      dirty.add('settings'); dirty.add('due');
    }
    if (/^\/api\/cards\/\d+\/review$/.test(path) ||
        /^\/api\/lessons\/\d+\/complete$/.test(path) ||
        path.startsWith('/api/quests/')) {
      dirty.add('streak'); dirty.add('due');
    }
    if (path.includes('/messages') || path.includes('/read') || path.includes('/friends/')) {
      dirty.add('notifications');
    }
  }

  window.fetch = function (input, init) {
    const requestUrl = typeof input === 'string' ? input : input.url;
    const url = new URL(requestUrl, location.origin);
    const method = ((init && init.method) || (typeof input !== 'string' && input.method) || 'GET').toUpperCase();
    const sameOrigin = url.origin === location.origin;
    const mapped = sameOrigin && method === 'GET' && !url.search ? endpointMap.get(url.pathname) : null;
    if (mapped) {
      const [key, shape] = mapped;
      return ready.then(data => {
        // `refresh()` may replace the snapshot after `ready` has settled. Read
        // the live snapshot so stale-while-revalidate callers receive the
        // refreshed value rather than the object captured on first load.
        const current = snapshot && snapshot.data ? snapshot.data : data;
        if (!dirty.has(key) && current && current[key] != null) return responseFor(shape(current[key]));
        return nativeFetch(input, init).then(response => {
          if (response.ok) response.clone().json().then(value => {
            const normalized = key === 'languages' ? (value.languages || []) : value;
            dirty.delete(key);
            updateSection(key, normalized);
          }).catch(() => {});
          return response;
        });
      });
    }
    return nativeFetch(input, init).then(response => {
      if (sameOrigin && method !== 'GET' && response.ok) markDirtyForMutation(url.pathname);
      return response;
    });
  };

  window.CantoShell = {
    ready,
    refresh: () => refresh(true),
    get: key => snapshot && snapshot.data ? snapshot.data[key] : null,
    invalidate: key => dirty.add(key),
  };

  const flame = '<svg viewBox="0 0 16 20" width="12" height="15" aria-hidden="true"><path fill="currentColor" d="M8 0C5.5 3.5 3 6.5 3 10.5a5 5 0 0010 0c0-2-.9-3.8-1.8-4.8-.4 1.6-1.1 2.6-2 2.2.4-2.5.2-5.2-1.2-7.9z"/></svg>';
  const star = '<svg viewBox="0 0 20 20" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M10 1l2.2 6.8H19l-5.6 4.1 2.1 6.6L10 14.4l-5.5 4.1 2.1-6.6L1 7.8h6.8z"/></svg>';

  function compactNumber(value) {
    if (value >= 10000) return Math.round(value / 1000) + 'k';
    if (value >= 1000) return (value / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(value);
  }

  window.renderHeaderStats = function (streak, points) {
    const parts = [];
    if (streak > 0) parts.push('<span class="hstat hstat-streak" title="' + streak.toLocaleString() + '-day streak">' + flame + compactNumber(streak) + '</span>');
    if (points > 0) parts.push('<span class="hstat hstat-xp" title="' + points.toLocaleString() + ' XP">' + star + compactNumber(points) + '</span>');
    document.querySelectorAll('.streak-display').forEach(el => {
      el.innerHTML = parts.join('');
      el.style.display = parts.length ? '' : 'none';
    });
  };

  function renderBadges(data) {
    const due = data.due && data.due.count || 0;
    document.querySelectorAll('.due-badge').forEach(el => {
      el.textContent = due > 99 ? '99+' : String(due);
      el.classList.toggle('visible', due > 0);
    });
    const notifications = data.notifications || {};
    const total = notifications.total != null
      ? notifications.total
      : (notifications.unread_messages || 0) + (notifications.friend_requests || 0);
    document.querySelectorAll('.notif-badge').forEach(el => {
      el.textContent = total > 99 ? '99+' : String(total);
      el.classList.toggle('visible', total > 0);
    });
    if (data.me && data.me.is_admin) {
      document.querySelectorAll('.nav-admin,.more-admin').forEach(el => { el.style.display = ''; });
    }
    if (data.streak) window.renderHeaderStats(data.streak.streak || 0, data.streak.points || 0);
  }

  function installLanguageControl(data) {
    if (document.getElementById('shell-lang-control')) return;
    const languages = data.languages || [];
    if (languages.length < 2) return;
    const sorted = languages.slice().sort((a, b) => a.name.localeCompare(b.name));
    const currentCode = data.settings && data.settings.default_target_lang || 'yue';
    const wrap = document.createElement('div');
    wrap.id = 'shell-lang-control';
    wrap.className = 'shell-lang-control';
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'shell-lang-pill';
    pill.setAttribute('aria-label', 'Change learning language');
    pill.setAttribute('aria-haspopup', 'listbox');
    pill.setAttribute('aria-expanded', 'false');
    const flag = document.createElement('span');
    flag.className = 'shell-lang-flag';
    const name = document.createElement('span');
    name.className = 'shell-lang-name';
    const chevron = document.createElement('span');
    chevron.className = 'shell-lang-chevron';
    chevron.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';
    pill.append(flag, name, chevron);

    const menu = document.createElement('div');
    menu.className = 'shell-lang-menu';
    menu.setAttribute('role', 'listbox');
    function syncControl(code) {
      const selected = sorted.find(lang => lang.code === code) || { code, name: code, flag: '🌐' };
      flag.textContent = selected.flag || '🌐';
      name.textContent = selected.name;
      menu.querySelectorAll('[data-code]').forEach(option => {
        const active = option.dataset.code === code;
        option.classList.toggle('active', active);
        option.setAttribute('aria-selected', active ? 'true' : 'false');
        option.querySelector('.shell-lang-check').textContent = active ? '✓' : '';
      });
    }
    function closeMenu() {
      menu.classList.remove('open');
      pill.setAttribute('aria-expanded', 'false');
    }
    function positionMenu() {
      const rect = pill.getBoundingClientRect();
      const menuRect = menu.getBoundingClientRect();
      const below = rect.bottom + 6;
      const top = below + menuRect.height <= innerHeight - 8
        ? below : Math.max(8, rect.top - menuRect.height - 6);
      const left = Math.max(8, Math.min(rect.left, innerWidth - menuRect.width - 8));
      menu.style.top = top + 'px';
      menu.style.left = left + 'px';
    }
    sorted.forEach(lang => {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'shell-lang-option';
      option.dataset.code = lang.code;
      option.setAttribute('role', 'option');
      const check = document.createElement('span');
      check.className = 'shell-lang-check';
      const label = document.createElement('span');
      label.textContent = (lang.flag ? lang.flag + ' ' : '') + lang.name;
      option.append(check, label);
      option.onclick = async event => {
        event.stopPropagation();
        option.disabled = true;
        await window.setAppLanguage(lang.code);
        option.disabled = false;
        closeMenu();
      };
      menu.appendChild(option);
    });
    pill.onclick = event => {
      event.stopPropagation();
      const opening = !menu.classList.contains('open');
      closeMenu();
      if (opening) {
        menu.classList.add('open');
        pill.setAttribute('aria-expanded', 'true');
        positionMenu();
      }
    };
    wrap.append(pill, menu);
    const slot = matchMedia('(max-width: 1199px)').matches
      ? document.querySelector('[data-lang-slot]')
      : document.querySelector('[data-lang-slot-rail]');
    if (slot) slot.appendChild(wrap);
    else {
      const menuSlot = document.querySelector('[data-shell-lang-slot]');
      if (menuSlot) menuSlot.appendChild(wrap);
    }
    syncControl(currentCode);
    window._syncLanguageControl = syncControl;
    document.addEventListener('click', event => {
      if (!wrap.contains(event.target)) closeMenu();
    });
    window.addEventListener('resize', closeMenu);
  }

  window.setAppLanguage = function (code) {
    const current = snapshot && snapshot.data && snapshot.data.settings && snapshot.data.settings.default_target_lang;
    if (!code || code === current) return Promise.resolve(false);
    return window.fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ default_target_lang: code }),
    }).then(response => {
      if (!response.ok) throw new Error('language update failed');
      if (snapshot && snapshot.data && snapshot.data.settings) {
        snapshot.data.settings.default_target_lang = code;
        store(snapshot.data);
      }
      if (window._syncLanguageControl) window._syncLanguageControl(code);
      const lang = snapshot.data.languages.find(item => item.code === code);
      document.dispatchEvent(new CustomEvent('langchange', { detail: { code, lang } }));
      return true;
    }).catch(() => false);
  };

  function installPlanUi(data) {
    const billing = data.billing;
    if (!billing) return;
    const h1 = document.querySelector('header h1');
    if (h1 && !document.getElementById('plan-pill')) {
      const label = billing.unlimited ? '∞' : billing.plan === 'pro' ? 'Pro' : 'Free';
      const pill = document.createElement('a');
      pill.id = 'plan-pill';
      pill.href = '/settings#plan-section';
      pill.textContent = label;
      pill.title = 'Your plan: ' + label;
      h1.appendChild(pill);
    }
    if (!billing.billing_enabled || billing.unlimited || billing.plan !== 'free' ||
        location.pathname === '/settings' || localStorage.getItem('canto_hide_upgrade') ||
        document.getElementById('shell-upgrade-bar')) return;
    const bar = document.createElement('div');
    bar.id = 'shell-upgrade-bar';
    bar.className = 'shell-upgrade-bar';
    const text = document.createElement('span');
    text.textContent = 'Free plan · ' + billing.used + '/' + billing.limit + ' AI uses this month';
    const link = document.createElement('a');
    link.href = '/settings#plan-section';
    link.textContent = 'View plan';
    const close = document.createElement('button');
    close.type = 'button'; close.textContent = '×'; close.setAttribute('aria-label', 'Dismiss plan reminder');
    close.onclick = () => { localStorage.setItem('canto_hide_upgrade', '1'); bar.remove(); };
    bar.append(text, link, close);
    document.body.insertBefore(bar, document.body.firstChild);
  }

  const moreIcon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.9"/><circle cx="12" cy="12" r="1.9"/><circle cx="19" cy="12" r="1.9"/></svg>';
  const menuIcons = {
    quick: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>',
    browse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    feedback: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-2.82 1.18V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00-1.18-2.82H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009.92 4.6H10a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9v.08A1.65 1.65 0 0020.91 10H21a2 2 0 010 4h-.09A1.65 1.65 0 0019.4 15z"/></svg>',
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 20V10"/><path d="M12 20V4"/><path d="M6 20v-6"/></svg>',
    signout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
  };
  function quickAddLink() {
    const link = document.createElement('a');
    link.className = 'more-link shell-quick-add';
    link.href = '/translate';
    link.innerHTML = menuIcons.quick + 'Quick add / translate';
    return link;
  }

  function menuLink(href, label, icon) {
    return '<a class="shell-more-item" href="' + href + '">' + menuIcons[icon] + '<span>' + label + '</span></a>';
  }

  function installMoreMenu(data) {
    const navTrigger = document.querySelector('[data-shell-more-trigger]');
    const homeSheet = document.querySelector('.more-sheet');
    if (homeSheet) {
      if (!homeSheet.querySelector('.shell-quick-add')) homeSheet.insertBefore(quickAddLink(), homeSheet.children[1] || null);
      if (navTrigger) navTrigger.onclick = () => window.openMore();
      return;
    }
    if (document.getElementById('shell-more-overlay')) return;
    let button = navTrigger;
    if (!button) {
      button = document.createElement('button');
      button.id = 'shell-more-btn'; button.className = 'shell-more-btn in-toolbar'; button.type = 'button';
      button.setAttribute('aria-label', 'More destinations'); button.innerHTML = moreIcon;
      const tutorTop = document.querySelector('.tutor-top');
      if (tutorTop) tutorTop.appendChild(button);
    }
    const overlay = document.createElement('div');
    overlay.id = 'shell-more-overlay'; overlay.className = 'shell-more-overlay';
    overlay.innerHTML = '<div class="shell-more-sheet" role="dialog" aria-modal="true" aria-label="More destinations">' +
      '<div class="shell-more-handle"></div><div class="shell-more-lang" data-shell-lang-slot></div>' +
      menuLink('/translate', 'Quick add / translate', 'quick') +
      menuLink('/browse', 'Browse & decks', 'browse') +
      menuLink('/feedback', 'Feedback', 'feedback') +
      menuLink('/settings', 'Settings', 'settings') +
      (data.me && data.me.is_admin ? menuLink('/admin/dashboard', 'Admin dashboard', 'dashboard') : '') +
      '<button type="button" class="shell-more-item danger" data-shell-logout>' + menuIcons.signout + '<span>Sign out</span></button></div>';
    const close = () => { overlay.classList.remove('open'); button.setAttribute('aria-expanded', 'false'); };
    button.onclick = () => { overlay.classList.add('open'); button.setAttribute('aria-expanded', 'true'); };
    overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
    overlay.querySelector('[data-shell-logout]').onclick = () => window.doLogout();
    document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
    document.body.appendChild(overlay);
  }

  function syncNotificationButton() {
    const supported = 'Notification' in window && 'PushManager' in window && 'serviceWorker' in navigator;
    const on = supported && Notification.permission === 'granted' && localStorage.getItem('push_subscribed') === '1';
    document.querySelectorAll('.notif-bell-btn').forEach(button => {
      button.textContent = on ? 'Notifications on' : 'Enable notifications';
      button.classList.toggle('enabled', on);
    });
    document.querySelectorAll('.notif-status-desc').forEach(el => {
      el.textContent = !supported ? 'Not supported in this browser' : on ? 'On' : 'Off';
    });
  }

  window.toggleNotifications = async function () {
    const toast = typeof window.showToast === 'function' ? window.showToast : function () {};
    if (!('Notification' in window) || !('PushManager' in window) || !('serviceWorker' in navigator)) {
      toast('Push notifications are not supported in this browser.'); return;
    }
    if (Notification.permission === 'denied') { toast('Notifications are blocked in browser settings.'); return; }
    try {
      const registration = await navigator.serviceWorker.ready;
      const existing = await registration.pushManager.getSubscription();
      if (existing) {
        const value = existing.toJSON();
        await existing.unsubscribe();
        await nativeFetch('/api/push/subscribe', {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: value.endpoint, p256dh: value.keys.p256dh, auth: value.keys.auth }),
        });
        localStorage.setItem('push_subscribed', '0'); syncNotificationButton(); return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') { syncNotificationButton(); return; }
      const keyResponse = await nativeFetch('/api/push/vapid-public-key').then(r => r.json());
      const padding = '='.repeat((4 - keyResponse.public_key.length % 4) % 4);
      const raw = atob((keyResponse.public_key + padding).replace(/-/g, '+').replace(/_/g, '/'));
      const key = Uint8Array.from(Array.from(raw).map(char => char.charCodeAt(0)));
      const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const value = subscription.toJSON();
      await nativeFetch('/api/push/subscribe', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: value.endpoint, p256dh: value.keys.p256dh, auth: value.keys.auth }),
      });
      localStorage.setItem('push_subscribed', '1'); syncNotificationButton();
    } catch (error) { toast('Could not update notifications.'); }
  };

  window._refreshNotifCounts = function () {
    return nativeFetch('/api/notifications/counts').then(r => r.json()).then(value => {
      dirty.delete('notifications'); updateSection('notifications', value);
      if (snapshot && snapshot.data) renderBadges(snapshot.data);
      return value;
    }).catch(() => null);
  };

  function initialize(data) {
    renderBadges(data);
    installMoreMenu(data);
    installLanguageControl(data);
    installPlanUi(data);
    syncNotificationButton();
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  function onReady(data) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initialize(data), { once: true });
    else initialize(data);
  }
  ready.then(onReady).catch(() => {});
  document.addEventListener('canto:bootstrap', event => {
    if (document.readyState !== 'loading') renderBadges(event.detail);
  });
})();
