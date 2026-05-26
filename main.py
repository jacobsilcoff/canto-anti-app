import base64
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

import audio
import db
import srs
import translation

_APP_PASSWORD = __import__("os").getenv("APP_PASSWORD")

_SESSION_TTL = 30 * 86400  # 30 days in seconds
_sessions: dict[str, float] = {}  # token -> expiry timestamp

_NO_AUTH_PATHS = {"/login", "/api/login", "/manifest.json", "/sw.js"}


def _new_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + _SESSION_TTL
    return token


def _valid_session(token: str) -> bool:
    exp = _sessions.get(token)
    return exp is not None and time.time() < exp


def _purge_expired():
    now = time.time()
    expired = [t for t, exp in _sessions.items() if exp < now]
    for t in expired:
        del _sessions[t]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not _APP_PASSWORD:
        return await call_next(request)

    # Paths that are always public
    if request.url.path in _NO_AUTH_PATHS:
        return await call_next(request)

    # Cookie session (primary — used by browser/PWA)
    session_token = request.cookies.get("session")
    if session_token and _valid_session(session_token):
        return await call_next(request)

    # HTTP Basic Auth (fallback for curl/dev)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            _, password = base64.b64decode(auth[6:]).decode().split(":", 1)
            if secrets.compare_digest(password, _APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass

    # Unauthenticated — redirect HTML requests to /login, 401 everything else
    accept = request.headers.get("Accept", "")
    if "text/html" in accept:
        return Response(status_code=302, headers={"Location": "/login"})
    return Response(status_code=401, content="Unauthorized")


app.mount("/static", StaticFiles(directory="static"), name="static")

_static = Path("static")


def _html(name: str) -> HTMLResponse:
    return HTMLResponse((_static / name).read_text())


# ── PWA assets (no auth, correct MIME) ───────────────────────────────────────

@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _html("login.html")


class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
async def login(req: LoginRequest, response: Response):
    if not _APP_PASSWORD or not secrets.compare_digest(req.password, _APP_PASSWORD):
        raise HTTPException(401, "Wrong password")
    _purge_expired()
    token = _new_session()
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "session",
        token,
        max_age=_SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return response


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        _sessions.pop(token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


# ── Pages ─────────────────────────────────────────────────────────────────────

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
        # Generate and cache on first request (cards imported without audio)
        card = await db.get_card(card_id)
        if not card:
            raise HTTPException(404, "Audio not found")
        data = await audio.generate(card["chinese"])
        await db.set_audio(card_id, data)
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
