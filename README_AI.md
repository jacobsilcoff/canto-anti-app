# Cantonese Learning App — AI Context

Personal HK Cantonese learning app with two features: a translation UI and Anki-style spaced-repetition flashcards. Built for a user learning conversational Hong Kong Cantonese.

## Stack

- **Backend:** Python 3.12 + FastAPI, SQLite via `aiosqlite`
- **Translation:** Gemini 2.5 Flash Lite (`google-genai` SDK), free tier via Google AI Studio
- **Jyutping:** Provided by Gemini in the JSON translation response
- **TTS audio:** `edge-tts`, voice `zh-HK-HiuMaanNeural`, stored as BLOB in SQLite
- **SRS:** SM-2 algorithm (`srs.py`)
- **Auth:** Cookie-based session auth + HTTP Basic Auth fallback; password via `APP_PASSWORD` env var; skipped if unset (local dev)
- **Frontend:** Vanilla JS, served by FastAPI

## File Map

```
main.py          — FastAPI app, all routes, auth middleware
db.py            — SQLite operations (cards + card_faces tables, audio BLOB)
translation.py   — Gemini translation + jyutping
audio.py         — edge-tts generation, returns bytes
srs.py           — SM-2 spaced repetition algorithm
import_vocab.py  — Bulk-import extracted_vocab.json into the DB (Gemini for jyutping)
start.sh         — local dev launcher (loads .env, runs uvicorn --reload)
Dockerfile       — container image
docker-compose.yml — production stack (app + Caddy for automatic HTTPS)
Caddyfile        — Caddy reverse proxy config, domain from $DOMAIN env var
tests/
  test_srs.py    — SM-2 algorithm unit tests
  test_db.py     — SQLite integration tests (temp DB per test)
static/
  index.html     — Translation page (/ route)
  cards.html     — Flashcard review page (/cards route)
  login.html     — Login page
  style.css      — All styles, mobile-first, CSS variables for theming
  manifest.json  — PWA manifest
  sw.js          — Service worker (cache-first for assets, network-first for nav)
  icons/         — PWA icons (192px, 512px)
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
  created_at    TEXT
)

card_faces (
  id            INTEGER PRIMARY KEY,
  card_id       INTEGER,     -- FK → cards.id
  face          TEXT,        -- "english" | "chinese" | "cantonese"
  next_review   TEXT,        -- ISO datetime, UTC
  interval_days INTEGER,     -- SM-2
  ease_factor   REAL,        -- SM-2, min 1.3
  repetitions   INTEGER      -- SM-2
)
```

Each card has 3 faces (english, chinese, cantonese) tracked independently in `card_faces`.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | / | Translation page |
| GET | /cards | Flashcard page |
| GET | /login | Login page |
| POST | /api/login | Authenticate, set session cookie |
| POST | /api/logout | Clear session cookie |
| POST | /api/translate | Translate text, create card, generate audio |
| GET | /api/cards/due | Card faces due for review |
| GET | /api/cards/all-faces | All card faces |
| GET | /api/cards/all | All cards |
| GET | /api/cards/due-count | Count of due faces |
| GET | /api/audio/{id} | Serve card audio as audio/mpeg |
| POST | /api/cards/{id}/review | Submit SM-2 rating (again/hard/good/easy) |
| PUT | /api/cards/{id} | Update card fields |
| DELETE | /api/cards/{id} | Delete a card |

## Environment

```
GEMINI_API_KEY=...   # Google AI Studio — aistudio.google.com/apikey
APP_PASSWORD=...     # Protects the deployed app
DB_PATH=...          # default: data/cards.db (overridden to /data/cards.db in Docker)
DOMAIN=...           # Your domain for HTTPS, e.g. yourname.duckdns.org
```

Copy `.env.example` → `.env` and fill in values. Never commit `.env`.

## Running Locally

```bash
./start.sh
# opens on http://localhost:8000
```

## Running Tests

```bash
venv/bin/pytest tests/ -v
```

## Deployment (Oracle Cloud Free Tier)

Hosted on an Oracle A1 ARM VM (always free). Docker Compose runs the app + Caddy (automatic HTTPS via Let's Encrypt).

```bash
# On the VM
git clone https://github.com/jacobsilcoff/canto-anti-app.git
cd canto-anti-app
cp .env.example .env && nano .env   # fill in GEMINI_API_KEY, APP_PASSWORD, DOMAIN
docker compose up -d --build
```

**CI/CD:** Pushing to `main` runs tests (`ci.yml`) then auto-deploys to the VM via SSH (`deploy.yml`). Requires `VM_HOST` and `VM_SSH_KEY` set as GitHub Actions secrets.

## PWA / iPhone Home Screen

Open the app in Safari → Share → Add to Home Screen. Launches full-screen with the HK skyline icon.

## Flashcard Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Space / Enter | Reveal answer |
| 1 | Again |
| 2 | Hard |
| 3 | Good |
| 4 | Easy |
