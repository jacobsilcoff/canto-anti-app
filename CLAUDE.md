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
| `learning.py` | AI Learning Path — CEFR curriculum generation + deterministic segmented-lesson assembly (vocab + grammar-first segments) |
| `grammar.py` | Reliable verb conjugation engine (French present) — rules + curated irregulars; an independent oracle, never trusts the LLM |
| `grammar_lessons.py` | Generator + **critic** pipeline producing the VERIFIED canonical grammar artifact (explicit rule + minimal pairs + cloze/reorder drills) per `(lang, concept)` |
| `foundations.py` | Curated script/pronunciation module (Hangul jamo engine); gates non-Latin scripts |

### Database schema

- **cards** — source_text, target_text, romanization, target_lang, audio_data (BLOB), notes, priority (1–5), tutor_flag, suspended
- **card_faces** — one row per face per card (`source`, `target`, `pronunciation`); each face has independent SM-2 state (next_review, interval_days, ease_factor, repetitions, first_seen_date, learning_step). `learning_step` non-NULL = card is in (re)learning with sub-day steps; NULL = graduated review card.
- **labels / card_labels** — per-user tags; many-to-many with cards
- **users** — scrypt-hashed passwords, is_admin flag
- **user_settings** — key-value store (new_cards_per_day, default_target_lang)
- **courses / course_units / course_lessons / course_concepts / course_progress** — per-user AI Learning Path (curriculum skeleton; `course_lessons.content` = cached generated exercises, NULL until first open)
- **concept_content** — `(lang, concept_key)` → verified canonical grammar artifact, **shared across users** (not user-scoped); the expensive-once generator+critic output

Per-face SRS is the central design: each card has 3 independently scheduled faces so recognition and production are practiced separately. New words are **staggered** — only the primary `target` face is introduced first; `source`/`pronunciation` unlock once the primary graduates (see `db.get_study_session`).

### Auth & sessions

Sessions are in-memory (`_sessions` dict in `main.py`): token → (user_id, expiry). Auth middleware runs on every request; unauthenticated HTML requests redirect to `/login`, API requests get 401. Sessions expire after 30 days and are purged on next login.

### SRS scheduling (`srs.update`)

SM-2 with sub-day **learning steps** (`LEARNING_STEPS_MIN = [1, 10]` minutes). A new or lapsed card walks the steps before graduating to a day-level interval, so "again" reschedules in ~1 min (reappears the same session, re-queued client-side) instead of vanishing for a day. "Easy" graduates straight to 4 days; review-card hard/good/easy intervals are differentiated. Pure/stateless — pass `learning_step`/`first_seen_date`/etc. in, get the new state back.

### Study session logic (`db.get_study_session`)

Returns due review faces (next_review ≤ now) + new faces up to the daily cap (default 20 new/day, `new_cards_per_day` setting). New faces are **staggered**: a brand-new word only offers its `PRIMARY_FACE` (`target`); the other faces become eligible once the primary has graduated (`learning_step IS NULL AND first_seen_date IS NOT NULL`). New faces ordered by priority DESC then id ASC. `db.get_due_count` applies the same staggering gate so the badge matches.

### Translation flow

`POST /api/translate` → `translation.translate()` builds a language-specific Gemini prompt → parses JSON response into up to 3 candidates (for ambiguous inputs) with target_text, romanization, notes, priority. `POST /api/cards` then generates audio via edge-tts and stores everything including the MP3 BLOB.

### AI Learning Path — grammar-first lessons

A lesson is assembled deterministically from a curriculum's concepts (`learning.generate_lesson`). **Vocab** concepts → recognition/production/listening/match segments. **Grammar** concepts → a dedicated **grammar-first segment** (explicit English rule + minimal pairs in the teach screen, then cloze/reorder drills) — kept OUT of the vocab pipeline so we never ask an ambiguous "how do you say X" where several forms fit.

Grammar content is produced by a **generator + critic** pipeline (`grammar_lessons.generate_grammar_content`): a generator LLM writes the rule, minimal pairs, drills, and optional reference **tables**; a *critic* LLM independently re-derives and judges each item, and **rejected items are dropped** (thinner-but-correct over complete-but-wrong). Where a free non-LLM oracle exists it overrides the LLM — **romanization** is always recomputed (`tokenizer.romanize_text`, never trusted from the model); **French present-tense** cloze answers/options come from `grammar.py`; and a verb concept's **conjugation table** (`grammar.conjugation_table`) is engine-computed, not generated (the generator is told NOT to make conjugation tables — only *other* paradigms like article/pronoun grids, which the critic verifies cell-by-cell). The conjugation override only fires when the model's own cloze answer is a real paradigm cell, and reflexive/pronominal verbs are refused (`grammar.is_reflexive`) since we don't model the reflexive paradigm. The verified artifact (`explain`, `tables`, `minimal_pairs`, `exercises`) is cached **shared across users** in `concept_content (lang, concept_key)` — generation is the expensive step, replay is cheap. Drills reuse the existing `choice` (cloze, minimal-pair recognition) and `word_bank` (reorder) renderers; the teach screen renders `explain` + tables + minimal-pair contrast blocks. Orchestrated in `main._ensure_grammar_content` (cache-miss → generate+verify+store) before `generate_lesson`.

NOT yet built (deferred): controlled-vocab **enrichment** (filling drill slots from the user's known SRS deck via typed slots), a grammar-**DAG** syllabus, more tenses/languages, and error-correction drills.

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
