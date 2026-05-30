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
| `translation.py` | Gemini prompt construction, JSON parsing, retry logic |
| `audio.py` | edge-tts wrapper; returns MP3 bytes |
| `srs.py` | Pure SM-2 implementation; takes card state, returns new state |
| `auth.py` | scrypt password hashing + timing-safe verification |

### Database schema

- **cards** — source_text, target_text, romanization, target_lang, audio_data (BLOB), notes, priority (1–5), tutor_flag, suspended
- **card_faces** — one row per face per card (`source`, `target`, `pronunciation`); each face has independent SM-2 state (next_review, interval_days, ease_factor, repetitions, first_seen_date)
- **labels / card_labels** — per-user tags; many-to-many with cards
- **users** — scrypt-hashed passwords, is_admin flag
- **user_settings** — key-value store (new_cards_per_day, default_target_lang)

Per-face SRS is the central design: each card has 3 independently scheduled faces so recognition and production are practiced separately.

### Auth & sessions

Sessions are in-memory (`_sessions` dict in `main.py`): token → (user_id, expiry). Auth middleware runs on every request; unauthenticated HTML requests redirect to `/login`, API requests get 401. Sessions expire after 30 days and are purged on next login.

### Study session logic (`db.get_study_session`)

Returns due review faces (next_review ≤ now) + new faces (first_seen_date IS NULL) up to the daily cap (default 20 new/day). New faces are ordered by priority DESC then id ASC.

### Translation flow

`POST /api/translate` → `translation.translate()` builds a language-specific Gemini prompt → parses JSON response into up to 3 candidates (for ambiguous inputs) with target_text, romanization, notes, priority. `POST /api/cards` then generates audio via edge-tts and stores everything including the MP3 BLOB.

### Multi-language support

`translation.LANG_INFO` maps language codes (`yue`, `cmn`, `fr`, `es`, `de`) to per-language config: romanization scheme, script, TTS voice, Gemini prompt rules. Adding a new language means adding an entry there and a voice in `audio.VOICES`.

### Multi-user isolation

Every `db.py` function filters by `user_id`. Admin bootstrap (`db.bootstrap_admin`) migrates any legacy single-user data to the configured admin username on first run.

## Feature Tracking

Ideas and backlog live in [`IDEAS.md`](IDEAS.md). During any conversation:

- **New ideas that come up** (even incidentally) → add to `IDEAS.md` with a complexity/cost estimate.
- **Implemented features** → move to a `## ✅ Shipped` section at the top of `IDEAS.md` with a one-line summary.

## Code Conventions

- All DB access goes through `db.py`.
- Keep API responses lean — no fields the frontend doesn't use.
- `srs.py` is pure/stateless — pass state in, get new state back, no side effects.
