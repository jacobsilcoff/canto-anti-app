# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A self-hosted multi-user Anki-style spaced repetition (SRS) flashcard app for language learning. Users translate words/phrases via Gemini AI and study them with SM-2 scheduling. Deployed on Oracle Cloud Free Tier via Docker + Caddy.

**Deployment:** `git push` is all that's needed — the server auto-deploys on push. Do NOT SSH in manually to pull/rebuild.

SSH to production (only if debugging): `ssh -i ssh-key-2026-05-26.key ubuntu@40.233.111.173`

## Commands

```bash
# Run locally
docker compose up

# Or without Docker (requires .env with GEMINI_API_KEY, APP_PASSWORD, etc.)
source venv/bin/activate
uvicorn main:app --reload

# Run all tests
venv/bin/pytest tests/ -v

# Run a single test file
venv/bin/pytest tests/test_db.py -v

# Run a single test
venv/bin/pytest tests/test_srs.py::test_ease_floor -v
```

## Architecture

**Stack:** FastAPI + aiosqlite (backend), SQLite (data), Gemini 2.5 Flash Lite (translation), edge-tts (audio), Vanilla JS (frontend), Caddy (reverse proxy + HTTPS).

### Layer overview

| File | Role |
|------|------|
| `main.py` | All FastAPI routes, auth middleware, session management |
| `db.py` | All DB access — schema, migrations, CRUD. Every function takes `user_id` for isolation |
| `translation.py` | Gemini prompt construction, JSON parsing, retry logic; `LANG_INFO` + `SCRIPT_BY_LANG` language registry |
| `audio.py` | edge-tts wrapper; returns MP3 bytes; `VOICES` map |
| `srs.py` | SM-2 with sub-day learning steps; pure/stateless — takes card state, returns new state |
| `tokenizer.py` | Reader word-segmentation (CJK via jieba/pycantonese, Thai TBD, else alphabetic regex incl. Devanagari/Telugu/Hangul) + offline romanization for ruby |
| `auth.py` | scrypt password hashing + timing-safe verification |
| `learning.py` | AI Learning Path — unit-plan generation + unified micro-lesson authoring (teach blocks + drills together) + deterministic drill assembly/validation |
| `grammar.py` | Reliable verb conjugation engine (French present) — rules + curated irregulars; an independent oracle, never trusts the LLM |
| `grammar_lessons.py` | Legacy per-concept grammar generator (shared `concept_content` cache); **no longer called by the lesson route** — `learning.py` reuses its block/cloze helpers + `GENERATION_MODEL` |
| `foundations.py` | Curated script/pronunciation module (Hangul jamo engine); gates non-Latin scripts |

### Database schema

- **cards** — source_text, target_text, romanization, target_lang, audio_data (BLOB), notes, priority (1–5), tutor_flag, suspended
- **card_faces** — one row per face per card (`source`, `target`, `pronunciation`); each face has independent SM-2 state (next_review, interval_days, ease_factor, repetitions, first_seen_date, learning_step). `learning_step` non-NULL = card is in (re)learning with sub-day steps; NULL = graduated review card.
- **labels / card_labels** — per-user tags; many-to-many with cards
- **users** — scrypt-hashed passwords, is_admin flag
- **user_settings** — key-value store (new_cards_per_day, default_target_lang, learner_profile, …)
- **courses / course_units / course_lessons / course_concepts** — per-user AI Learning Path. `courses.active_plan` = JSON outline of the in-progress unit (`{title, objective, summary, concepts:[...], cursor}`), NULL between units. `course_lessons.content` = the authored `{segments:[...]}` (set at creation, since lessons are authored one at a time). Units still close reactively (`close_unit` back-assigns `unit_id` when a plan is exhausted). `course_concepts` registers only concepts actually taught.
- **concept_mastery** — `(user_id, lang, concept_key, correct, total, last_seen)`. Incremented each time the learner completes a lesson (first-pass drill outcomes only). Fed back to the unit planner: weak concepts (≥3 attempts, <70% accuracy) are surfaced in the prompt so the planner can weave in extra practice.
- **concept_content** — `(lang, concept_key)` → legacy shared grammar artifact; **retained but unused** by the new lesson path.

Per-face SRS is the central design: each card has 3 independently scheduled faces so recognition and production are practiced separately. New words are **staggered** — only the primary `target` face is introduced first; `source`/`pronunciation` unlock once the primary graduates (see `db.get_study_session`).

### Auth & sessions

Sessions are in-memory (`_sessions` dict in `main.py`): token → (user_id, expiry). Auth middleware runs on every request; unauthenticated HTML requests redirect to `/login`, API requests get 401. Sessions expire after 30 days and are purged on next login.

### SRS scheduling (`srs.update`)

