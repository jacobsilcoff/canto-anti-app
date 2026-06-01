import asyncio
import datetime
import hashlib
import json
import math
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

load_dotenv()

import audio
import auth
import billing
import crypto
import db
import email_utils
import srs
import tokenizer
import translation

_BOOTSTRAP_PASSWORD = os.getenv("APP_PASSWORD")
_BOOTSTRAP_USERNAME = os.getenv("APP_ADMIN_USERNAME", "jsilcoff")
_BOOTSTRAP_EMAIL = os.getenv("APP_ADMIN_EMAIL") or None

_SESSION_TTL = 30 * 86400  # 30 days

_NO_AUTH_PATHS = {
    "/login", "/api/login",
    "/register", "/api/register",
    "/verify-email",
    "/forgot-password", "/api/forgot-password",
    "/reset-password", "/api/reset-password",
    "/api/resend-verification",
    "/api/webhooks/stripe",
    "/manifest.json", "/sw.js",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    if _BOOTSTRAP_PASSWORD:
        await db.bootstrap_admin(_BOOTSTRAP_USERNAME, auth.hash_password(_BOOTSTRAP_PASSWORD), email=_BOOTSTRAP_EMAIL)
    yield


app = FastAPI(lifespan=lifespan)


def _rate_limit_key(request: Request) -> str:
    """Rate-limit per authenticated user when known, else per client IP.

    auth_middleware sets request.state.user_id before the route runs, so AI
    endpoints are capped per account; unauthenticated routes (login) fall back
    to IP so a single host can't brute-force credentials."""
    user_id = getattr(request.state, "user_id", None)
    return f"user:{user_id}" if user_id is not None else get_remote_address(request)


# In-memory storage: counters reset on restart/deploy. Adequate for brute-force
# and per-user abuse caps; move to Redis if you run multiple app containers or
# need quotas that survive deploys.
limiter = Limiter(key_func=_rate_limit_key)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path in _NO_AUTH_PATHS or request.url.path.startswith("/static/"):
        return await call_next(request)

    user_id = None
    token = request.cookies.get("session")
    if token:
        user_id = await db.get_session_user(token)

    if user_id is None:
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            return Response(status_code=302, headers={"Location": "/login"})
        return Response(status_code=401, content="Unauthorized")

    request.state.user_id = user_id
    return await call_next(request)


# Registered after auth_middleware, so it runs outermost and applies headers to
# every response — including the 302/401 from auth and 429 from the limiter.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "media-src 'self' blob: data:; "
    # Inline <script>/<style> blocks are used throughout the app; 'unsafe-inline'
    # is required until they're moved to nonces (tracked under the XSS hardening).
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Ignored by browsers over plain HTTP; takes effect behind Caddy's TLS in prod.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": _CSP,
}


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


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


