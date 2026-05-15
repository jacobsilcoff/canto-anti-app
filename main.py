from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

import audio
import db
import srs
import translation


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

_static = Path("static")


def _html(name: str) -> HTMLResponse:
    return HTMLResponse((_static / name).read_text())


# ── Pages ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return _html("index.html")


@app.get("/cards", response_class=HTMLResponse)
async def cards_page():
    return _html("cards.html")


# ── Translation ───────────────────────────────────────────────────────────────

class TranslateRequest(BaseModel):
    text: str
    source_lang: str  # "english" | "cantonese"


@app.post("/api/translate")
async def translate(req: TranslateRequest):
    if not req.text.strip():
        raise HTTPException(400, "Text is empty")

    result = await translation.translate(req.text.strip(), req.source_lang)
    audio_data = await audio.generate(result["chinese"])
    card_id = await db.create_card(
        english=result["english"],
        chinese=result["chinese"],
        jyutping=result["jyutping"],
        audio_data=audio_data,
    )
    return {**result, "card_id": card_id}


# ── Cards ─────────────────────────────────────────────────────────────────────

@app.get("/api/cards/due")
async def get_due_cards():
    cards = await db.get_due_cards()
    return {"cards": cards, "count": len(cards)}


@app.get("/api/cards/all")
async def get_all_cards():
    cards = await db.get_all_cards()
    return {"cards": cards}


@app.get("/api/cards/due-count")
async def due_count():
    return {"count": await db.get_due_count()}


@app.get("/api/audio/{card_id}")
async def get_audio(card_id: int):
    data = await db.get_audio(card_id)
    if not data:
        raise HTTPException(404, "Audio not found")
    return Response(content=bytes(data), media_type="audio/mpeg")


class ReviewRequest(BaseModel):
    quality: str  # "again" | "hard" | "good" | "easy"


@app.post("/api/cards/{card_id}/review")
async def review_card(card_id: int, req: ReviewRequest):
    if req.quality not in ("again", "hard", "good", "easy"):
        raise HTTPException(400, "quality must be again/hard/good/easy")
    card = await db.get_card(card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    new_state = srs.update(card, req.quality)
    await db.update_card_review(card_id, new_state)
    return {"success": True, **new_state}


@app.delete("/api/cards/{card_id}")
async def delete_card(card_id: int):
    await db.delete_card(card_id)
    return {"success": True}
