(function () {
  var TOUR_VERSION = 12;
  var STEPS = [
    { icon: '✏️', title: 'Type any word to add it',
      body: 'Translate an English word or phrase and it becomes a flashcard in your deck — with audio, romanization, and AI notes included.', v: 1 },
    { icon: '🃏', title: 'Study your cards daily',
      body: 'The Flashcards tab shows you cards due today. Each card has three faces: recognition, production, and pronunciation — spaced repetition handles the scheduling.', v: 1 },
    { icon: '📖', title: 'Read & share stories',
      body: 'The Reader generates texts in your language. Publish your stories for the community to read and rate, or browse stories shared by others.', v: 3 },
    { icon: '🎓', title: 'AI-guided lessons',
      body: 'The Learn tab builds a personalized course that adapts to what you know. Each lesson teaches grammar or vocabulary with interactive drills and earns you XP.', v: 2 },
    { icon: '💬', title: 'Chat with a tutor',
      body: 'The Tutor is an AI conversation partner. It replies in your target language, corrects your mistakes, and suggests new words you can add to your deck in one tap.', v: 2 },
    { icon: '✉️', title: 'Message other learners',
      body: 'The Messages tab lets you chat with friends. Every message gets romanization, word-by-word translations, and corrections — like having a tutor in every conversation.', v: 2 },
    { icon: '💡', title: 'Type English to learn vocab',
      body: 'In friend chats, type in English and your message auto-translates. A breakdown panel shows the original, explains the translation, and lists words you can add to your deck. On photos, tap the lightbulb for suggested phrases.', v: 4 },
    { icon: '🔍', title: 'Browse shared decks',
      body: 'The Browse page lets you create card decks and share them with friends or the community. Import decks from other learners to jumpstart your vocabulary.', v: 3 },
    { icon: '🏷️', title: 'Manage your labels',
      body: 'The Labels tab in Browse lets you rename, merge, and delete labels. It auto-suggests merges for similar labels (like "food & drinks" and "foods and drinks") so your deck stays tidy.', v: 5 },
    { icon: '⚠️', title: 'Report bugs & ideas',
      body: 'Found a bug or have a feature request? Use the Feedback page to submit a report — you can attach a screenshot and track the status of your submissions.', v: 2 },
    { icon: '✨', title: 'Populate a label',
      body: 'Each label in Browse has a Populate button that suggests vocab for that category — tag words already in your deck or add brand-new ones in one tap.', v: 6 },
    { icon: '🔗', title: 'Read real articles & PDFs',
      body: 'In the Reader, paste a news article URL or upload a PDF to turn it into a reading. If it\'s already in your target language, tick the box to import it as-is; otherwise it\'s translated to your level.', v: 7 },
    { icon: '📊', title: 'Simplify imports to your level',
      body: 'URL and PDF imports now have a Level selector. Pick a CEFR level (A1-C2) to simplify the text to your level, or keep "Keep original" for the full authentic text.', v: 8 },
    { icon: '🌍', title: '8 new languages',
      body: 'Now supporting Japanese, Bengali, Urdu, Arabic, Swahili, Russian, Vietnamese, and Farsi — with native fonts, audio, and romanization. Change your language in Settings.', v: 9 },
    { icon: '📖', title: 'Script reading tracks',
      body: 'New optional reading foundations for Japanese (hiragana & katakana), Russian (Cyrillic), Bengali, Arabic, Urdu, and Farsi. Learn to read the script before diving into vocabulary — available in the Learn tab.', v: 10 },
    { icon: '🌐', title: '10 more languages',
      body: 'Now supporting Turkish, Dutch, Polish, Swedish, Norwegian, Romanian, Ukrainian, Greek, Thai, and Hebrew — with native fonts, audio, romanization, and optional script reading tracks for non-Latin scripts.', v: 11 },
    { icon: '🎮', title: 'Mini-games in reading lessons',
      body: 'Script reading lessons now end with a bonus mini-game: speed rounds with a countdown timer, audio blitz challenges, or memory match. Build combos and earn XP while mastering characters!', v: 12 },
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