def _build_nav(active: str = "", extra_desktop: str = "", extra_dropdown: str = "") -> str:
    """Return the full <header> inner HTML with the active page highlighted."""
    _i = ('class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
          'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"')
    svgs = {
        "translate": f'<svg {_i}><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
        "cards":     f'<svg {_i}><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
        "reader":    f'<svg {_i}><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
        "settings":  (f'<svg {_i}><circle cx="12" cy="12" r="3"/>'
                      '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0'
                      'l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09'
                      'A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83'
                      'l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09'
                      'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0'
                      'l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09'
                      'a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83'
                      'l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09'
                      'a1.65 1.65 0 0 0-1.51 1z"/></svg>'),
        "signout":   f'<svg {_i}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
        "browse":    f'<svg {_i}><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
        "hamburger": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>',
    }

    def link(href: str, label: str, icon: str, badge: bool = False) -> str:
        hl = ' style="color:var(--primary)"' if href == active else ""
        bdg = ' <span class="badge due-badge"></span>' if badge else ""
        return f'    <a href="{href}" class="nav-link"{hl}>\n      {svgs[icon]}\n      {label}{bdg}\n    </a>'

    nav_links = [
        link("/",         "Translate",  "translate"),
        link("/cards",    "Flashcards", "cards",    badge=True),
        link("/reader",   "Reader",     "reader"),
        link("/settings", "Settings",   "settings"),
    ]
    signout_btn = (
        '    <button class="nav-link" onclick="doLogout()" '
        'style="border:none;cursor:pointer;background:none" title="Sign out">\n'
        f'      {svgs["signout"]}\n      Sign out\n    </button>'
    )
    signout_dropdown = (
        '    <button class="nav-link nav-signout" onclick="doLogout()" '
        'style="border:none;cursor:pointer;background:none">\n'
        f'      {svgs["signout"]}\n      Sign out\n    </button>'
    )
    desktop_extra = f"\n{extra_desktop}" if extra_desktop else ""
    dropdown_extra = f"\n{extra_dropdown}" if extra_dropdown else ""

    return (
        "  <h1>{{APP_NAME_HTML}}</h1>\n"
        "  <nav class=\"nav-desktop\">\n"
        + "\n".join(nav_links)
        + desktop_extra + "\n"
        "    <span class=\"streak-display\" id=\"streak-display\" style=\"display:none\"></span>\n"
        + signout_btn + "\n"
        "  </nav>\n"
        "  <div class=\"nav-mobile\">\n"
        "    <span class=\"streak-display\" id=\"streak-display-mobile\" style=\"display:none\"></span>\n"
        "    <button class=\"nav-hamburger\" onclick=\"toggleMobileMenu()\" aria-label=\"Menu\">\n"
        f"      {svgs['hamburger']}\n"
        "    </button>\n"
        "  </div>\n"
        "  <nav class=\"nav-dropdown\" id=\"nav-dropdown\">\n"
        + "\n".join(nav_links)
        + dropdown_extra + "\n"
        + signout_dropdown + "\n"
        "  </nav>\n"
    )


def _html(name: str, active: str = "", extra_desktop: str = "", extra_dropdown: str = "") -> HTMLResponse:
    content = (_static / name).read_text()
    content = content.replace("{{NAV}}", _build_nav(active, extra_desktop, extra_dropdown))
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
@limiter.limit("10/minute")
async def login(request: Request, req: LoginRequest):
    identifier = req.username.strip()
    if "@" in identifier:
        user = await db.get_user_by_email(identifier)
    else:
        user = await db.get_user_by_username(identifier)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Wrong username or password")
    if not user.get("email_verified", True):
        return JSONResponse(
            status_code=403,
            content={"code": "email_not_verified", "detail": "Please verify your email before signing in."},
        )
    await db.purge_expired_sessions()
    token = secrets.token_hex(32)
    await db.create_session(token, user["id"], time.time() + _SESSION_TTL)
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
        await db.delete_session(token)
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


@app.get("/api/profile")
async def get_profile(user: dict = Depends(current_user)):
    return {
        "username": user["username"],
        "display_name": user.get("display_name") or "",
        "email": user.get("email") or "",
        "email_verified": bool(user.get("email_verified", True)),
    }


class ProfileUpdate(BaseModel):
    display_name: str | None = None
    username: str | None = None
    email: str | None = None


@app.put("/api/profile")
@limiter.limit("10/minute")
async def update_profile(request: Request, req: ProfileUpdate, user: dict = Depends(current_user)):
    updates: dict = {}
    email_changed = False

    if req.display_name is not None:
        dn = req.display_name.strip()
        if not dn:
            raise HTTPException(400, "Full name cannot be empty.")
        updates["display_name"] = dn

    if req.username is not None:
        uname = req.username.strip()
        if not _USERNAME_RE.match(uname):
            raise HTTPException(400, "Username must be 2–30 characters: letters, numbers, _ or -")
        if uname != user["username"]:
            existing = await db.get_user_by_username(uname)
            if existing and existing["id"] != user["id"]:
                raise HTTPException(409, "That username is already taken.")
            updates["username"] = uname

    if req.email is not None:
        new_email = req.email.strip().lower()
        if new_email and "@" not in new_email:
            raise HTTPException(400, "Enter a valid email address.")
        if new_email != (user.get("email") or "").lower():
            if new_email:
                existing = await db.get_user_by_email(new_email)
                if existing and existing["id"] != user["id"]:
                    raise HTTPException(409, "That email is already in use.")
            # Email changed: require re-verification (unless clearing it).
            token = secrets.token_urlsafe(32) if new_email else None
            updates["email"] = new_email or None
            updates["email_verified"] = False if new_email else True
            updates["verification_token"] = token
            email_changed = bool(new_email)
            if new_email and token:
                await email_utils.send_verification(new_email, token, APP_NAME_DISPLAY)

    if updates:
        # Pass verification_token only if it was explicitly set above.
        vt = updates.pop("verification_token", ...)
        await db.update_user_profile(user["id"], verification_token=vt, **updates)

    return {"ok": True, "email_changed": email_changed}


