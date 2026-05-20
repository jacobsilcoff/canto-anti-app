import base64
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

import audio
import db
import srs
import translation

_APP_PASSWORD = __import__("os").getenv("APP_PASSWORD")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not _APP_PASSWORD:
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            _, password = base64.b64decode(auth[6:]).decode().split(":", 1)
            if secrets.compare_digest(password, _APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Cantonese App"'},
    )


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
    faces = await db.get_due_faces()
    return {"cards": faces, "count": len(faces)}


@app.get("/api/cards/all-faces")
async def get_all_faces():
    faces = await db.get_all_faces()
    return {"cards": faces, "count": len(faces)}


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
    face: str     # "english" | "chinese" | "cantonese"


@app.post("/api/cards/{card_id}/review")
async def review_card(card_id: int, req: ReviewRequest):
    if req.quality not in ("again", "hard", "good", "easy"):
        raise HTTPException(400, "quality must be again/hard/good/easy")
    if req.face not in ("english", "chinese", "cantonese"):
        raise HTTPException(400, "face must be english/chinese/cantonese")
    card = await db.get_card(card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    face_state = await db.get_face_state(card_id, req.face)
    if not face_state:
        raise HTTPException(404, "Face not found")
    new_state = srs.update(face_state, req.quality)
    await db.update_face_review(card_id, req.face, new_state)
    return {"success": True, **new_state}


class UpdateCardRequest(BaseModel):
    english: str
    chinese: str
    jyutping: str


@app.put("/api/cards/{card_id}")
async def update_card(card_id: int, req: UpdateCardRequest):
    existing = await db.get_card(card_id)
    if not existing:
        raise HTTPException(404, "Card not found")
    audio_data = None
    if req.chinese.strip() != existing["chinese"]:
        audio_data = await audio.generate(req.chinese.strip())
    await db.update_card(card_id, req.english.strip(), req.chinese.strip(), req.jyutping.strip(), audio_data)
    return {"success": True}


@app.delete("/api/cards/{card_id}")
async def delete_card(card_id: int):
    await db.delete_card(card_id)
    return {"success": True}
