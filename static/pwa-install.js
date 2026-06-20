(function () {
  if (localStorage.getItem('pwa_dismissed')) return;
  // Already running as installed PWA
  if (window.matchMedia('(display-mode: standalone)').matches) return;
  if (window.navigator.standalone) return; // iOS standalone
  // Only show on mobile-sized screens
  if (window.innerWidth > 768) return;

  var deferredPrompt = null;
  var isIOS = /iP(hone|ad|od)/.test(navigator.userAgent);
  var isAndroid = /Android/.test(navigator.userAgent);
  if (!isIOS && !isAndroid) return;

  // Android: capture the install prompt so we can trigger it from our banner
  if (!isIOS) {
    window.addEventListener('beforeinstallprompt', function (e) {
      e.preventDefault();
      deferredPrompt = e;
    });
  }

  // Delay showing the banner so it doesn't compete with page load
  setTimeout(function () {
    var banner = document.getElementById('pwa-install-banner');
    if (!banner) return;
    banner.style.display = '';
  }, 2000);

  window._pwaDismiss = function () {
    localStorage.setItem('pwa_dismissed', '1');
    var b = document.getElementById('pwa-install-banner');
    if (b) b.style.display = 'none';
  };

  window._pwaInstall = function () {
    if (deferredPrompt) {
      deferredPrompt.prompt();
      deferredPrompt.userChoice.then(function () { deferredPrompt = null; });
      window._pwaDismiss();
    }
  };
})();