# ── Pages ─────────────────────────────────────────────────────────────────────

_BROWSE_ICON = (
    'class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'
)
_BROWSE_BTN_DESKTOP = (
    '    <button class="nav-link" onclick="showBrowse()" '
    'style="border:none;cursor:pointer;background:none">\n'
    f'      <svg {_BROWSE_ICON}><circle cx="11" cy="11" r="8"/>'
    '<line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>\n'
    '      Browse\n    </button>'
)
_BROWSE_BTN_DROPDOWN = (
    '    <button class="nav-link" onclick="closeMobileMenu();showBrowse()" '
    'style="border:none;cursor:pointer;background:none">\n'
    f'      <svg {_BROWSE_ICON}><circle cx="11" cy="11" r="8"/>'
    '<line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>\n'
    '      Browse\n    </button>'
)


@app.get("/", response_class=HTMLResponse)
async def index():
    return _html("index.html", active="/")


@app.get("/cards", response_class=HTMLResponse)
async def cards_page():
    return _html("cards.html", active="/cards",
                 extra_desktop=_BROWSE_BTN_DESKTOP,
                 extra_dropdown=_BROWSE_BTN_DROPDOWN)


@app.get("/reader", response_class=HTMLResponse)
async def reader_page():
    return _html("reader.html", active="/reader")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return _html("settings.html", active="/settings")


@app.get("/admin")
async def admin_page(_: dict = Depends(current_admin)):
    return RedirectResponse("/settings", status_code=301)


@app.get("/register", response_class=HTMLResponse)
async def register_page():
    return _html("register.html")


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page():
    return _html("forgot-password.html")


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page():
    return _html("reset-password.html")


@app.get("/verify-email")
async def verify_email(token: str = ""):
    if not token:
        return RedirectResponse("/login?error=invalid_token", status_code=302)
    user = await db.get_user_by_token(token, "verification")
    if not user:
        return RedirectResponse("/login?error=invalid_token", status_code=302)
    await db.set_email_verified(user["id"])
    return RedirectResponse("/login?verified=1", status_code=302)


# ── Self-service registration ─────────────────────────────────────────────────

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,30}$")

APP_NAME_DISPLAY = os.getenv("APP_NAME", APP_NAME)


class RegisterRequest(BaseModel):
    email: str
    username: str
    display_name: str
    password: str


@app.post("/api/register")
@limiter.limit("5/minute;20/hour")
async def register(request: Request, req: RegisterRequest):
    email = req.email.strip().lower()
    username = req.username.strip()
    display_name = req.display_name.strip()
    if not email or "@" not in email:
        raise HTTPException(400, "A valid email address is required.")
    if not _USERNAME_RE.match(username):
        raise HTTPException(400, "Username must be 2–30 characters: letters, numbers, _ or -")
    if not display_name:
        raise HTTPException(400, "Full name is required.")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if await db.get_user_by_email(email):
        raise HTTPException(409, "An account with that email already exists.")
    if await db.get_user_by_username(username):
        raise HTTPException(409, "That username is already taken.")
    token = secrets.token_urlsafe(32)
    await db.create_user(
        username=username,
        password_hash=auth.hash_password(req.password),
        email=email,
        display_name=display_name,
        email_verified=False,
        verification_token=token,
    )
    await email_utils.send_verification(email, token, APP_NAME_DISPLAY)
    return {"ok": True}


