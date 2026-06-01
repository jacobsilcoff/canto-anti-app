# Feature Ideas & Backlog

## ✅ Shipped

- **Subscription billing + AI usage metering** — Stripe-hosted Checkout + Customer Portal (no card data on-server). Plans: Free (30 shared-key AI calls/mo), Pro ($5/mo, 600/mo); own-key/admin/granted-friend accounts are unlimited & unmetered. Quota enforced + metered centrally in `_resolve_gemini`; monthly `usage_counters` reset automatically by `YYYY-MM` period key. Webhook (`/api/webhooks/stripe`, signature-verified) syncs `users.plan`. Settings page shows a plan/usage card with upgrade + manage buttons.
- **Cache-busting / auto asset versioning** — Startup content-hash of `static/` produces `ASSET_VERSION`; CSS/JS URLs are fingerprinted (`?v=…`), the service-worker cache name embeds the version, and HTML + `sw.js` are served `no-cache`. New deploys (= rebuild + restart) are picked up on the next normal load — no more Safari force-reload.
- **Reader (8a + 8c)** — AI-generated texts from an English prompt; tokenized reader view with familiarity highlighting (known/weak/new); tap any word to see translation or existing card data; one-tap add to deck; texts saved for re-reading.
- **Auto-labeling** — Gemini suggests 2–5 broad topic labels (food, cooking, animal…) on every translation; auto-created and assigned when a card is saved.
- **Classifiers / articles** — Translation response now includes the definite article (fr/es/de) or measure word (yue/cmn) for nouns; stored on the card and shown as a badge in the output and card list.
- **Reader story labels** — Words added from reader mode are auto-tagged with a "📖 [Title]" label; "Study vocab" button in the reader header opens a filtered study session for that text's vocabulary.
- **Vector embeddings** — Each card gets a background Gemini embedding (text-embedding-004); `GET /api/labels/suggest-cards?name=X` returns top cosine-similar unassigned cards. Label manage panel now has a "✦ Suggest" button per label.
- **Canonical card (word families)** — `canonical_card_id` FK on cards lets inflected/conjugated forms point to their base form; set via the card edit form's canonical search field; displayed as "Form of: X" in the card list.

---

Complexity ratings: **Low** (days), **Medium** (1–2 weeks), **High** (weeks+)

**Cost baseline:** Compute runs on Oracle Cloud Free Tier (4 OCPU ARM, 24 GB RAM) — $0. Gemini 2.5 Flash Lite via AI Studio free tier (1,500 req/day) — $0. `edge-tts` is free. At small family/friend scale (~5–15 active users) virtually everything stays within free tiers. Costs noted below are what would kick in if free tiers are exceeded. Paid Gemini Flash Lite rates: input $0.075/1M tokens, output $0.30/1M tokens.

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
