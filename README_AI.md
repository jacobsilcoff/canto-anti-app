# Cantonese Learning App — AI Context

Personal HK Cantonese learning app with two features: a translation UI and Anki-style spaced-repetition flashcards. Built for a user learning conversational Hong Kong Cantonese.

## Stack

- **Backend:** Python 3.12 + FastAPI, SQLite via `aiosqlite`
- **Translation:** Google Cloud Translation API (`yue` language code for Cantonese), free tier (500K chars/month)
- **Jyutping:** Derived from translated Chinese characters using `pycantonese`
- **TTS audio:** `edge-tts`, voice `zh-HK-HiuMaanNeural`, audio stored as BLOB in SQLite
- **SRS:** SM-2 algorithm (`srs.py`)
- **Auth:** HTTP Basic Auth middleware (password via `APP_PASSWORD` env var); skipped if unset (local dev)
- **Frontend:** Vanilla JS, two HTML pages served by FastAPI

## File Map

```
main.py          — FastAPI app, all routes, Basic Auth middleware
db.py            — SQLite operations (cards table, audio BLOB storage)
translation.py   — Google Cloud Translation API call + pycantonese jyutping
audio.py         — edge-tts generation, returns bytes
srs.py           — SM-2 spaced repetition algorithm
start.sh         — local dev launcher (loads .env, runs uvicorn --reload)
Dockerfile       — container image for Fly.io deployment
fly.toml         — Fly.io deployment config (always-on, 1GB volume)
render.yaml      — legacy Render config (kept for reference)
tests/
  test_srs.py    — SM-2 algorithm unit tests
  test_db.py     — SQLite integration tests (temp DB per test)
static/
  index.html     — Translation page (/ route)
  cards.html     — Flashcard review page (/cards route)
  style.css      — All styles, mobile-first, CSS variables for theming
data/
  cards.db       — SQLite DB (gitignored, created on first run)
```

## Database Schema

```sql
cards (
  id            INTEGER PRIMARY KEY,
  english       TEXT,
  chinese       TEXT,        -- Traditional Chinese characters (Cantonese vocab)
  jyutping      TEXT,        -- e.g. "nei5 hou2 aa3"
  audio_data    BLOB,        -- edge-tts MP3 bytes
  created_at    TEXT,
  next_review   TEXT,        -- ISO datetime, UTC
  interval_days INTEGER,     -- SM-2
  ease_factor   REAL,        -- SM-2, min 1.3
  repetitions   INTEGER      -- SM-2
)
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | / | Translation page |
| GET | /cards | Flashcard page |
| POST | /api/translate | Translate text, create card, generate audio |
| GET | /api/cards/due | Cards due for review (next_review <= now) |
| GET | /api/cards/all | All cards (for browse modal) |
| GET | /api/cards/due-count | Count of due cards (for badge) |
| GET | /api/audio/{id} | Serve card audio as audio/mpeg |
| POST | /api/cards/{id}/review | Submit SM-2 rating (again/hard/good/easy) |
| DELETE | /api/cards/{id} | Delete a card |

## Key Design Decisions

- **Google Cloud Translation API with `yue` language code** — uses the Cantonese model (not Mandarin/zh-TW), so output uses authentic Cantonese vocabulary (食 not 吃, etc.). Free 500K chars/month; set a daily quota cap in GCP Console to prevent any billing.
- **Jyutping via pycantonese** — derived locally from the returned Chinese characters. `characters_to_jyutping()` returns (chars, jyutping) pairs; syllables extracted with regex `[a-z]+[1-6]`.
- **Audio stored as BLOB in SQLite** — avoids needing a separate filesystem or object storage for cloud deployment. Keeps everything in one file.
- **Translation always creates a card** — every translate call saves to DB. No separate "save" step.
- **Three quiz faces:** english / chinese / cantonese (jyutping + audio). Randomly rotated each review.
- **Traditional Chinese only** — Google's `yue` target produces Traditional characters with Cantonese-specific vocabulary.
- **Basic Auth skipped locally** — `APP_PASSWORD` env var gates access; if unset (local dev), middleware is a no-op.

## Environment

```
GOOGLE_TRANSLATE_API_KEY=...   # Google Cloud Console, Cloud Translation API
APP_PASSWORD=...               # Protects the deployed app (any username works)
DB_PATH=...                    # default: data/cards.db
```

Copy `.env.example` → `.env` and fill in keys. Never commit `.env`.

## Running Locally

```bash
./start.sh
# opens on http://localhost:8000
# on iPhone: http://<mac-local-IP>:8000
```

## Running Tests

```bash
venv/bin/pytest tests/ -v
```

## Deployment (Fly.io)

App: `cantonese-anki-app`, region: `yyz` (Toronto), always-on (min 1 machine).
SQLite persisted on a 1GB Fly volume (`cantonese_data` → `/var/data`).

**First-time setup:**
```bash
fly volumes create cantonese_data --size 1 --region yyz
fly secrets set GOOGLE_TRANSLATE_API_KEY=... APP_PASSWORD=...
fly deploy
```

**CI/CD:** Pushing to `main` on GitHub runs tests then auto-deploys via `.github/workflows/ci.yml`.
Requires `FLY_API_TOKEN` set as a GitHub Actions secret (`fly tokens create deploy`).

## iPhone Home Screen

Open the app in Safari → Share → Add to Home Screen. Launches full-screen without browser chrome.

## Flashcard Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Space / Enter | Reveal answer |
| 1 | Again |
| 2 | Hard |
| 3 | Good |
| 4 | Easy |

## Planned / Possible Future Features

- Browse/search cards on translation page
- Stats page (streak, cards learned, retention rate)
- Import from CSV / export to Anki format
- Cantonese-only input mode (draw characters)
- Multiple card decks / tags