class ResendVerificationRequest(BaseModel):
    email: str


@app.post("/api/resend-verification")
@limiter.limit("3/minute;10/hour")
async def resend_verification(request: Request, req: ResendVerificationRequest):
    email = req.email.strip().lower()
    if not email:
        return {"ok": True}  # silent; don't leak info
    user = await db.get_user_by_email(email)
    if user and not user.get("email_verified", True):
        token = secrets.token_urlsafe(32)
        await db.set_verification_token(user["id"], token)
        await email_utils.send_verification(user["email"], token, APP_NAME_DISPLAY)
    return {"ok": True}


class ForgotPasswordRequest(BaseModel):
    email: str


@app.post("/api/forgot-password")
@limiter.limit("3/minute;10/hour")
async def forgot_password(request: Request, req: ForgotPasswordRequest):
    email = req.email.strip().lower()
    user = await db.get_user_by_email(email)
    if user and user.get("email_verified", True):
        token = secrets.token_urlsafe(32)
        expiry = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=1)).isoformat()
        await db.set_reset_token(user["id"], token, expiry)
        await email_utils.send_password_reset(user["email"], token, APP_NAME_DISPLAY)
    # Always 200 — don't reveal whether the email is registered.
    return {"ok": True}


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


@app.post("/api/reset-password")
@limiter.limit("5/minute")
async def reset_password(request: Request, req: ResetPasswordRequest):
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    user = await db.get_user_by_token(req.token.strip(), "reset")
    if not user:
        raise HTTPException(400, "Link expired or invalid. Request a new one.")
    expiry_str = user.get("reset_token_expiry")
    if not expiry_str:
        raise HTTPException(400, "Link expired or invalid. Request a new one.")
    try:
        expiry = datetime.datetime.fromisoformat(expiry_str)
    except ValueError:
        raise HTTPException(400, "Link expired or invalid. Request a new one.")
    if datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) > expiry:
        raise HTTPException(400, "This reset link has expired. Request a new one.")
    await db.update_user_password(user["id"], auth.hash_password(req.password))
    await db.set_reset_token(user["id"], None, None)
    # Invalidate all existing sessions so stolen sessions can't persist.
    await db.delete_user_sessions(user["id"])
    return {"ok": True}


# ── Translation ───────────────────────────────────────────────────────────────

class TranslateRequest(BaseModel):
    text: str
    target_lang: str
    source_is_target: bool = False  # True if user typed in target_lang and wants English
    context: str | None = None


# ── Gemini access resolution ────────────────────────────────────────────────
# A user's own key (with their chosen per-task models) takes priority — it is
# never metered, since they pay Google directly. Without a key:
#   - admins spend their own env key (own models, unmetered),
#   - explicitly-granted friends share the admin's key (admin's models, unmetered),
#   - everyone else is on a paid plan tier and shares the key under a monthly
#     quota (free 30/mo, pro 600/mo) — the metered path.
# On the shared key the model is fixed (no spending choices on someone else's dime).
_SHARED_API_KEY = os.getenv("GEMINI_API_KEY")

# Monthly shared-key AI-call allowance per plan. Own-key/admin/granted users are
# unlimited. These caps bound cost exposure: even pro's 600 calls is ~$0.08/mo
# of Gemini at current Flash-Lite rates.
PLAN_LIMITS = {"free": 30, "pro": 600}


class _GeminiAccess:
    def __init__(self, api_key: str, model_translate: str, model_reader: str):
        self.api_key = api_key
        self.model_translate = model_translate
        self.model_reader = model_reader


def _valid_model(value: str | None) -> str:
    return value if value in translation.MODEL_ALLOWLIST else translation.DEFAULT_MODEL


def _plan_limit(user: dict) -> int:
    return PLAN_LIMITS.get(user.get("plan") or "free", PLAN_LIMITS["free"])


