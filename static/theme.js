// Theme runtime — the inline pre-paint snippet in each page's <head> has
// already set data-theme before first paint; this keeps it in sync afterwards:
//  • re-resolves 'auto' when the OS scheme changes live,
//  • keeps the browser-chrome <meta name="theme-color"> matching --bg,
//  • exposes setThemePref()/getThemePref() for the Settings page control.
(function () {
  const META_COLORS = { light: '#f5f4ef', dark: '#14181b' };

  function resolve(pref) {
    return (!pref || pref === 'auto')
      ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
      : pref;
  }

  function apply(pref) {
    const t = resolve(pref);
    document.documentElement.dataset.theme = t;
    let meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement('meta');
      meta.name = 'theme-color';
      document.head.appendChild(meta);
    }
    meta.content = META_COLORS[t];
  }

  window.getThemePref = function () {
    return localStorage.getItem('theme') || 'auto';
  };
  window.setThemePref = function (pref) {
    if (pref === 'auto') localStorage.removeItem('theme');
    else localStorage.setItem('theme', pref);
    apply(pref);
  };

  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
    if (window.getThemePref() === 'auto') apply('auto');
  });

  apply(window.getThemePref());
})();
