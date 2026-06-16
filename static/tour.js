(function () {
  var TOUR_VERSION = 2;
  var STEPS = [
    { icon: '✏️', title: 'Type any word to add it',
      body: 'Translate an English word or phrase and it becomes a flashcard in your deck — with audio, romanization, and AI notes included.', v: 1 },
    { icon: '🃏', title: 'Study your cards daily',
      body: 'The Flashcards tab shows you cards due today. Each card has three faces: recognition, production, and pronunciation — spaced repetition handles the scheduling.', v: 1 },
    { icon: '📖', title: 'Read real stories',
      body: 'The Reader generates texts in your language — from a prompt, an uploaded image, or completely at random. Tap any word to look it up or add it to your deck.', v: 1 },
    { icon: '🎓', title: 'AI-guided lessons',
      body: 'The Learn tab builds a personalized course that adapts to what you know. Each lesson teaches grammar or vocabulary with interactive drills and earns you XP.', v: 2 },
    { icon: '💬', title: 'Chat with a tutor',
      body: 'The Tutor is an AI conversation partner. It replies in your target language, corrects your mistakes, and suggests new words you can add to your deck in one tap.', v: 2 },
    { icon: '✉️', title: 'Message other learners',
      body: 'The Messages tab lets you chat with friends. Every message gets romanization, word-by-word translations, and corrections — like having a tutor in every conversation.', v: 2 },
    { icon: '⚠️', title: 'Report bugs & ideas',
      body: 'Found a bug or have a feature request? Use the Feedback page to submit a report — you can attach a screenshot and track the status of your submissions.', v: 2 },
  ];

  var steps, stepIdx;

  function render() {
    var s = steps[stepIdx];
    var el = document.getElementById('tour-step-num');
    var prefix = (el.dataset.prefix || '');
    el.textContent = prefix + 'Step ' + (stepIdx + 1) + ' of ' + steps.length;
    document.getElementById('tour-icon').textContent = s.icon;
    document.getElementById('tour-title').textContent = s.title;
    document.getElementById('tour-body').textContent = s.body;
    var btn = document.getElementById('tour-next-btn');
    btn.textContent = stepIdx < steps.length - 1 ? 'Next →' : 'Got it';
    var dots = document.getElementById('tour-dots');
    dots.innerHTML = '';
    steps.forEach(function (_, i) {
      var d = document.createElement('div');
      d.className = 'tour-dot' + (i === stepIdx ? ' active' : '');
      dots.appendChild(d);
    });
  }

  window._tourNext = function () {
    if (stepIdx < steps.length - 1) { stepIdx++; render(); }
    else { window._tourDismiss(); }
  };

  window._tourDismiss = function () {
    document.getElementById('tour-overlay').style.display = 'none';
    try {
      fetch('/api/tour-seen', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: TOUR_VERSION }),
      });
    } catch (_) {}
  };

  function show(seenVersion) {
    var sv = parseInt(seenVersion, 10) || 0;
    if (sv >= TOUR_VERSION) return;
    var isUpdate = sv > 0;
    if (isUpdate) {
      steps = STEPS.filter(function (s) { return s.v > sv; });
    } else {
      steps = STEPS;
    }
    if (!steps.length) return;
    stepIdx = 0;
    var heading = document.getElementById('tour-step-num');
    if (isUpdate && heading) heading.dataset.prefix = "What's new — ";
    render();
    document.getElementById('tour-overlay').style.display = 'flex';
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var ov = document.getElementById('tour-overlay');
      if (ov && ov.style.display !== 'none') { window._tourDismiss(); }
    }
  });

  fetch('/api/settings').then(function (r) { return r.json(); }).then(function (s) {
    show(s.tour_seen);
  }).catch(function () {});
})();