async def _resolve_gemini(user: dict, *, meter: bool = True) -> _GeminiAccess:
    """Resolve which Gemini key + models a user gets, enforcing the monthly quota.

    For metered (shared-key plan) users this checks the quota and, when `meter`
    is True, records one AI call. Pass `meter=False` for derived/background calls
    (e.g. card-creation embeddings) that shouldn't count against the allowance.
    """
    own_enc = await db.get_setting(user["id"], "gemini_api_key")
    if own_enc:
        try:
            api_key = crypto.decrypt(own_enc)
        except Exception:
            raise HTTPException(400, "Stored API key could not be read; please re-enter it in Settings.")
        return _GeminiAccess(
            api_key,
            _valid_model(await db.get_setting(user["id"], "model_translate")),
            _valid_model(await db.get_setting(user["id"], "model_reader")),
        )
    # The admin spends their own (env) key, so they pick their own models.
    if user.get("is_admin"):
        if not _SHARED_API_KEY:
            raise HTTPException(503, "Shared API key is not configured.")
        return _GeminiAccess(
            _SHARED_API_KEY,
            _valid_model(await db.get_setting(user["id"], "model_translate")),
            _valid_model(await db.get_setting(user["id"], "model_reader")),
        )

    if not _SHARED_API_KEY:
        raise HTTPException(503, "Shared API key is not configured.")

    # Granted friends are unlimited on the shared key; plan users are metered.
    if not user.get("can_use_shared_key"):
        limit = _plan_limit(user)
        if await db.get_usage(user["id"]) >= limit:
            if (user.get("plan") or "free") == "free":
                raise HTTPException(402, (
                    f"You've used your {limit} free AI translations this month. "
                    f"Upgrade to Pro for {PLAN_LIMITS['pro']}/month, or add your own "
                    "Gemini key in Settings for unlimited use."
                ))
            raise HTTPException(402, (
                f"You've reached your monthly limit of {limit} AI translations. "
                "It resets on the 1st. Add your own Gemini key in Settings for "
                "unlimited use."
            ))
        if meter:
            await db.increment_usage(user["id"])

    admin_id = await db.get_primary_admin_id()
    mt = mr = translation.DEFAULT_MODEL
    if admin_id is not None:
        mt = _valid_model(await db.get_setting(admin_id, "shared_model_translate"))
        mr = _valid_model(await db.get_setting(admin_id, "shared_model_reader"))
    return _GeminiAccess(_SHARED_API_KEY, mt, mr)