SM-2 with sub-day **learning steps** (`LEARNING_STEPS_MIN = [1, 10]` minutes). A new or lapsed card walks the steps before graduating to a day-level interval, so "again" reschedules in ~1 min (reappears the same session, re-queued client-side) instead of vanishing for a day. "Easy" graduates straight to 4 days; review-card hard/good/easy intervals are differentiated. Pure/stateless — pass `learning_step`/`first_seen_date`/etc. in, get the new state back.

### Study session logic (`db.get_study_session`)

Returns due review faces (next_review ≤ now) + new faces up to the daily cap (default 20 new/day, `new_cards_per_day` setting). New faces are **staggered**: a brand-new word only offers its `PRIMARY_FACE` (`target`); the other faces become eligible once the primary has graduated (`learning_step IS NULL AND first_seen_date IS NOT NULL`). New faces ordered by priority DESC then id ASC. `db.get_due_count` applies the same staggering gate so the badge matches.

### Translation flow

`POST /api/translate` → `translation.translate()` builds a language-specific Gemini prompt → parses JSON response into up to 3 candidates (for ambiguous inputs) with target_text, romanization, notes, priority. `POST /api/cards` then generates audio via edge-tts and stores everything including the MP3 BLOB.

### AI Learning Path — unit-plan-first micro-lessons

