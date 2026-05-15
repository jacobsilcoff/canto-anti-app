# Cantonese Learning App — AI Context

Personal HK Cantonese learning app with two features: a translation UI and Anki-style spaced-repetition flashcards. Built for a user learning conversational Hong Kong Cantonese.

## Stack

- **Backend:** Python 3.12 + FastAPI, SQLite via `aiosqlite`
- **Translation:** Groq API (`llama-3.3-70b-versatile`), free tier, no CC needed
- **TTS audio:** `edge-tts`, voice `zh-HK-HiuMaanNeural`, audio stored as BLOB in SQLite
- **Jyutping:** Groq provides it in the JSON translation response
- **SRS:** SM-2 algorithm (`srs.py`)
- **Frontend:** Vanilla JS, two HTML pages served by FastAPI

## File Map

```
main.py          — FastAPI app, all routes
db.py            — SQLite operations (cards table, audio BLOB storage)
translation.py   — Groq API call, returns {english, chinese, jyutping}
audio.py         — edge-tts generation, returns bytes
srs.py           — SM-2 spaced repetition algorithm
start.sh         — local dev launcher (loads .env, runs uvicorn --reload)
render.yaml      — Render deployment config ($1/month disk for persistence)
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
  chinese       TEXT,        -- Traditional Chinese characters
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

- **Audio stored as BLOB in SQLite** — avoids needing a separate filesystem or object storage for cloud deployment. Keeps everything in one file.
- **Translation always creates a card** — every translate call saves to DB. No separate "save" step.
- **Three quiz faces:** english / chinese / cantonese (jyutping + audio). Randomly rotated each review.
- **Groq not Gemini** — switched from Gemini (quota issues on new keys) to Groq free tier.
- **Traditional Chinese only** — prompt instructs Cantonese vocab (係/唔/喺 etc.), not Mandarin.

## Environment

```
GROQ_API_KEY=...   # from console.groq.com, free, no CC
DB_PATH=...        # default: data/cards.db
```

Copy `.env.example` → `.env` and fill in the key. Never commit `.env`.

## Running Locally

```bash
./start.sh
# opens on http://localhost:8000
# on iPhone: http://<mac-local-IP>:8000
```

## Deployment (Render)

Push repo → new Web Service from `render.yaml`. Set `GROQ_API_KEY` in Render dashboard. Add $1/month persistent disk to keep cards across deploys (without it, SQLite resets on each deploy).

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