@app.post("/api/translate")
@limiter.limit("120/minute;2000/day")
async def translate_endpoint(request: Request, req: TranslateRequest, user: dict = Depends(current_user)):
    if not req.text.strip():
        raise HTTPException(400, "Text is empty")
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, f"Unsupported target language: {req.target_lang}")
    access = await _resolve_gemini(user)
    result = await translation.translate(
        req.text.strip(),
        req.target_lang,
        source_is_target=req.source_is_target,
        context=(req.context or "").strip(),
        api_key=access.api_key,
        model=access.model_translate,
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


async def _generate_and_store_embedding(card_id: int, text: str, api_key: str):
    embedding = await translation.get_embedding(text, api_key=api_key)
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

    # Generate embedding in the background (best-effort; skip if no usable key).
    # Not metered — it's a derived side-effect of saving a card, not a user action.
    try:
        access = await _resolve_gemini(user, meter=False)
        embed_text = f"{req.source_text.strip()} {target_text}"
        background_tasks.add_task(_generate_and_store_embedding, card_id, embed_text, access.api_key)
    except HTTPException:
        pass

    return {"card_id": card_id, "notes": notes, "labels": []}


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(user: dict = Depends(current_user)):
    new_cards_per_day = int(await db.get_setting(user["id"], "new_cards_per_day") or 20)
    default_target_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    auto_add_reader_vocab = (await db.get_setting(user["id"], "auto_add_reader_vocab") or "false") == "true"
    audio_show_romanization = (await db.get_setting(user["id"], "audio_show_romanization") or "true") == "true"
    has_api_key = bool(await db.get_setting(user["id"], "gemini_api_key"))
    return {
        "new_cards_per_day": new_cards_per_day,
        "default_target_lang": default_target_lang,
        "auto_add_reader_vocab": auto_add_reader_vocab,
        "audio_show_romanization": audio_show_romanization,
        "has_api_key": has_api_key,
        "is_admin": bool(user.get("is_admin")),
        # You pick models when spending your own money: your own key, or (for the
        # admin) the env key. Granted friends get the admin's fixed shared model.
        "can_choose_models": has_api_key or bool(user.get("is_admin")),
        "using_shared_key": (not has_api_key) and (not user.get("is_admin")) and bool(user.get("can_use_shared_key")),
        "model_translate": _valid_model(await db.get_setting(user["id"], "model_translate")),
        "model_reader": _valid_model(await db.get_setting(user["id"], "model_reader")),
        "available_models": translation.MODEL_ALLOWLIST,
        "default_model": translation.DEFAULT_MODEL,
    }


class SettingsUpdate(BaseModel):
    new_cards_per_day: int | None = None
    default_target_lang: str | None = None
    auto_add_reader_vocab: bool | None = None
    audio_show_romanization: bool | None = None
    gemini_api_key: str | None = None
    model_translate: str | None = None
    model_reader: str | None = None


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
    if req.gemini_api_key is not None:
        val = req.gemini_api_key.strip()
        # Empty string clears the stored key (user reverts to shared/blocked).
        await db.set_setting(user["id"], "gemini_api_key", crypto.encrypt(val) if val else "")
    if req.model_translate is not None:
        if req.model_translate not in translation.MODEL_ALLOWLIST:
            raise HTTPException(400, "Unsupported model")
        await db.set_setting(user["id"], "model_translate", req.model_translate)
    if req.model_reader is not None:
        if req.model_reader not in translation.MODEL_ALLOWLIST:
            raise HTTPException(400, "Unsupported model")
        await db.set_setting(user["id"], "model_reader", req.model_reader)
    return {"success": True}


# ── Billing ───────────────────────────────────────────────────────────────────

@app.get("/api/billing/status")
async def billing_status(user: dict = Depends(current_user)):
    """Plan + monthly usage for the settings UI. `unlimited` means no quota
    applies (own key, admin, or a granted friend)."""
    has_api_key = bool(await db.get_setting(user["id"], "gemini_api_key"))
    unlimited = has_api_key or bool(user.get("is_admin")) or bool(user.get("can_use_shared_key"))
    return {
        "plan": user.get("plan") or "free",
        "subscription_status": user.get("subscription_status"),
        "unlimited": unlimited,
        "used": await db.get_usage(user["id"]),
        "limit": _plan_limit(user),
        "billing_enabled": billing.is_configured(),
        "has_subscription": bool(user.get("stripe_customer_id")),
        "pro_limit": PLAN_LIMITS["pro"],
    }


@app.post("/api/billing/checkout")
@limiter.limit("10/minute")
async def billing_checkout(request: Request, user: dict = Depends(current_user)):
    """Create a Stripe Checkout session and return its hosted URL."""
    if not billing.is_configured():
        raise HTTPException(503, "Billing is not configured.")
    if (user.get("plan") or "free") == "pro":
        raise HTTPException(400, "You're already on the Pro plan.")
    base = email_utils.APP_URL
    try:
        session = await asyncio.to_thread(
            billing.create_checkout_session,
            customer_id=user.get("stripe_customer_id"),
            customer_email=user.get("email"),
            client_reference_id=str(user["id"]),
            success_url=f"{base}/settings?upgraded=1",
            cancel_url=f"{base}/settings",
        )
    except Exception:
        raise HTTPException(502, "Could not start checkout. Please try again.")
    return {"url": session.url}


@app.post("/api/billing/portal")
@limiter.limit("10/minute")
async def billing_portal(request: Request, user: dict = Depends(current_user)):
    """Create a Stripe Customer Portal session for managing the subscription."""
    if not billing.is_configured():
        raise HTTPException(503, "Billing is not configured.")
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(400, "No subscription to manage.")
    try:
        session = await asyncio.to_thread(
            billing.create_portal_session,
            customer_id=customer_id,
            return_url=f"{email_utils.APP_URL}/settings",
        )
    except Exception:
        raise HTTPException(502, "Could not open the billing portal. Please try again.")
    return {"url": session.url}


def _subscription_period_end(obj: dict) -> str | None:
    ts = obj.get("current_period_end")
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Stripe-to-server subscription events. Verifies the signature against the
    raw body, then syncs users.plan. Idempotent — Stripe may retry."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = billing.construct_event(payload, sig)
    except Exception:
        raise HTTPException(400, "Invalid webhook signature.")

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        customer_id = obj.get("customer")
        user_ref = obj.get("client_reference_id")
        if customer_id and user_ref:
            await db.set_stripe_customer(int(user_ref), customer_id)
            await db.set_plan_by_customer(customer_id, "pro", "active", None)
    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        customer_id = obj.get("customer")
        status = obj.get("status")
        plan = "pro" if status in ("active", "trialing", "past_due") else "free"
        if customer_id:
            await db.set_plan_by_customer(customer_id, plan, status, _subscription_period_end(obj))
    elif etype == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        if customer_id:
            await db.set_plan_by_customer(customer_id, "free", "canceled", None)

    return {"received": True}


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
    try:
        access = await _resolve_gemini(user, meter=False)
    except HTTPException:
        return {"cards": []}
    query_embedding = await translation.get_embedding(name, api_key=access.api_key)
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


class SharedKeyGrant(BaseModel):
    can_use_shared_key: bool


@app.put("/api/admin/users/{user_id}/shared-key")
async def admin_set_shared_key(user_id: int, req: SharedKeyGrant, user: dict = Depends(current_admin)):
    """Grant/revoke a friend's permission to use the admin's shared Gemini key."""
    target = await db.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    await db.set_user_shared_key(user_id, req.can_use_shared_key)
    return {"success": True}


@app.get("/api/admin/shared-key-models")
async def admin_get_shared_models(user: dict = Depends(current_admin)):
    """The models granted friends get when spending the admin's shared key."""
    admin_id = await db.get_primary_admin_id()
    return {
        "model_translate": _valid_model(await db.get_setting(admin_id, "shared_model_translate")),
        "model_reader": _valid_model(await db.get_setting(admin_id, "shared_model_reader")),
        "available_models": translation.MODEL_ALLOWLIST,
        "default_model": translation.DEFAULT_MODEL,
    }


class SharedModelsUpdate(BaseModel):
    model_translate: str | None = None
    model_reader: str | None = None


@app.put("/api/admin/shared-key-models")
async def admin_set_shared_models(req: SharedModelsUpdate, user: dict = Depends(current_admin)):
    # Stored on the primary admin so it's a single source of truth for the key.
    admin_id = await db.get_primary_admin_id()
    if req.model_translate is not None:
        if req.model_translate not in translation.MODEL_ALLOWLIST:
            raise HTTPException(400, "Unsupported model")
        await db.set_setting(admin_id, "shared_model_translate", req.model_translate)
    if req.model_reader is not None:
        if req.model_reader not in translation.MODEL_ALLOWLIST:
            raise HTTPException(400, "Unsupported model")
        await db.set_setting(admin_id, "shared_model_reader", req.model_reader)
    return {"success": True}


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


@app.post("/api/admin/users")
async def admin_create_user(req: CreateUserRequest, user: dict = Depends(current_admin)):
    username = req.username.strip()
    if not username or len(username) > 50:
        raise HTTPException(400, "Username must be 1–50 characters")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    existing = await db.get_user_by_username(username)
    if existing:
        raise HTTPException(409, "Username already exists")
    new_id = await db.create_user(username, auth.hash_password(req.password), is_admin=req.is_admin)
    return {"id": new_id, "username": username, "is_admin": req.is_admin}


class UpdatePasswordRequest(BaseModel):
    password: str


@app.put("/api/admin/users/{user_id}/password")
async def admin_update_password(user_id: int, req: UpdatePasswordRequest, user: dict = Depends(current_admin)):
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    target = await db.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    await db.update_user_password(user_id, auth.hash_password(req.password))
    return {"success": True}


@app.post("/api/admin/email-test")
async def admin_email_test(user: dict = Depends(current_admin)):
    """Send a test email to the admin's own address and return the raw result."""
    to = user.get("email")
    if not to:
        raise HTTPException(400, "Your account has no email address set.")
    key = email_utils.RESEND_API_KEY
    config = {
        "resend_api_key_set": bool(key),
        "resend_api_key_prefix": key[:8] + "…" if key else None,
        "from_email": email_utils.FROM_EMAIL,
        "app_url": email_utils.APP_URL,
        "sending_to": to,
    }
    ok, detail = await email_utils._send(
        to,
        "Test email from your app",
        email_utils._base("It works!", "<p>This is a test email sent from the admin panel.</p>"),
    )
    return {"config": config, "sent": ok, "detail": detail}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, user: dict = Depends(current_admin)):
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete yourself")
    target = await db.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    await db.delete_user(user_id)
    # Invalidate all sessions for that user.
    await db.delete_user_sessions(user_id)
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
@limiter.limit("20/minute;100/day")
async def reader_generate(request: Request, req: ReaderGenerateRequest, user: dict = Depends(current_user)):
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    access = await _resolve_gemini(user)
    result = await translation.generate_reader_text(
        req.prompt, req.target_lang, req.difficulty,
        api_key=access.api_key, model=access.model_reader,
    )
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
            try:
                access = await _resolve_gemini(user)
                background_tasks.add_task(_auto_add_vocab_bg, user["id"], text_id, text, access)
            except HTTPException:
                pass
    return resp


