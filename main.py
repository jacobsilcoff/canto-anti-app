import asyncio
import hashlib
import json
import math
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

import audio
import auth
import db
import srs
import tokenizer
import translation

_BOOTSTRAP_PASSWORD = os.getenv("APP_PASSWORD")
_BOOTSTRAP_USERNAME = os.getenv("APP_ADMIN_USERNAME", "jsilcoff")

_SESSION_TTL = 30 * 86400  # 30 days
# token -> (user_id, expiry timestamp)
_sessions: dict[str, tuple[int, float]] = {}

_NO_AUTH_PATHS = {"/login", "/api/login", "/manifest.json", "/sw.js"}


def _new_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    _sessions[token] = (user_id, time.time() + _SESSION_TTL)
    return token


def _session_user_id(token: str) -> int | None:
    entry = _sessions.get(token)
    if not entry:
        return None
    user_id, exp = entry
    if time.time() >= exp:
        _sessions.pop(token, None)
        return None
    return user_id


def _purge_expired():
    now = time.time()
    for t in [t for t, (_, exp) in _sessions.items() if exp < now]:
        del _sessions[t]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    if _BOOTSTRAP_PASSWORD:
        await db.bootstrap_admin(_BOOTSTRAP_USERNAME, auth.hash_password(_BOOTSTRAP_PASSWORD))
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path in _NO_AUTH_PATHS or request.url.path.startswith("/static/"):
        return await call_next(request)

    user_id = None
    token = request.cookies.get("session")
    if token:
        user_id = _session_user_id(token)

    if user_id is None:
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            return Response(status_code=302, headers={"Location": "/login"})
        return Response(status_code=401, content="Unauthorized")

    request.state.user_id = user_id
    return await call_next(request)


async def current_user(request: Request) -> dict:
    user_id = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(401, "Unauthorized")
    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(401, "Unauthorized")
    return user


