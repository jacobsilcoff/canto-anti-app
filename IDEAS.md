# Feature Ideas & Backlog

## ✅ Shipped

- **Deck ratings** — Community decks can be rated 1–5 stars per user (migration 031, `deck_ratings` table). Interactive star rating in deck detail view, average rating shown on deck cards in the listing. Community deck sort options: newest (default), top rated, most imported. Language filter on community decks (default to user's target language).
- **Fix: received messages shown in English instead of recipient's target language** — Messages are now always displayed in the viewer's target language. On-demand retranslation at retrieval time when the viewer's language isn't in the stored translations dict (e.g. recipient changed their study language). The result is persisted back for instant future loads. The Aa toggle now correctly shows: original text + English when original was not English; just English when original was English (no redundant duplication).
- **Enhanced browse card search** — The "My Cards" tab on `/browse` now has a full filter toolbar: language dropdown (populated from the user's actual card languages), label filter, CEFR level (A1–C2), status (active/suspended/flagged), sort (newest/oldest/priority/alphabetical/CEFR), and a clear-filters button. Text search also matches card notes.
- **Messages: English auto-translate with vocab breakdown** — When a user types English in a friend chat, the message bubble displays the auto-translated target-language text (already worked) and now also shows a collapsible "Translation" panel below with: (1) the original English text, (2) an explanation of how the translation works (grammar/word order), (3) a list of key vocabulary words with romanization (oracle) and one-click "Add to deck" buttons (pre-filtered against the user's existing deck). `translate_message` was enhanced to return `explanation` and `vocab` in the same LLM call (no extra cost). Photo info button changed from an `i` circle to a lightbulb icon with "Phrases" label and a subtle glow animation for discoverability.
- **Phrase gating in SRS** — Phrase cards (multi-word `target_text`) are now deferred in `get_study_session` until all constituent single-word cards have their primary face graduated. Constituent words are detected by `tokenizer.phrase_words`; graduated status checked via a per-language query of cards with `learning_step IS NULL`. Over-fetches 3× and post-filters in Python. Ensures learners study individual words before the phrase that contains them.
- **Sharing/browse polish round 2** — (1) **Auto-labels everywhere**: cards added via tutor chips / lesson "add to deck" / atomize previously had no labels; now any card created without labels gets 2–4 category labels generated in the background (`translation.suggest_labels` → `db.add_labels_by_name`), matching the translate flow. (2) **Single-language decks**: deck creation is restricted to one language (server-validated + the picker filters by language and clears selection on switch) — simpler than the brief mixed-language support. (3) **Study a deck directly**: the `/browse` deck detail now has a **Study this deck** button for both the creator (tagged at creation) and importers (`get_shared_deck` returns `import_label_id` for either) → `/cards?label_id=…&study=1`. (4) **Full card browser on `/browse`**: the "My Cards" tab is now the complete editor ported from the flashcards page (inline edit with `LabelPicker` + canonical search, delete, priority/flag/suspend/reset, CEFR badges, same styling) instead of a read-only list.
- **Shared decks + community story sharing (polish round)** — Decks and stories can be shared with friends/public and browsed/imported at `/browse` (+ reader Community tab). Fixes & improvements this round: (1) **mixed-language decks** — `shared_deck_items` carries a per-item `target_lang` (migration 030) so a deck holding cards from several languages imports each card back into its own language; the deck's top-level `target_lang` is the dominant one (shown as a flag badge). (2) **creator can study their own deck** — creating a deck now tags the creator's selected cards with a `📦 {deck_name}` label (`db.label_cards_for_deck`), so it appears in the flashcards label filter immediately (previously only importers got the label). (3) **community lists no longer hard-filter by the viewer's language** — a friend learning a different language now actually sees what was shared (this silently hid everything before); language is shown as a badge, with an optional `lang` query filter. (4) **story sharing moved to the listing** — each saved story in the reader list has a 🔒/👥/🌐 visibility dropdown (`shareSavedText`); the in-reader publish control was removed (reader view shows only ratings for community stories).
- **Fix: can't scroll within a long tutor drill** — Once a drill ran several turns, its bottom became unreachable and the thread wouldn't scroll to it. Cause: `.drill-panel` has `overflow: hidden`, which (per flexbox) gives a flex item an automatic `min-height: 0`, so as a flex child of the scrolling `.thread` it COMPRESSED to fit and clipped its own content instead of staying full-height and letting the thread scroll. Confirmed by measurement: a 2601px drill collapsed to 102px with the thread non-scrollable. Fix: `.drill-panel { flex-shrink: 0 }` so the panel keeps its full height and the thread scrolls normally. Verified on a mobile viewport (full height preserved, thread scrollable, last turn reachable).
- **Fix: tutor drill started as an empty box / collapsed to a thin line** — Tapping 🎯 Drill sometimes showed a drill panel with no content, and "End drill" then collapsed it to just its header (a thin line). Root cause: when the drill opener's `reply` came back blank (a transient empty/safety-stopped model response), nothing guarded it — the empty bubble rendered AND was persisted, so it stayed broken on reload. Fix (defense-in-depth): `tutor.start_drill` retries the opener once on a blank reply (small + large/regen paths); the `/drill` route refuses to persist a blank opener (clean 502 → retry); `startDrill` treats a blank reply as a failure instead of creating an empty panel; and `appendMessage` renders a clear "⚠️ This didn't load — please try again." placeholder for any blank reply (covers already-persisted broken drills on reload). Verified live (happy-path drill renders; injected blank shows the placeholder, not a void).
- **Fix: "generate N lessons" stalled after 1 on a premium model** — Batch generation (the frontend fires `?count=1` N times) reliably failed after the first lesson when `lesson_model` was Pro/Claude. Cause: the Phase-4 rearchitecture added a second LLM call per lesson (the planner), and running BOTH planner + author on Pro made each lesson ~45s (planner alone ~15s on Pro) and doubled 503-overload exposure, so the 2nd request timed out / 503'd. Fix: **the planner now always runs on the fast reader model** (`access.model_reader`); only the quality-critical author uses `lesson_model`. Pro lessons drop to ~30s (one slow call, like the old per-lesson cost). Verified by timing (planner 14–17s→1.3s on the split). Workaround for older state: set Settings → lesson model back to Flash-Lite.
- **Skill-tree path polish — angled connectors + crown level-up (43 Phase 5, part 2 follow-ups)** — The trail is now drawn as an SVG polyline through the node centres (`drawPathConnectors`, redrawn on render/show/resize) so it bends to follow the weaving nodes instead of a straight central spine. Crown level-ups are now celebrated: `db.complete_lesson` returns `leveled_up`, and the results screen shows "Crown level up → N" with a bounce when a replay raises the crown (vs. a flat "Crown N" once maxed). Verified live.
- **Learn-page skill-tree path + crown levels (43 Phase 5, part 2)** — Replaced the flat collapsible unit/lesson list with a Duolingo-style **vertical winding node path** for the AI vocab course (`static/learn.html` `renderCourse` + `_pathRow`/`_unitBanner`, `.lpath` CSS): circular lesson nodes weave over a central spine, unit titles render as banners along the path, the next available lesson gets a pulsing **START** flag, and done nodes show 👑 **crown pips**. Crowns persist: new `course_lessons.crown_level` (0–3, migration 018) bumps by 1 on every completion (`db.complete_lesson` now returns `(found, first, crown)`, capped `db.CROWN_MAX`); replays earn crowns but **not** XP (XP stays first-completion only). The `/complete` route surfaces the crown as a results-screen badge. The foundations reading track keeps its compact card list; only the main course uses the path. Verified live (rendered done+3-crowns / available+START+pulse / locked nodes + unit banners, no console errors) + tests green. **Phase 5 now complete** (combo/XP/ring in part 1).
- **Learn-page gamification v1 — combo meter + XP + daily-goal ring (43 Phase 5, part 1)** — Duolingo-style juice in the lesson player (`static/learn.html`), all testable on Gemini. (1) **Combo meter**: consecutive first-pass correct answers build an amber "🔥 ×N" chip (pulses on each hit) + a floating "+N XP"; XP/correct = base 10 + an escalating, capped combo bonus; a first-pass miss resets the combo. (2) **XP results tally**: animated count-up on the results screen with a `★ Perfect lesson +25` bonus (all first-pass correct) and a `🔥 Best combo ×N` badge + double-confetti on a flawless run. (3) **Daily-goal XP ring**: an SVG ring in the course header showing `points_today`/`daily_goal`, green when met. XP persists to `points_ledger` once per lesson — `db.complete_lesson` now returns `(found, first)` so replays don't re-award; the `/complete` route clamps client XP ≤300; `/api/streak` gained `points_today` + `daily_goal` (new `db.get_points_today`, local-time). Verified live (combo chip ×4, 121-XP perfect result, 35/50 ring, no console errors) + 201 tests green. **Still TODO (Phase 5 part 2):** skill-tree path with per-skill crown levels.
- **Learning Path redesign → just-in-time planner + broad lessons + pluggable model (43 Phase 4)** — Replaced the frozen unit-plan-with-cursor with a per-lesson **planner** (`learning.plan_next_lesson`) that picks the single best next lesson from live state (registry, in-progress chapter, weak skills, known/weak/**recently-added** deck words via new `db.get_recent_cards` — the cross-app signal so tutor/flashcard activity steers the course, CEFR spread, learner profile). It returns a `lesson_spec` with `scope`/`focus`/`target_items`, so one lesson can now cover a **whole grammar family** (e.g. all regular -er verbs + the être paradigm in one go) with irregular cases scheduled as their own `focus:"exceptions"` lesson — fixing the "too slow / too granular" problem. Chapters are emergent (`courses.active_plan` = `{title,objective,summary}`, opened/closed lesson-by-lesson via `close_unit`, so the roadmap UI is unchanged). `author_lesson` keeps its spirit (teach + drills in one call, deterministic assembler + oracles intact) but now teaches a skill spanning many `target_items`. Both calls route through a new provider-pluggable `llm.py` (`call(...)` → Anthropic SDK for `claude-*`, else Gemini), exposed as an **admin-only `lesson_model` A/B knob** (Gemini Flash-Lite/Flash/Pro + Claude Sonnet 4.6 / Opus 4.8; Claude on a server-side `ANTHROPIC_API_KEY`). Deleted `generate_unit_plan` + `_next_batch`. Verified: 201 tests green (incl. new planner-normalization, `_concepts_from_spec`, and end-to-end chapter-open/close + registration tests) + a live Gemini run producing a broad être/pronouns lesson. **Deferred to Phase 5 (gamification):** skill-tree path UI, in-lesson combo meter, XP/daily-goal ring.
- **Lesson construction-drill UX overhaul (slow / unclear / mixed-language)** — Three reported problems, one main cause: the drill payload carried a target-language `reply`/`reply_en`/`gloss` lead-in that the player rendered *next to* the unrelated English "Translate: …" phrase, so the learner saw two texts that "don't say the same thing" — and generating all of it made every turn slow. Fix: stripped the lead-in entirely (payload is now just `{feedback, phrase, done}`), moved the drill onto the fast `flash-lite` model (it's a simple pose/judge task and formative), and rebuilt feedback to show the learner's struck-through answer (**You wrote**) vs the natural **Answer** (+ romanization) + the rule note. Opener latency dropped to ~0.5s (was a multi-second flash call); the task is now clean English-only. Verified end-to-end (timed API turns + rendered feedback).
- **Contextual tutor pop-over on the flashcard page** — A 💬 button on the study card header opens a bottom-sheet chat that knows the card you're looking at (target/source/romanization/notes + SRS progress). One metered, **ephemeral** call per question (`POST /api/tutor/ask` → `tutor.ask_about_card`); short follow-ups are kept client-side and passed back in (nothing stored). The explanation comes back in **English** with examples in target script + romanization + glosses (study help, not conversation practice), and any words worth saving appear as ＋Add-to-deck chips (oracle romanization, deduped against the deck). Preset question chips ("Why this word here?", "Use it in a sentence", …) for one-tap asks. Verified end-to-end in the player (個 vs 隻 explained in English with examples; chips add to the deck). The reader-page half of the idea (knowing the current sentence/story) is still backlog #45.
- **Inline LLM-graded construction drill in lessons** — The tutor's construction drill is now also a lesson exercise type (`construction_drill`): `assemble_lesson` auto-adds one per new grammar concept, and the lesson player renders a self-managed 4-turn "translate this phrase" widget graded by `POST /api/lesson/drill` (`tutor.run_lesson_drill`) — built from the learner's known words, oracle romanization, 1 metered call/turn, formative (doesn't skew the score). The one intentional exception to lesson determinism. Verified end-to-end in the player: start → judged turns with rule notes → 4/4 → Continue advances the lesson.
- **Fix per-card embeddings / label suggest-cards** — `translation.get_embedding` used the dead `text-embedding-004` (404, silently returned None), so `cards.embedding` was never populated and `GET /api/labels/suggest-cards` always returned empty. It now delegates to `embeddings.embed` (`gemini-embedding-001`, 768-dim) — one embedding code path. Because every pre-existing card had a NULL embedding, `suggest-cards` lazily backfills a bounded batch (≤300/call) via new `main._backfill_card_embeddings` → `db.get_cards_missing_embedding`, so an established deck converges over a few calls. Verified end-to-end: "food and drink" surfaces drink/飲品, snack/小食, restaurant/餐廳 by cosine.
- **Tutor — verify-then-snap drills (embeddings as a true fallback)** — Large-deck construction drills now run a cheap opener first (sample + CEFR profile + count, no embeddings) and ask it for the answer's `expected_words`; those are checked by cheap string membership against the full known set. Only when the opener leans on words the learner doesn't know do we lazily embed the deck, snap the misses to known substitutes, and regenerate the opener avoiding them. Result: 1 LLM call / 0 embeddings on success (the common case), 2 calls + 1 embed on a miss. Verified with stubs (pass→1 call/0 embed; fail→2 calls/1 embed + AVOID block in the regen).
- **Tutor — CEFR vocab profile for large-deck drills** — Large-deck drill prompts now include a compact CEFR spread of the learner's known words (e.g. "A1:40, A2:22, B1:9") so the model can pitch level-appropriate vocab from just a sample. Reuses the existing `cards.cefr_level` (assigned at translation time); new `db.get_known_cefr_distribution` (known-word subset, per language) + `cefr.py` lazy backfill (bounded ≤60/call, persisted) for cards added via lesson/tutor/starter paths that skip CEFR. (Also found: `translation.get_embedding`/per-card embeddings use the dead `text-embedding-004` model — silently no-ops label-suggestions; flagged for separate fix.)
- **Tutor — tier construction-drill vocab by deck size** — Embeddings are now a *large-deck fallback*, not an always-on cost. Decks ≤150 known words hand the whole list to the model (one opener call, no embeddings); larger decks embedding-snap a relevant palette + pass only a ~40-word sample + total count so a 2000-word deck never floods the prompt (`db.count_known_words` picks the tier). Open follow-ups: no per-word CEFR data exists (only priority + course level), so large-deck "stats" are just the count for now; a verify-then-snap loop and reusing the drill inside lessons are deferred.
- **Tutor — embedding-anchored construction drills** — Construction drills now snap to the learner's own vocab via embeddings (`gemini-embedding-001`, 768-dim, new `embeddings.py` + shared `embedding_cache` table). `start_drill` asks the model for the construction's content words (call 1), embeds them, and snaps each to the nearest known deck word (cosine ≥ 0.62): matches become the drill's "palette" (the opener builds phrases from them), non-matches are taught as add-to-deck chips. Known-word vectors are cached per (lang, model, word) and shared across users; the plan is best-effort (degrades to the plain opener on any failure). Verified end-to-end: French comparatives drilled with the learner's known nouns/adjectives, and a sparse deck correctly surfaced grand/rapide/lent as new vocab.
- **Tutor — enforce English→target suggestions + construction-focused drills** — Two upgrades: (1) When the learner uses English (or asks "how do you say…?"), the tutor now MUST return the natural target rendering(s) — up to 3 options with register notes — as Add-to-deck `new_items`, exempt from the usual selectivity cap (`MAX_NEW_ITEMS`→4). (2) Corrections now name the underlying **construction/form** (`construction` field, shown as a 📐 grammar pill) with an explanation of the rule, not just the one-off fix; the 🎯 Drill button targets that construction and the drill walks the learner through 3–4 examples built from vocab they already know (deck/known list fed to the model). **Deferred:** the proposed embedding-similarity substitution (snap construction filler words to nearest known deck word) — approximated for now by prompting the model to build from the known-words list; revisit if it drifts.
- **Tutor chat — stop iOS rubber-band revealing space under the composer** — Touch-dragging on the textbox bounced the whole fixed page (iOS document overscroll), exposing empty space below the composer. `overscroll-behavior` doesn't cover document rubber-band on iOS Safari, so added a `{ passive:false }` `touchmove` listener that `preventDefault`s everywhere except real scrollers (`.thread`, `.chats-list`) and the composer when it overflows.
- **Tutor composer — keyboard flash + stuck-tall textarea** — On mobile the composer flashed above the keyboard for a split second on every keystroke (our `scrollTo(0,0)` in the `visualViewport` scroll handler was fighting iOS's caret-reveal auto-scroll) and the textarea sat permanently ~110px tall instead of auto-sizing. Fixes: made the chat `body` `position: fixed` so the page is genuinely unscrollable (no more scroll war — removed the scroll handler) and overrode the global `textarea { min-height: 120px }` with `min-height: 0` on the composer so `scrollHeight`-based auto-grow works (one line ≈ 44px, grows to the 110px cap, shrinks back).
- **Streak now counts lessons + tutor, not just reviews** — `study_activity` (which drives the 🔥 streak) was recorded ONLY on SRS card reviews, so using the tutor/lessons earned ⭐ but silently let the streak lapse ("since I added stars the streak disappeared"). Added `db.record_study_activity`, called on lesson completion and every tutor turn. Also added the header streak/points pill to the Learn page (it was the one page missing `loadStreak`), so ⭐/🔥 now show consistently across all tabs.
- **Lesson inline-example ruby + meaning, and no-resize** — Teach `prose`/`note` blocks now ruby-annotate inline target-script examples (e.g. a Cantonese tones lesson's `詩·史·試·時·市·事`): each character shows its jyutping and a meaning tooltip (stored gloss, else tap-to-translate), without disturbing the surrounding English/markdown (`applyProseRuby` ruby-izes only the target-script runs). Fixed the tokenizer so the middle-dot family (`·・‧•`) splits syllable lists into individual characters, and made yue `romanize_words` romanize each word on its own (the old batched join dropped single-char readings → no jyutping). Reserved ruby line-height in lesson teach text and in tutor chat bubbles so romanization loading in / the あ toggle no longer re-spaces or jumps the text.
- **Fix: Learn page stuck on spinner / no lessons loading** — A comment inside learn.html's `<script>` literally contained the `{{NAV}}` placeholder. `_html()` does a global `.replace("{{NAV}}", …)`, so it substituted the multi-line `<nav>` HTML into that comment; the newlines broke out of the `//` comment and dumped raw HTML into the JS → `SyntaxError: Unexpected token '<'` → the entire learn script failed to execute → page hung on the loading spinner. Introduced in the "Fix hamburger menu" push. Fixed the comment and hardened `_html` to replace only the FIRST `{{NAV}}` (the one in `<header>`) so a stray placeholder can never inject markup into a script again.
- **Tutor chat — ChatGPT-style rebuild** — Mobile rendering overhauled: `html/body` locked to the viewport so the thread is the only scroller and the composer stays pinned; a `visualViewport` handler (resize + scroll + focus) compresses the layout above the mobile keyboard instead of letting the composer slide under it, and the textarea shrinks when the keyboard is up. Per-bubble icon-only tools row (🔊 listen · Aa full-English · あ ruby toggle, reader-style with strike-through · 🎯 Drill). Tapping a word now shows the English gloss ONLY (romanization lives in the ruby — no redundant reveal). **Corrections now sit right under the learner's own message** (not buried in the tutor reply). Teaching/suggested-word sections are collapsible; new-word chips are dismissable (✕). **Drill** is now conditional — the tutor flags a generalizable pattern (if…then…, -er conjugation, …) and only then shows a "🎯 Drill: <skill>" button; tapping it hits a dedicated endpoint that starts the drill with NO visible "drill me" prompt in the chat (isolated call, swappable to a cheaper model later). **Speed:** conversation list returns the newest chat's messages inline (1 round trip), and all ruby for a render comes from one `POST /api/ruby/batch` (was N per-bubble GETs). **Bug fixes:** quickly switching chats no longer interleaves two conversations (generation guard); broken/slow auto-play TTS removed (listen is on-demand). `new_items` made much more selective via the prompt (≤2, empty is normal, no proper nouns / niche words) so it stops suggesting low-value deck adds.
- **Tutor chat polish (mobile + UX overhaul)** — Reply is now ENTIRELY in the target language; the same LLM call also returns `reply_en` (sentence translation) + a word-for-word `gloss`, so the chat gets the reader-style reveal the user asked for: jyutping ruby, per-word English on tap, and a per-bubble "Aa" full-translation toggle — no extra API cost. Prompt rewritten to be an engaging partner (open-ended questions, personality, cultural tidbits; no more bland yes/no). `new_items` no longer re-suggests words the learner just used or already has in their deck (they earn points for those instead). Optional auto-play TTS toggle (default off). Mobile layout fixed (bounded flex column → thread is the only scroller, pinned composer, truncated conversation chips) so old messages no longer get cut off.
- **Lesson teach-text ruby + hybrid translation** — All target-language teach text shows romanization ruby (offline, retroactive on old lessons). English meaning on hover/tap: stored `vocab_glossary` first (free), with a live AI fallback (`.gl-live` → `/api/reader/translate-word`, session-cached) for words the lesson never glossed. AI-lesson teach only — never exercises (answer leak) or the foundations reading track.
- **Hamburger menu fix + button cleanup** — The mobile menu did nothing on the Learn page (`toggleMobileMenu` was never defined there). Fixed. Merged the redundant "Reset lessons" + "New course" into one "↻ Restart course".
- **Lesson-generation timeout fix** — "Generate 5" authored all 5 lessons in one ~150s request on the premium model and tripped the browser/proxy timeout (Safari "Load failed"). Now authors one lesson per request client-side; clearer error classification for overload/rate-limit/malformed-JSON.

- **Tutor chat (⭐ new flagship)** — `/tutor`: free conversation with an AI tutor in the target language (gemini-2.5-flash, one metered call per message, history serialized into a single prompt). The tutor knows what the learner knows — prompt context = SRS deck known words (`db.get_known_words`), course concept registry, weak mastery concepts, and the `learner_profile` setting — replies mostly in the target language calibrated to that, answers MEANING first, and corrects via a structured JSON side-channel rendered as: correction cards (struck-through quote → fixed version + why), teachable **new-word chips with one-click Add-to-deck** (audio + oracle romanization server-side, deduped via `/api/cards/status`), and **light points** (`points_ledger`; awarded only for correctly using known material, clamped 1–3, shown as toasts + a ⭐ total beside the streak on every page). Normalization is strict (filter-then-clip, romanization recomputed offline, malformed JSON degrades to plain text). Conversations persist (`tutor_conversations`/`tutor_messages`, migration 016) with a chip strip + per-bubble TTS + inline-ruby toggle.
- **Dark mode (Auto/Light/Dark)** — Semantic color-token layer in `style.css` (`--primary-soft`, `--good-soft`, `--warn-*`, `--danger*`, `--grammar-*`, `--reader-known/-weak`, `--surface-alt`, `--tooltip-bg`, …) swept over ~100 hardcoded colors across all pages; a single `[data-theme=dark]` block overrides the tokens (low-alpha tints, `color-scheme: dark`). An inline pre-paint `<head>` snippet on every page resolves the stored preference (`auto` → `prefers-color-scheme`) before first paint (no flash); `/static/theme.js` re-resolves live on OS changes and syncs `<meta name=theme-color>`. Settings has an Auto/Light/Dark segmented control (localStorage-only). Also fixed the previously-undefined `var(--danger)`.
- **SRS deck ↔ lessons (43 Phase 3, both directions)** — (1) *Lessons read the deck*: `db.get_known_words` (graduated target faces with traction, strongest-first, cap 150) + `db.get_weak_cards` (low-ease/relapsed) feed both the unit planner (never proposes known words as new vocab; sequences grammar over familiar words; deterministic label-filter backstop) and the lesson author (prefers known words as helper vocab; weaves 1–2 struggling deck words into drills). (2) *Deck reads lessons*: the lesson results screen lists the new vocab concepts with ＋Add / Add-all to flashcards (server fills audio + romanization; in-deck words show a ✓ chip).
- **Spiral review in micro-lessons** — Each lesson now interleaves up to 2 review drills for previously-taught concepts: weak mastery first (≥3 attempts, <70%), then older concepts rotated by lesson number. Review concepts join assembly (correct grammar flags + glossary) but are not re-registered; outcomes feed `concept_mastery` as usual. Golden example models the shape.
- **Lesson generation hardening (the "lessons feel off" fixes)** — (1) *Gloss-spoiler*: exercise prompts received the vocab glossary, so hovering a recognition prompt revealed its own answer — exercises are now romanization-tooltip-only again. (2) *Locking*: batch generation force-enabled "unlock all" for every user, killing sequential progression — removed + stale flag cleared. (3) *Distractor hygiene*: distractors equal to the answer after normalisation (case/punct/leading articles) are dropped; listening distractors homophonous with the answer (identical romanization) are dropped. (4) *Reorder*: token-tiling now backtracks (longest-first DFS) so short-token shadowing no longer drops valid drills. (5) *Concept dedup*: plan concepts are filtered against the course registry (key + vocab label) and within the plan — `INSERT OR IGNORE` no longer silently re-teaches. (6) Registry block in prompts capped at 150; cloze `prompt_roman` keeps the blank.

- **Textbook-style lesson redesign** — Lessons now follow a textbook structure: 4–8 teach blocks for grammar (prose rule → paradigm table → examples with word-for-word `lit` glosses → notes on common errors); drills authored easy→hard (recognition → listening → production → cloze → reorder) and no longer shuffled; at least 2 reorder drills required for grammar concepts. New `lit` field in `examples` items renders as "(lit. word-for-word)" in muted italic between the native text and natural English translation — most useful when target-language word order diverges from English (verb-final, topic-comment, particles). Block type descriptions in the prompt now explain what each block *renders as* with markdown/bold support noted. Few-shot example rewritten as Spanish possessives (mi/tu/su) with 6 teach blocks, `lit` fields, and 9 ordered drills. New "↺ Reset lessons" button clears AI-generated lessons while preserving the foundations reading track (`DELETE /api/courses/{id}/ai_lessons`).
- **Tonal foundations track (Cantonese + Mandarin)** — New `script_type="tonal"` engine in `foundations.py` teaching the phonological system (syllable structure, tones, initials, finals) via deterministic listening drills. Cantonese: 3 units — syllable/jyutping intro, all six tones (two staged lessons on si1–si6 + a review on fu1–fu6), tricky initials (ng-/gw-/kw-/j-/z-/c-), and finals (long vowels, diphthongs, unreleased stops -p/-t/-k, nasals -m/-n/-ng). Mandarin: same structure — four tones staged over two lessons (mā·má·mǎ·mà + tāng·táng·tǎng·tàng) + review, retroflex/palatal initials (zh/ch/sh/r vs z/c/s vs j/q/x), and key finals (ü, er, -n/-ng). All exercises set `hide_roman: True` (romanisation is what's being tested). 10 new tests.
- **Foundations reading-track polish (Telugu audio + romanization spoiler)** — (1) The Telugu neural voice barely articulates a bare independent vowel (≈ −40 dB, inaudible); `_TE_VOWELS` now set `audio = symbol + visarga (ః)` so the clip is audible (≈ −5 dB) while staying essentially the pure vowel — display + romanization unchanged. (2) Romanization was a spoiler in the reading exercises (it's the answer, yet shown as ruby). Every foundations exercise now sets `hide_roman: True`; the player tucks romanization into the hover/tap `.gl` tooltip (`applyRuby(root, null, ex.hide_roman)`) instead of rendering ruby. Teach blocks still show it openly.
- **Foundations reading track — wired in + generalized to abugida (Hindi/Telugu)** — `foundations.py` was a complete-but-dead Korean prototype; now `main.create_course` seeds it via `db.seed_foundation_units` for any non-Latin language. Persisted as **closed** `course_units` rows (`theme='foundations'`, migration 015) at the front of the course, registering no vocab concepts. **Skippable**: `get_course` marks all foundations lessons `available` (any order) while AI vocab lessons keep sequential locking among themselves. Generalized the engine beyond Hangul: a new **abugida** engine (Devanagari + Telugu) — Indic scripts are already decomposed at the code-point level (कि = क + ि), so `decompose_indic` is char iteration and composition is plain concat. New lesson type `matras` (vowel-sign blends); `block_build` got a `compose:"concat"` mode (tap consonant + sign → concatenate, graded by string equality). Curated, validated Hindi + Telugu tracks (words filtered by decomposition to taught letters). Romanization from offline oracles only. Live-verified end-to-end (render, blend, grading). Chinese tones-track still deferred (different type — no alphabet).
- **Faster + implicit vocab** — `_next_batch` packs up to 3 consecutive vocab concepts per micro-lesson (was 2); the author prompt makes teach blocks proportionate/optional so straightforward vocab debuts directly in a glossed drill rather than each needing a teach block.
- **Word glossing in lessons (`vocab_glossary`)** — Every word in a lesson's teach text gets an English gloss, hidden behind a dotted underline and revealed on **hover/tap** (inline `.gl` tooltip). The LLM glosses *everything* (no known/unknown filtering — completeness is free when hidden), `assemble_lesson` stores it as `content.vocab_glossary`, and the client glosses **teach text only** (never exercise prompts → no answer leak). Replaced an earlier approach that filtered by a deterministic known-words set (`db.get_known_words`, removed) and rendered always-visible stacked labels — those broke inline text flow and over-glossed. Reorder tiles keep their own focused per-drill helper glosses.
- **Reorder drill answer-order fix** — The word-bank grader now re-derives the correct token order by walking the `sentence` (`_order_tokens_from_sentence`) instead of trusting the model's `tokens` array order, which was sometimes scrambled (e.g. tokens `["香港人","你","係"]` for sentence `你係香港人`), mis-grading correct answers. Drills whose tokens can't tile the sentence are dropped.
- **Mastery ledger + learner profile** — Per-concept `concept_mastery` table tracks first-pass drill accuracy per `(user, lang, concept_key)`. Accumulated on `POST /api/lessons/{id}/complete` (frontend sends `results: [{concept_key, correct, total}]` for all concepts practised). The unit planner now receives weak spots (≥3 attempts, <70% accuracy) and the user's free-text `learner_profile` setting so it can steer the next unit around real weaknesses. Settings page has an "AI Tutor Profile" textarea for background/goals, and a "Concept Mastery" panel showing a bar+percentage per concept (by language, with a language selector).
- **Learning Path redesign → unit-plan-first cohesive micro-lessons (43)** — Replaced the 3-way authoring split (planner + per-concept shared grammar cache + deterministic vocab templates) with two-level adaptive generation in `learning.py`: (1) `generate_unit_plan` drafts a coherent chapter of 6–10 ordered concepts (vocab + grammar interleaved), stored on `courses.active_plan`; (2) `author_lesson` authors a whole **small** lesson (1–2 concepts) — teach blocks AND drills together in one call — so teach/practice cohere and it's all one inspectable artifact. Drills are authored as `{answer + distractors}`; we assemble the graded exercise so the **answer key is correct by construction** (oracles still guard romanization + French conjugation; unverifiable drills dropped). A hand-editable few-shot golden example (`examples/lesson_example.json`) steers style. Admin-only `lesson_premium` toggle runs the whole pipeline on gemini-2.5-pro to compare quality. Generation is **batched** (the UI's "Generate" authors 5 lessons via `?count=`, looping `_author_next_lesson`) so you can browse ahead and watch content evolve without playing through. Works for **all languages** (verified fr/de Latin + yue Cantonese with jyutping ruby), not just French. The old `grammar_lessons.py` per-concept pipeline + `concept_content` cache are retained but no longer on the lesson path.
- **Non-Latin scripts: Korean, Hindi, Telugu (with reader ruby)** — Added Korean (Hangul, Revised Romanization), Hindi (Devanagari, IAST), and Telugu (Telugu script, IAST). Required decoupling the overloaded `logographic` flag into a machine `script_family` (exposed via `/api/languages`) and a `--script-font` CSS variable set by a `script-<family>` container class, so flashcards/reader/browse/translate/onboarding all pick the right font. `tokenizer.py` generalised to recognise Devanagari/Telugu (incl. combining vowel marks, excl. the danda ।॥) and Hangul; offline reader-ruby romanisation via `korean-romanizer` + `indic-transliteration` (both new deps). TTS: ko-KR/hi-IN/te-IN voices. **Thai still pending** — needs dictionary word-segmentation (no spaces). Haitian Creole still pending (no TTS voice — see item 41).
- **5 new languages: Italian, Brazilian Portuguese, Tagalog, Malay, Indonesian** — Added entries to `translation.LANG_INFO` (per-language rules, frequency scale, flag), TTS voices in `audio.VOICES` (it-IT/pt-BR/fil-PH/ms-MY/id-ID), gendered definite-article classifier hints for it/pt, 12-word starter decks each, and onboarding greeting samples. Everything else (settings dropdown, language pill, `/api/languages`) derives automatically. Haitian Creole was requested too but **skipped — edge-tts has no Creole voice**; revisit if another TTS provider is added.
- **SRS learning steps + face staggering** — Reworked scheduling so it behaves like real spaced repetition. `srs.update` now has sub-day learning steps (1 min → 10 min → graduate to 1 day); "again" sends a card back through the steps so it reappears *this session* instead of vanishing for a day, and the frontend re-queues any still-learning card a few slots ahead. "Easy" now graduates immediately to a 4-day interval (vs 1 for "good"), and review-card hard/good/easy intervals are differentiated. New `learning_step` column on `card_faces`. New words are also staggered to their primary face (`target`) — the source/pronunciation faces unlock only once the primary graduates — so one word no longer shows up 3× in its first session. `get_due_count` honors the same gate so the badge matches.
- **Language-first onboarding (30)** — `/welcome` now asks "What are you learning?" as step 1 (language cards with script samples); plan picker is step 2 and only shown when billing is configured. Saves `default_target_lang` and seeds a starter deck in the background. All non-admin users who haven't onboarded go through this flow on first login.
- **Persistent language pill in header (31)** — Flag + language name pill injected into every nav page via `_LANG_WIDGET`; clicking opens a dropdown to switch language, which saves the preference and reloads. Language-scoped: study sessions, due-count badge, reader saved texts, and flashcard browse all default to the current learning language.
- **Guided tour (32)** — 3-step dismissible overlay on the translate page (shown until `tour_seen` setting is set server-side). Explains: add a word → daily flashcard review → reader. Escape key also dismisses. Shown to all accounts until they see it.
- **Teaching empty state (33)** — Flashcards empty state now explains the three-face SRS loop and links directly to the translate page.
- **Starter deck seeding (34)** — 12 high-frequency words per language auto-created at onboarding (tagged "🌱 Starter") so the first study session is non-empty. Runs as a background task; guarded against re-seeding if the user already has cards.

- **Subscription billing + AI usage metering** — Stripe-hosted Checkout + Customer Portal (no card data on-server). Plans: Free (30 shared-key AI calls/mo), Pro ($5/mo, 600/mo); own-key + admin accounts are unlimited & unmetered. Quota enforced + metered centrally in `_resolve_gemini`; monthly `usage_counters` reset automatically by `YYYY-MM` period key. Webhook (`/api/webhooks/stripe`, signature-verified) syncs `users.plan`. Settings page shows a plan/usage card with upgrade + manage buttons.
- **Plan UX + admin comp** — Replaced the old per-friend shared-key grant with direct plan control: a plan badge (Free/Pro/∞) in the header on every app page, a dismissible "Upgrade to Pro" banner for free users, and a one-time first-login plan-picker (`/welcome`, gated by an `onboarded` setting). Admin can comp any user to Pro (or back to Free) from the Users list via `PUT /api/admin/users/{id}/plan` (`db.set_user_plan`); comped users have no Stripe customer so webhooks never touch them. Stripe-subscribed users are shown read-only ("Pro (Stripe)") to keep state in sync via the portal.
- **Cache-busting / auto asset versioning** — Startup content-hash of `static/` produces `ASSET_VERSION`; CSS/JS URLs are fingerprinted (`?v=…`), the service-worker cache name embeds the version, and HTML + `sw.js` are served `no-cache`. New deploys (= rebuild + restart) are picked up on the next normal load — no more Safari force-reload.
- **Reader (8a + 8c)** — AI-generated texts from an English prompt; tokenized reader view with familiarity highlighting (known/weak/new); tap any word to see translation or existing card data; one-tap add to deck; texts saved for re-reading.
- **Auto-labeling** — Gemini suggests 2–5 broad topic labels (food, cooking, animal…) on every translation; auto-created and assigned when a card is saved.
- **Classifiers / articles** — Translation response now includes the definite article (fr/es/de) or measure word (yue/cmn) for nouns; stored on the card and shown as a badge in the output and card list.
- **Reader story labels** — Words added from reader mode are auto-tagged with a "📖 [Title]" label; "Study vocab" button in the reader header opens a filtered study session for that text's vocabulary.
- **Vector embeddings** — Each card gets a background Gemini embedding (`gemini-embedding-001`, 768-dim); `GET /api/labels/suggest-cards?name=X` returns top cosine-similar unassigned cards. Label manage panel now has a "✦ Suggest" button per label.
- **Canonical card (word families)** — `canonical_card_id` FK on cards lets inflected/conjugated forms point to their base form; set via the card edit form's canonical search field; displayed as "Form of: X" in the card list.

---

Complexity ratings: **Low** (days), **Medium** (1–2 weeks), **High** (weeks+)

**Cost baseline:** Compute runs on Oracle Cloud Free Tier (4 OCPU ARM, 24 GB RAM) — $0. Gemini 2.5 Flash Lite via AI Studio free tier (1,500 req/day) — $0. `edge-tts` is free. At small family/friend scale (~5–15 active users) virtually everything stays within free tiers. Costs noted below are what would kick in if free tiers are exceeded. Paid Gemini Flash Lite rates: input $0.075/1M tokens, output $0.30/1M tokens.

---

## 45. Contextual tutor pop-over on the Reader page (flashcards shipped)
**Complexity: Medium | Cost: ~1 metered LLM call per question (same as a tutor turn)**

The **flashcard** half shipped (see ✅ Shipped: the 💬 pop-over on the study page, ephemeral `POST /api/tutor/ask` → `tutor.ask_about_card`). Remaining: bring the same contextual pop-over to the **reader**, where the interesting/harder part lives.

- **Context to inject:** the story title + the **current sentence** (and maybe a window of surrounding sentences) the learner is on, so "what does this 嘅 do here?" resolves against the actual text.
- **Hard part:** the reader must reliably know the "current sentence" — a scroll/tap heuristic (e.g. tap a sentence to ask about it, or track the sentence nearest viewport center).
- **Reuse:** the backend is mostly there — `tutor.ask_about_card` already takes a free-form `card` context block + question + ephemeral history; a reader variant would pass `{target_text: <sentence>, notes: <story title/context>}` (or a small dedicated `build_*` prompt if sentence-level help wants different guidance than word-level). The `/api/tutor/ask` route + the pop-over widget (CSS/JS in `cards.html`) can be lifted to `reader.html` — a good moment to finally factor the tutor bubble/ruby/gloss + pop-over into a shared file (long-standing follow-up).

Open question still: ephemeral one-off asks (current flashcard behavior) vs. threading reader asks into the persisted tutor history.

## 44. Per-card embedding coverage tidy-ups (low priority)
**Complexity: Low | Cost: negligible**

Two small follow-ups from the per-card-embedding fix. Neither is urgent — the lazy backfill in `suggest-cards` (`db.get_cards_missing_embedding`, source-agnostic on `embedding IS NULL`) already covers every NULL-embedding card eventually:

1. **Eagerly embed starter-deck + import cards.** `starter_deck.seed` and `import_vocab.py` create cards via `db.create_card` without generating an embedding, so those cards only get vectorised when the user later opens label-suggestions (the backfill). Could call the embed path at creation so they're covered immediately.
2. **Batch the reader-generate embeddings.** `POST /api/reader/generate` embeds *awaited, one call per new word*, which slows the request when many words are added. Could collect the new cards and do a single batched `embeddings.embed([...])` (or hand them to the background backfill) instead of per-word.

## 41. Haitian Creole audio via Meta MMS
**Complexity: Medium | Cost: $0 (local inference)**

Haitian Creole (`ht`) was deferred when adding the 5 new languages because **edge-tts has no Creole voice** — and the `voice_for` fallback would read Creole text in a *Cantonese* voice (worse than silence). Confirmed: Google Cloud TTS, Amazon Polly, Azure, and gTTS/Google Translate also have **no** Haitian Creole voice.

The one solid free option is **Meta MMS** (`facebook/mms-tts-hat`) — a purpose-built VITS model (part of Massively Multilingual Speech, 1,100+ langs), run locally via `transformers` + `torch`. Verified the model exists on Hugging Face.

**Plan:** route `ht` in `audio.py` to a lazy-loaded MMS backend; leave every other language on edge-tts so the weight only matters when Creole is actually used. Encode the model's raw waveform to MP3 (or serve WAV) to match the current BLOB storage/serving.

**Main cost is infra, not money:** adds the full PyTorch stack (~hundreds of MB) + the model (~140 MB) to the Docker image → much larger/slower builds & deploys on the Oracle ARM box, for a single language. CPU inference works on ARM, just slower per card. Decision pending: is Creole worth the heavier container? Prototype on `develop` first to judge voice quality before committing.

## 42. Thai support (needs word segmentation)
**Complexity: Medium | Cost: $0**

Thai (`th`) is the remaining language from the "non-Latin scripts" batch. TTS is fine (`th-TH-PremwadeeNeural`), but Thai is written **without spaces between words**, so the reader's whitespace tokenizer (`_tokenize_latin`) would treat a whole sentence as one untappable blob. Needs dictionary-based segmentation like CJK — add a `th` branch in `tokenizer.tokenize` using **`pythainlp`** (`word_tokenize`), plus `pythainlp.transliterate.romanize` for reader ruby. Everything else follows the established pattern: `LANG_INFO` + `SCRIPT_BY_LANG['th']='thai'`, `--thai-font` (Noto Sans Thai), starter deck, onboarding sample. `pythainlp` is the one new dep (pure-Python but bundles data). Romanization scheme decision: RTGS (standard, drops tones) vs a tone-marked scheme — Thai is tonal so tones matter for learners.

## 43. AI Learning Path (Duolingo-style course) — ⭐ FLAGSHIP
**Complexity: High | Cost: ~$0 (Gemini free tier) | the big bet**

A generative, SRS-aware course that teaches a language from the ground up. **Design decisions locked** (2026-06): CEFR-scaffolded curriculum, unified with the existing SRS deck, lean deterministic MVP first.

**Architecture:**
- **Two-layer generation** — keeps an AI course coherent: (1) *curriculum* skeleton = units + lessons + objectives + target new-concept counts, generated once and regeneratable; (2) *lesson content* = the exercises, generated on demand and **cached** in `lessons.content` (JSON).
- **Concept ledger** — each lesson declares the concepts it teaches (vocab + grammar points). Next-lesson generation is fed a compact *digest* (concept IDs + mastery + weak items to recycle), never raw lesson text → scales without context bloat. The app owns concept IDs; the AI references the provided list and proposes canonical keys for new ones (app dedupes).
- **CEFR scaffold** — a per-level, language-agnostic can-do/topic checklist (A1: greetings, numbers, family, ordering food…) guides the curriculum generator. Reliable + generalizable since CEFR is language-independent. Store as a `learning.CEFR_SYLLABUS` constant (like `LANG_INFO`).
- **SRS fusion** (the differentiator) — lesson vocab becomes **tagged cards** in the existing deck (reuse `create_card` + a course/unit label, like reader story-labels). Generator checks `get_word_statuses` to skip already-known words and recycle weak ones into review exercises. Path ↔ flashcards reinforce each other; this is the "aware of what the user has learned" edge.
- **Exercise-type registry** — each type = JSON schema (what the AI emits) + frontend renderer (widget) + grader. Adding a type later touches only those three. **MVP types are all auto-gradable**: vocab multiple-choice, word-bank sentence build (Duolingo-style tiles), listening (edge-tts audio), cloze/fill-blank, match-pairs. AI-graded free production (translation, comprehension) is **deferred** — costs a call per answer.
- **Foundations module (script + sound system) — ✅ SHIPPED for Hangul + abugida (see Shipped section); wired in skippable (not a hard gate).** Remaining: tonal (Chinese) track, SRS sound-cards. The writing/sound system is a different kind of learning (perceptual/production skill, not meaning), finite & factual, shared per-language. **Key architectural decisions:**
  - **Reuses the segmented-lesson player** — a Foundations lesson is the same `{segments:[{teach,exercises}]}` shape: orientation = a teach segment with no exercises; a grapheme unit = teach (letters+audio) → drills. No new player.
  - **Generalizable = data + per-script-type engines** (must be droppable-in for languages not yet supported). (1) A declarative `FOUNDATIONS` registry per language: `script_type` (alphabetic/abugida/tonal/logographic/latin) + ordered units of type `info` (non-testable orientation, e.g. "Korean letters stack into syllable blocks", "French is a Romance language"), `graphemes` (`{symbol, roman, audio, example}`), `tones` (`{name, number, example_char}`), `words` (curated or AI-proposed-then-validated). Adding a language = fill in data, no code. (2) Per-script-*type* engines (the only script-specific code, shared by all langs of that type): a **decomposer** (`word→[graphemes]`: Hangul jamo math, Devanagari akshara split) powering "words from known letters", plus Foundations exercise builders.
  - **New concept kind `sound`/`grapheme`** → when added to SRS, a **sound-card** (symbol↔sound faces, NO meaning face) via a `card_type` flag (the one schema touchpoint into Phase 3).
  - **New exercise types:** grapheme↔sound, **tone discrimination** ("ma" → which of the 4 Mandarin tones?), consonant+matra **blend** (abugida), **syllable-block assembly** (Hangul). All deterministic from curated graphemes.
  - **"Words from known letters":** curate a grapheme order so useful words unlock early; AI proposes frequent words; we **filter by decomposition** to those using only known graphemes (generic via the decomposer). Logographic (Chinese) has no alphabet → its Foundations = orientation + tones + romanization literacy instead.
  - **Per-script-family shape differs:** Latin = orientation + optional pronunciation primer (no gate); Hangul = letters→blocks→words (gate); abugida = vowels→consonants→matras→conjuncts→words (gate); Chinese = orientation + jyutping/pinyin literacy + tones.
  - **Integration:** Foundations units sit at the FRONT of the `/learn` path, gating vocab for non-Latin scripts. The CEFR vocab syllabus already excludes script/sound.
  - **✓ Korean/Hangul MVP BUILT (on staging):** `foundations.py` = jamo engine (compose/decompose with compound splitting) + curated Hangul track (orientation → vowels → blocks → consonants → words, ordered so 안녕/네 unlock early) + `build_units()` producing pre-built lesson `content` (segments). Reuses choice/listening/match; new `block_build` type (assemble consonant+vowel into a syllable, live-composed in JS via the Unicode formula). Words validated by `decompose_hangul ⊆ taught`. Romanisation from korean-romanizer; letters voiced via a representative syllable (가 for ㄱ). Wired into course creation (`foundations.build_units` prepended in `/api/courses`), `db._insert_units` persists pre-built content, info-only lessons supported by the player. Sound-card SRS integration still deferred to Phase 3.

**Data model:** `courses (user_id, target_lang, level, status)` / `units (course_id, idx, title, theme, objective, status)` / `lessons (unit_id, idx, title, objectives, status, content JSON null=ungenerated, concepts_introduced JSON)` / `concepts (course_id, kind, key, label, introduced_lesson_id)` / `user_progress (user_id, lesson_id, score, completed_at)`. Exercises live as a JSON blob in `lessons.content` (regeneratable content, not relational); attempts/scores are durable. New module `learning.py` (mirrors `translation.py`). New `/learn` page: path view + lesson player + results.

**Phased build:** (1) ✓ DONE — CEFR scaffold + curriculum generator (`learning.py`) + persistence (courses/units/lessons/concepts/progress tables) + read-only `/learn` path view with create/regenerate; (2) ✓ DONE — `generate_lesson()` + exercise-type registry (choice / word_bank / listening / match) + lesson player UI + deterministic client grading + completion tracking (`course_progress`, score, unlock-next) + per-lesson regenerate + `/api/tts`. Romanisation in exercises comes from our offline romanisers (correct jyutping), never the AI. (3) SRS fusion (vocab→cards, skip-known, recycle-weak, ledger mastery); (4) background pre-gen, more exercise types, later AI grading.

**Phase 4 ✓ DONE (just-in-time planner, see Shipped):** dropped the frozen unit plan; `plan_next_lesson` re-decides each lesson from live state and can scope a whole grammar family per lesson; model is provider-pluggable (Gemini/Claude `lesson_model` A/B knob).

**Phase 5 — gamification. ✓ DONE (see Shipped).** (1) skill-tree path = a vertical winding node trail with unit banners, a pulsing START flag, and per-lesson 👑 crown levels (`course_lessons.crown_level`, bumped per completion); (2) in-lesson combo meter (🔥 ×N chip + escalating XP, wrong-resets); (3) daily-goal XP ring + animated end-of-lesson XP count-up with perfect-lesson bonus (XP persisted to `points_ledger` once per lesson). Possible polish later: JS-angled connectors between offset nodes (currently a straight central spine + gentle wave); crown-driven "practice to level up" that routes through the planner `focus:"review"`; legendary/timed challenge mode.

**Adding an exercise type:** (a) add its schema to `learning._EXERCISE_CONTRACT` + a romanisation plan in `_attach_romanization`; (b) add a renderer/grader entry to `EXERCISE_TYPES` in `static/learn.html` (contract: `render(ex,root,lang)->{isReady,grade,answerText,lock}`). Nothing else changes.

**Segmented lessons + pronunciation exclusion, DONE:** (1) lessons are now split into SEGMENTS — teach ~3 concepts → practise them → teach ~3 more → practise (later segments add 1–2 refresh exercises recycling earlier-in-lesson material). Lesson content shape is now `{segments:[{teach,exercises},…]}`; the player iterates segments (teach screen between each, "part i/n"), with end-of-lesson mistake review across all segments and progress = answered/total. Old single-block lessons still play via a back-compat shim. (2) The curriculum prompt now explicitly forbids pronunciation/tone/script-system concepts (e.g. "high level tone") — those belong to the future Foundations module, not the vocab course.

**Grammar drilling (constructions + forms), STARTED:** flashcards can't teach grammar because it's generative/paradigmatic/contextual. Design: two mechanisms — (A) **constructions** = sentence frames with typed slots filled procedurally from the learner's KNOWN vocab (SRS-aware → endless fresh practice; slots that are verbs call the conjugation engine); (B) **form/paradigm drills** = produce the right conjugation/declension, distractors are other cells of the same paradigm; plus (C) **contrast** drills (ser/estar…) reusing `choice` + the 💡 tips. Key principle (same as romanisation/Foundations): grammar forms are systematic → **compute/curate, don't trust the AI**. `grammar.py` (DONE): French present-tense engine = regular -er/-ir/-re rules (incl. -ger/-cer spelling) + a curated irregular table, **verified 100% against verbecc/Verbiste** which is used only as a dev-time oracle (NOT shipped — it pulls ~90 MB scikit-learn + retrains on load). `build_conjugation_exercises()` makes `form_drill` exercises (reusing `choice`). Wired into `generate_lesson`: French verb concepts (gloss "to …") auto-get conjugation drills.

**Grammar-first lessons + critic pass, DONE:** big reframe (per the "emphasise grammar/sentence construction, vocab is trivial" goal). A grammar concept now produces a dedicated **grammar-first segment**: an explicit English **rule** + **minimal pairs** (two sentences differing in ONE feature) on the teach screen, then **cloze** + **reorder** drills — kept entirely OUT of the vocab pipeline (no ambiguous "say X"). Content comes from a **generator + critic** pipeline (`grammar_lessons.py`): a generator LLM writes rule/pairs/cloze/reorder; a *critic* LLM independently re-derives and judges each item; **rejected items are dropped** (thinner-but-correct). Conceded the "only drill computable forms" rule once generation moved offline — BUT kept the carve-out where a free non-LLM oracle exists: romanization is always recomputed (never trusted from the model) and French present-tense cloze answers/options come from `grammar.py`. Verified artifacts are cached **shared across users** in `concept_content (lang, concept_key)` (expensive once, cheap replay); `main._ensure_grammar_content` generates on cache-miss. Exercises reuse `choice` (cloze + minimal-pair recognition) and `word_bank` (reorder) — no new renderers. Migration 006 adds the table + re-clears cached lessons.

**Rich-block teach content + coherence, DONE:** reframed on **liberal in what you SHOW, strict in what you GRADE**. The teach/explanation content is now authored FREELY by the LLM as an ordered list of typed **blocks** (prose / arbitrary table / examples / contrast / note) — a grammar-textbook page that generalises across languages (the model makes whatever paradigm tables/explanations the language needs), rendered by us (no raw HTML, no hardcoded French-shaped tables; audio + romanization survive). The critic verifies each block and drops rejected ones. INTERACTIONS stay constrained with strict verified answer keys (engine-checked French cloze, recomputed romanization). Also: **curriculum coherence** — `_build_curriculum_prompt` now forces every lesson to be a single coherent chapter (title/objective/concepts match; LLM picks the axis — communicative topic OR grammar point), fixing the "être conjugation under Hello & Goodbye" discord caused by the earlier grammar-backbone over-correction. Player layout switched to natural height + a reserved feedback slot (fixes the full-viewport-gap "weird resizing"). Migrations 008–010 cleared the now-stale cache. NOTE: existing courses keep their old (incoherent) curriculum until **regenerated** — only lesson *content* auto-refreshes.

**Reference tables on the teach screen (SUPERSEDED by rich blocks):** grammar explanations can now show a table (generic `{title, columns, rows}` shape, rendered as an HTML table with script-font cells). A verb concept's **conjugation paradigm** is **engine-computed** (`grammar.conjugation_table`) — never the LLM; the generator is told NOT to produce conjugation tables. The generator MAY emit *other* paradigm tables (article/pronoun/ending grids), critic-verified cell-by-cell and dropped if any cell is wrong. **Reflexive-verb fix (DONE alongside):** `grammar.conjugate_present` refuses reflexive/pronominal verbs (was silently producing wrong "s'appelent"); the conjugation override only fires when the model's cloze answer is a real paradigm cell; a `_cloze_is_sane` guard drops clozes whose filled sentence duplicates the answer word.

TODO (deferred): **constructions / controlled-vocab enrichment** (fill drill slots from the learner's KNOWN SRS deck via *typed* slots — noun swaps change agreement, so slots carry gender/number/classifier) — the user's main excitement; a grammar-**DAG** syllabus (LLM-named concepts + cross-lingual tags + L1-contrast weight, generated once per language, replacing vocab-themed units); concept mastery → sequencing via the existing SRS; error-correction drills; German article/case tables; more tenses & languages.

**Nuance / "teach the distinction", DONE:** keep the FULL contextual gloss ("thank you (for a gift)" vs "(for a favour)") instead of stripping it — so near-synonyms (多謝/唔該, formal/informal you) stay distinct, production questions are unambiguous, and the other synonym shows up as a *teaching distractor* in recognition. Materialisation asks the AI for distinct targets + a one-sentence usage `note` per confusable/grammar item; that note is attached to each exercise as a `tip` and shown as a 💡 after answering (the "AI info on trap questions" idea).

**Reliability rework (critical fix), DONE:** the original Phase 2 let the AI generate target text AND answer keys for exercises — which violated the "content from translate()+romanizers, never the AI" principle and produced wrong answers/characters/directions (esp. Cantonese: "comment"="what", 呢好 `ne1 hou2`, "si1"→"four"). Rewrote `generate_lesson` to **materialise** each concept's accurate target via one translation call (English gloss → target), compute romanisation ourselves, then **build the choice/listening/match exercises deterministically in Python** so prompts, answers, and directions are guaranteed correct. The AI now only translates + writes the teach intro/notes. `word_bank` dropped from generation (needs validated sentences — deferred). Per-lesson "↻ regenerate" button to refresh stale/old lessons.

**Phase 2.5 (pre-ship polish), DONE:** teach-first screen (each lesson opens with a `teach` block — new words + audio + romanisation + grammar notes — before exercises); more review (generator now tests each new concept 2×+ and recycles earlier concepts; completed lessons become "↻ Play again"); **continuation** — finishing the whole course shows "Continue to {next CEFR level}", which generates the next level's units building on the concept-ledger digest (`get_course_concept_digest` → `generate_curriculum(known_summary=…)` → `append_units`); UI cleanup (completed lessons show score badge, end-of-course CTA). Deep spaced repetition still pending = Phase 3 SRS fusion. **Foundations module** (script + tones/pronunciation; curated, gates non-Latin) is its own track — sequence it relative to the main build once we pick a starting language (Latin-script first sidesteps the gate; a non-Latin pilot needs Foundations first).

**Spike findings (curriculum gen, fr + yue A1):** structure/progression excellent and genuinely language-adapted (fr → gender/articles/conjugation; yue → final particles/嘅/唔·冇/classifiers) — validates CEFR-scaffolding. BUT inline target text + romanization are unreliable (二→"yee5", 五→"mm5", one wrong vocab item). **Design principle locked:** the curriculum AI plans *what* to teach in **English glosses + stable keys**; the actual target text + romanization come from our trusted stack (`translation.translate()` + offline romanizers), never the curriculum's inline labels. Concept keys are English-gloss-based (language-independent → nicer for the SRS "already known?" check).

**Risks:** curriculum quality (#1 — mitigated by CEFR scaffold + per-lesson/unit regenerate); concept-ID stability (app owns IDs); open-ended grading cost (deferred); scope (tight MVP, expand). Per-lesson and per-unit **regenerate** buttons throughout.

---

# Onboarding & intuitiveness

New users report (a) not understanding how the app works and (b) not finding how to switch off the default language. The app's loop — *translate a word → it becomes a flashcard → study it daily → read real text* — is invisible, and the first screen is a blank translate box defaulted to Cantonese. Ideas 30–35 fix discoverability; 36–40 bootstrap absolute beginners toward Duolingo parity.

## 30. Language-first onboarding ✅ (implementing)
**Complexity: Low | Cost: $0**

Make "What do you want to learn?" the first step of `/welcome`, before the plan picker, so nobody lands on Cantonese by accident. Saves `default_target_lang`; the plan step only appears when billing is configured for a free user.

## 31. Persistent language switcher in the header ✅ (implementing)
**Complexity: Low | Cost: $0**

A "🌐 [language]" pill in the nav on every app page, one tap to change the learning language (writes `default_target_lang` + reloads). The current chevron dropdown on the translate page is too subtle and only exists on that one page.

## 32. First-run guided tour ✅ (implementing)
**Complexity: Low–Medium | Cost: $0**

A 3-step dismissible coach overlay on first visit to the translate page: ① type a word → AI makes a flashcard, ② study daily on Flashcards, ③ read stories in Reader. Gated by a localStorage flag.

## 33. Teaching empty states ✅ (implementing)
**Complexity: Low | Cost: $0**

Make zero-data states explain the loop instead of being blank — richer Flashcards empty state that describes per-face study (recognition vs. production) and points back to translating.

## 34. Seed a starter deck on signup ✅ (implementing)
**Complexity: Low–Medium | Cost: $0**

Auto-create ~15 high-frequency cards in the chosen language at onboarding (with audio via edge-tts, tagged "🌱 Starter") so the very first study session is non-empty and the loop is experienced immediately. Guarded to only seed when the user has zero cards.

## 35. Reframe the home tab & translate copy
**Complexity: Low | Cost: $0**

"Translate" reads like a utility, not a learning app. Consider renaming to "Add a word"/"Learn" and adding a one-line subtitle under the input for new users: *"Translate any word — it becomes a flashcard you'll review."*

## 36. Guided beginner course / skill tree
**Complexity: High | Cost: ~$0/month | *the* Duolingo-competitive bet**

A curated unit sequence per language (Greetings → Numbers → Food → Getting around…). Each unit bundles a few pre-made vocab cards, one short grammar note, a tiny reader text, and 2–3 sentence drills — turning the blank box into "do the next lesson." Everything below can feed into it.

## 37. Grammar note card type
**Complexity: Medium | Cost: ~$0/month**

Add short grammar-explainer cards that are also SM-2 scheduled (e.g. "Cantonese verbs don't conjugate; 咗 marks completed action"). Closes the biggest content gap for beginners. Pairs with idea 16 (grammar info on demand) for generation.

## 38. Explain-this-sentence in the reader
**Complexity: Medium | Cost: ~$0/month**

Tapping a sentence already gives a translation; add a grammar/word-by-word breakdown ("這=this · 係=is · 嘅 marks possession") so beginners learn structure, not just isolated vocab.

## 39. Unified "Daily Practice" flow
**Complexity: Medium | Cost: ~$0/month**

One button that runs today's new words + due reviews + one short reading as a single guided session with a progress bar and end-of-session reward. Combine with the streak (idea 20). Removes the need to self-direct across tabs.

## 40. Romanization-first / script-toggle mode
**Complexity: Low | Cost: $0**

For logographic languages, let beginners learn in jyutping/pinyin + audio first and reveal characters progressively — removes the steepest day-one wall for yue/cmn.

> **See also:** ideas 9 (novel sentence review) and 13 (typing/cloze) give the "produce the language" practice Duolingo is known for and are especially valuable early — prioritize them for beginners.

---

## 29. Get a Real Domain for Email + Cleaner URLs
**Complexity: Low | Cost: ~$10/year**

DuckDNS doesn't support TXT records, so Resend domain verification is impossible and email currently sends via Resend's sandbox (`onboarding@resend.dev`) which only delivers to verified addresses. A real domain unlocks proper transactional email and a cleaner public URL.

**Scope:**
- Register a domain (e.g. Cloudflare Registrar — at-cost, ~$10/yr for `.com`)
- Point A record at the Oracle VM
- Add Resend's SPF/DKIM TXT records in Cloudflare DNS (3 records, takes ~5 min)
- Update `APP_URL`, `FROM_EMAIL` GitHub secrets + Caddy config to use the new domain
- Decommission DuckDNS

---

## 28. Move Browse Button Out of Nav Bar
**Complexity: Low | Cost: $0**

The "Browse" button on the Flashcards page is a page-specific action (opens a card search modal) but currently sits in the shared nav bar as a special-cased extra. It should live in the page content instead — e.g. as a button near the top of the flashcard view or inside the study controls area — so the nav bar contains only true navigation links.

**Scope:**
- Remove the Browse button from the injected nav (`extra_desktop` / `extra_dropdown` params in `_build_nav`)
- Add a Browse button directly in the cards page UI (e.g. alongside the study controls or as a floating action button)
- No backend changes needed

---

## 24. Label Merging
**Complexity: Low | Cost: $0**

Allow users to merge two or more labels they consider synonymous or too granular into a single unified label.

**Scope:**
- UI in the label management panel: select multiple labels → "Merge into…" action (pick or type a target label name)
- Server: reassign all `card_labels` rows from the source labels to the target label; delete source labels
- Deduplication: if a card already has the target label, drop the duplicate row rather than inserting twice
- Confirmation dialog listing how many cards will be affected per source label

**Open questions:**
- Should the merge target be an existing label or can the user type a new name on the spot?

---

## 25. AI-Powered "Generate More Words for Label"
**Complexity: Low–Medium | Cost: ~$0/month**

From a label's manage panel, ask Gemini to suggest additional vocabulary that fits the label's theme — excluding words the user already has in their deck.

**Scope:**
- "Generate more words" button per label in the manage panel
- Send label name + existing card source_text list to Gemini; ask for N new vocab suggestions in the same schema as single-card translation
- Filter out any suggestions that match existing cards (fuzzy match on source text)
- Show a preview/checklist; user selects which to add, then runs normal card creation (audio, romanization, etc.)

**Cost notes:** One Gemini call per request (~1,000 output tokens). Free tier easily handles it; paid tier ~$0.0003/request.

---

## 26. Reader Difficulty Level at Generation Time
**Complexity: Low | Cost: $0**

Add a difficulty selector to the reader text generation form so users can tune how complex the generated text is.

**Scope:**
- UI: difficulty dropdown (Beginner / Intermediate / Advanced / Custom) next to the existing prompt input
- Pass selected difficulty to the Gemini prompt as an explicit instruction (e.g. sentence length, vocabulary frequency, grammatical complexity)
- "Custom" option reveals a free-text field for fine-grained constraints (e.g. "use only present tense, no classifiers")
- Works alongside the existing vocab-constrained mode (idea 21)

**Note:** Supersedes / extends idea 21 — implement together.

---

## 27. User-Configurable API Keys and Model Selection
**Complexity: Medium | Cost: shifts API costs to users**

Allow each user to supply their own Gemini API key and choose which model is used for translation, embedding, and reader generation. Enables cost offloading and power-user customization.

**Scope:**
- New "API Settings" section in user settings: Gemini API key field (stored encrypted or hashed in `user_settings`), model selector per task type (translation, embedding, reader generation)
- Backend: if a user-supplied key is present, use it for that user's AI calls instead of the server key; fall back to server key if absent
- Validate the key on save (cheap test call) and surface errors clearly
- Admin view: see which users are using their own keys vs. the shared key
- Model list fetched dynamically or hard-coded from a known-good set (Gemini Flash Lite, Flash, Pro…)

**Open questions:**
- How to store API keys securely (encrypt at rest? treat like a password with scrypt?)?
- Should the server key be disabled entirely for users who supply their own, or always available as fallback?
- Do we expose model selection per-task or a single global model choice?

---

## 17. Reader Audio Playback Mode
**Complexity: Low–Medium | Cost: ~$0/month**

A "read-aloud" mode in the reader: tap Play and the app reads the full text sentence by sentence, highlighting the active sentence in the panel as it plays. Auto-advances to the next sentence when audio finishes.

**Scope:**
- Pre-generate (or lazily fetch) TTS for each sentence on page load
- Sequentially highlight each sentence as its audio plays
- Pause/resume/stop controls
- Auto-scroll to keep the active sentence in view

**Open questions:**
- Pre-generate all sentence audio on text open (adds latency but smoother playback), or fetch each sentence on demand just before it plays?

---

## 18. Reader Performance: Pre-generate Translations and Audio
**Complexity: Medium | Cost: ~$0/month**

The reader currently fetches sentence translation and word audio on demand, causing noticeable latency. Pre-compute these in the background when a text is opened.

**Scope:**
- After a text loads, silently call `/api/reader/tts` for each sentence and cache the audio blobs client-side (or server-side in `reader_texts`)
- Similarly pre-fetch sentence translations and store them in a `reader_text_sentences` table (id, text_id, sentence_idx, translation)
- Word translations are harder to pre-generate since there are many; consider pre-translating all "new" tokens at load time and caching in memory
- Show a background-loading indicator so the user knows audio will be ready soon

---

## 19. Better Reader Loading Animation
**Complexity: Low | Cost: $0**

The current generate/open flow shows a basic spinner. Improve to feel more polished.

**Scope:**
- Skeleton loader for the text body (grey placeholder lines) while tokens are being fetched
- Progress indication when generating (e.g. "Generating text… Tokenising… Done")
- Smooth fade-in of the reader text once loaded

---

## 20. Duolingo-Style Streak
**Complexity: Low | Cost: $0/month**

Show each user a daily streak counter: consecutive days where the user completed at least one review.

**Scope:**
- Track streak in `user_settings` or a new `user_stats` table (last_active_date, current_streak, longest_streak)
- Increment streak on first review of a new day; reset to 0 if a day is missed
- Display streak prominently on the flashcard page (e.g. 🔥 7 days)
- Optional: "streak freeze" if the user does a reader session but no reviews

---

## 21. Reader Difficulty / Vocab-Constrained Generation
**Complexity: Medium | Cost: ~$0/month**

Let users select a difficulty level before generating, or explicitly limit the number of new words the text may introduce.

**Scope:**
- UI: difficulty selector (Beginner / Intermediate / Advanced) or a "max new words" slider (e.g. 0–10)
- In vocab-constrained mode, pass the user's deck word list to Gemini and instruct it to use only known words plus at most N new ones (see idea 8c)
- Beginner: short sentences, high-frequency vocab, no new words beyond a small cap
- Advanced: longer sentences, idiomatic expressions, fewer constraints
- The existing familiarity highlighting makes it immediately visible how well the constraint was respected

**Cost notes:** Passing the user's deck (up to a few thousand words) as context adds ~2–5K tokens per generation call. At free-tier rates this is still $0; on paid tier ~$0.001/generation.

---

## 22. Card Search in Browse Tab
**Complexity: Low | Cost: $0**

Add a search/filter box to the Flashcards browse view so users can find specific cards by keyword.

**Scope:**
- Text input at the top of the browse list
- Filter cards client-side (already have all cards loaded) by matching against source_text, target_text, and romanization
- Debounce input; clear button; highlight matched substrings
- Works in addition to (not instead of) the existing label filter

---

## 23. Share Reader Stories with Other Users
**Complexity: Medium | Cost: ~$0/month**

Allow a user to share a generated reader text with other users on the same instance.

**Scope:**
- "Share" button on a saved text → generates a short share token (stored in `reader_texts` as a `share_token` column)
- Public URL: `/reader/shared/<token>` — readable without login, or login-gated (TBD)
- Recipient can open it in their own reader view and save a copy to their account
- Word familiarity highlighting uses the recipient's own deck, not the sharer's
- No real-time sync; it's a one-time copy, similar to card set sharing (idea 10)

---

## 16. Language-Specific Grammar Info on Demand
**Complexity: Low–Medium | Cost: ~$0/month**

Click on a word/card to see language-relevant grammatical metadata inline — e.g. noun gender in French/German, measure words (classifiers) in Cantonese/Mandarin, full declension tables in German, aspect markers in Chinese, etc. Each language defines which grammar dimensions are relevant; the info is fetched on demand via Gemini.

**Scope (rough — not fully designed):**
- Per-card or per-word "grammar info" button/tap target
- Gemini prompt asks for language-specific grammar dimensions (parameterized by `target_lang`)
- Display inline or in a modal: e.g. "粒 (for small round objects)", "der/die/das → Nom: der, Acc: den, …"
- Cache results on the card row to avoid repeated API calls
- Language config in `translation.LANG_INFO` drives which dimensions to fetch per language

**Open questions:**
- Should grammar info be fetched at card creation time (bundled into the translation call) or lazily on demand?
- How to surface this in the review UI without cluttering the card face?
- Should grammar notes be editable by the user?

**Cost notes:** One short Gemini call per unique word (if lazy) or bundled into the existing translation call at no extra cost. At small user scale: effectively $0.

---

## 4. Images for Vocab Cards
**Complexity: Medium | Cost: $0 manual / ~$1/month AI-assisted | Priority: Low**

Attach an image to a card to aid memory. Images are optional and don't affect existing cards.

**Scope:**
- Add `image_data` BLOB (or `image_url` TEXT) to `cards` table — storage decision deferred, either works
- Card edit UI: image upload widget + optional AI image suggestion
- Render image on card front/back during review
- Keep images optional — no existing cards break

**Cost notes:**
- Manual upload only: $0. Images (~50–200 KB each) add ~100 MB to DB for 1,000 cards — fine for Oracle free storage.
- AI image generation (Gemini Imagen 3): ~$0.02/image. At 50 new cards/month = ~$1/month.

---

## 5. AI Auto-Labeling & Semantic Similarity for Review Prioritization
**Complexity: High (split below)**

**5a. Auto-labeling**
**Complexity: Medium | Cost: ~$0/month**

On card creation, Gemini suggests topic labels (e.g. "food", "greetings"). User can accept, edit, or reject.

- One short additional Gemini call per card creation (~50-token output)
- Pre-populate label UI with suggestions

**Cost notes:** Trivially small token count. Free tier easily covers it; paid tier ~$0.005/month.

**5b. Vector embeddings for semantic prioritization**
**Complexity: High | Cost: ~$0/month**

Generate an embedding per card and use cosine similarity to cluster semantically related cards, enabling smarter review ordering and a "related cards" feature — independent of manual labels.

- Use Gemini `text-embedding-004` API to generate embeddings on card creation/edit
- Store vectors in SQLite via `sqlite-vss` extension (or a flat numpy file as a simpler start)
- Review scheduler can optionally surface semantically clustered cards together
- Alternative: use a local embedding model (e.g. `nomic-embed-text` via Ollama) to avoid any API dependency

**Cost notes:** Gemini text-embedding-004 costs $0.00001/1K chars. At ~100 chars/card and 1,000 cards: $0.001 total. Effectively $0. Local model alternative has zero API cost but adds RAM usage on the VM (~500 MB for a small model).

**Open questions:**
- Similarity used for review ordering, or as a standalone "explore related" feature, or both?

---

## 6. Prompt-Based Vocab Sets
**Complexity: Low–Medium | Cost: ~$0/month**

User types a topic (e.g. "vegetables", "hospital visit") and gets a batch of relevant vocab cards generated and added at once.

**Scope:**
- New UI panel: "Generate vocab set" text input + language selector
- Send prompt to Gemini requesting N vocab items as a JSON array (same schema as single-card translation)
- Deduplicate against existing cards before inserting
- Preview/confirmation screen before bulk-adding
- Reuse existing audio generation pipeline per card

**Cost notes:** One Gemini call per set (~1,000 output tokens) + N `edge-tts` calls. At 20 sets/month: ~20K tokens = $0.006 on paid tier. Free tier handles this easily.

---

## 7. Sentence Parsing → Sub-Card Creation
**Complexity: Medium | Cost: ~$0/month**

When a longer phrase is translated, automatically decompose it into component words/sub-phrases and offer to create cards for each part. Avoids duplicating cards already in the deck.

**Scope:**
- After translating a phrase, call Gemini to decompose it into meaningful sub-units (words and short phrases worth learning independently)
- Granularity: word-level and short idiomatic phrases — not single characters
- Filter out items already in the user's deck (fuzzy match on Chinese field)
- Show a checklist UI: "Also add these words?" — user selects which to add
- Each sub-card goes through the normal creation pipeline (audio, jyutping, etc.)

**Cost notes:** One extra Gemini call per translation (~500 output tokens). At 10 users × 5 translations/week = 200 extra calls/month → ~100K tokens = $0.03/month on paid tier. Free tier handles it.

**Open questions:**
- Should sub-cards link back to the parent sentence card?

---

## 8. Reader Mode — remaining sub-features
**8a + 8c shipped.** Remaining:

An immersive reading experience in the target language, modelled on Du Chinese. Unknown or weakly-known words are highlighted by familiarity; hover/tap reveals translation and lets you add a card in one tap. Supports dialogue with per-speaker audio. Text can be AI-generated or imported.

**Tokenization approach (Du Chinese style):**
- Use **word-level** segmentation, not character-level — e.g. 喜歡 is one tappable unit
- **Cantonese:** `pycantonese` library (dictionary + UD Cantonese treebank)
- **Mandarin:** `jieba` library
- **Latin-script languages:** standard word splitting (whitespace + punctuation)
- Each token is looked up in the user's deck and classified: known / weak (low ease or recent again) / new
- Colors match the classification (e.g. Du Chinese's green/yellow/red scheme)

**Sub-features (roughly in order of implementation):**

**8a. Basic reader view (Medium | Cost: ~$0/month)**
- Paste text or type; tokenize locally with pycantonese/jieba (no API call)
- Render tokens with familiarity highlight colors
- Hover/tap: show translation, jyutping, card status; one-tap add to deck

**8b. Dialogue mode (Medium | Cost: ~$0/month)**
- Structured input: alternating speaker lines with speaker label
- Per-speaker TTS audio using different `edge-tts` voices
- Playback controls per turn and for full dialogue

**8c. AI text generation (Medium | Cost: ~$0–$1/month)**
- Prompt input: "conversation about ordering dim sum"
- Option: vocab-constrained mode — Gemini instructed to use only the user's existing deck words plus a configurable number of new words (minimizes unknown tokens in the reader)
- Output feeds directly into reader view (or dialogue mode)
- Generated texts optionally saved and named for re-reading

**8d. Bulk card creation from reader (Low — once 8a exists | Cost: ~$0)**
- "Add all new words" button — batch-creates cards for all new tokens in the current text
- Same deduplication logic as feature 7

**8e. External text import (Low–Medium | Cost: ~$0)**
- Paste a URL or raw text from a news/content source; strip boilerplate; feed into reader view
- Possible free sources: VOA Cantonese, RFI Chinese, Wikipedia articles

**Cost notes (8 overall):**
- Local tokenization (pycantonese/jieba): CPU only, ~$0, runs fine on Oracle ARM
- AI generation (8c): ~1,500 output tokens/text. At 50 generations/month = 75K tokens = $0.023 on paid tier. Free tier handles it.
- Audio (8b): `edge-tts` is free regardless of volume.

**Open questions:**
- Should generated texts be saved/named for re-reading?

---

## 9. Novel Sentence Review Mode
**Complexity: Medium | Cost: ~$0/month**

A review mode where the user reads AI-generated sentences built from their existing vocab, testing comprehension in context rather than isolated word recall.

**Scope:**
- Generate a batch of sentences using words from the user's deck (Gemini prompt with vocab list as context)
- Prefer words that are due for review or recently introduced
- Display sentence in target language; user attempts comprehension, then reveals full translation
- Rate comprehension (Again/Hard/Good/Easy); optionally propagate rating to constituent word cards' SM-2 scores
- Use vocab-constrained generation (see 8c) to minimize unknown words in generated sentences
- Generated sentences optionally cached per session to avoid redundant API calls

**Cost notes:** One Gemini call per session batch (~500 output tokens). At 10 users × 4 sessions/week = 160 calls/month × 500 tokens = 80K tokens = $0.024/month on paid tier. Free tier handles it.

**Open questions:**
- Does a sentence comprehension rating update SM-2 scores for all constituent word cards?
- Should sentences be cached between sessions or always freshly generated?

---

## 10. Card Set Sharing by Label
**Complexity: Medium | Cost: ~$0/month**

Allow users to share a labelled subset of their deck with other users. The receiving user gets a copy of the shared cards added to their own deck.

**Scope:**
- Export a label as a shareable link or code (server-side: serialize card set by label)
- Import flow: preview the incoming cards, deduplicating against the user's existing deck
- Imported cards are fully owned by the importing user (siloed — edits don't sync back)
- No real-time sync; this is a one-time copy operation

**Cost notes:** No AI calls. Pure data operation.

**Open questions:**
- Should imports be versioned (re-import to get new cards added to the source label later)?
- Permission model: public share link vs. invite-only by username?

---

## 11. Stats Dashboard
**Complexity: Low–Medium | Cost: $0/month**

Show the user a summary of their learning progress.

**Scope:**
- Cards due today vs. overdue count
- Daily review count (bar chart, last 30 days)
- Retention rate per card and overall
- Streak tracker (consecutive days with at least one review)
- Cards by ease distribution (how many struggling vs. mature)

**Cost notes:** Pure DB queries — no API calls.

---

## 12. Anki Import / Export
**Complexity: Medium | Cost: $0/month**

Allow users to migrate decks in and out via `.apkg` files.

**Scope:**
- **Export:** serialize user's cards (or a label subset) into a valid `.apkg` (SQLite + media zip)
- **Import:** parse an `.apkg`, map fields to the app's schema, deduplicate against existing cards, preview before confirming
- Audio files included in the media bundle where available

**Cost notes:** No API calls. `genanki` library handles `.apkg` generation.

**Open questions:**
- Which Anki note type to target for export (Basic, Basic+Reversed)?
- Should import attempt to preserve SM-2 state from the source deck?

---

## 13. Typing / Cloze Review Mode
**Complexity: Low | Cost: $0/month**

An alternate review mode where the user types the answer instead of flipping a card.

**Scope:**
- Per-card or global toggle: flip mode vs. typing mode
- Show the prompt side; user types the target-language answer
- On submit: highlight correct/incorrect characters; reveal full card
- Grade as Again/Good based on whether the typed answer matches (fuzzy match to handle tone marks)

**Cost notes:** No API calls.

---

## 14. PWA Offline Mode
**Complexity: Medium | Cost: $0/month**

Allow review sessions to work without an internet connection on mobile.

**Scope:**
- Service worker caches the review queue (cards + audio) on load
- Reviews conducted offline are queued locally and synced on reconnect
- Visual indicator when offline; graceful degradation (no new card creation offline)
- Manifest already present or easy to add for home-screen install prompt

**Cost notes:** No API calls. Storage is bounded by the cached review queue size.

---

## 15. Pronunciation Recording
**Complexity: High | Cost: $0/month**

Let users record their own pronunciation and compare it to the TTS reference audio.

**Scope:**
- Record button on card front/back using `MediaRecorder` API
- Play back both recordings side-by-side
- Optional: waveform visualization
- Optional: phoneme-level scoring using a local speech model or Gemini audio input
- Recordings optionally saved per card for self-review history

**Cost notes:** Basic record+playback is $0. Automated scoring via Gemini audio input would use the free tier (short clips, low volume).

**Open questions:**
- Is automated scoring worth the complexity, or is subjective self-comparison enough?
- Should recordings be persisted or session-only?