async def _auto_add_vocab_bg(user_id: int, text_id: int, text: dict, access: "_GeminiAccess"):
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
                result = await translation.translate(
                    word, text["target_lang"], source_is_target=True,
                    api_key=access.api_key, model=access.model_translate,
                )
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
                await _generate_and_store_embedding(card_id, embed_text, access.api_key)
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
    access = await _resolve_gemini(user)

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
                        sent_text, text["target_lang"], source_is_target=True,
                        api_key=access.api_key, model=access.model_translate,
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
@limiter.limit("10/minute;50/day")
async def reader_add_all_vocab(
    request: Request,
    text_id: int,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
):
    """Translate and add every unseen word from a reader text to the user's deck."""
    text = await db.get_reader_text(user["id"], text_id)
    if not text:
        raise HTTPException(404, "Text not found")
    access = await _resolve_gemini(user)

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
                    word, text["target_lang"], source_is_target=True,
                    api_key=access.api_key, model=access.model_translate,
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
                background_tasks.add_task(_generate_and_store_embedding, card_id, embed_text, access.api_key)
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
@limiter.limit("120/minute;1000/day")
async def reader_tts(request: Request, req: ReaderTTSRequest, user: dict = Depends(current_user)):
    if not req.text.strip():
        raise HTTPException(400, "Text is empty")
    data = await audio.generate(req.text.strip(), req.target_lang)
    return Response(content=data, media_type="audio/mpeg")


@app.post("/api/reader/translate-word")
@limiter.limit("120/minute;2000/day")
async def reader_translate_word(request: Request, req: ReaderTranslateWordRequest, user: dict = Depends(current_user)):
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
                    access = await _resolve_gemini(user)
                    ctx_result = await translation.translate(
                        req.word, req.target_lang, source_is_target=True, context=req.context,
                        api_key=access.api_key, model=access.model_translate,
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
    access = await _resolve_gemini(user)
    result = await translation.translate(
        req.word, req.target_lang, source_is_target=True, context=req.context,
        api_key=access.api_key, model=access.model_translate,
    )
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