async def current_admin(user: dict = Depends(current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")
    return user


app.mount("/static", StaticFiles(directory="static"), name="static")

_static = Path("static")

APP_NAME = "廣東卡"
_APP_NAME_HTML = '廣東<span class="logo-accent">卡</span>'


def _compute_asset_version() -> str:
    """Content hash of all static files. Changes only when an asset changes,
    so deploys bust browser/service-worker caches without manual version bumps."""
    h = hashlib.sha256()
    for p in sorted(_static.rglob("*")):
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


ASSET_VERSION = _compute_asset_version()


def _html(name: str) -> HTMLResponse:
    content = (_static / name).read_text()
    content = content.replace("{{APP_NAME}}", APP_NAME)
    content = content.replace("{{APP_NAME_HTML}}", _APP_NAME_HTML)
    content = content.replace("/static/style.css", f"/static/style.css?v={ASSET_VERSION}")
    content = content.replace("/static/label-picker.js", f"/static/label-picker.js?v={ASSET_VERSION}")
    content = content.replace("{{ASSET_VERSION}}", ASSET_VERSION)
    content = content.replace(
        "</head>",
        f'<script>window.__VERSION__="{ASSET_VERSION}"</script></head>',
        1,
    )
    # no-cache forces Safari to revalidate the HTML, so it always sees the
    # current fingerprinted asset URLs instead of serving a stale page.
    return HTMLResponse(content, headers={"Cache-Control": "no-cache"})


# ── PWA assets ────────────────────────────────────────────────────────────────

@app.get("/sw.js")
async def service_worker():
    content = (_static / "sw.js").read_text().replace("{{VERSION}}", ASSET_VERSION)
    return Response(
        content,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _html("login.html")


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(req: LoginRequest):
    user = await db.get_user_by_username(req.username.strip())
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Wrong username or password")
    _purge_expired()
    token = _new_session(user["id"])
    response = JSONResponse({"ok": True, "user": {"username": user["username"], "is_admin": bool(user["is_admin"])}})
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
async def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        _sessions.pop(token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return {
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "native_lang": user.get("native_lang", "en"),
    }


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return _html("index.html")


@app.get("/cards", response_class=HTMLResponse)
async def cards_page():
    return _html("cards.html")


@app.get("/reader", response_class=HTMLResponse)
async def reader_page():
    return _html("reader.html")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return _html("settings.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(user: dict = Depends(current_admin)):
    return _html("admin.html")


# ── Translation ───────────────────────────────────────────────────────────────

class TranslateRequest(BaseModel):
    text: str
    target_lang: str
    source_is_target: bool = False  # True if user typed in target_lang and wants English
    context: str | None = None


@app.post("/api/translate")
async def translate_endpoint(req: TranslateRequest, user: dict = Depends(current_user)):
    if not req.text.strip():
        raise HTTPException(400, "Text is empty")
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, f"Unsupported target language: {req.target_lang}")
    result = await translation.translate(
        req.text.strip(),
        req.target_lang,
        source_is_target=req.source_is_target,
        context=(req.context or "").strip(),
    )
    return {
        "target_lang": req.target_lang,
        "candidates": result["candidates"],
        "priority": result["priority"],
        "suggested_labels": result.get("suggested_labels", []),
        "classifier": result.get("classifier", ""),
        "cefr_level": result.get("cefr_level"),
    }


class CreateCardRequest(BaseModel):
    source_text: str
    target_text: str
    romanization: str = ""
    target_lang: str
    notes: str | None = None
    priority: int = 3
    label_ids: list[int] | None = None
    suggested_labels: list[str] | None = None
    classifier: str = ""
    canonical_card_id: int | None = None
    reader_text_id: int | None = None
    cefr_level: str | None = None


async def _generate_and_store_embedding(card_id: int, text: str):
    embedding = await translation.get_embedding(text)
    if embedding:
        await db.update_card_embedding(card_id, json.dumps(embedding))


@app.post("/api/cards")
async def create_card(
    req: CreateCardRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
):
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, f"Unsupported target language: {req.target_lang}")
    target_text = req.target_text.strip()
    if not target_text or not req.source_text.strip():
        raise HTTPException(400, "source_text and target_text are required")
    audio_data = await audio.generate(target_text, req.target_lang)
    notes = (req.notes or "").strip() or None

    # Collect extra label ids — story label if reader_text_id provided.
    extra_label_ids: list[int] = list(req.label_ids or [])
    if req.reader_text_id:
        story_label = await db.get_or_create_story_label(user["id"], req.reader_text_id)
        if story_label.get("id"):
            extra_label_ids.append(story_label["id"])

    card_id = await db.create_card(
        user_id=user["id"],
        source_text=req.source_text.strip(),
        target_text=target_text,
        romanization=req.romanization.strip(),
        target_lang=req.target_lang,
        audio_data=audio_data,
        notes=notes,
        label_ids=extra_label_ids,
        priority=req.priority,
        classifier=req.classifier or "",
        canonical_card_id=req.canonical_card_id,
        suggested_label_names=req.suggested_labels or [],
        cefr_level=req.cefr_level,
    )

    # Generate embedding in the background.
    embed_text = f"{req.source_text.strip()} {target_text}"
    background_tasks.add_task(_generate_and_store_embedding, card_id, embed_text)

    return {"card_id": card_id, "notes": notes, "labels": []}


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(user: dict = Depends(current_user)):
    new_cards_per_day = int(await db.get_setting(user["id"], "new_cards_per_day") or 20)
    default_target_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    auto_add_reader_vocab = (await db.get_setting(user["id"], "auto_add_reader_vocab") or "false") == "true"
    audio_show_romanization = (await db.get_setting(user["id"], "audio_show_romanization") or "true") == "true"
    return {
        "new_cards_per_day": new_cards_per_day,
        "default_target_lang": default_target_lang,
        "auto_add_reader_vocab": auto_add_reader_vocab,
        "audio_show_romanization": audio_show_romanization,
    }


class SettingsUpdate(BaseModel):
    new_cards_per_day: int | None = None
    default_target_lang: str | None = None
    auto_add_reader_vocab: bool | None = None
    audio_show_romanization: bool | None = None


@app.put("/api/settings")
async def update_settings(req: SettingsUpdate, user: dict = Depends(current_user)):
    if req.new_cards_per_day is not None:
        if not 1 <= req.new_cards_per_day <= 500:
            raise HTTPException(400, "new_cards_per_day must be 1–500")
        await db.set_setting(user["id"], "new_cards_per_day", req.new_cards_per_day)
    if req.default_target_lang is not None:
        if req.default_target_lang not in translation.LANG_INFO:
            raise HTTPException(400, "Unsupported default_target_lang")
        await db.set_setting(user["id"], "default_target_lang", req.default_target_lang)
    if req.auto_add_reader_vocab is not None:
        await db.set_setting(user["id"], "auto_add_reader_vocab", "true" if req.auto_add_reader_vocab else "false")
    if req.audio_show_romanization is not None:
        await db.set_setting(user["id"], "audio_show_romanization", "true" if req.audio_show_romanization else "false")
    return {"success": True}


# ── Cards ─────────────────────────────────────────────────────────────────────

@app.get("/api/cards/due")
async def get_due_cards(label_id: int | None = None, user: dict = Depends(current_user)):
    return await db.get_study_session(user["id"], label_id=label_id)


@app.get("/api/cards/all-faces")
async def get_all_faces(label_id: int | None = None, user: dict = Depends(current_user)):
    faces = await db.get_all_faces(user["id"], label_id=label_id)
    return {"cards": faces, "count": len(faces)}


@app.get("/api/cards/all")
async def get_all_cards(user: dict = Depends(current_user)):
    cards = await db.get_all_cards(user["id"])
    return {"cards": cards}


@app.get("/api/cards/due-count")
async def due_count(label_id: int | None = None, user: dict = Depends(current_user)):
    return {"count": await db.get_due_count(user["id"], label_id=label_id)}


@app.get("/api/cards/cefr-distribution")
async def cefr_distribution(user: dict = Depends(current_user)):
    return await db.get_cefr_distribution(user["id"])


@app.get("/api/streak")
async def get_streak(user: dict = Depends(current_user)):
    return {"streak": await db.get_streak(user["id"])}


@app.get("/api/audio/{card_id}")
async def get_audio(card_id: int, user: dict = Depends(current_user)):
    data = await db.get_audio(user["id"], card_id)
    if not data:
        card = await db.get_card(user["id"], card_id)
        if not card:
            raise HTTPException(404, "Audio not found")
        data = await audio.generate(card["target_text"], card.get("target_lang", "yue"))
        await db.set_audio(user["id"], card_id, data)
    return Response(content=bytes(data), media_type="audio/mpeg")


class ReviewRequest(BaseModel):
    quality: str  # "again" | "hard" | "good" | "easy"
    face: str     # "source" | "target" | "pronunciation"


@app.post("/api/cards/{card_id}/review")
async def review_card(card_id: int, req: ReviewRequest, user: dict = Depends(current_user)):
    if req.quality not in ("again", "hard", "good", "easy"):
        raise HTTPException(400, "quality must be again/hard/good/easy")
    if req.face not in db.FACES:
        raise HTTPException(400, f"face must be one of {db.FACES}")
    card = await db.get_card(user["id"], card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    face_state = await db.get_face_state(user["id"], card_id, req.face)
    if not face_state:
        raise HTTPException(404, "Face not found")
    new_state = srs.update(face_state, req.quality)
    await db.update_face_review(user["id"], card_id, req.face, new_state)
    return {"success": True, **new_state}


class UpdateCardRequest(BaseModel):
    source_text: str
    target_text: str
    romanization: str = ""
    notes: str | None = None
    label_ids: list[int] | None = None


@app.put("/api/cards/{card_id}")
async def update_card(card_id: int, req: UpdateCardRequest, user: dict = Depends(current_user)):
    existing = await db.get_card(user["id"], card_id)
    if not existing:
        raise HTTPException(404, "Card not found")
    audio_data = None
    target_text = req.target_text.strip()
    if target_text != existing["target_text"]:
        audio_data = await audio.generate(target_text, existing.get("target_lang", "yue"))
    notes = (req.notes or "").strip() or None
    await db.update_card(
        user["id"],
        card_id,
        req.source_text.strip(),
        target_text,
        req.romanization.strip(),
        audio_data=audio_data,
        notes=notes,
        label_ids=req.label_ids,
    )
    return {"success": True}


@app.delete("/api/cards/{card_id}")
async def delete_card(card_id: int, user: dict = Depends(current_user)):
    await db.delete_card(user["id"], card_id)
    return {"success": True}


class PriorityRequest(BaseModel):
    priority: int


@app.patch("/api/cards/{card_id}/priority")
async def set_priority(card_id: int, req: PriorityRequest, user: dict = Depends(current_user)):
    if not 1 <= req.priority <= 5:
        raise HTTPException(400, "priority must be 1–5")
    await db.set_card_priority(user["id"], card_id, req.priority)
    return {"success": True}


class TutorFlagRequest(BaseModel):
    flagged: bool


@app.patch("/api/cards/{card_id}/tutor-flag")
async def set_tutor_flag(card_id: int, req: TutorFlagRequest, user: dict = Depends(current_user)):
    await db.set_card_tutor_flag(user["id"], card_id, req.flagged)
    return {"success": True}


class SuspendRequest(BaseModel):
    suspended: bool


@app.patch("/api/cards/{card_id}/suspend")
async def set_suspended(card_id: int, req: SuspendRequest, user: dict = Depends(current_user)):
    await db.set_card_suspended(user["id"], card_id, req.suspended)
    return {"success": True}


@app.post("/api/cards/{card_id}/reset")
async def reset_card(card_id: int, user: dict = Depends(current_user)):
    card = await db.get_card(user["id"], card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    await db.reset_card_to_new(user["id"], card_id)
    return {"success": True}


class SetCanonicalRequest(BaseModel):
    canonical_card_id: int | None = None


@app.put("/api/cards/{card_id}/canonical")
async def set_canonical(card_id: int, req: SetCanonicalRequest, user: dict = Depends(current_user)):
    ok = await db.set_canonical_card(user["id"], card_id, req.canonical_card_id)
    if not ok:
        raise HTTPException(404, "Card not found")
    return {"success": True}


@app.get("/api/cards/{card_id}/forms")
async def get_card_forms(card_id: int, user: dict = Depends(current_user)):
    card = await db.get_card(user["id"], card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    forms = await db.get_card_forms(user["id"], card_id)
    return {"forms": forms}


# ── Labels ────────────────────────────────────────────────────────────────────

class LabelRequest(BaseModel):
    name: str


@app.get("/api/labels")
async def list_labels(user: dict = Depends(current_user)):
    return {"labels": await db.list_labels(user["id"])}


@app.post("/api/labels")
async def create_label(req: LabelRequest, user: dict = Depends(current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name is empty")
    if len(name) > 50:
        raise HTTPException(400, "Name too long (max 50 chars)")
    return await db.create_label(user["id"], name)


@app.put("/api/labels/{label_id}")
async def rename_label(label_id: int, req: LabelRequest, user: dict = Depends(current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name is empty")
    if len(name) > 50:
        raise HTTPException(400, "Name too long (max 50 chars)")
    ok = await db.rename_label(user["id"], label_id, name)
    if not ok:
        raise HTTPException(409, "A label with that name already exists")
    return {"success": True}


class LabelMergeRequest(BaseModel):
    source_ids: list[int]
    target_id: int


@app.post("/api/labels/merge")
async def merge_labels(req: LabelMergeRequest, user: dict = Depends(current_user)):
    if not req.source_ids:
        raise HTTPException(400, "No source labels provided")
    deleted = await db.merge_labels(user["id"], req.source_ids, req.target_id)
    return {"deleted": deleted}


@app.delete("/api/labels/{label_id}")
async def delete_label(label_id: int, user: dict = Depends(current_user)):
    await db.delete_label(user["id"], label_id)
    return {"success": True}


class LabelCardRequest(BaseModel):
    card_id: int


@app.post("/api/labels/{label_id}/cards")
async def add_card_to_label(label_id: int, req: LabelCardRequest, user: dict = Depends(current_user)):
    """Add a single card to a label without touching its other labels."""
    import aiosqlite as _aiosqlite
    async with _aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("PRAGMA foreign_keys = ON")
        # Verify label and card belong to this user.
        async with conn.execute(
            "SELECT 1 FROM labels WHERE id=? AND user_id=?", (label_id, user["id"])
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(404, "Label not found")
        async with conn.execute(
            "SELECT 1 FROM cards WHERE id=? AND user_id=?", (req.card_id, user["id"])
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(404, "Card not found")
        await conn.execute(
            "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
            (req.card_id, label_id),
        )
        await conn.commit()
    return {"success": True}


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if not mag_a or not mag_b:
        return 0.0
    return dot / (mag_a * mag_b)


@app.get("/api/labels/suggest-cards")
async def suggest_cards_for_label(name: str, label_id: int | None = None, limit: int = 20, user: dict = Depends(current_user)):
    """Embed 'name' and return the top cards by cosine similarity, optionally excluding cards already in label_id."""
    query_embedding = await translation.get_embedding(name)
    if not query_embedding:
        return {"cards": []}

    all_embeddings = await db.get_all_embeddings(user["id"])
    if not all_embeddings:
        return {"cards": []}

    scored = []
    for row in all_embeddings:
        try:
            emb = json.loads(row["embedding"])
        except Exception:
            continue
        score = _cosine_similarity(query_embedding, emb)
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [r for _, r in scored[:limit]]

    # If filtering by label, fetch cards already in the label to exclude them.
    if label_id is not None:
        import aiosqlite as _aiosqlite
        async with _aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT card_id FROM card_labels WHERE label_id=?", (label_id,)
            ) as cur:
                already = {r[0] for r in await cur.fetchall()}
        top = [r for r in top if r["id"] not in already]

    return {"cards": top[:limit]}


@app.get("/api/reader/texts/{text_id}/vocab-label")
async def reader_vocab_label(text_id: int, user: dict = Depends(current_user)):
    """Get or create the story label for this reader text."""
    label = await db.get_or_create_story_label(user["id"], text_id)
    if not label:
        raise HTTPException(404, "Reader text not found")
    return label


# ── Languages (metadata) ──────────────────────────────────────────────────────

@app.get("/api/languages")
async def list_languages():
    return {
        "languages": [
            {
                "code": code,
                "name": info["name"],
                "flag": info.get("flag", ""),
                "script": info["script"],
                "romanization": info["romanization"],
                "logographic": info["romanization"] is not None,
            }
            for code, info in translation.LANG_INFO.items()
        ]
    }


# ── Admin: user management ────────────────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_list_users(user: dict = Depends(current_admin)):
    return {"users": await db.list_users()}


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


@app.post("/api/admin/users")
async def admin_create_user(req: CreateUserRequest, user: dict = Depends(current_admin)):
    username = req.username.strip()
    if not username or len(username) > 50:
        raise HTTPException(400, "Username must be 1–50 characters")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    existing = await db.get_user_by_username(username)
    if existing:
        raise HTTPException(409, "Username already exists")
    new_id = await db.create_user(username, auth.hash_password(req.password), is_admin=req.is_admin)
    return {"id": new_id, "username": username, "is_admin": req.is_admin}


class UpdatePasswordRequest(BaseModel):
    password: str


@app.put("/api/admin/users/{user_id}/password")
async def admin_update_password(user_id: int, req: UpdatePasswordRequest, user: dict = Depends(current_admin)):
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    target = await db.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    await db.update_user_password(user_id, auth.hash_password(req.password))
    return {"success": True}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, user: dict = Depends(current_admin)):
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete yourself")
    target = await db.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    await db.delete_user(user_id)
    # Invalidate all sessions for that user.
    for tok, (uid, _) in list(_sessions.items()):
        if uid == user_id:
            _sessions.pop(tok, None)
    return {"success": True}


# ── Reader ────────────────────────────────────────────────────────────────────

class ReaderGenerateRequest(BaseModel):
    prompt: str
    target_lang: str = "yue"
    difficulty: str = "B1"


class ReaderTranslateWordRequest(BaseModel):
    word: str
    context: str = ""
    target_lang: str = "yue"


def _annotate_tokens(tokens: list[dict], statuses: dict[str, str]) -> list[dict]:
    for t in tokens:
        if t["is_word"]:
            t["status"] = statuses.get(t["text"], "new")
    return tokens


async def _build_text_response(user_id: int, text: dict) -> dict:
    """Assemble the full response for a reader text: tokens + cached sentences."""
    tokens = tokenizer.tokenize(text["content"], text["target_lang"])
    words = [t["text"] for t in tokens if t["is_word"]]
    unique_words = list(dict.fromkeys(words))
    statuses = await db.get_word_statuses(user_id, unique_words, text["target_lang"])
    sentences = await db.get_reader_sentences(user_id, text["id"])
    preload_complete = bool(sentences) and all(
        s["translation"] and s["has_audio"] for s in sentences
    )
    rom_map = tokenizer.romanize_words(words, text["target_lang"])
    all_vocab_added = bool(unique_words) and all(w in statuses for w in unique_words)
    return {
        **text,
        "tokens": _annotate_tokens(tokens, statuses),
        "sentences": sentences,
        "preload_complete": preload_complete,
        "romanization": rom_map,
        "all_vocab_added": all_vocab_added,
    }


@app.post("/api/reader/generate")
async def reader_generate(req: ReaderGenerateRequest, user: dict = Depends(current_user)):
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    result = await translation.generate_reader_text(req.prompt, req.target_lang, req.difficulty)
    text_id = await db.create_reader_text(
        user["id"], result["title"], req.prompt, result["content"], req.target_lang
    )
    text = await db.get_reader_text(user["id"], text_id)
    return await _build_text_response(user["id"], text)


@app.get("/api/reader/texts")
async def reader_list_texts(user: dict = Depends(current_user)):
    return {"texts": await db.list_reader_texts(user["id"])}


@app.get("/api/reader/texts/{text_id}")
async def reader_get_text(text_id: int, background_tasks: BackgroundTasks, user: dict = Depends(current_user)):
    text = await db.get_reader_text(user["id"], text_id)
    if not text:
        raise HTTPException(404, "Text not found")
    resp = await _build_text_response(user["id"], text)
    if not resp["all_vocab_added"]:
        auto_add = (await db.get_setting(user["id"], "auto_add_reader_vocab") or "false") == "true"
        if auto_add:
            background_tasks.add_task(_auto_add_vocab_bg, user["id"], text_id, text)
    return resp


async def _auto_add_vocab_bg(user_id: int, text_id: int, text: dict):
    """Background task: add all unseen words from a reader text (no HTTP context needed)."""
    tokens = tokenizer.tokenize(text["content"], text["target_lang"])
    seen: set[str] = set()
    words = []
    for t in tokens:
        if t["is_word"] and t["text"] not in seen:
            seen.add(t["text"])
            words.append(t["text"])
    statuses = await db.get_word_statuses(user_id, words, text["target_lang"])
    new_words = [w for w in words if w not in statuses]
    if not new_words:
        return
    story_label = await db.get_or_create_story_label(user_id, text_id)
    story_label_id = story_label.get("id")
    sem = asyncio.Semaphore(5)

    async def _add_word(word: str):
        async with sem:
            try:
                result = await translation.translate(word, text["target_lang"], source_is_target=True)
                candidate = result["candidates"][0] if result["candidates"] else {}
                if not candidate.get("english"):
                    return
                audio_data = await audio.generate(word, text["target_lang"])
                label_ids = [story_label_id] if story_label_id else []
                card_id = await db.create_card(
                    user_id=user_id,
                    source_text=candidate["english"],
                    target_text=word,
                    romanization=candidate.get("romanization", ""),
                    target_lang=text["target_lang"],
                    audio_data=audio_data,
                    notes=candidate.get("notes") or None,
                    label_ids=label_ids,
                    priority=result.get("priority", 3),
                    classifier=result.get("classifier", ""),
                    suggested_label_names=result.get("suggested_labels", []),
                    cefr_level=result.get("cefr_level"),
                )
                embed_text = f"{candidate['english']} {word}"
                await _generate_and_store_embedding(card_id, embed_text)
            except Exception:
                pass

    await asyncio.gather(*[_add_word(w) for w in new_words])


@app.post("/api/reader/texts/{text_id}/preload")
async def reader_preload(text_id: int, user: dict = Depends(current_user)):
    """Pre-generate translations and audio for every sentence in the text.
    Skips sentences already cached. Returns the completed sentence list."""
    text = await db.get_reader_text(user["id"], text_id)
    if not text:
        raise HTTPException(404, "Text not found")

    tokens = tokenizer.tokenize(text["content"], text["target_lang"])
    sent_texts = tokenizer.split_sentences(tokens)
    existing = {s["sentence_idx"]: s for s in await db.get_reader_sentences(user["id"], text_id)}

    import asyncio as _asyncio
    sem = _asyncio.Semaphore(3)

    async def process(idx: int, sent_text: str):
        cached = existing.get(idx, {})
        need_translation = not cached.get("translation")
        need_audio = not cached.get("has_audio")
        if not need_translation and not need_audio:
            return

        trans_text = cached.get("translation")
        rom_text = cached.get("romanization")
        audio_bytes = None

        async with sem:
            if need_translation:
                try:
                    tr = await translation.translate(
                        sent_text, text["target_lang"], source_is_target=True
                    )
                    cand = tr["candidates"][0] if tr["candidates"] else {}
                    trans_text = cand.get("english", "")
                    rom_text = cand.get("romanization") or rom_text
                except Exception:
                    trans_text = ""
            if need_audio:
                try:
                    audio_bytes = await audio.generate(sent_text, text["target_lang"])
                except Exception:
                    audio_bytes = None

        await db.upsert_reader_sentence(text_id, idx, sent_text, trans_text, audio_bytes, rom_text)

    await _asyncio.gather(*[process(i, s) for i, s in enumerate(sent_texts)])

    sentences = await db.get_reader_sentences(user["id"], text_id)
    return {"sentences": sentences, "preload_complete": True}


@app.get("/api/reader/texts/{text_id}/sentences/{idx}/audio")
async def sentence_audio(text_id: int, idx: int, user: dict = Depends(current_user)):
    data = await db.get_sentence_audio(user["id"], text_id, idx)
    if not data:
        raise HTTPException(404, "Audio not ready")
    return Response(content=data, media_type="audio/mpeg")


@app.delete("/api/reader/texts/{text_id}")
async def reader_delete_text(text_id: int, user: dict = Depends(current_user)):
    await db.delete_reader_text(user["id"], text_id)
    return {"success": True}


@app.post("/api/reader/texts/{text_id}/add-all-vocab")
async def reader_add_all_vocab(
    text_id: int,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
):
    """Translate and add every unseen word from a reader text to the user's deck."""
    text = await db.get_reader_text(user["id"], text_id)
    if not text:
        raise HTTPException(404, "Text not found")

    tokens = tokenizer.tokenize(text["content"], text["target_lang"])
    # Deduplicate while preserving order.
    seen: set[str] = set()
    words = []
    for t in tokens:
        if t["is_word"] and t["text"] not in seen:
            seen.add(t["text"])
            words.append(t["text"])

    statuses = await db.get_word_statuses(user["id"], words, text["target_lang"])
    new_words = [w for w in words if w not in statuses]

    story_label = await db.get_or_create_story_label(user["id"], text_id)
    story_label_id = story_label.get("id")

    sem = asyncio.Semaphore(5)

    async def _add_word(word: str) -> bool:
        async with sem:
            try:
                result = await translation.translate(
                    word, text["target_lang"], source_is_target=True
                )
                candidate = result["candidates"][0] if result["candidates"] else {}
                if not candidate.get("english"):
                    return False
                audio_data = await audio.generate(word, text["target_lang"])
                label_ids = [story_label_id] if story_label_id else []
                card_id = await db.create_card(
                    user_id=user["id"],
                    source_text=candidate["english"],
                    target_text=word,
                    romanization=candidate.get("romanization", ""),
                    target_lang=text["target_lang"],
                    audio_data=audio_data,
                    notes=candidate.get("notes") or None,
                    label_ids=label_ids,
                    priority=result.get("priority", 3),
                    classifier=result.get("classifier", ""),
                    suggested_label_names=result.get("suggested_labels", []),
                    cefr_level=result.get("cefr_level"),
                )
                embed_text = f"{candidate['english']} {word}"
                background_tasks.add_task(_generate_and_store_embedding, card_id, embed_text)
                return True
            except Exception:
                return False

    results = await asyncio.gather(*[_add_word(w) for w in new_words])
    added = sum(1 for r in results if r)
    skipped = len(results) - added

    return {"added": added, "skipped": skipped, "total_new": len(new_words)}


class ReaderTTSRequest(BaseModel):
    text: str
    target_lang: str = "yue"


@app.get("/api/reader/texts/{text_id}/romanize")
async def reader_romanize(text_id: int, user: dict = Depends(current_user)):
    """Return a word→romanization map for all tokens in the text."""
    text = await db.get_reader_text(user["id"], text_id)
    if not text:
        raise HTTPException(404, "Text not found")
    tokens = tokenizer.tokenize(text["content"], text["target_lang"])
    words = [t["text"] for t in tokens if t["is_word"]]
    rom_map = tokenizer.romanize_words(words, text["target_lang"])
    return {"romanization": rom_map, "lang": text["target_lang"]}


@app.post("/api/reader/tts")
async def reader_tts(req: ReaderTTSRequest, user: dict = Depends(current_user)):
    if not req.text.strip():
        raise HTTPException(400, "Text is empty")
    data = await audio.generate(req.text.strip(), req.target_lang)
    return Response(content=data, media_type="audio/mpeg")


@app.post("/api/reader/translate-word")
async def reader_translate_word(req: ReaderTranslateWordRequest, user: dict = Depends(current_user)):
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    # Check if word is already in the user's deck.
    statuses = await db.get_word_statuses(user["id"], [req.word], req.target_lang)
    if req.word in statuses:
        # Word exists — find the card and return its data.
        import aiosqlite as _aiosqlite
        async with _aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = _aiosqlite.Row
            async with conn.execute(
                """SELECT id, source_text, target_text, romanization, notes
                   FROM cards WHERE user_id=? AND target_lang=? AND target_text=?
                   LIMIT 1""",
                (user["id"], req.target_lang, req.word),
            ) as cur:
                row = await cur.fetchone()
        if row:
            resp: dict = {
                "source": "deck",
                "card_id": row["id"],
                "target_text": row["target_text"],
                "source_text": row["source_text"],
                "romanization": row["romanization"],
                "notes": row["notes"],
                "status": statuses[req.word],
            }
            # If context is provided, also translate to detect a different sense.
            if req.context:
                try:
                    ctx_result = await translation.translate(
                        req.word, req.target_lang, source_is_target=True, context=req.context
                    )
                    ctx_candidate = ctx_result["candidates"][0] if ctx_result["candidates"] else {}
                    ctx_english = ctx_candidate.get("english", "")
                    stored = (row["source_text"] or "").lower()
                    # Surface the contextual meaning if it differs from what's stored.
                    if ctx_english and ctx_english.lower() != stored:
                        resp["context_source_text"] = ctx_english
                        resp["context_romanization"] = ctx_candidate.get("romanization", "")
                        resp["context_notes"] = ctx_candidate.get("notes", "")
                except Exception:
                    pass
            return resp
    # Not in deck — translate via Gemini.
    result = await translation.translate(req.word, req.target_lang, source_is_target=True, context=req.context)
    candidate = result["candidates"][0] if result["candidates"] else {}
    return {
        "source": "gemini",
        "target_text": req.word,
        "source_text": candidate.get("english", ""),
        "romanization": candidate.get("romanization", ""),
        "notes": candidate.get("notes", ""),
        "priority": result.get("priority", 3),
        "suggested_labels": result.get("suggested_labels", []),
        "classifier": result.get("classifier", ""),
        "cefr_level": result.get("cefr_level"),
        "status": "new",
    }
