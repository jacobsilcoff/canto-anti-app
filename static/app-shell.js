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
    if (document.getElementById('shell-lang-select')) return;
    const languages = data.languages || [];
    if (languages.length < 2) return;
    const current = data.settings && data.settings.default_target_lang || 'yue';
    const select = document.createElement('select');
    select.id = 'shell-lang-select';
    select.className = 'shell-lang-select';
    select.setAttribute('aria-label', 'Learning language');
    languages.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach(lang => {
      const option = document.createElement('option');
      option.value = lang.code;
      option.textContent = (lang.flag ? lang.flag + ' ' : '') + lang.name;
      option.selected = lang.code === current;
      select.appendChild(option);
    });
    const slot = matchMedia('(max-width: 1199px)').matches
      ? document.querySelector('[data-lang-slot]')
      : document.querySelector('[data-lang-slot-rail]');
    if (slot) slot.appendChild(select);
    else {
      const menuSlot = document.querySelector('[data-shell-lang-slot]');
      if (menuSlot) menuSlot.appendChild(select);
    }
    select.addEventListener('change', () => window.setAppLanguage(select.value));
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
      const select = document.getElementById('shell-lang-select');
      if (select) select.value = code;
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
  function quickAddLink() {
    const link = document.createElement('a');
    link.className = 'more-link shell-quick-add';
    link.href = '/translate';
    link.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>Quick add / translate';
    return link;
  }

  function installMoreMenu(data) {
    const homeSheet = document.querySelector('.more-sheet');
    if (homeSheet) {
      if (!homeSheet.querySelector('.shell-quick-add')) homeSheet.insertBefore(quickAddLink(), homeSheet.children[1] || null);
      return;
    }
    if (document.getElementById('shell-more-btn')) return;
    const button = document.createElement('button');
    button.id = 'shell-more-btn'; button.className = 'shell-more-btn'; button.type = 'button';
    button.setAttribute('aria-label', 'More destinations'); button.innerHTML = moreIcon;
    const overlay = document.createElement('div');
    overlay.id = 'shell-more-overlay'; overlay.className = 'shell-more-overlay';
    overlay.innerHTML = '<div class="shell-more-sheet" role="dialog" aria-modal="true" aria-label="More destinations">' +
      '<div class="shell-more-handle"></div><div class="shell-more-lang" data-shell-lang-slot></div>' +
      '<a href="/translate">Quick add / translate</a><a href="/browse">Browse & decks</a>' +
      '<a href="/feedback">Feedback</a><a href="/settings">Settings</a>' +
      (data.me && data.me.is_admin ? '<a href="/admin/dashboard">Admin dashboard</a>' : '') +
      '<button type="button" class="danger" data-shell-logout>Sign out</button></div>';
    button.onclick = () => overlay.classList.add('open');
    overlay.addEventListener('click', event => { if (event.target === overlay) overlay.classList.remove('open'); });
    overlay.querySelector('[data-shell-logout]').onclick = () => window.doLogout();
    document.addEventListener('keydown', event => { if (event.key === 'Escape') overlay.classList.remove('open'); });
    const tutorTop = document.querySelector('.tutor-top');
    if (tutorTop) {
      button.classList.add('in-toolbar');
      tutorTop.appendChild(button);
    } else document.body.appendChild(button);
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
