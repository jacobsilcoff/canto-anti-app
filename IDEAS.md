# Feature Ideas & Backlog

Complexity ratings: **Low** (days), **Medium** (1–2 weeks), **High** (weeks+)

**Cost baseline:** Compute runs on Oracle Cloud Free Tier (4 OCPU ARM, 24 GB RAM) — $0. Gemini 2.5 Flash Lite via AI Studio free tier (1,500 req/day) — $0. `edge-tts` is free. At small family/friend scale (~5–15 active users) virtually everything stays within free tiers. Costs noted below are what would kick in if free tiers are exceeded. Paid Gemini Flash Lite rates: input $0.075/1M tokens, output $0.30/1M tokens.

---

## ✅ Shipped

- **#1 Multi-user support** — users table, scrypt password hashing, admin web UI at `/admin`, per-user siloed cards/labels/settings. Existing cards migrated to `jsilcoff`. `APP_PASSWORD` env var seeds the initial admin on first run.
- **#2 Multi-language support** — `yue` (Traditional), `cmn` (Simplified), `fr`, `es`. Per-language Gemini prompt, per-language `edge-tts` voice, language selector on translate page, default-language per user, dynamic face labels in review.
- **#3 AI notes + ambiguity clarification** — Gemini returns usage notes auto-populated on cards, and a candidates array shown as a "Did you mean…" picker when the input is ambiguous.

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

## 8. Reader Mode
**Complexity: High** *(break into sub-tasks before implementing)*

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