Language-agnostic — works for every `LANG_INFO` language (native script + per-language romanization via `tokenizer`; the `grammar.py` conjugation oracle only engages for French). Two-level adaptive generation, all in `learning.py`, orchestrated by `main._author_next_lesson` (the per-lesson helper) which `main.next_lesson` loops `count` times (1–6; the UI's "Generate" button batches `BATCH_AHEAD`=5 so the learner can browse ahead). Each looped call re-reads context, so a batch builds on the lessons just authored:

1. **Unit plan** (`generate_unit_plan`) — once per unit. One LLM call drafts a coherent *chapter*: an ordered list of 6–10 concepts (vocab + grammar **interleaved**, foundational first). Stored as JSON on `courses.active_plan` with a `cursor`. **Coherence lives here** — the unit is the chapter; each lesson is a micro-step within it.
2. **Micro-lesson** (`author_lesson`) — once per lesson. One LLM call authors the WHOLE small lesson (teach blocks AND drills) for the next **1–2 concepts** from the plan (`main._next_batch`: a grammar concept teaches alone; two consecutive vocab concepts pair up). Grammar and vocab are **not** segregated — the model sees one palette and picks what the point needs. This unified authoring is what makes teach and practice cohere.

Principle **liberal in what you SHOW, strict in what you GRADE**:

- **Teach blocks** are authored freely as an ordered list of typed blocks (`prose`, `table`, `examples`, `contrast`, `note`) — a page of a textbook. Rendered client-side by `renderBlock` (learn.html); romanization on every cell is recomputed by us. **No hardcoded language-specific presentation, no raw HTML.**
- **Drills** are authored as `{correct answer + distractors}` (a few constrained kinds: recognition / production / listening / cloze / reorder / match) — **never an answer index**. We assemble the graded exercise ourselves (`assemble_lesson` → `_assemble_drill`): place the known-correct option, shuffle, index. So the answer key is **correct by construction**. Free non-LLM oracles still guard graded items: **romanization** via `tokenizer.romanize_text` (never the model), and **French present-tense** cloze answers/options via `grammar.py` (`_conj_cloze`, fires only when the model tags `verb`/`person` and its answer is a real paradigm cell; else `_free_cloze`). Drills that can't be validated are **dropped**.
- **Untaught-word glosses**: `author_lesson` is passed the `taught` concept registry; for `reorder` drills the model returns an optional `glossary` `[{token, gloss}]` for helper words the learner hasn't been taught (short English, or a POS abbrev like PRT/CL for function words). `_assemble_drill` filters it to `{token: gloss}` for real tiles only; the word-bank renderer shows the gloss as a third line under each tile (jyutping ruby on top, native char, English gloss beneath). **Word-bank grading compares `tile.dataset.tok` (the raw token), never `textContent`** — ruby annotation would otherwise mangle CJK tokens (`你好` → `你nei5好hou2`) and fail every correct answer. Tiles leave a frozen-size empty `.wb-slot` placeholder in the bank when moved (Duolingo-style; no reflow).

A real, hand-written **few-shot golden example** (`examples/lesson_example.json`, deliberately Spanish so it never leaks answers into other targets) is injected into the author prompt — edit it to steer lesson STYLE implicitly. Default model is the cheap reader model; an **admin-only** `lesson_premium` user-setting runs the whole pipeline (plan + author) on `grammar_lessons.GENERATION_MODEL` (default `gemini-2.5-pro`) to compare quality.

Stored lesson `content` = `{"segments":[{"teach":{"intro","blocks":[...]}, "exercises":[...drills...]}]}` (one teach→drill segment per micro-lesson; the player still supports multi-segment + end-of-lesson mistake review).

`grammar_lessons.py` (the older per-concept generator that produced a shared `concept_content` artifact) is **no longer called by the lesson route** — only its block/cloze helpers (`_clean_block`, `_conj_cloze`, `_free_cloze`) and `GENERATION_MODEL` are reused by `learning.py`. The `concept_content` table is retained but unused by the new path.

NOT yet built (deferred): per-concept **mastery ledger** + free-text `learner_profile` feeding the planner (the substrate for a future weakness-detecting chatbot); first-class up-front **unit rows** with a visible roadmap (currently units still close reactively when a plan is exhausted, via `close_unit`); controlled-vocab **enrichment** from the user's SRS deck; more tenses/languages; error-correction drills.

### Multi-language support

`translation.LANG_INFO` is the language registry — maps codes to per-language config (name, flag, script, romanization scheme, frequency scale, Gemini prompt rules). Supported: `yue cmn` (Chinese), `fr es de it pt tl ms id` (Latin), `ko hi te` (Hangul/Devanagari/Telugu). `/api/languages` derives the frontend language list from it, so the settings dropdown, language pill, and onboarding update automatically.

**Adding a Latin-script language** = an entry in `LANG_INFO` + a voice in `audio.VOICES`. That's it.

**Adding a non-Latin script** also requires:
- `translation.SCRIPT_BY_LANG[code] = "<family>"` (machine script key; default `"latin"`). Exposed as `script_family` in `/api/languages`.
- `tokenizer.py`: if space-delimited, add the script's Unicode ranges to `_ALPHA` (mind combining marks and which punctuation must stay separate); if no spaces (Thai/CJK), add a dictionary-segmentation branch in `tokenize`. Add an offline romanizer branch in `romanize_words` for reader ruby.
- Frontend font: a `--<family>-font` CSS var + `.script-<family>` rule in `style.css`, the Google-Fonts `<link>` in the 4 rendering pages (cards/reader/index/welcome), and add the family to the `script-*` reset lists in the `applyScript` helpers + the inline-edit/onboarding font maps.

The `logographic` flag (`romanization is not None`) controls *whether a romanization face is shown*; the **`--script-font` CSS variable** (set via a `script-<family>` container class) controls *which font renders* — these are decoupled. Never reintroduce a hardcoded `.is-chinese` font assumption for non-CJK scripts.

### Multi-user isolation

Every `db.py` function filters by `user_id`. Admin bootstrap (`db.bootstrap_admin`) migrates any legacy single-user data to the configured admin username on first run.

## Feature Tracking & doc upkeep

Ideas and backlog live in [`IDEAS.md`](IDEAS.md). These are **standing rules** — do them automatically, without being asked:

- **New ideas that come up** (even incidentally) → add to `IDEAS.md` with a complexity/cost estimate.
- **Whenever a feature ships** → move it to the `## ✅ Shipped` section at the top of `IDEAS.md` with a one-line summary. Do this every time, as part of finishing the work.
- **Keep this file (`CLAUDE.md`) current** → when a change alters the architecture, schema, language registry, or a documented convention here, update the relevant section in the same change so this file never drifts from the code.

## Code Conventions

- All DB access goes through `db.py`.
- Keep API responses lean — no fields the frontend doesn't use.
- `srs.py` is pure/stateless — pass state in, get new state back, no side effects.

## UI / CSS Conventions

**Selects / dropdowns**  
Always use the `.settings-select` class for `<select>` elements (defined in `style.css`). It applies `appearance: none; -webkit-appearance: none;` with a custom SVG chevron, consistent border/radius, and no system drop-shadow. Never use a raw `<select>` without this class — browsers add ugly system styling (box-shadow, OS-native arrow) that breaks visual consistency.

**Nav collapse breakpoint**  
The desktop nav collapses to the hamburger at **760 px** (`@media (max-width: 759px)`). The language pill's full name (`.lang-pill-name`) is hidden below **900 px** — a wider range than the nav collapse — so the h1 block stays compact (~135 px) at every width where the desktop nav is visible. Above 900 px the full name fits with ~115 px to spare. Do not lower either threshold without re-checking the header arithmetic: h1 width + nav width must be ≤ viewport − 32 px at the collapse point.

**Shadows**  
Use `var(--shadow)` (defined in the CSS variables) for card/surface elevation. Avoid hardcoded `box-shadow` values on interactive elements — the existing `var(--shadow)` token and the few modal-specific values in `style.css` cover all cases.
