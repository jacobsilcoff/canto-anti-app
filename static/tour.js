(function () {
  var TOUR_VERSION = 31;
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
    { icon: '🏠', title: 'Flashcards are now home',
      body: 'Your deck is the home page now. Tap the + button to translate words or browse community decks — no page reload. Lessons have bonus mini-games, and the flashcard tutor is smarter.', v: 13 },
    { icon: '🚀', title: 'Top 100 words, ready to add',
      body: 'Every language now has a ready-made "Top 100 Words" deck. New learners can add it in one tap at signup, or find it any time in Browse → Community to jump-start a real deck.', v: 14 },
    { icon: '⭐', title: 'Rate the decks you study',
      body: 'Studying an imported community deck? You can now rate it right from the Flashcards page — and we\'ll nudge you once you\'ve worked through a good chunk, so other learners know which decks are worth it.', v: 14 },
    { icon: '🎯', title: 'Smoother lesson drills',
      body: 'The Check button and feedback now stay pinned to the bottom of the screen in lessons — no more scrolling past a long word bank. On desktop, press 1–9 to pick an answer and Enter to check.', v: 15 },
    { icon: '🧭', title: 'A fresh look + new navigation',
      body: 'The app has a new design and a single bottom tab bar: Home, Learn, Cards, Chat, and Reader are always one tap away. Home shows your daily goal, your streak, and the \u22ef menu (Browse, Feedback, Settings, Sign out). Your AI tutor now lives at the top of Chat alongside your friends.', v: 16 },
    { icon: '\ud83e\ude9c', title: 'Lessons come in bite-sized steps',
      body: 'New lessons are split into short steps \u2014 a warm-up on things you know, teach-a-little-then-practice cards with instant quick checks, a mix-it-up round, and a skippable \u2728 AI Speak finale. The step bar at the top shows exactly where you are.', v: 17 },
    { icon: '\ud83c\udf81', title: 'Daily quests & checkpoints',
      body: 'Three fresh quests every day \u2014 finish all three to open a bonus-XP chest. Finished units on the Learn path now end in a \ud83d\udee1 checkpoint quiz that seals the unit, and you can set your own daily XP goal in Settings.', v: 18 },
    { icon: '\ud83c\udfc6', title: 'Compete, shape, and skip',
      body: 'Race friends on a weekly XP league right on the Learn page. Tapping a lesson opens an overview where you can pick its length or \ud83c\udf93 test out if you already know it. Set a course focus (grammar, vocab, or conversation) in Settings.', v: 19 },
    { icon: '\u23ee', title: 'Pause, rewind & fine-tune lessons',
      body: 'Need to stop mid-lesson? Just quit \u2014 your progress is saved and the overview offers Resume. Tap \u2190 to step back to a previous card, and switch \u2728 AI Speak practice on or off (per-lesson or in Settings) to trade speed for depth.', v: 20 },
    { icon: '\ud83d\udcf7', title: 'Add a profile picture',
      body: 'Set a profile photo in Settings \u2192 Account \u2014 it\u2019s cropped to a circle and shows up next to your chats. Add your name there too and Home will greet you by your first name.', v: 21 },
    { icon: '\ud83d\udcd5', title: 'Turn your textbooks into lessons',
      body: 'On the Learn page, tap \ud83d\udcd5 My books to upload textbook or grammar-book PDFs. The app finds each book\u2019s chapters (you can check and fix the page ranges), and \u26a1 Generate turns any chapter into interactive lessons with drills \u2014 come back for more chapters whenever you\u2019re ready. Course chapters also show their length (\u201cLesson 2 of ~4\u201d) and close into units on their own.', v: 22 },
    { icon: '\u26a1', title: 'Practice, lightning & streak freezes',
      body: 'Tap \ud83c\udfaf Practice on the Learn page for a \u26a1 lightning round (a 60-second remix of your drills) or a review of the concepts you find hardest. Finish lessons to earn \ud83d\udee1 streak freezes that save your streak if you miss a day \u2014 and give a lesson a \ud83d\udc4d/\ud83d\udc4e afterwards to shape what comes next.', v: 22 },
    { icon: '\ud83d\udc53', title: 'Review on Even Realities glasses',
      body: 'Connect your Even Realities G2 glasses to review flashcards hands-free \u2014 tap to reveal, tap again for \u201cgot it\u201d or double-tap for \u201cmissed it.\u201d XP, streak, and quests all stay in sync. Generate an API token in Settings \u2192 Even glasses.', v: 23 },
    { icon: '\u2728', title: 'AI can re-read messy PDFs',
      body: 'If a textbook\u2019s extracted text comes out garbled, out of order, or shows only romanization with no native script, tap \u201c\u2728 Re-read these pages with AI\u201d on the review screen. It reads the page images and rewrites them cleanly \u2014 recovering the native characters \u2014 so lessons are built from faithful source.', v: 24 },
    { icon: '\ud83d\udcc7', title: 'Turn a chapter into a flashcard deck',
      body: 'Just want the words? On the textbook review screen tap \ud83d\udcc7 Build vocab deck to pull every word from those pages into an editable list \u2014 uncheck any you don\u2019t want, then add them to your deck and study straight away. It skips words you already have, and it\u2019s quicker than building full lessons.', v: 25 },
    { icon: '\ud83d\udcd5', title: 'A home for your textbooks',
      body: 'Your books now live on their own \u201cTextbooks\u201d page (in the More menu). Upload a PDF and read it page by page \u2014 it remembers where you left off and lets you bookmark pages. Tap \u2630 Contents (or a book\u2019s \u201cChapters\u201d button) to jump straight to any chapter, fix the chapter page ranges by hand, or re-detect them with AI. When a unit begins or ends partway down a page, tap \u2702 in the reader and mark the break right on the page: tap where the next unit starts, drag the dashed line to fine-tune (it snaps to the actual text lines), or \u2715 to remove it. The same break is marked in the extracted text so you can double-check it landed in the right spot, and lessons for each unit then skip the neighbouring unit\u2019s text. From any chapter, tap \ud83d\udcc7 Build vocab deck or \ud83d\udcd8 Turn into lessons. Textbook units now sit in their own \u201cFrom your textbooks\u201d section on the Learn page, separate from your AI course, and you generate their lessons one tap at a time.', v: 26 },
    { icon: '\ud83d\udcd8', title: 'Textbook units, end to end',
      body: 'Every unit break now shows right on the page \u2014 including plain page breaks, marked at the top and bottom of the pages they divide. Drag any \u2702 mark up or down to move it (onto the page, or off to a clean break), or tap it to merge two units into one (renamed with AI). On the Learn page a chapter shows all its lessons before they exist: \u26a1 Build all makes them in one go, delete or regenerate as you like, and finishing one hands you to the next while the following lesson builds in the background.', v: 27 },
    { icon: '\ud83d\udd25', title: 'Your streak now runs on your clock',
      body: 'Your day used to end at midnight UTC \u2014 5pm in California, noon in New Zealand \u2014 so two evenings of study could count as one day and break a streak you\u2019d actually kept. Your \ud83d\udd25 streak, XP ring, daily quests and new-card limit now roll over at midnight where you are (Settings shows the time zone we detected). Flashcards reviewed offline count for the day you answered them, not the day they sync. And \ud83d\udee1 streak freezes are more forgiving: they apply the moment you study rather than depending on what you opened first, two shields can cover two missed days, and they still work if you come back later in the week.', v: 28 },
    { icon: '📚', title: 'Fewer repeated lessons, every chapter in one place',
      body: 'Your AI course used to lose track of words it taught inside a grammar lesson, so the same material could come back later under a new name. It now sees every word and every lesson it has already made — including ones from your textbooks — and a plan that repeats one gets sent back before it’s written. On the Learn page, 📕 From your textbooks now lists every chapter of every book, not just the ones you’ve built: tap “＋ Build lessons from this chapter” to start a new chapter right there, no trip to the Textbooks page.', v: 29 },
    { icon: '\u270d\ufe0f', title: 'Write it yourself — and a progress bar that means something',
      body: 'Lessons were nearly all multiple choice, so you could finish one without ever writing the language. Now every lesson (including the ones built from your textbooks) asks you to type real sentences in your target language, and the grading is deliberately generous: any natural translation counts, not just the one the lesson had in mind. Word it differently and it tells you it works. Miss the mark and it shows you the fix and why. The step bar at the top is also honest now — steps are sized by how much is in them and teach cards count as progress, so it moves at a steady rate instead of stalling and then leaping. And on iPhone, app sounds now play over your music or podcast instead of stopping it \u2014 there\u2019s a toggle in Settings if you\u2019d rather they didn\u2019t.', v: 30 },
    { icon: '\ud83c\udfa4', title: 'Say it out loud',
      body: 'Lessons now sometimes ask you to SPEAK. Tap the mic, say the line, and your phone checks it \u2014 generously, so a near-miss still counts (it listens for how you said it, not which characters it guessed). For a full round of it, open \ud83c\udfaf Practice \u2192 \ud83c\udfa4 Speaking practice, built from the words and sentences your lessons have already taught, or tap \ud83c\udfa4 Speaking on any finished lesson. Turn it off in Settings if you\u2019d rather not. And when a sound can\u2019t be loaded, the \ud83d\udd0a button now shows as unavailable instead of quietly doing nothing \u2014 listening questions we can\u2019t play are skipped rather than asked in silence.', v: 31 },
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

  var seenMarked = false;

  function markSeen() {
    if (seenMarked) return;
    seenMarked = true;
    try {
      fetch('/api/tour-seen', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: TOUR_VERSION }),
      });
    } catch (_) {}
    // The app shell answers /api/settings from a sessionStorage snapshot that
    // OUTLIVES the page, so persisting tour_seen server-side is not enough:
    // without patching the cached copy, the next page reads the old version and
    // replays the same "What's new" steps — on every navigation, for the whole
    // tab session. (That is the bug where the update notice kept reappearing.)
    try {
      if (window.CantoShell && window.CantoShell.patch) {
        window.CantoShell.patch('settings', { tour_seen: String(TOUR_VERSION) });
      }
    } catch (_) {}
  }

  window._tourDismiss = function () {
    document.getElementById('tour-overlay').style.display = 'none';
    markSeen();
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
    // Record it as seen the moment it's shown, not only on dismiss — otherwise
    // navigating away (home → textbooks) before tapping ✕/Got it re-triggers the
    // same steps on the next page. The content is identical everywhere, so once
    // it's on screen the version is "seen".
    markSeen();
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
