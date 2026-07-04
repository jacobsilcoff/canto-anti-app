// Shared mockup runtime: resolve theme (same convention as the app — data-theme
// on <html>), accept a {theme} postMessage from the index page's global toggle,
// and stamp the tab bar's active state from data-tab on <body>.
(function () {
  var t = 'auto';
  try { t = localStorage.getItem('mock-theme') || 'auto'; } catch (e) {}
  if (t === 'auto') t = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = t;
  addEventListener('message', function (e) {
    if (e.data && e.data.mockTheme) document.documentElement.dataset.theme = e.data.mockTheme;
  });
})();

const TABS = [
  { id: 'home',  label: 'Home',  href: 'home.html',
    d: 'M12 3l9 8h-3v9h-5v-6h-2v6H6v-9H3z' },
  { id: 'learn', label: 'Learn', href: 'learn.html',
    d: 'M4 5h13a3 3 0 010 6H8a3 3 0 000 6h12M20 17l-2-2m2 2l-2 2' },
  { id: 'cards', label: 'Cards', href: 'cards.html',
    d: 'M5 8h11a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2v-8a2 2 0 012-2zm3-4h11a2 2 0 012 2v9' },
  { id: 'tutor', label: 'Tutor', href: 'tutor.html',
    d: 'M4 5h16a1 1 0 011 1v10a1 1 0 01-1 1H9l-5 4V6a1 1 0 011-1z' },
  { id: 'read',  label: 'Reader', href: 'reader.html',
    d: 'M12 6c-1.6-1.4-3.8-2-6-2-1 0-2 .12-3 .4v13.2C4 17.3 5 17.2 6 17.2c2.2 0 4.4.6 6 2 1.6-1.4 3.8-2 6-2 1 0 2 .1 3 .4V4.4c-1-.28-2-.4-3-.4-2.2 0-4.4.6-6 2zm0 0v13' },
];

function renderTabbar() {
  var active = document.body.dataset.tab;
  if (!active) return;   // desktop mockup uses the left rail instead
  var el = document.createElement('nav');
  el.className = 'tabbar';
  el.setAttribute('aria-label', 'Main navigation');
  el.innerHTML = TABS.map(function (t) {
    return '<a class="tab' + (t.id === active ? ' active' : '') + '" href="' + t.href + '">' +
      '<span class="tab-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"><path d="' + t.d + '"/></svg></span>' +
      t.label + '</a>';
  }).join('');
  document.body.appendChild(el);
}
document.addEventListener('DOMContentLoaded', renderTabbar);
