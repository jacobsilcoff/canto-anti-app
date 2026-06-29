import asyncio
import datetime
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
import uuid as _uuid

import httpx
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
try:
    from PIL import Image, ImageOps as _ImageOps
    _PIL_OK = True
except ImportError:
    _PIL_OK = False
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
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
import starter_deck
import tokenizer
import translation
import learning
import grammar_lessons
import foundations
import tutor
import embeddings
import cefr
import extract
import messenger as _messenger

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


_TEST_USERS = os.getenv("TEST_USERS", "").lower() in ("1", "true", "yes")
_TEST_USER_PASSWORD = os.getenv("TEST_USER_PASSWORD", "test")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    if _BOOTSTRAP_PASSWORD:
        await db.bootstrap_admin(_BOOTSTRAP_USERNAME, auth.hash_password(_BOOTSTRAP_PASSWORD), email=_BOOTSTRAP_EMAIL)
    if _TEST_USERS:
        # "new" — email verified, no cards, no onboarding; use to test first-time UX
        existing = await db.get_user_by_username("new")
        if not existing:
            await db.create_user(
                "new",
                auth.hash_password(_TEST_USER_PASSWORD),
                email="new@test.local",
                email_verified=True,
            )
    yield


app = FastAPI(lifespan=lifespan)
logger = logging.getLogger("app")


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
    "img-src 'self' data: blob:; "
    "media-src 'self' blob: data:; "
    # Inline <script>/<style> blocks are used throughout the app; 'unsafe-inline'
    # is required until they're moved to nonces (tracked under the XSS hardening).
    # challenges.cloudflare.com: the Turnstile (CAPTCHA) script + its iframe.
    "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "frame-src https://challenges.cloudflare.com; "
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
_SERVER_START = time.time()

APP_NAME = "廣東卡"
_APP_NAME_HTML = '廣東<span class="logo-accent">卡</span>'

IS_DEV = os.getenv("ENVIRONMENT", "").lower() == "dev"

# ── VAPID keys for Web Push ───────────────────────────────────────────────────

def _init_vapid() -> tuple[str, str]:
    """Load or auto-generate VAPID keys. Returns (private_key_raw_b64url, public_key_b64url).

    The private key is stored/returned as a RAW base64url string (the 32-byte EC
    private scalar) — not PEM. py_vapid mis-parses PEM ("could not deserialize key
    data / invalid length"); the raw base64url form is the universal VAPID format
    and is loaded unambiguously by pywebpush. Existing PEM key files are migrated
    in place so the SAME keypair is preserved (browser subscriptions stay valid).
    """
    import base64
    from cryptography.hazmat.primitives.asymmetric.ec import (
        generate_private_key, SECP256R1)
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, load_pem_private_key)

    def _raw_priv(key) -> str:
        val = key.private_numbers().private_value
        return base64.urlsafe_b64encode(val.to_bytes(32, "big")).decode().rstrip("=")

    def _pub(key) -> str:
        b = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    vapid_file = os.path.join(os.path.dirname(db.DB_PATH) or "data", "vapid_keys.json")
    if os.path.exists(vapid_file):
        try:
            with open(vapid_file) as f:
                keys = json.load(f)
            if keys.get("private_key_raw") and keys.get("public_key"):
                return keys["private_key_raw"], keys["public_key"]
            # Migrate an old PEM-format file → raw, preserving the SAME keypair.
            if keys.get("private_key_pem"):
                key = load_pem_private_key(keys["private_key_pem"].encode(), password=None)
                raw, pub = _raw_priv(key), _pub(key)
                with open(vapid_file, "w") as f:
                    json.dump({"private_key_raw": raw, "public_key": pub}, f)
                return raw, pub
        except Exception:
            logging.exception("Failed to load VAPID keys; regenerating")

    key = generate_private_key(SECP256R1())
    raw, pub = _raw_priv(key), _pub(key)
    os.makedirs(os.path.dirname(vapid_file) or ".", exist_ok=True)
    with open(vapid_file, "w") as f:
        json.dump({"private_key_raw": raw, "public_key": pub}, f)
    return raw, pub


_VAPID_PRIVATE_KEY, _VAPID_PUBLIC_KEY = _init_vapid()
_VAPID_CLAIMS_EMAIL = os.getenv("APP_ADMIN_EMAIL") or "admin@example.com"

# ── Media upload storage ───────────────────────────────────────────────────────
# Anchor to the same parent directory as DB_PATH so media lives in the same
# persistent volume as the database regardless of working directory.
MEDIA_DIR = Path(os.getenv("DB_PATH", "data/cards.db")).parent.resolve() / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024   # 20 MB raw input limit
_MAX_IMAGE_DIM    = 1280               # longest edge after resize
_JPEG_QUALITY     = 82

# Pre-generated, committed dev-variant icons (orange tint + "DEV" badge). Served
# as static files in dev — no runtime image library needed. See
# scripts/make_dev_icons.py for how they were produced.


def _compute_asset_version() -> str:
    """Content hash of all static files. Changes only when an asset changes,
    so deploys bust browser/service-worker caches without manual version bumps."""
    h = hashlib.sha256()
    for p in sorted(_static.rglob("*")):
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


ASSET_VERSION = _compute_asset_version()


def _build_nav(active: str = "") -> str:
    """Return the full <header> inner HTML with the active page highlighted."""
    _i = ('class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
          'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"')
    svgs = {
        "translate": f'<svg {_i}><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
        "cards":     f'<svg {_i}><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
        "reader":    f'<svg {_i}><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
        "learn":     f'<svg {_i}><path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1 2.5 2.5 6 2.5s6-1.5 6-2.5v-5"/></svg>',
        "tutor":     f'<svg {_i}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
        "messages":  f'<svg {_i}><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>',
        "feedback":  f'<svg {_i}><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
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
        "dashboard": f'<svg {_i}><path d="M18 20V10"/><path d="M12 20V4"/><path d="M6 20v-6"/></svg>',
        "signout":   f'<svg {_i}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
        "browse":    f'<svg {_i}><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
        "hamburger": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>',
    }

    def link(href: str, label: str, icon: str, badge: bool = False, notif: bool = False) -> str:
        hl = ' style="color:var(--primary)"' if href == active else ""
        bdg = ' <span class="badge due-badge"></span>' if badge else ""
        nbd = ' <span class="badge notif-badge"></span>' if notif else ""
        return f'    <a href="{href}" class="nav-link"{hl}>\n      {svgs[icon]}\n      {label}{bdg}{nbd}\n    </a>'

    def admin_link(href: str, label: str, icon: str) -> str:
        extra = "color:var(--primary);" if href == active else ""
        return (f'    <a href="{href}" class="nav-link nav-admin"'
                f' style="display:none;{extra}">\n      {svgs[icon]}\n      {label}\n    </a>')

    nav_links = [
        link("/",         "Add Vocab",  "translate"),
        link("/cards",    "Flashcards", "cards",    badge=True),
        link("/reader",   "Reader",     "reader"),
        link("/learn",    "Learn",      "learn"),
        link("/tutor",    "Tutor",      "tutor"),
        link("/messages", "Messages",   "messages", notif=True),
        link("/browse",   "Browse",     "browse"),
        link("/feedback", "Feedback",   "feedback"),
        link("/settings", "Settings",   "settings"),
        admin_link("/admin/dashboard", "Dashboard", "dashboard"),
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
    return (
        "  <h1>{{APP_NAME_HTML}}</h1>\n"
        "  <nav class=\"nav-desktop\">\n"
        + "\n".join(nav_links) + "\n"
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
        + "\n".join(nav_links) + "\n"
        + signout_dropdown + "\n"
        "  </nav>\n"
        "  <script>"
        "function toggleMobileMenu(){document.getElementById('nav-dropdown').classList.toggle('open')}\n"
        "function closeMobileMenu(){document.getElementById('nav-dropdown').classList.remove('open')}\n"
        "function doLogout(){fetch('/api/logout',{method:'POST'}).catch(function(){}).then(function(){window.location.replace('/login')})}\n"
        "document.addEventListener('click',function(e){"
        "if(!e.target.closest('header')){var d=document.getElementById('nav-dropdown');if(d)d.classList.remove('open')}"
        "});\n"
        "fetch('/api/me').then(function(r){return r.json()}).then(function(u){"
        "if(u.is_admin)document.querySelectorAll('.nav-admin').forEach(function(el){el.style.display=''})"
        "}).catch(function(){});"
        "</script>\n"
    )


_LANG_WIDGET = """
<script>
(function () {
  Promise.all([
    fetch('/api/languages').then(function (r) { return r.ok ? r.json() : null; }),
    fetch('/api/settings').then(function (r) { return r.ok ? r.json() : null; }),
  ]).then(function (results) {
    var langRes = results[0]; var settingsRes = results[1];
    if (!langRes || !settingsRes) return;
    var langs = langRes.languages || [];
    var currentCode = settingsRes.default_target_lang || 'yue';
    var current = langs.find(function (l) { return l.code === currentCode; }) || { name: currentCode, flag: '🌐' };
    if (langs.length < 2) return;  // nothing to switch to
    // Alphabetize so options are easy to scan.
    langs = langs.slice().sort(function (a, b) { return (a.name || '').localeCompare(b.name || ''); });

    var wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative;display:inline-flex;align-items:center;';

    var pill = document.createElement('button');
    pill.id = 'lang-pill';
    pill.title = 'Change learning language';
    pill.style.cssText = 'background:var(--surface);border:1px solid var(--border);border-radius:999px;'
      + 'padding:3px 9px 3px 7px;font-size:0.7em;font-weight:600;cursor:pointer;'
      + 'display:inline-flex;align-items:center;gap:4px;color:var(--text);line-height:1.4;'
      + 'margin-left:8px;white-space:nowrap;vertical-align:middle;';
    // Split flag and name into separate spans so CSS can hide the name on narrow screens
    // (below 760px the nav collapses and only the flag emoji + chevron remain visible).
    var flagSpan = document.createElement('span');
    flagSpan.textContent = current.flag || '🌐';
    var nameSpan = document.createElement('span');
    nameSpan.className = 'lang-pill-name';
    nameSpan.textContent = current.name;
    var chevronSpan = document.createElement('span');
    chevronSpan.innerHTML = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
    pill.appendChild(flagSpan);
    pill.appendChild(nameSpan);
    pill.appendChild(chevronSpan);

    var dd = document.createElement('div');
    // Grid laid out column-by-column (fill a column top-to-bottom, then wrap to
    // the next). Column/row counts are computed at show time from the viewport
    // (layoutDropdown) so all options try to stay on screen, growing taller
    // before wider and falling back to vertical scroll when they can't fit.
    dd.style.cssText = 'display:none;position:fixed;box-sizing:border-box;'
      + 'background:var(--surface);border:1px solid var(--border);border-radius:10px;'
      + 'box-shadow:var(--shadow-pop);z-index:2000;padding:4px;max-height:84vh;overflow-y:auto;'
      + 'grid-auto-flow:column;';

    langs.forEach(function (l) {
      var opt = document.createElement('div');
      opt.dataset.code = l.code;
      opt.style.cssText = 'padding:9px 12px;cursor:pointer;font-size:0.85em;border-radius:6px;'
        + 'font-weight:500;display:flex;align-items:center;gap:8px;break-inside:avoid;'
        + 'color:' + (l.code === currentCode ? 'var(--primary)' : 'var(--text)') + ';';
      var check = document.createElement('span');
      check.style.cssText = 'width:14px;font-size:0.8em;flex-shrink:0;';
      check.textContent = l.code === currentCode ? '✓' : '';
      opt.appendChild(check);
      var lbl = document.createElement('span');
      lbl.textContent = (l.flag ? l.flag + ' ' : '') + l.name;
      opt.appendChild(lbl);
      opt.addEventListener('mouseenter', function () { opt.style.background = 'var(--bg)'; });
      opt.addEventListener('mouseleave', function () { opt.style.background = ''; });
      opt.addEventListener('click', function (e) {
        e.stopPropagation();
        if (l.code === currentCode) { dd.style.display = 'none'; return; }
        opt.style.opacity = '0.5';
        opt.style.pointerEvents = 'none';
        pill.style.pointerEvents = 'none';
        fetch('/api/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ default_target_lang: l.code }),
        }).then(function () {
          flagSpan.textContent = l.flag || '🌐';
          nameSpan.textContent = l.name;
          dd.style.display = 'none';
          // Update checkmarks and highlight
          var opts = dd.querySelectorAll('[data-code]');
          for (var i = 0; i < opts.length; i++) {
            opts[i].querySelector('span').textContent = '';
            opts[i].style.color = 'var(--text)';
          }
          check.textContent = '✓';
          opt.style.color = 'var(--primary)';
          opt.style.opacity = '';
          opt.style.pointerEvents = '';
          pill.style.pointerEvents = '';
          currentCode = l.code;
          document.dispatchEvent(new CustomEvent('langchange', { detail: { code: l.code, lang: l } }));
        }).catch(function () {
          opt.style.opacity = ''; opt.style.pointerEvents = '';
          pill.style.pointerEvents = '';
        });
      });
      dd.appendChild(opt);
    });

    // Compute a column-first grid that fills vertical space first, adds columns
    // only as needed, stays within the viewport width, and falls back to a
    // vertical scroll when even the widest fit can't hold everything.
    function layoutDropdown() {
      var n = langs.length;
      var rowH = 36, colW = 168;
      var availH = window.innerHeight * 0.84 - 16;
      var availW = window.innerWidth - 16;
      var maxRows = Math.max(4, Math.floor(availH / rowH));
      var maxCols = Math.max(1, Math.floor(availW / colW));
      var cols = Math.max(1, Math.min(maxCols, Math.ceil(n / maxRows)));
      var rows = Math.ceil(n / cols);
      dd.style.gridTemplateRows = 'repeat(' + rows + ', auto)';
      dd.style.gridTemplateColumns = 'repeat(' + cols + ', 1fr)';
      dd.style.width = Math.min(cols * colW, availW) + 'px';
    }

    pill.addEventListener('click', function (e) {
      e.stopPropagation();
      var show = dd.style.display === 'none';
      if (show) {
        layoutDropdown();
        dd.style.display = 'grid';
        var pr = pill.getBoundingClientRect();
        dd.style.top = (pr.bottom + 6) + 'px';
        dd.style.left = '0'; dd.style.right = 'auto';
        var dr = dd.getBoundingClientRect();
        var centerX = pr.left + pr.width / 2 - dr.width / 2;
        var clampedX = Math.max(8, Math.min(centerX, window.innerWidth - dr.width - 8));
        dd.style.left = clampedX + 'px';
      } else {
        dd.style.display = 'none';
      }
    });
    document.addEventListener('click', function () { dd.style.display = 'none'; });

    wrap.appendChild(pill);
    wrap.appendChild(dd);

    var h1 = document.querySelector('header h1');
    if (h1) h1.appendChild(wrap);
  }).catch(function () {});
})();
</script>
"""

_PLAN_WIDGET = """
<script>
(function () {
  fetch('/api/billing/status').then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (!d) return;
    var h1 = document.querySelector('header h1');
    if (h1 && !document.getElementById('plan-pill')) {
      var label = d.unlimited ? '\\u221E' : (d.plan === 'pro' ? 'Pro' : 'Free');
      var pill = document.createElement('a');
      pill.id = 'plan-pill';
      pill.href = '/settings#plan-section';
      pill.textContent = label;
      pill.title = 'Your plan: ' + label + ' \\u2014 manage in Settings';
      var hot = (d.plan === 'pro' || d.unlimited);
      pill.style.cssText = 'margin-left:8px;font-size:0.6em;font-weight:700;padding:2px 9px;border-radius:999px;'
        + 'text-decoration:none;vertical-align:middle;letter-spacing:0.02em;'
        + (hot ? 'background:var(--primary);color:#fff;' : 'background:var(--border);color:var(--text-muted);');
      h1.appendChild(pill);
    }
    if (d.billing_enabled && !d.unlimited && d.plan === 'free'
        && location.pathname !== '/settings'
        && !localStorage.getItem('canto_hide_upgrade')) {
      var bar = document.createElement('div');
      bar.style.cssText = 'display:flex;align-items:center;gap:12px;justify-content:center;flex-wrap:wrap;'
        + 'padding:8px 16px;background:var(--primary);color:#fff;font-size:0.9rem;';
      var msg = document.createElement('span');
      msg.textContent = "You're on the Free plan (" + d.used + "/" + d.limit
        + " AI uses this month). Upgrade to Pro for " + d.pro_limit + "/month.";
      var up = document.createElement('button');
      up.textContent = 'Upgrade \\u2014 $5/mo';
      up.style.cssText = 'background:#fff;color:var(--primary);border:none;border-radius:6px;'
        + 'padding:5px 12px;font-weight:600;cursor:pointer;';
      up.onclick = function () {
        up.disabled = true;
        fetch('/api/billing/checkout', { method: 'POST' }).then(function (r) { return r.json(); })
          .then(function (b) { if (b && b.url) { location.href = b.url; } else { up.disabled = false; } })
          .catch(function () { up.disabled = false; });
      };
      var x = document.createElement('button');
      x.textContent = '\\u2715';
      x.title = 'Dismiss';
      x.style.cssText = 'background:none;border:none;color:#fff;cursor:pointer;font-size:1rem;line-height:1;';
      x.onclick = function () { localStorage.setItem('canto_hide_upgrade', '1'); bar.remove(); };
      bar.appendChild(msg); bar.appendChild(up); bar.appendChild(x);
      document.body.insertBefore(bar, document.body.firstChild);
    }
  }).catch(function () {});
})();
</script>
"""

_NOTIF_WIDGET = """
<script>
// ── Notification badge polling ────────────────────────────────────────────────
(function () {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }
  function _updateNotifBadges(total) {
    document.querySelectorAll('.notif-badge').forEach(function (b) {
      b.textContent = total > 99 ? '99+' : String(total);
      b.classList.toggle('visible', total > 0);
    });
  }
  function _loadNotifCounts() {
    fetch('/api/notifications/counts')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        _updateNotifBadges((d.unread_messages || 0) + (d.friend_requests || 0));
      })
      .catch(function () {});
  }
  _loadNotifCounts();
  setInterval(_loadNotifCounts, 60000);
  window._refreshNotifCounts = _loadNotifCounts;
})();

// ── Push notification helpers (global — used by .notif-bell-btn on any page) ──
const _SVG_BELL_ON = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>`;
const _SVG_BELL_OFF = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8.56 2.9A7 7 0 0 1 19 9v4m-2 4H3s3-2 3-9a4.67 4.67 0 0 1 .3-1.7"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

function _urlBase64ToUint8Array(s) {
  const padding = '='.repeat((4 - s.length % 4) % 4);
  const b64 = (s + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
}

function _syncBellState() {
  const supported = 'Notification' in window && 'PushManager' in window && 'serviceWorker' in navigator;
  const perm = supported ? Notification.permission : 'denied';
  const subscribed = localStorage.getItem('push_subscribed') === '1';
  const on = supported && perm === 'granted' && subscribed;
  document.querySelectorAll('.notif-bell-btn').forEach(btn => {
    btn.innerHTML = on ? _SVG_BELL_ON : _SVG_BELL_OFF;
    btn.classList.toggle('enabled', on);
    btn.title = !supported ? 'Push notifications not supported in this browser'
      : perm === 'denied' ? 'Notifications blocked — change in browser settings'
      : on ? 'Notifications on — tap to turn off'
      : 'Enable push notifications';
  });
  document.querySelectorAll('.notif-status-desc').forEach(el => {
    el.textContent = !supported ? 'Not supported in this browser'
      : perm === 'denied' ? 'Blocked — change in browser or OS settings'
      : on ? 'On — you will be notified about messages and friend requests'
      : 'Off';
  });
}

async function _verifyBellState() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  try {
    const reg = await navigator.serviceWorker.getRegistration('/');
    if (!reg) return;
    const sub = await reg.pushManager.getSubscription();
    localStorage.setItem('push_subscribed', (!!sub && Notification.permission === 'granted') ? '1' : '0');
    _syncBellState();
  } catch { /* leave cached state */ }
}

async function _getSwRegistration() {
  const existing = await navigator.serviceWorker.getRegistration('/');
  if (existing && existing.active) return existing;
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise((_, rej) => setTimeout(() => rej(new Error('Service worker not ready — reload and try again')), 15000)),
  ]);
}

async function toggleNotifications() {
  const _toast = typeof showToast === 'function' ? showToast : () => {};
  const btns = [...document.querySelectorAll('.notif-bell-btn')];
  if (!('Notification' in window) || !('PushManager' in window) || !('serviceWorker' in navigator)) {
    _toast(/iPad|iPhone|iPod/.test(navigator.userAgent)
      ? 'Open Settings → Safari and allow notifications, or add the app to your Home Screen.'
      : 'Push notifications are not supported in this browser.');
    return;
  }
  if (Notification.permission === 'denied') {
    _toast('Notifications are blocked. Go to your browser or OS settings to re-enable.');
    return;
  }
  if (Notification.permission === 'granted') {
    btns.forEach(b => { b.disabled = true; });
    try {
      const reg = await _getSwRegistration();
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        const json = sub.toJSON();
        await sub.unsubscribe();
        await fetch('/api/push/subscribe', {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: json.endpoint, p256dh: json.keys.p256dh, auth: json.keys.auth }),
        }).catch(() => {});
        localStorage.setItem('push_subscribed', '0');
        _toast('Notifications turned off.');
        btns.forEach(b => { b.disabled = false; });
        _syncBellState();
        return;
      }
    } catch (e) {
      _toast('Error: ' + (e.message || 'could not check subscription'));
      btns.forEach(b => { b.disabled = false; });
      return;
    }
    btns.forEach(b => { b.disabled = false; });
  }
  // iOS REQUIREMENT: requestPermission() must be the FIRST await from the click handler.
  let perm;
  try { perm = await Notification.requestPermission(); }
  catch (e) { _toast('Permission request failed: ' + (e.message || 'unknown')); return; }
  if (perm !== 'granted') { _toast('Notification permission not granted.'); _syncBellState(); return; }
  btns.forEach(b => { b.disabled = true; });
  try {
    const { public_key } = await fetch('/api/push/vapid-public-key').then(r => r.json());
    const reg = await _getSwRegistration();
    const newSub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: _urlBase64ToUint8Array(public_key) });
    const j = newSub.toJSON();
    await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth }),
    });
    localStorage.setItem('push_subscribed', '1');
    _syncBellState();
    _toast('Notifications enabled!');
  } catch (e) {
    localStorage.setItem('push_subscribed', '0');
    _toast('Could not subscribe: ' + (e.message || 'unknown error'));
  }
  btns.forEach(b => { b.disabled = false; });
  _syncBellState();
}

_syncBellState();
_verifyBellState();

// ── Offline / online indicator ────────────────────────────────────────────────
(function () {
  function _setOfflineBanner(offline) {
    var el = document.getElementById('_sw-offline-bar');
    if (offline) {
      if (el) return;
      el = document.createElement('div');
      el.id = '_sw-offline-bar';
      el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9998;'
        + 'background:#ef4444;color:#fff;text-align:center;padding:6px 16px;'
        + 'font-size:0.82rem;font-weight:600;letter-spacing:0.01em;';
      el.textContent = "You’re offline — studying still works, changes will sync when reconnected";
      document.body.insertBefore(el, document.body.firstChild);
    } else {
      if (el) el.remove();
    }
  }
  if (!navigator.onLine) _setOfflineBanner(true);
  window.addEventListener('offline', function () { _setOfflineBanner(true); });
  window.addEventListener('online',  function () { _setOfflineBanner(false); });
})();
</script>
"""


_TOUR_WIDGET = """
<div id="tour-overlay" class="tour-overlay" style="display:none" aria-modal="true" role="dialog">
  <div class="tour-card">
    <button class="tour-skip" onclick="_tourDismiss()" title="Skip tour">✕</button>
    <div class="tour-step-num" id="tour-step-num"></div>
    <div class="tour-icon" id="tour-icon"></div>
    <h2 class="tour-title" id="tour-title"></h2>
    <p class="tour-body" id="tour-body"></p>
    <div class="tour-footer">
      <div class="tour-dots" id="tour-dots"></div>
      <button class="tour-next-btn" id="tour-next-btn" onclick="_tourNext()">Next →</button>
    </div>
  </div>
</div>
<script src="/static/tour.js"></script>
"""

_PWA_INSTALL_WIDGET = """
<div id="pwa-install-banner" style="display:none;position:fixed;bottom:0;left:0;right:0;z-index:500;padding:0 12px 12px;pointer-events:none">
  <div style="max-width:440px;margin:0 auto;background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow-pop);padding:14px 16px;display:flex;align-items:flex-start;gap:12px;pointer-events:auto">
    <div style="font-size:1.6rem;line-height:1;flex-shrink:0">📲</div>
    <div style="flex:1;min-width:0">
      <div style="font-weight:700;font-size:0.92rem;margin-bottom:3px">Install this app</div>
      <div id="pwa-install-body" style="font-size:0.82rem;color:var(--text-muted);line-height:1.4">
        Add to your home screen for a full-screen experience with faster loading.
      </div>
      <div style="margin-top:10px;display:flex;gap:8px">
        <button id="pwa-install-action" style="background:var(--primary);color:#fff;border:none;border-radius:8px;padding:6px 14px;font-size:0.82rem;font-weight:600;cursor:pointer;display:none" onclick="_pwaInstall()">Install</button>
        <button style="background:none;border:1px solid var(--border);border-radius:8px;padding:6px 14px;font-size:0.82rem;color:var(--text-muted);cursor:pointer" onclick="_pwaDismiss()">Not now</button>
      </div>
    </div>
    <button style="background:none;border:none;color:var(--text-muted);font-size:1.1rem;cursor:pointer;padding:0 2px;line-height:1;flex-shrink:0" onclick="_pwaDismiss()">✕</button>
  </div>
</div>
<script src="/static/pwa-install.js"></script>
<script>
// Platform-specific instructions + install button
(function(){
  var isIOS = /iP(hone|ad|od)/.test(navigator.userAgent);
  var body = document.getElementById('pwa-install-body');
  var btn = document.getElementById('pwa-install-action');
  if (isIOS && body) {
    body.innerHTML = 'Tap <svg style="display:inline-block;vertical-align:-3px" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--primary)" stroke-width="2.5" stroke-linecap="round"><path d="M4 12v7a2 2 0 002 2h12a2 2 0 002-2v-7"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg> then <b>Add to Home Screen</b>';
  } else if (btn) {
    btn.style.display = '';
  }
})();
</script>
"""


def _html(name: str, active: str = "") -> HTMLResponse:
    content = (_static / name).read_text()
    has_nav = "{{NAV}}" in content
    content = content.replace("{{NAV}}", _build_nav(active), 1)
    content = content.replace("{{APP_NAME}}", APP_NAME)
    content = content.replace("{{APP_NAME_HTML}}", _APP_NAME_HTML)
    content = content.replace("{{TURNSTILE_SITE_KEY}}", _TURNSTILE_SITE_KEY)
    content = content.replace("/static/style.css", f"/static/style.css?v={ASSET_VERSION}")
    content = content.replace("/static/label-picker.js", f"/static/label-picker.js?v={ASSET_VERSION}")
    content = content.replace("/static/tour.js", f"/static/tour.js?v={ASSET_VERSION}")
    content = content.replace("/static/pwa-install.js", f"/static/pwa-install.js?v={ASSET_VERSION}")
    content = content.replace("{{ASSET_VERSION}}", ASSET_VERSION)
    # In dev, point the favicon + apple-touch-icon at the badged dev icons so the
    # browser tab and iOS homescreen visibly differ from prod. (The manifest alone
    # isn't enough — iOS prefers apple-touch-icon over it.)
    if IS_DEV:
        content = content.replace("/static/icons/icon-192.png", "/static/icons/icon-dev-192.png")
        content = content.replace("/static/icons/icon-512.png", "/static/icons/icon-dev-512.png")
    content = content.replace(
        "</head>",
        f'<script>window.__VERSION__="{ASSET_VERSION}"</script></head>',
        1,
    )
    # Inject the plan badge + upgrade banner on authenticated app pages (those
    # with the shared nav); login/register pages have no nav and are skipped.
    if has_nav:
        content = content.replace("</body>", _LANG_WIDGET + _PLAN_WIDGET + _NOTIF_WIDGET + _TOUR_WIDGET + _PWA_INSTALL_WIDGET + "</body>", 1)
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
    if IS_DEV:
        data = {
            "name": "廣東卡 DEV",
            "short_name": "卡 DEV",
            "description": "HK Cantonese translation and flashcards (dev)",
            "start_url": "/cards",
            "display": "standalone",
            "background_color": "#fff7ed",   # warm orange tint
            "theme_color": "#ea580c",        # orange-600
            "orientation": "portrait",
            "icons": [
                {"src": "/static/icons/icon-dev-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-dev-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            ],
        }
        return JSONResponse(data, headers={"Content-Type": "application/manifest+json"})
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
        # "lax" (not "strict") so the cookie survives Stripe's cross-site
        # redirect back to /settings after Checkout. Still CSRF-safe: not sent
        # on cross-site POSTs, only top-level GET navigations.
        samesite="lax",
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

@app.get("/", response_class=HTMLResponse)
async def index():
    return _html("index.html", active="/")


@app.get("/cards", response_class=HTMLResponse)
async def cards_page():
    return _html("cards.html", active="/cards")


@app.get("/reader", response_class=HTMLResponse)
async def reader_page():
    return _html("reader.html", active="/reader")


@app.get("/learn", response_class=HTMLResponse)
async def learn_page():
    return _html("learn.html", active="/learn")


@app.get("/tutor", response_class=HTMLResponse)
async def tutor_page():
    return _html("tutor.html", active="/tutor")


@app.get("/messages", response_class=HTMLResponse)
async def messages_page():
    return _html("messages.html", active="/messages")


@app.get("/browse", response_class=HTMLResponse)
async def browse_page():
    return _html("browse.html", active="/browse")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return _html("settings.html", active="/settings")


@app.get("/feedback", response_class=HTMLResponse)
async def feedback_page():
    return _html("feedback.html", active="/feedback")


@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page():
    return _html("welcome.html")


@app.get("/admin")
async def admin_page(_: dict = Depends(current_admin)):
    return RedirectResponse("/settings", status_code=301)


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard_page(_: dict = Depends(current_admin)):
    return _html("dashboard.html", active="/admin/dashboard")


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


# Cloudflare Turnstile (signup CAPTCHA). Both keys unset = disabled: the widget
# doesn't render and _verify_turnstile passes, so local/dev signup still works.
_TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")
_TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")
_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def _verify_turnstile(token: str, remoteip: str | None) -> bool:
    """Validate a Turnstile token with Cloudflare. Returns True when CAPTCHA is
    not configured (no secret); otherwise fails closed on a bad/empty token or
    any network error."""
    if not _TURNSTILE_SECRET_KEY:
        return True
    if not token:
        return False
    data = {"secret": _TURNSTILE_SECRET_KEY, "response": token}
    if remoteip:
        data["remoteip"] = remoteip
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_TURNSTILE_VERIFY_URL, data=data)
            return bool(resp.json().get("success"))
    except Exception:
        return False


class RegisterRequest(BaseModel):
    email: str
    username: str
    display_name: str
    password: str
    turnstile_token: str = ""


@app.post("/api/register")
@limiter.limit("5/minute;20/hour")
async def register(request: Request, req: RegisterRequest):
    if not await _verify_turnstile(req.turnstile_token, get_remote_address(request)):
        raise HTTPException(400, "Captcha verification failed. Please try again.")
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
# Server-side Anthropic key (admin-billed) for the pluggable lesson model. Only
# used when an admin selects a `claude-*` lesson_model; unset = Gemini-only.
_SHARED_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

# Facebook Messenger integration (optional — features degrade gracefully when unset).
_FB_APP_ID = os.getenv("FACEBOOK_APP_ID")
_FB_APP_SECRET = os.getenv("FACEBOOK_APP_SECRET")
_FB_WEBHOOK_VERIFY_TOKEN = os.getenv("FACEBOOK_WEBHOOK_VERIFY_TOKEN", "canto_verify")

# Monthly shared-key AI-call allowance per plan. Own-key/admin/granted users are
# unlimited. These caps bound cost exposure: even pro's 600 calls is ~$0.08/mo
# of Gemini at current Flash-Lite rates.
PLAN_LIMITS = {"free": 30, "pro": 600}

# App-wide DAILY ceiling on shared-key AI calls — a circuit breaker protecting
# the provider's free-tier quota (Gemini ~1,500 req/day) from aggregate load
# across ALL users, independent of any single user's monthly plan cap. Set below
# the hard ceiling because one user action can fan out into several upstream
# calls (e.g. a lesson = planner + author). Admins bypass the block so the owner
# can always test. Override via env in case the provider quota changes.
SHARED_KEY_DAILY_CAP = int(os.getenv("SHARED_KEY_DAILY_CAP", "1200"))


class _GeminiAccess:
    def __init__(self, api_key: str, model_translate: str, model_reader: str):
        self.api_key = api_key
        self.model_translate = model_translate
        self.model_reader = model_reader
        # Server-side Claude key, threaded through to the pluggable lesson model.
        self.anthropic_key = _SHARED_ANTHROPIC_KEY


def _valid_model(value: str | None) -> str:
    return value if value in translation.MODEL_ALLOWLIST else translation.DEFAULT_MODEL


# Models allowed for the admin lesson-pipeline A/B knob (Gemini + Claude). Claude
# ids route through llm.call to the Anthropic SDK on the shared server key.
LESSON_MODEL_ALLOWLIST = translation.MODEL_ALLOWLIST + ["claude-sonnet-4-6", "claude-opus-4-8"]


def _valid_lesson_model(value: str | None) -> str | None:
    return value if value in LESSON_MODEL_ALLOWLIST else None


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
        # The owner is never blocked by the daily breaker, but still counts toward
        # the shared-key tally so it reflects true provider load.
        if meter:
            await db.increment_global_usage_today()
        return _GeminiAccess(
            _SHARED_API_KEY,
            _valid_model(await db.get_setting(user["id"], "model_translate")),
            _valid_model(await db.get_setting(user["id"], "model_reader")),
        )

    if not _SHARED_API_KEY:
        raise HTTPException(503, "Shared API key is not configured.")

    # Global circuit breaker: refuse all non-admin shared-key calls once the
    # app-wide daily ceiling is hit, so aggregate load can't exhaust the
    # provider's free-tier quota and 503 everyone uncontrollably. Checked even
    # for derived (meter=False) calls — at capacity the key is fully protected —
    # but only metered calls add to the tally below.
    if await db.get_global_usage_today() >= SHARED_KEY_DAILY_CAP:
        raise HTTPException(503, (
            "AI features are temporarily at capacity for today. Please try again "
            "tomorrow, or add your own Gemini key in Settings for uninterrupted use."
        ))

    # Everyone else spends the shared key, metered against their plan's monthly
    # allowance (free 30 / pro 600). Pro can be self-serve via Stripe or comped
    # by the admin. The shared key always runs the default (cheapest) model.
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
        await db.increment_global_usage_today()

    return _GeminiAccess(_SHARED_API_KEY, translation.DEFAULT_MODEL, translation.DEFAULT_MODEL)


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


async def _backfill_card_embeddings(user_id: int, api_key: str, *, limit: int) -> None:
    """Embed a bounded batch of cards that lack an embedding, storing each.
    Best-effort — used by suggest-cards so a pre-existing deck converges over a
    few calls (cards.embedding was NULL for all cards before embeddings worked)."""
    missing = await db.get_cards_missing_embedding(user_id, limit)
    if not missing:
        return
    texts = [f"{(c['source_text'] or '').strip()} {(c['target_text'] or '').strip()}".strip()
             for c in missing]
    try:
        vecs = await embeddings.embed(texts, api_key)
    except Exception as e:
        logger.warning("card embedding backfill failed user=%s: %s", user_id, e)
        return
    for card, vec in zip(missing, vecs):
        if vec:
            await db.update_card_embedding(card["id"], json.dumps(vec))


async def _atomize_phrase_bg(
    user_id: int, phrase: str, lang: str,
    label_ids: list[int], priority: int, api_key: str, model: str,
) -> None:
    """Background: translate each word in a phrase and create missing atomic cards.

    Skips words already in the user's deck (exact or normalized match).
    Best-effort — any individual failure is swallowed so the task always completes.
    """
    words = tokenizer.phrase_words(phrase, lang)
    if len(words) <= 1:
        return
    statuses = await db.get_word_statuses(user_id, words, lang)
    for word in words:
        if word in statuses:
            continue
        try:
            result = await translation.translate(
                word, lang, source_is_target=True,
                api_key=api_key, model=model,
            )
            candidate = result["candidates"][0] if result["candidates"] else {}
            if not candidate.get("english"):
                continue
            audio_data = await audio.generate(word, lang)
            rom = tokenizer.romanize_text(word, lang) or candidate.get("romanization", "")
            await db.create_card(
                user_id=user_id,
                source_text=candidate["english"],
                target_text=word,
                romanization=rom,
                target_lang=lang,
                audio_data=audio_data,
                notes=candidate.get("notes"),
                label_ids=label_ids,
                priority=result.get("priority", priority),
                classifier=result.get("classifier", ""),
                suggested_label_names=result.get("suggested_labels", []),
                cefr_level=result.get("cefr_level"),
            )
        except Exception:
            pass


async def _autolabel_card_bg(
    user_id: int, card_id: int, source_text: str, target_text: str,
    target_lang: str, api_key: str, model: str,
) -> None:
    """Background: generate + attach organisational labels for a card that was
    created without any. Keeps every add path (tutor chips, lesson 'add to deck',
    atomized words) consistently labeled. Best-effort."""
    try:
        names = await translation.suggest_labels(
            source_text, target_text, target_lang, api_key=api_key, model=model,
        )
        if names:
            await db.add_labels_by_name(user_id, card_id, names)
    except Exception:
        pass


async def _backfill_classifiers_bg(user_id: int, lang: str, api_key: str) -> None:
    """Background: fill missing classifiers/articles for all cards of a language.
    Processes in batches of 60. Best-effort."""
    import asyncio as _asyncio
    cards = await db.get_cards_missing_classifier(user_id, lang)
    for i in range(0, len(cards), 60):
        batch = cards[i:i + 60]
        words = [c["target_text"] for c in batch]
        try:
            classifiers = await _asyncio.to_thread(
                translation.get_classifiers_batch, words, lang, api_key
            )
            for card in batch:
                cl = classifiers.get(card["target_text"], "")
                if cl:
                    await db.update_card_classifier(card["id"], cl, user_id)
        except Exception:
            pass


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

    # Fill missing romanization from the offline oracle (lesson "Add to deck"
    # and tutor chips send none) — never ask the LLM for it.
    # For Thai: always prefer the offline oracle — it adds tone diacritics
    # (à â á ǎ) that the LLM omits from RTGS romanization.
    romanization = req.romanization.strip()
    if translation.LANG_INFO[req.target_lang].get("romanization"):
        oracle_rom = tokenizer.romanize_text(target_text, req.target_lang)
        if oracle_rom:
            romanization = oracle_rom

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
        romanization=romanization,
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

    # Background side-effects (best-effort; skip if no usable key).
    try:
        access = await _resolve_gemini(user, meter=False)
        embed_text = f"{req.source_text.strip()} {target_text}"
        background_tasks.add_task(_generate_and_store_embedding, card_id, embed_text, access.api_key)
        # Auto-atomize phrase cards: translate each constituent word into its own card.
        if len(tokenizer.phrase_words(target_text, req.target_lang)) > 1:
            background_tasks.add_task(
                _atomize_phrase_bg,
                user["id"], target_text, req.target_lang,
                extra_label_ids, req.priority, access.api_key, access.model_translate,
            )
        # Auto-label cards added without any labels (tutor chips, lesson "add to
        # deck", etc.) so every word in the deck is organised, like translate adds.
        if not extra_label_ids and not (req.suggested_labels or []):
            background_tasks.add_task(
                _autolabel_card_bg,
                user["id"], card_id, req.source_text.strip(), target_text,
                req.target_lang, access.api_key, access.model_translate,
            )
    except HTTPException:
        pass

    return {"card_id": card_id, "notes": notes, "labels": []}


class CardStatusRequest(BaseModel):
    words: list[str]
    lang: str


@app.post("/api/cards/status")
async def card_statuses(req: CardStatusRequest, user: dict = Depends(current_user)):
    """Deck status for a list of words: {word: 'known'|'weak'}; absent = not in
    deck. Used by lesson results + tutor 'Add to deck' chips to avoid duplicates."""
    if req.lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    words = [w.strip() for w in req.words[:100] if (w or "").strip()]
    return {"statuses": await db.get_word_statuses(user["id"], words, req.lang)}


class DeckMaintenanceRequest(BaseModel):
    lang: str


@app.post("/api/cards/atomize")
async def atomize_cards(
    req: DeckMaintenanceRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
):
    """Create atomic word cards for all phrase cards of a language.

    Processes up to 10 phrase cards per call; call again while remaining > 0.
    Returns {total_phrase_cards, queued, remaining}.
    """
    if req.lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    access = await _resolve_gemini(user)
    cards = await db.get_all_cards_basic(user["id"], req.lang)
    phrase_cards = [c for c in cards if len(tokenizer.phrase_words(c["target_text"], req.lang)) > 1]
    batch = phrase_cards[:10]
    queued = 0
    for card in batch:
        words = tokenizer.phrase_words(card["target_text"], req.lang)
        statuses = await db.get_word_statuses(user["id"], words, req.lang)
        new_words = [w for w in words if w not in statuses]
        if new_words:
            queued += len(new_words)
            background_tasks.add_task(
                _atomize_phrase_bg,
                user["id"], card["target_text"], req.lang,
                [], card["priority"], access.api_key, access.model_translate,
            )
    return {
        "total_phrase_cards": len(phrase_cards),
        "queued": queued,
        "remaining": max(0, len(phrase_cards) - len(batch)),
    }


@app.post("/api/cards/backfill-classifiers")
async def backfill_classifiers(
    req: DeckMaintenanceRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
):
    """Fill missing classifiers/articles for all cards of a language in the background."""
    if req.lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    if req.lang not in translation._CLASSIFIER_HINT:
        return {"message": "No classifier system for this language", "total": 0}
    access = await _resolve_gemini(user)
    cards = await db.get_cards_missing_classifier(user["id"], req.lang)
    if not cards:
        return {"message": "All cards already have classifiers", "total": 0}
    background_tasks.add_task(_backfill_classifiers_bg, user["id"], req.lang, access.api_key)
    return {"message": f"Backfilling classifiers for {len(cards)} cards in background", "total": len(cards)}


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(user: dict = Depends(current_user)):
    new_cards_per_day = int(await db.get_setting(user["id"], "new_cards_per_day") or 20)
    default_target_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    auto_add_reader_vocab = (await db.get_setting(user["id"], "auto_add_reader_vocab") or "false") == "true"
    audio_show_romanization = (await db.get_setting(user["id"], "audio_show_romanization") or "true") == "true"
    has_api_key = bool(await db.get_setting(user["id"], "gemini_api_key"))
    tour_seen = await db.get_setting(user["id"], "tour_seen") or "0"
    default_reader_difficulty = await db.get_setting(user["id"], "default_reader_difficulty") or "B1"
    lesson_buffer = max(0, min(10, int(await db.get_setting(user["id"], "lesson_buffer") or 3)))
    populate_min_score = max(0.0, min(1.0, float(await db.get_setting(user["id"], "populate_min_score") or 0.55)))
    return {
        "new_cards_per_day": new_cards_per_day,
        "default_target_lang": default_target_lang,
        "auto_add_reader_vocab": auto_add_reader_vocab,
        "audio_show_romanization": audio_show_romanization,
        "default_reader_difficulty": default_reader_difficulty,
        "has_api_key": has_api_key,
        "is_admin": bool(user.get("is_admin")),
        "tour_seen": tour_seen,
        # You pick models when spending your own money: your own key, or (for the
        # admin) the env key. Plan users on the shared key get the fixed default.
        "can_choose_models": has_api_key or bool(user.get("is_admin")),
        "model_translate": _valid_model(await db.get_setting(user["id"], "model_translate")),
        "model_reader": _valid_model(await db.get_setting(user["id"], "model_reader")),
        "available_models": translation.MODEL_ALLOWLIST,
        "default_model": translation.DEFAULT_MODEL,
        # Admin-only: author Learning-Path lessons on the premium model.
        "lesson_premium": await db.get_setting(user["id"], "lesson_premium") == "1",
        "lesson_premium_model": grammar_lessons.GENERATION_MODEL,
        # Admin-only A/B knob: which model authors lessons (Gemini or Claude).
        # Empty string = follow lesson_premium / the default reader model.
        "lesson_model": _valid_lesson_model(await db.get_setting(user["id"], "lesson_model")) or "",
        "lesson_model_options": LESSON_MODEL_ALLOWLIST,
        "learner_profile": await db.get_setting(user["id"], "learner_profile") or "",
        "lesson_buffer": lesson_buffer,
        "chat_compact_phrases": (await db.get_setting(user["id"], "chat_compact_phrases") or "false") == "true",
        "populate_min_score": populate_min_score,
    }


class SettingsUpdate(BaseModel):
    new_cards_per_day: int | None = None
    default_target_lang: str | None = None
    auto_add_reader_vocab: bool | None = None
    audio_show_romanization: bool | None = None
    default_reader_difficulty: str | None = None
    gemini_api_key: str | None = None
    model_translate: str | None = None
    model_reader: str | None = None
    lesson_premium: bool | None = None
    lesson_model: str | None = None
    learner_profile: str | None = None
    lesson_buffer: int | None = None
    chat_compact_phrases: bool | None = None
    populate_min_score: float | None = None


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
    if req.default_reader_difficulty is not None:
        _VALID_CEFR = {"A1", "A2", "B1", "B2", "C1", "C2"}
        if req.default_reader_difficulty not in _VALID_CEFR:
            raise HTTPException(400, "Invalid CEFR level")
        await db.set_setting(user["id"], "default_reader_difficulty", req.default_reader_difficulty)
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
    if req.lesson_premium is not None:
        if not user.get("is_admin"):
            raise HTTPException(403, "Admins only")
        await db.set_setting(user["id"], "lesson_premium", "1" if req.lesson_premium else "0")
    if req.lesson_model is not None:
        if not user.get("is_admin"):
            raise HTTPException(403, "Admins only")
        val = req.lesson_model.strip()
        if val and val not in LESSON_MODEL_ALLOWLIST:
            raise HTTPException(400, "Unsupported lesson model")
        await db.set_setting(user["id"], "lesson_model", val)
    if req.learner_profile is not None:
        await db.set_setting(user["id"], "learner_profile", req.learner_profile[:2000].strip())
    if req.lesson_buffer is not None:
        await db.set_setting(user["id"], "lesson_buffer", max(0, min(10, req.lesson_buffer)))
    if req.chat_compact_phrases is not None:
        await db.set_setting(user["id"], "chat_compact_phrases", "true" if req.chat_compact_phrases else "false")
    if req.populate_min_score is not None:
        val = max(0.0, min(1.0, req.populate_min_score))
        await db.set_setting(user["id"], "populate_min_score", f"{val:.2f}")
    return {"success": True}


# ── Billing ───────────────────────────────────────────────────────────────────

@app.get("/api/billing/status")
async def billing_status(user: dict = Depends(current_user)):
    """Plan + monthly usage for the settings UI. `unlimited` means no quota
    applies (own key, admin, or a granted friend)."""
    has_api_key = bool(await db.get_setting(user["id"], "gemini_api_key"))
    unlimited = has_api_key or bool(user.get("is_admin"))
    plan = user.get("plan") or "free"
    onboarded = bool(await db.get_setting(user["id"], "onboarded"))
    default_target_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    return {
        "plan": plan,
        "default_target_lang": default_target_lang,
        "onboarded": onboarded,
        "subscription_status": user.get("subscription_status"),
        "subscription_period_end": user.get("subscription_period_end"),
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
        "unlimited": unlimited,
        "used": await db.get_usage(user["id"]),
        "limit": _plan_limit(user),
        "billing_enabled": billing.is_configured(),
        "has_subscription": bool(user.get("stripe_customer_id")),
        "pro_limit": PLAN_LIMITS["pro"],
        # Language + plan picker: shown once for any new non-admin user.
        "show_welcome": not onboarded and not bool(user.get("is_admin")),
    }


class OnboardRequest(BaseModel):
    lang: str | None = None


@app.post("/api/onboard")
async def mark_onboarded(
    req: OnboardRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
):
    """Complete onboarding: save chosen language, seed starter deck, mark done."""
    lang = req.lang
    if lang and lang in translation.LANG_INFO:
        await db.set_setting(user["id"], "default_target_lang", lang)
        background_tasks.add_task(starter_deck.seed, user["id"], lang)
    await db.set_setting(user["id"], "onboarded", "1")
    return {"ok": True}


@app.post("/api/tour-seen")
async def mark_tour_seen(request: Request, user: dict = Depends(current_user)):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    version = str(int(body.get("version", 1)))
    await db.set_setting(user["id"], "tour_seen", version)
    return {"ok": True}


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


@app.post("/api/billing/cancel")
@limiter.limit("10/minute")
async def billing_cancel(request: Request, user: dict = Depends(current_user)):
    """Set cancel_at_period_end on the subscription. Pro access continues until
    the period end date; the subscription is not deleted immediately."""
    if not billing.is_configured():
        raise HTTPException(503, "Billing is not configured.")
    if user.get("cancel_at_period_end"):
        raise HTTPException(400, "Subscription is already set to cancel.")
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(400, "No active subscription to cancel.")
    sub_id = user.get("stripe_subscription_id")
    # Existing Pro accounts may not have sub_id stored yet (pre-migration rows).
    # Look it up from Stripe and persist it so future calls are instant.
    if not sub_id:
        try:
            sub_id = await asyncio.to_thread(billing.get_active_subscription_id, customer_id)
        except Exception:
            pass
        if not sub_id:
            raise HTTPException(400, "No active subscription to cancel.")
        await db.set_plan_by_customer(
            customer_id, user.get("plan") or "free",
            user.get("subscription_status"), user.get("subscription_period_end"),
            sub_id=sub_id, cancel_at_period_end=False,
        )
    try:
        sub = await asyncio.to_thread(billing.cancel_subscription, sub_id)
    except Exception:
        raise HTTPException(502, "Could not cancel subscription. Please try again.")
    # Extract the real period end from Stripe's response (may be missing from
    # the DB for accounts created before we started storing it).
    period_end = _subscription_period_end(sub) or user.get("subscription_period_end")
    # Update DB immediately so billing_status reflects the change before the
    # webhook arrives (which may take a few seconds).
    await db.set_plan_by_customer(
        customer_id, user.get("plan") or "free",
        user.get("subscription_status"), period_end,
        sub_id=sub_id, cancel_at_period_end=True,
    )
    return {"ok": True, "period_end": period_end}


@app.post("/api/billing/resume")
@limiter.limit("10/minute")
async def billing_resume(request: Request, user: dict = Depends(current_user)):
    """Clear cancel_at_period_end — subscription will renew as normal."""
    if not billing.is_configured():
        raise HTTPException(503, "Billing is not configured.")
    sub_id = user.get("stripe_subscription_id")
    if not sub_id:
        raise HTTPException(400, "No active subscription.")
    if not user.get("cancel_at_period_end"):
        raise HTTPException(400, "Subscription is not pending cancellation.")
    try:
        await asyncio.to_thread(billing.resume_subscription, sub_id)
    except Exception:
        raise HTTPException(502, "Could not resume subscription. Please try again.")
    # Update DB immediately so billing_status reflects the change before webhook.
    customer_id = user.get("stripe_customer_id")
    await db.set_plan_by_customer(
        customer_id, user.get("plan") or "free",
        user.get("subscription_status"), user.get("subscription_period_end"),
        sub_id=sub_id, cancel_at_period_end=False,
    )
    return {"ok": True}


def _subscription_period_end(obj) -> str | None:
    """Extract and format current_period_end from a Stripe sub dict or object."""
    try:
        ts = obj["current_period_end"]  # works for both dicts and StripeObjects
    except (KeyError, TypeError):
        return None
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
        billing.construct_event(payload, sig)  # verify signature against raw body
    except Exception:
        raise HTTPException(400, "Invalid webhook signature.")

    # Parse the raw body as a plain dict. The verified StripeObject from
    # construct_event doesn't support dict-style .get() in this SDK version,
    # so we re-read the (already-trusted) payload as JSON.
    event = json.loads(payload)
    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        customer_id = obj.get("customer")
        user_ref = obj.get("client_reference_id")
        if customer_id and user_ref:
            await db.set_stripe_customer(int(user_ref), customer_id)
            # subscription.created fires alongside this, so plan/period are
            # set there; here we just ensure the customer link exists.
            await db.set_plan_by_customer(customer_id, "pro", "active", None)
    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        customer_id = obj.get("customer")
        sub_id = obj.get("id")
        status = obj.get("status")
        cancel_flag = bool(obj.get("cancel_at_period_end", False))
        plan = "pro" if status in ("active", "trialing", "past_due") else "free"
        if customer_id:
            await db.set_plan_by_customer(
                customer_id, plan, status, _subscription_period_end(obj),
                sub_id=sub_id, cancel_at_period_end=cancel_flag,
            )
    elif etype == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        if customer_id:
            await db.set_plan_by_customer(
                customer_id, "free", "canceled", None,
                sub_id=None, cancel_at_period_end=False,
            )

    return {"received": True}


# ── Cards ─────────────────────────────────────────────────────────────────────

def _parse_label_ids(label_id: int | None, label_ids: str | None) -> list[int] | None:
    if label_ids:
        return [int(x) for x in label_ids.split(",") if x.strip().isdigit()]
    if label_id is not None:
        return [label_id]
    return None

@app.get("/api/cards/due")
async def get_due_cards(label_id: int | None = None, label_ids: str | None = None, user: dict = Depends(current_user)):
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    return await db.get_study_session(user["id"], label_ids=_parse_label_ids(label_id, label_ids), target_lang=lang)


@app.get("/api/cards/all-faces")
async def get_all_faces(label_id: int | None = None, label_ids: str | None = None, user: dict = Depends(current_user)):
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    faces = await db.get_all_faces(user["id"], label_ids=_parse_label_ids(label_id, label_ids), target_lang=lang)
    return {"cards": faces, "count": len(faces)}


@app.get("/api/cards/all")
async def get_all_cards(user: dict = Depends(current_user)):
    cards = await db.get_all_cards(user["id"])
    return {"cards": cards}


@app.get("/api/cards/due-count")
async def due_count(label_id: int | None = None, label_ids: str | None = None, user: dict = Depends(current_user)):
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    return {"count": await db.get_due_count(user["id"], label_ids=_parse_label_ids(label_id, label_ids), target_lang=lang)}


@app.get("/api/cards/cefr-distribution")
async def cefr_distribution(user: dict = Depends(current_user)):
    return await db.get_cefr_distribution(user["id"])


_DAILY_XP_GOAL = 50   # XP target for the daily-goal ring on the Learn page


@app.get("/api/streak")
async def get_streak(user: dict = Depends(current_user)):
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    return {"streak": await db.get_streak(user["id"]),
            "points": await db.get_points_total(user["id"], lang),
            "points_today": await db.get_points_today(user["id"], lang),
            "daily_goal": _DAILY_XP_GOAL}


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
    await db.record_study_activity(user["id"])
    xp = 0
    if req.quality in ("good", "easy"):
        xp = 7 if req.quality == "easy" else 5
        await db.add_points(user["id"], card.get("target_lang", "yue"), xp, "review")
    return {"success": True, "xp": xp, **new_state}


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


@app.post("/api/cards/{card_id}/regenerate")
@limiter.limit("60/minute;1000/day")
async def regenerate_card(request: Request, card_id: int, user: dict = Depends(current_user)):
    """Re-author the AI fields (notes + romanization) for an existing card. Returns
    the fresh values WITHOUT persisting — the editor fills them in so the learner
    can review and Save. 1 metered call."""
    card = await db.get_card(user["id"], card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    access = await _resolve_gemini(user)
    try:
        out = await translation.regenerate_card_fields(
            card["target_text"], card.get("source_text", ""),
            card.get("target_lang", "yue"),
            api_key=access.api_key, model=access.model_translate,
        )
    except Exception as e:
        logger.error("Card regenerate failed card=%s: %s", card_id, e, exc_info=True)
        raise HTTPException(502, "Couldn't regenerate — please try again.")
    return out


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
async def create_label(req: LabelRequest, bg: BackgroundTasks, user: dict = Depends(current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name is empty")
    if len(name) > 50:
        raise HTTPException(400, "Name too long (max 50 chars)")
    result = await db.create_label(user["id"], name)
    bg.add_task(_embed_label_bg, user, name)
    return result


async def _embed_label_bg(user: dict, name: str):
    """Pre-compute the embedding for a newly created label so merge suggestions
    don't have to embed it lazily later."""
    try:
        await _label_vectors(user, [name])
    except Exception:
        pass


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


# Label-merge tuning. The semantic pass catches synonyms that share no letters
# ("drinks"/"beverages"); the string pass catches typos/plurals/punctuation
# ("food & drinks"/"foods and drinks"). Label-name embeddings are cached under a
# sentinel "lang" so they never collide with target-vocabulary embeddings.
_LABEL_EMBED_LANG = "__label__"
_SEMANTIC_MERGE_THRESHOLD = 0.84   # cosine; synonyms ~0.80–0.90, unrelated words lower


def _label_norm(name: str) -> str:
    """Normalize a label name for cheap string comparison (lowercase, drop
    punctuation/articles, naive depluralize)."""
    import re, unicodedata
    s = unicodedata.normalize("NFKD", name).lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\b(and|the|a|an|of)\b", "", s)
    s = re.sub(r"s\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _string_sim(na: str, nb: str) -> float:
    """Similarity from normalized names: 1.0 if identical, else an affix-aware
    Levenshtein ratio if it clears the bar, else 0. The affix guard stops
    substring near-matches like 'motion'/'emotion' (one is a suffix of the
    other) from passing on edit distance alone."""
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = _levenshtein_ratio(na, nb)
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    is_affix = shorter != longer and (longer.startswith(shorter) or longer.endswith(shorter))
    threshold = 0.92 if is_affix else 0.8
    return ratio if ratio >= threshold else 0.0


def _label_embed_text(name: str) -> str:
    """Strip a leading emoji/symbol prefix (e.g. the '📦 ' deck marker) so the
    embedding reflects the semantic word, not the decoration."""
    import re
    return re.sub(r"^[\W_]+", "", name).strip().lower() or name.strip().lower()


async def _label_vectors(user: dict, names: list[str]) -> dict[str, list[float]]:
    """Embedding vectors for label names via the shared cache (embed only misses).
    Best-effort: returns {} if no key / any embedding error, so the caller falls
    back to string-only matching. Not metered — this is a derived background call."""
    texts = list({_label_embed_text(n) for n in names if n.strip()})
    if not texts:
        return {}
    try:
        access = await _resolve_gemini(user, meter=False)
    except HTTPException:
        return {}
    cached = await db.get_cached_embeddings(_LABEL_EMBED_LANG, embeddings.EMBED_MODEL, texts)
    missing = [t for t in texts if t not in cached]
    if missing:
        try:
            vecs = await embeddings.embed(missing, access.api_key)
        except Exception as e:
            logger.warning("label embedding failed user=%s: %s", user["id"], e)
            vecs = []
        if vecs:
            packed = {t: embeddings.pack(v) for t, v in zip(missing, vecs)}
            await db.put_cached_embeddings(_LABEL_EMBED_LANG, embeddings.EMBED_MODEL, packed)
            cached.update(packed)
    return {t: embeddings.unpack(b) for t, b in cached.items()}


@app.get("/api/labels/suggest-merges")
async def suggest_label_merges(user: dict = Depends(current_user)):
    """Find groups of labels that are likely duplicates — combining a cheap string
    pass (typos/plurals) with a semantic embedding pass (synonyms that share no
    letters, e.g. 'drinks'/'beverages'). Groups are connected components over both
    signals, sorted by confidence then size."""
    labels = await db.list_labels(user["id"])
    if len(labels) < 2:
        return {"groups": []}

    normed = {l["id"]: _label_norm(l["name"]) for l in labels}
    vectors = await _label_vectors(user, [l["name"] for l in labels])

    def _vec(l: dict) -> list[float] | None:
        return vectors.get(_label_embed_text(l["name"]))

    # Union-find over labels; an edge connects two labels that are string- or
    # semantically similar. We track the best similarity per edge for sorting.
    parent = {l["id"]: l["id"] for l in labels}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    edges: list[tuple[int, int, float]] = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            la, lb = labels[i], labels[j]
            sim = _string_sim(normed[la["id"]], normed[lb["id"]])
            va, vb = _vec(la), _vec(lb)
            if va and vb:
                cos = embeddings.cosine(va, vb)
                if cos >= _SEMANTIC_MERGE_THRESHOLD:
                    sim = max(sim, cos)
            if sim > 0:
                union(la["id"], lb["id"])
                edges.append((la["id"], lb["id"], sim))

    # Group labels by their union-find root, then keep components of size ≥ 2.
    by_root: dict[int, list[dict]] = {}
    for l in labels:
        by_root.setdefault(find(l["id"]), []).append(l)

    groups: list[dict] = []
    for root, members in by_root.items():
        if not (2 <= len(members) <= 6):
            continue
        member_ids = {m["id"] for m in members}
        score = max((s for a, b, s in edges if a in member_ids and b in member_ids), default=0.0)
        groups.append({
            "labels": [{"id": m["id"], "name": m["name"], "card_count": m["card_count"]} for m in members],
            "total_cards": sum(m["card_count"] for m in members),
            "score": round(score, 3),
        })
    groups.sort(key=lambda g: (g["score"], g["total_cards"]), reverse=True)
    return {"groups": groups}


_merge_review_cache: dict[tuple[str, ...], dict] = {}


class MergeReviewRequest(BaseModel):
    names: list[str]


@app.post("/api/labels/review-merge")
async def review_merge(req: MergeReviewRequest, user: dict = Depends(current_user)):
    """LLM-vet a proposed label merge: suggest a name or reject.
    Results are cached server-side by the sorted name set."""
    names = [n.strip() for n in req.names if n.strip()]
    if len(names) < 2:
        raise HTTPException(400, "Need at least 2 label names")
    cache_key = tuple(sorted(n.lower() for n in names))
    cached = _merge_review_cache.get(cache_key)
    if cached:
        return cached
    try:
        access = await _resolve_gemini(user, meter=False)
        loop = asyncio.get_event_loop()
        review = await loop.run_in_executor(
            None, translation.review_label_merge, names, access.api_key
        )
    except Exception:
        review = {"verdict": "merge", "suggested_name": names[0], "reason": ""}
    _merge_review_cache[cache_key] = review
    return review


def _levenshtein_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    n, m = len(a), len(b)
    if n > m:
        a, b, n, m = b, a, m, n
    prev = list(range(n + 1))
    for j in range(1, m + 1):
        curr = [j] + [0] * n
        for i in range(1, n + 1):
            curr[i] = min(prev[i] + 1, curr[i - 1] + 1,
                          prev[i - 1] + (0 if a[i - 1] == b[j - 1] else 1))
        prev = curr
    dist = prev[n]
    return 1.0 - dist / max(n, m)


class LabelPopulateRequest(BaseModel):
    label_name: str
    label_id: int
    lang: str | None = None
    count: int = 10


@app.post("/api/labels/populate")
async def populate_label(req: LabelPopulateRequest, user: dict = Depends(current_user)):
    """Ask an LLM to suggest vocab words that fit a label category."""
    lang = req.lang or (await db.get_setting(user["id"], "default_target_lang")) or "yue"
    if lang not in translation.LANG_INFO:
        raise HTTPException(400, f"Unsupported language: {lang}")
    access = await _resolve_gemini(user, meter=False)
    # Feed the LLM ONLY the words already under this label (token-lean) — not the
    # whole deck. Suggestions that happen to already be cards become "tag this
    # card" suggestions instead of being silently dropped.
    label_words = await db.get_label_words(user["id"], req.label_id, lang)
    llm_count = min(req.count * 2, 20)
    loop = asyncio.get_event_loop()
    suggestions = await loop.run_in_executor(
        None, translation.suggest_vocab_for_label,
        req.label_name, lang, [], access.api_key, llm_count, label_words,
    )
    if not suggestions:
        logger.info("populate_label %r lang=%s: LLM returned 0 (in-label=%d)",
                    req.label_name, lang, len(label_words))
        return {"new": [], "existing": [], "lang": lang}

    matches = await db.match_cards_by_target(
        user["id"], [s["target"] for s in suggestions], lang, req.label_id,
        sources=[s["source"] for s in suggestions],
    )
    # Build a reverse lookup by lowercase source_text for source-based matches
    # (catches cards where the LLM's target form differs from the stored one)
    source_index: dict[str, dict] = {}
    for m in matches.values():
        if m.get("source_text"):
            key = m["source_text"].strip().lower()
            prev = source_index.get(key)
            if prev is None or (m["in_label"] and not prev["in_label"]):
                source_index[key] = m
    new_words: list[dict] = []
    existing_cards: list[dict] = []
    seen_card_ids: set[int] = set()
    for s in suggestions:
        m = matches.get(s["target"]) or source_index.get(s["source"].strip().lower())
        if m is None:
            new_words.append(s)
        elif m["id"] in seen_card_ids:
            continue
        elif m["in_label"]:
            seen_card_ids.add(m["id"])
            continue
        else:
            seen_card_ids.add(m["id"])
            existing_cards.append({
                "id": m["id"], "target": s["target"],
                "source": m["source_text"] or s["source"],
            })
    logger.info("populate_label %r lang=%s: LLM=%d → new=%d, tag-existing=%d (in-label=%d)",
                req.label_name, lang, len(suggestions), len(new_words),
                len(existing_cards), len(label_words))
    return {"new": new_words[:req.count], "existing": existing_cards[:req.count], "lang": lang}


@app.get("/api/labels/suggest-cards")
async def suggest_cards_for_label(name: str, label_id: int | None = None, limit: int = 20,
                                  min_score: float = 0.0, user: dict = Depends(current_user)):
    """Embed 'name' and return the top cards by cosine similarity, optionally excluding cards already in label_id.

    `min_score` (0–1) filters out cards below that cosine similarity so callers
    can ask for only genuinely-related cards (the Populate deck search) instead
    of the unconditional top-N (the legacy cards.html ✦ Suggest)."""
    try:
        access = await _resolve_gemini(user, meter=False)
    except HTTPException:
        return {"cards": []}
    # Use the shared label-embedding cache (pre-computed on label creation) so we
    # don't re-embed the query name on every call.
    label_vecs = await _label_vectors(user, [name])
    query_embedding = label_vecs.get(_label_embed_text(name))
    if not query_embedding:
        query_embedding = await translation.get_embedding(name, api_key=access.api_key)
    if not query_embedding:
        return {"cards": []}

    # Lazy backfill: existing cards predate working embeddings (the column is
    # NULL until a card is (re)embedded). Embed a bounded batch of missing cards
    # per call so the deck converges over a few requests. Best-effort.
    await _backfill_card_embeddings(user["id"], access.api_key, limit=300)

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
        if score >= min_score:
            scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)

    # If filtering by label, fetch cards already in the label to exclude them.
    already: set[int] = set()
    if label_id is not None:
        import aiosqlite as _aiosqlite
        async with _aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT card_id FROM card_labels WHERE label_id=?", (label_id,)
            ) as cur:
                already = {r[0] for r in await cur.fetchall()}

    # Strip the heavy embedding blob — the client only needs id/text/score.
    cards = [
        {"id": r["id"], "source_text": r["source_text"],
         "target_text": r["target_text"], "score": round(score, 3)}
        for score, r in scored
        if r["id"] not in already
    ]
    return {"cards": cards[:limit]}


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
                "full_name": info.get("full_name", info["name"]),
                "flag": info.get("flag", ""),
                "script": info["script"],
                "script_family": translation.SCRIPT_BY_LANG.get(code, "latin"),
                "romanization": info["romanization"],
                "logographic": info["romanization"] is not None,
            }
            for code, info in translation.LANG_INFO.items()
        ]
    }


# ── Learning path (AI course) ─────────────────────────────────────────────────

class CreateCourseRequest(BaseModel):
    target_lang: str | None = None
    level: str = "A1"


@app.post("/api/courses")
@limiter.limit("10/minute;20/day")
async def create_course(request: Request, req: CreateCourseRequest, user: dict = Depends(current_user)):
    """Create an empty course. Lessons are generated one at a time via /next."""
    lang = req.target_lang or await db.get_setting(user["id"], "default_target_lang") or "yue"
    if lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    course_id = await db.create_course(user["id"], lang, req.level or "A1")
    # Non-Latin scripts get a pre-built, skippable Foundations (reading) track
    # prepended — the learner masters the writing system before/alongside vocab.
    foundation_units = foundations.build_units(lang)
    if foundation_units:
        await db.seed_foundation_units(course_id, foundation_units)
    return await db.get_course(user["id"], course_id)


@app.get("/api/courses")
async def list_courses(user: dict = Depends(current_user)):
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    return {"courses": await db.get_courses(user["id"], target_lang=lang)}


@app.get("/api/courses/active")
async def active_course(user: dict = Depends(current_user)):
    """The current language's active course (full nested structure), or null.
    Auto-seeds the Foundations track for any existing course that predates it."""
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    course = await db.get_active_course(user["id"], lang)
    if course and foundations.has_foundations(lang):
        if not any(u.get("theme") == "foundations" for u in (course.get("units") or [])):
            await db.seed_foundation_units(course["id"], foundations.build_units(lang))
            course = await db.get_active_course(user["id"], lang)
    return {"course": course}


@app.get("/api/courses/{course_id}")
async def get_course(course_id: int, user: dict = Depends(current_user)):
    course = await db.get_course(user["id"], course_id)
    if not course:
        raise HTTPException(404, "Course not found")
    return course


@app.get("/api/courses/{course_id}/vocab")
async def get_course_vocab(course_id: int, user: dict = Depends(current_user)):
    rows = await db.get_course_vocab(user["id"], course_id)
    if not rows:
        return {"vocab": []}
    lang = None
    course = await db.get_course(user["id"], course_id)
    if course:
        lang = course.get("target_lang")
    labels = [r["label"] for r in rows]
    statuses = await db.get_word_statuses(user["id"], labels, lang) if lang else {}
    for r in rows:
        r["in_deck"] = r["label"] in statuses
    return {"vocab": rows}


@app.delete("/api/courses/{course_id}")
async def delete_course(course_id: int, user: dict = Depends(current_user)):
    await db.delete_course(user["id"], course_id)
    return {"success": True}


@app.delete("/api/courses/{course_id}/ai_lessons")
async def reset_ai_lessons(course_id: int, user: dict = Depends(current_user)):
    """Delete only AI-generated lessons, preserving the foundations reading track."""
    import aiosqlite as _aiosqlite
    async with _aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT 1 FROM courses WHERE id=? AND user_id=?", (course_id, user["id"])
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Course not found")
    await db.delete_ai_lessons(course_id)
    return {"success": True}


def _filter_new_concepts(concepts: list[dict], registry: list[dict],
                         known_texts: set[str] | None = None) -> list[dict]:
    """Drop concepts already registered for this course (by key, or by native
    label for vocab) and dedupe within the list itself. The unit planner is told
    not to repeat concepts, but nothing else enforces it — without this filter a
    duplicate is silently swallowed by INSERT OR IGNORE and re-taught.

    `known_texts` — native words from the user's SRS deck; vocab concepts whose
    label matches one are dropped too (the learner already knows them)."""
    seen_keys = {(c.get("key") or "").strip() for c in registry}
    seen_labels = {(c.get("label") or "").strip()
                   for c in registry if (c.get("kind") or "vocab") == "vocab"}
    seen_labels |= (known_texts or set())
    seen_labels.discard("")
    out = []
    for c in concepts:
        key = (c.get("key") or "").strip()
        label = (c.get("label") or "").strip()
        is_vocab = (c.get("kind") or "vocab") == "vocab"
        if not key or key in seen_keys:
            continue
        if is_vocab and label and label in seen_labels:
            continue
        seen_keys.add(key)
        if is_vocab and label:
            seen_labels.add(label)
        out.append(c)
    return out


def _slug(text: str) -> str:
    """A stable-ish snake_case key from a label when the planner omits one."""
    base = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return base or "item"


def _concepts_from_spec(spec: dict) -> list[dict]:
    """Turn a planner lesson_spec into the concept list the author teaches AND the
    registry records. A GRAMMAR skill is one concept carrying its `items` (the
    forms/verbs to cover within the lesson — coverage, not separate registry rows).
    A VOCAB lesson registers each `target_item` as its own vocab concept (the words
    being taught); the skill label is just the theme."""
    skill = spec.get("skill") or {}
    kind = (skill.get("kind") or "vocab").strip()
    items = spec.get("target_items") or []
    if kind == "grammar":
        key = (skill.get("key") or "").strip() or _slug(skill.get("label") or "grammar")
        return [{
            "kind": "grammar", "key": key,
            "label": (skill.get("label") or "").strip(),
            "gloss": (skill.get("gloss") or "").strip(),
            "items": items,
        }]
    # Vocab: each item is a concept. Fall back to the skill itself if no items.
    out = []
    seen = set()
    for it in items:
        label = (it.get("label") or "").strip()
        if not label:
            continue
        key = (it.get("key") or "").strip() or _slug(it.get("gloss") or label)
        while key in seen:
            key += "_x"
        seen.add(key)
        out.append({"kind": "vocab", "key": key, "label": label,
                    "gloss": (it.get("gloss") or "").strip()})
    if not out and (skill.get("label") or "").strip():
        out.append({"kind": "vocab",
                    "key": (skill.get("key") or "").strip() or _slug(skill.get("label")),
                    "label": skill.get("label").strip(),
                    "gloss": (skill.get("gloss") or "").strip()})
    return out


def _pick_review_concepts(registry: list[dict], batch: list[dict],
                          mastery: list[dict], lesson_num: int, n: int = 2) -> list[dict]:
    """Spiral review: pick up to `n` previously-taught concepts to interleave as
    review drills in this lesson. Weak concepts first (≥3 attempts, <70% accuracy),
    then the rest rotated by lesson number so successive lessons revisit different
    old material instead of always the same one."""
    batch_keys = {(c.get("key") or "").strip() for c in batch}
    pool = [c for c in registry if (c.get("key") or "").strip() not in batch_keys]
    if not pool:
        return []
    acc = {m["concept_key"]: m for m in mastery}
    weak = [c for c in pool
            if (m := acc.get((c.get("key") or "").strip()))
            and m["total"] >= 3 and m["correct"] / m["total"] < 0.7]
    picked = weak[:n]
    rest = [c for c in pool if c not in picked]
    if len(picked) < n and rest:
        start = lesson_num % len(rest)
        picked += (rest[start:] + rest[:start])[: n - len(picked)]
    return picked


def _gen_error_detail(e: Exception, stage: str, model: str) -> str:
    """Turn a generation exception into a specific, actionable message for the
    client. `stage` is 'Lesson planning' or 'Lesson generation'."""
    name = type(e).__name__
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    msg = str(e).lower()
    premium = "pro" in (model or "")
    # Model overloaded / 5xx from the Gemini backend (ServerError after retries).
    if name == "ServerError" or status in (500, 502, 503, 504) or "overload" in msg or "unavailable" in msg:
        extra = " The premium model is slower and busier — switching it off in Settings is more reliable." if premium else ""
        return (f"{stage} failed: the AI model ({model}) is overloaded right now. "
                f"Wait a moment and tap Generate again.{extra}")
    # Quota / rate limit straight from the provider.
    if status == 429 or "quota" in msg or "rate limit" in msg or "resource_exhausted" in msg:
        return (f"{stage} failed: the AI provider rate-limited the request. "
                f"Wait a minute and try again.")
    # JSON parsing / empty body — the model returned something unusable.
    if name in ("ValueError", "JSONDecodeError") or "json" in msg or "expecting value" in msg:
        return (f"{stage} failed: the AI returned a malformed response. "
                f"This is usually transient — tap Generate to try again.")
    return f"{stage} failed — please try again."


async def _author_next_lesson(course: dict, access, lesson_model: str, user_id: int | None = None) -> int:
    """Plan + author + persist ONE lesson, just-in-time from live learner state.

    Two LLM calls: a cheap PLANNER picks the next skill + how broad to teach it
    (continuing or opening a chapter), then the AUTHOR writes teach blocks + drills.
    Re-reads context each call, so calling it in a loop adapts to prior lessons.
    `courses.active_plan` holds the in-progress chapter ({title,objective,summary});
    a finished chapter is closed into a unit via close_unit. Returns the new
    lesson_id; raises HTTPException(502) on generation failure."""
    course_id = course["id"]
    lang = course["target_lang"]
    # The planner is a cheap routing decision (pick the next skill), so it ALWAYS
    # runs on the fast reader model — only the quality-critical AUTHOR uses the
    # (possibly premium) lesson_model. This keeps each lesson to ONE slow call, not
    # two: a Pro/Claude planner adds ~15s/lesson and doubles overload exposure, which
    # made batch generation ("generate 5") time out / 503 after the first lesson.
    plan_model = access.model_reader
    ctx = await db.get_next_lesson_context(course_id)

    # The chapter currently in progress. Tolerate a stale OLD-format plan (with
    # concepts/cursor and no chapter title) by ignoring it — first plan overwrites.
    chapter = await db.get_active_plan(course_id)
    if chapter and not (chapter.get("title") or "").strip():
        chapter = None

    mastery: list[dict] = []
    known_words: list[dict] = []
    weak_words: list[dict] = []
    recent_cards: list[dict] = []
    learner_profile = ""
    cefr_spread = ""
    if user_id:
        mastery = await db.get_mastery_summary(user_id, lang)
        known_words = await db.get_known_words(user_id, lang)
        weak_words = await db.get_weak_cards(user_id, lang)
        recent_cards = await db.get_recent_cards(user_id, lang)
        learner_profile = await db.get_setting(user_id, "learner_profile") or ""
        try:
            cefr_spread = await _known_cefr_stats(user_id, lang, access.api_key)
        except Exception:
            cefr_spread = ""
    known_texts = {(w.get("target_text") or "").strip() for w in known_words}

    # 1. PLAN the next lesson from live state.
    try:
        spec = await learning.plan_next_lesson(
            lang, course["level"],
            concept_registry=ctx["concept_registry"],
            recent_summaries=ctx["recent_summaries"],
            current_chapter=chapter,
            learner_profile=learner_profile, mastery=mastery,
            known_words=known_words, weak_words=weak_words,
            recent_cards=recent_cards, cefr_spread=cefr_spread,
            api_key=access.api_key, anthropic_key=access.anthropic_key, model=plan_model,
        )
    except Exception as e:
        logger.error("Lesson planning failed lang=%s: %s", lang, e, exc_info=True)
        raise HTTPException(502, _gen_error_detail(e, "Lesson planning", plan_model))
    plan_prompt = spec.pop("_raw_prompt", "")
    plan_response = spec.pop("_raw_response", "")

    # 2. CHAPTER bookkeeping. Opening a new chapter closes the previous one into a
    #    unit (retrospective grouping for the roadmap UI).
    if spec.get("chapter_action") == "new" or chapter is None:
        if chapter:
            await db.close_unit(course_id, chapter.get("title") or "Unit", chapter.get("summary") or "")
        new_ch = spec.get("chapter") or {}
        chapter = {
            "title":     (new_ch.get("title") or spec.get("skill", {}).get("label") or "Lesson").strip(),
            "objective": (new_ch.get("objective") or "").strip(),
            "summary":   (new_ch.get("summary") or "").strip(),
        }
        await db.set_active_plan(course_id, chapter)

    # 3. Build the concept list (skill + items) and dedupe against the registry +
    #    known deck words. If dedup empties it (planner re-proposed only known
    #    material), fall back to the raw concepts — INSERT OR IGNORE keeps the
    #    registry clean and re-teaching is harmless.
    concepts = _concepts_from_spec(spec)
    if not concepts:
        raise HTTPException(502, "Lesson planning returned no skill — please try again.")
    deduped = _filter_new_concepts(concepts, ctx["concept_registry"], known_texts)
    concepts = deduped or concepts

    brief = {
        "title":     chapter.get("title", ""),
        "objective": spec.get("skill", {}).get("gloss", ""),
        "scope":     spec.get("scope", "broad"),
        "focus":     spec.get("focus", "new"),
    }
    review = _pick_review_concepts(ctx["concept_registry"], concepts, mastery, ctx["lesson_num"])

    # 4. AUTHOR the lesson for this skill + items.
    try:
        authored = await learning.author_lesson(
            lang, concepts, ctx["recent_summaries"],
            api_key=access.api_key, anthropic_key=access.anthropic_key, model=lesson_model,
            taught=ctx["concept_registry"], review=review,
            known_words=known_words, weak_words=weak_words, brief=brief,
        )
    except Exception as e:
        logger.error("Lesson authoring failed lang=%s concepts=%s: %s",
                     lang, [c.get("key") for c in concepts], e, exc_info=True)
        raise HTTPException(502, _gen_error_detail(e, "Lesson generation", lesson_model))

    content = authored["content"]
    total_ex = sum(len(s.get("exercises") or []) for s in content.get("segments") or [])
    if not total_ex:
        logger.error("Lesson has no exercises lang=%s concepts=%s raw=%r",
                     lang, [c.get("key") for c in concepts],
                     authored.get("_raw_response", "")[:500])
        raise HTTPException(502, "Lesson generation returned no exercises — please try again.")

    # Merge both LLM calls into the {prompt, response} the debug panel reads.
    sep = "\n\n══════ LESSON AUTHOR ══════\n\n"
    debug = {
        "prompt":   (plan_prompt + sep + authored["_raw_prompt"]) if plan_prompt else authored["_raw_prompt"],
        "response": (plan_response + sep + authored["_raw_response"]) if plan_response else authored["_raw_response"],
    }

    return await db.create_lesson(
        course_id, ctx["lesson_num"],
        authored["title"], authored["objective"],
        concepts,             # skill + vocab items get registered
        content,
        authored["summary"],
        debug,
    )


@app.post("/api/courses/{course_id}/next")
@limiter.limit("10/minute;40/day")
async def next_lesson(request: Request, course_id: int, count: int = 1,
                      user: dict = Depends(current_user)):
    """Author the next `count` micro-lessons (1–6) and return them. Generating
    several at once lets the learner browse ahead and see how content evolves.

    count==1 returns the full lesson (the player auto-opens it); count>1 returns
    a lightweight summary list (the UI just refreshes the roadmap)."""
    course = await db.get_course(user["id"], course_id)
    if not course:
        raise HTTPException(404, "Course not found")

    # Resolve access + check quota without metering yet — we meter per lesson below.
    access = await _resolve_gemini(user, meter=False)
    # Admin-only knob: run the whole pipeline (planner + author) on a chosen model
    # — Gemini Flash/Pro OR Claude Sonnet/Opus — to A/B lesson quality. Falls back
    # to the legacy lesson_premium→Pro toggle, then the normal reader model.
    # Everyone else uses the normal reader model.
    lesson_model = access.model_reader
    if user.get("is_admin"):
        chosen = _valid_lesson_model(await db.get_setting(user["id"], "lesson_model"))
        if chosen:
            lesson_model = chosen
        elif await db.get_setting(user["id"], "lesson_premium") == "1":
            lesson_model = grammar_lessons.GENERATION_MODEL

    # Metered = shared key, not admin, no own API key. Bill one usage unit per
    # lesson authored (not per batch) so generating 5 at once costs 5, not 1.
    own_enc = await db.get_setting(user["id"], "gemini_api_key")
    metered = not own_enc and not user.get("is_admin")

    count = max(1, min(int(count), 6))
    lesson_ids: list[int] = []
    for _ in range(count):
        if metered:
            # Re-check quota before each lesson (earlier passes may have consumed it).
            if await db.get_usage(user["id"]) >= _plan_limit(user):
                if not lesson_ids:
                    raise HTTPException(402, (
                        f"You've used your {_plan_limit(user)} free AI lessons this month. "
                        "Add your own Gemini key in Settings for unlimited use."
                    ))
                break
        try:
            lesson_ids.append(await _author_next_lesson(course, access, lesson_model, user["id"]))
            if metered:
                await db.increment_usage(user["id"])
        except HTTPException:
            if not lesson_ids:      # nothing succeeded → surface the error
                raise
            break                   # partial batch → return what we have

    if count == 1:
        lesson = await db.get_lesson(user["id"], lesson_ids[0])
        return _lesson_response(lesson, lesson["content"])
    return {"generated": len(lesson_ids), "lesson_ids": lesson_ids}


def _lesson_response(lesson: dict, content: dict) -> dict:
    return {
        "id":         lesson["id"],
        "title":      lesson["title"],
        "objective":  lesson["objective"],
        "target_lang": lesson["target_lang"],
        "completed":  lesson.get("completed", False),
        "score":      lesson.get("score"),
        "theme":      lesson.get("theme", ""),       # 'foundations' = reading track
        "concepts":   lesson.get("concepts", []),   # results screen "Add to deck"
        "content":    content,
        "llm_debug":  lesson.get("llm_debug"),
    }


@app.get("/api/lessons/{lesson_id}")
@limiter.limit("60/minute")
async def get_lesson(request: Request, lesson_id: int, user: dict = Depends(current_user)):
    """Return a lesson (content was generated and stored when the lesson was created)."""
    lesson = await db.get_lesson(user["id"], lesson_id)
    if not lesson:
        raise HTTPException(404, "Lesson not found")
    if not lesson.get("content"):
        raise HTTPException(404, "Lesson content not available")
    return _lesson_response(lesson, lesson["content"])


@app.get("/api/courses/{course_id}/foundations-practice")
@limiter.limit("30/minute")
async def foundations_practice(
    request: Request, course_id: int,
    game: str = "speed_round",
    count: int = 6,
    audio_mode: bool = False,
    user: dict = Depends(current_user),
):
    """Return a standalone foundations mini-game using all taught items."""
    course = await db.get_course(user["id"], course_id)
    if not course:
        raise HTTPException(404, "Course not found")
    lang = course["target_lang"]
    count = max(3, min(count, 12))
    pool_size = len(foundations.practice_game_pool(lang))
    content = foundations.build_practice_game(lang, game, count=count,
                                              audio_mode=audio_mode)
    if not content:
        raise HTTPException(404, "No practice game available for this language")
    return {
        "id": 0,
        "title": {"speed_round": "Speed Round", "audio_blitz": "Audio Blitz",
                   "memory_match": "Memory Match"}.get(game, "Practice"),
        "objective": "",
        "target_lang": lang,
        "completed": False,
        "score": None,
        "theme": "foundations",
        "concepts": [],
        "content": content,
        "pool_size": pool_size,
    }


class CompleteLessonRequest(BaseModel):
    score: int = 0
    results: list[dict] = []   # [{concept_key, correct, total}] per-concept drill outcomes
    xp: int = 0                # XP earned this lesson (base + combo + perfect), client-computed


_MAX_LESSON_XP = 300   # clamp client-reported XP so the ledger can't be inflated


async def _auto_add_lesson_vocab(user_id: int, lang: str, lesson: dict) -> None:
    """Background: auto-add vocab concepts from a completed lesson to the deck."""
    try:
        concepts = lesson.get("concepts") or []
        vocab = [
            c for c in concepts
            if (c.get("kind") or "vocab") == "vocab"
            and (c.get("label") or "").strip()
            and (c.get("gloss") or "").strip()
        ]
        if not vocab:
            return
        labels = [c["label"].strip() for c in vocab]
        existing = await db.get_word_statuses(user_id, labels, lang, exact_only=True)
        title = lesson.get("title") or "Lesson"
        added = 0
        for c in vocab:
            label = c["label"].strip()
            if label in existing:
                continue
            gloss = c["gloss"].strip()
            rom = ""
            if translation.LANG_INFO.get(lang, {}).get("romanization"):
                rom = tokenizer.romanize_text(label, lang)
            try:
                audio_data = await audio.generate(label, lang)
            except Exception:
                audio_data = None
            await db.create_card(
                user_id=user_id,
                source_text=gloss,
                target_text=label,
                romanization=rom,
                target_lang=lang,
                audio_data=audio_data,
                notes=f"From lesson: {title}",
                priority=3,
            )
            added += 1
        if added:
            logger.info("Auto-added %d vocab cards from lesson '%s' user=%d lang=%s",
                        added, title, user_id, lang)
    except Exception:
        logger.exception("_auto_add_lesson_vocab failed user=%d lang=%s", user_id, lang)


@app.post("/api/lessons/{lesson_id}/complete")
async def complete_lesson(
    lesson_id: int,
    req: CompleteLessonRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
):
    found, first, crown, leveled_up = await db.complete_lesson(user["id"], lesson_id, max(0, min(100, req.score)))
    if not found:
        raise HTTPException(404, "Lesson not found")
    lesson = await db.get_lesson(user["id"], lesson_id) if (req.results or first) else None
    if first and not lesson:
        lesson = await db.get_lesson(user["id"], lesson_id)
    lang = (lesson or {}).get("target_lang") or await db.get_setting(user["id"], "default_target_lang") or "yue"
    if req.results and lesson:
        await db.record_concept_results(user["id"], lang, req.results)
    await db.record_study_activity(user["id"])   # lessons count toward the 🔥 streak
    # Award XP only on the FIRST completion (replays don't re-award).
    awarded = 0
    if first:
        awarded = max(0, min(int(req.xp), _MAX_LESSON_XP))
        if awarded:
            await db.add_points(user["id"], lang, awarded, "lesson")
    # Auto-add lesson vocab to the flashcard deck on first completion.
    if first and lesson:
        background_tasks.add_task(
            _auto_add_lesson_vocab,
            user["id"], lang, lesson,
        )
    return {
        "success": True,
        "xp_awarded": awarded,
        "crown_level": crown,
        "crown_leveled_up": leveled_up,
        "points_today": await db.get_points_today(user["id"], lang),
        "points_total": await db.get_points_total(user["id"], lang),
        "daily_goal": _DAILY_XP_GOAL,
    }


@app.get("/api/mastery")
async def get_mastery(lang: str | None = None, user: dict = Depends(current_user)):
    """Per-concept mastery stats for the requesting user.
    If `lang` is omitted, uses the user's default_target_lang."""
    if not lang or lang not in translation.LANG_INFO:
        lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    rows = await db.get_mastery_summary(user["id"], lang)
    return {"lang": lang, "concepts": rows}


# ── Tutor chat ─────────────────────────────────────────────────────────────────

class TutorMessageRequest(BaseModel):
    text: str


class CardAskCard(BaseModel):
    target_text: str = ""
    source_text: str = ""
    romanization: str = ""
    notes: str = ""
    status: str = ""
    card_id: int | None = None


class CardAskRequest(BaseModel):
    question: str
    card: CardAskCard | None = None
    history: list[dict] = []
    lang: str | None = None


async def _tutor_lang(user: dict) -> str:
    return await db.get_setting(user["id"], "default_target_lang") or "yue"


async def _known_cefr_stats(user_id: int, lang: str, api_key: str) -> str:
    """A compact CEFR profile of the learner's known words, e.g. "A1:40, A2:22, B1:9".
    Lazily backfills CEFR onto known cards that lack it (lesson/tutor/starter adds),
    bounded per call and best-effort. Used in the large-deck drill prompt so the
    model can pitch vocab without us dumping the whole deck."""
    missing = await db.get_known_words_missing_cefr(user_id, lang, limit=60)
    if missing:
        try:
            tagged = await cefr.tag(lang, missing, api_key)
        except Exception as e:
            logger.warning("CEFR backfill failed lang=%s: %s", lang, e)
            tagged = {}
        if tagged:
            await db.set_cards_cefr(user_id, lang, tagged)
    dist = await db.get_known_cefr_distribution(user_id, lang)
    parts = [f"{lv}:{dist[lv]}" for lv in ("A1", "A2", "B1", "B2", "C1", "C2") if dist.get(lv)]
    if dist.get("unknown"):
        parts.append(f"untagged:{dist['unknown']}")
    return ", ".join(parts)


async def _known_word_vectors(lang: str, known_words: list[dict], api_key: str,
                              cap: int = 120) -> dict[str, list[float]]:
    """Embedding vectors for the learner's known deck words, via the shared DB cache
    (embed only the misses, then store). Returns {word: vector}; degrades to {} on
    any embedding error so the tutor drill still works without snapping."""
    words = [(w.get("target_text") or "").strip() for w in (known_words or [])]
    words = [w for w in words if w][:cap]
    if not words:
        return {}
    cached = await db.get_cached_embeddings(lang, embeddings.EMBED_MODEL, words)
    missing = [w for w in words if w not in cached]
    if missing:
        try:
            vecs = await embeddings.embed(missing, api_key)
        except Exception as e:
            logger.warning("known-word embedding failed lang=%s: %s", lang, e)
            vecs = []
        if vecs:
            packed = {w: embeddings.pack(v) for w, v in zip(missing, vecs)}
            await db.put_cached_embeddings(lang, embeddings.EMBED_MODEL, packed)
            cached.update(packed)
    return {w: embeddings.unpack(b) for w, b in cached.items()}


async def _tutor_messages_payload(user_id: int, conv_id: int) -> list[dict]:
    """Stored messages → client shape (tutor turns are their JSON payload).
    `drill_id`/`drill_skill` (non-NULL for drill turns) let the client group a
    drill into one collapsible panel."""
    messages = []
    for m in await db.get_tutor_messages(user_id, conv_id):
        drill = {"drill_id": m["drill_id"], "drill_skill": m["drill_skill"]} if m["drill_id"] else {}
        if m["role"] == "tutor":
            try:
                payload = json.loads(m["content"])
            except (ValueError, TypeError):
                payload = {"reply": m["content"]}
            messages.append({"id": m["id"], "role": "tutor", **payload, **drill})
        else:
            messages.append({"id": m["id"], "role": "user", "text": m["content"], **drill})
    return messages


@app.get("/api/tutor/conversations")
async def tutor_conversations(user: dict = Depends(current_user)):
    lang = await _tutor_lang(user)
    convs = await db.list_tutor_conversations(user["id"], lang)
    out = {"lang": lang, "conversations": convs,
           "points": await db.get_points_total(user["id"], lang)}
    # Include the most recent conversation's messages so the page renders in one
    # round trip (the client would otherwise immediately fetch it anyway).
    if convs:
        latest_conv = await db.get_tutor_conversation(user["id"], convs[0]["id"])
        out["latest"] = {"id": convs[0]["id"],
                         "active_drill_id": latest_conv["active_drill_id"] if latest_conv else None,
                         "messages": await _tutor_messages_payload(user["id"], convs[0]["id"])}
    return out


@app.post("/api/tutor/conversations")
@limiter.limit("10/minute")
async def tutor_new_conversation(request: Request, user: dict = Depends(current_user)):
    lang = await _tutor_lang(user)
    conv_id = await db.create_tutor_conversation(user["id"], lang)
    return {"id": conv_id, "lang": lang}


@app.get("/api/tutor/conversations/{conv_id}")
async def tutor_get_conversation(conv_id: int, user: dict = Depends(current_user)):
    conv = await db.get_tutor_conversation(user["id"], conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return {"id": conv["id"], "lang": conv["lang"], "title": conv["title"],
            "active_drill_id": conv["active_drill_id"],
            "messages": await _tutor_messages_payload(user["id"], conv_id)}


@app.delete("/api/tutor/conversations/{conv_id}")
async def tutor_delete_conversation(conv_id: int, user: dict = Depends(current_user)):
    await db.delete_tutor_conversation(user["id"], conv_id)
    return {"success": True}


@app.post("/api/tutor/conversations/{conv_id}/messages")
@limiter.limit("20/minute;400/day")
async def tutor_send_message(request: Request, conv_id: int, req: TutorMessageRequest,
                             user: dict = Depends(current_user)):
    """One learner message → one tutor reply (1 metered LLM call, like translate)."""
    text = (req.text or "").strip()[:1000]
    if not text:
        raise HTTPException(400, "text required")
    conv = await db.get_tutor_conversation(user["id"], conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    lang = conv["lang"]

    access = await _resolve_gemini(user)            # meters 1 unit (shared-key users)

    # Context: what the learner knows (deck + course registry + weak spots).
    known_words = await db.get_known_words(user["id"], lang)
    weak_words = await db.get_weak_cards(user["id"], lang, limit=8)
    learner_profile = await db.get_setting(user["id"], "learner_profile") or ""
    course = await db.get_active_course(user["id"], lang)
    registry: list[dict] = []
    level = "A1"
    if course:
        level = course.get("level") or "A1"
        ctx = await db.get_next_lesson_context(course["id"])
        registry = ctx["concept_registry"]

    active_drill_id = conv["active_drill_id"]

    # Build history. Drill turns are kept SEPARATE from normal chat context:
    #  - In a drill, the prompt sees ONLY that drill's turns (keeps it focused +
    #    cheap), and we tag the new messages with the drill id so they group.
    #  - In normal chat, drill turns are EXCLUDED entirely (they'd bloat context);
    #    we just pass a compact list of constructions already practiced.
    rows = await db.get_tutor_messages(user["id"], conv_id)
    history: list[dict] = []
    practiced: list[str] = []
    drill_skill = ""
    for m in rows:
        txt = m["content"]
        if m["role"] == "tutor":
            try:
                txt = json.loads(m["content"]).get("reply", "")
            except (ValueError, TypeError):
                pass
        in_active_drill = active_drill_id and m["drill_id"] == active_drill_id
        if active_drill_id:
            if in_active_drill:
                history.append({"role": m["role"], "text": txt})
                if m["drill_skill"]:
                    drill_skill = m["drill_skill"]
        else:
            if m["drill_id"]:
                if m["drill_skill"] and m["drill_skill"] not in practiced:
                    practiced.append(m["drill_skill"])
            else:
                history.append({"role": m["role"], "text": txt})

    try:
        out = await tutor.respond(
            lang, text, history,
            api_key=access.api_key, model=tutor.TUTOR_MODEL,
            level=level, learner_profile=learner_profile,
            known_words=known_words, concept_registry=registry,
            weak_concepts=weak_words,
            drill_skill=drill_skill, practiced=practiced[-6:] or None,
        )
    except Exception as e:
        logger.error("Tutor reply failed lang=%s: %s", lang, e, exc_info=True)
        raise HTTPException(502, "The tutor couldn't reply — please try again.")

    payload = {k: out[k] for k in ("reply", "reply_en", "gloss", "corrections", "new_items", "points", "drill")}
    if active_drill_id:
        payload["drill"] = ""                       # never offer a nested drill mid-drill
    await db.add_tutor_message(user["id"], conv_id, "user", text,
                               drill_id=active_drill_id, drill_skill=drill_skill or None)
    await db.add_tutor_message(user["id"], conv_id, "tutor", json.dumps(payload, ensure_ascii=False),
                               drill_id=active_drill_id, drill_skill=drill_skill or None)
    await db.record_study_activity(user["id"])   # tutor turns count toward the 🔥 streak
    for p in payload["points"]:
        await db.add_points(user["id"], lang, p["points"],
                            f'{p.get("concept", "")}: {p.get("reason", "")}'.strip(": "))

    msg = {"role": "tutor", **payload}
    if active_drill_id:
        msg["drill_id"] = active_drill_id
        msg["drill_skill"] = drill_skill
    return {"message": msg, "points_total": await db.get_points_total(user["id"], lang)}


async def _prepare_card_ask(req: "CardAskRequest", user: dict):
    """Shared setup for the card-ask routes (blocking + streaming): validate the
    question, resolve lang + metered Gemini access, and assemble the prompt
    context (known words, profile, level, card, bounded history)."""
    question = (req.question or "").strip()[:1000]
    if not question:
        raise HTTPException(400, "question required")
    lang = req.lang if (req.lang in translation.LANG_INFO) else await _tutor_lang(user)

    access = await _resolve_gemini(user)            # meters 1 unit (shared-key users)

    # A focused card Q&A doesn't need the whole deck for context — a smaller
    # sample keeps the prompt (and so the latency) down.
    known_words = await db.get_known_words(user["id"], lang, limit=40)
    learner_profile = await db.get_setting(user["id"], "learner_profile") or ""
    course = await db.get_active_course(user["id"], lang)
    level = (course.get("level") or "A1") if course else "A1"

    card = req.card.model_dump() if req.card else None
    if card:
        card.pop("card_id", None)
        card = {k: (v or "").strip()[:600] for k, v in card.items()}

    # Bounded, plain-text history for the prompt (last few turns of this pop-over).
    history = []
    for m in (req.history or [])[-8:]:
        role = "tutor" if (m.get("role") == "tutor") else "user"
        txt = (m.get("text") or "").strip()[:1000]
        if txt:
            history.append({"role": role, "text": txt})

    return {
        "question": question, "lang": lang, "access": access,
        "known_words": known_words, "learner_profile": learner_profile,
        "level": level, "card": card, "history": history,
    }


@app.post("/api/tutor/ask/stream")
@limiter.limit("20/minute;400/day")
async def tutor_ask_stream(request: Request, req: CardAskRequest, user: dict = Depends(current_user)):
    """Streaming version of the card Q&A — forwards the tutor's prose answer
    token-by-token (NDJSON `delta` events), then a single `final` event with the
    parsed/normalized new_items + card_updates. Big perceived-latency win: the
    learner reads the answer as it's written instead of waiting for the whole
    JSON. Same metering/limits as the blocking route (1 unit, charged up front)."""
    ctx = await _prepare_card_ask(req, user)
    await db.record_study_activity(user["id"])   # asking about a card counts as study

    async def _gen():
        try:
            async for evt in tutor.stream_card_ask(
                ctx["lang"], ctx["question"], ctx["card"], ctx["history"],
                api_key=ctx["access"].api_key, model=ctx["access"].model_reader,
                level=ctx["level"], learner_profile=ctx["learner_profile"],
                known_words=ctx["known_words"],
            ):
                yield evt
        except Exception as e:
            logger.error("Tutor card-ask stream failed lang=%s: %s", ctx["lang"], e, exc_info=True)
            yield json.dumps({"type": "error",
                              "message": "The tutor couldn't answer — please try again."}) + "\n"

    return StreamingResponse(_gen(), media_type="application/x-ndjson",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.post("/api/tutor/ask")
@limiter.limit("20/minute;400/day")
async def tutor_ask(request: Request, req: CardAskRequest, user: dict = Depends(current_user)):
    """Contextual, EPHEMERAL study Q&A about a specific flashcard (1 metered call).
    Nothing is stored — short follow-up history (if any) lives client-side and is
    passed back in. Powers the 'Ask the tutor' pop-over on the study page.
    (Kept as a non-streaming fallback; the pop-over uses /ask/stream.)"""
    ctx = await _prepare_card_ask(req, user)
    question, lang, access = ctx["question"], ctx["lang"], ctx["access"]
    known_words, learner_profile = ctx["known_words"], ctx["learner_profile"]
    level, card, history = ctx["level"], ctx["card"], ctx["history"]

    try:
        out = await tutor.ask_about_card(
            lang, question, card, history,
            # Use the fast reader model (flash-lite), not the heavier chat model —
            # a card explanation is low-reasoning, so the cheaper/faster model is
            # plenty and shaves another chunk off the response time.
            api_key=access.api_key, model=access.model_reader,
            level=level, learner_profile=learner_profile, known_words=known_words,
        )
    except Exception as e:
        logger.error("Tutor card-ask failed lang=%s: %s", lang, e, exc_info=True)
        raise HTTPException(502, "The tutor couldn't answer — please try again.")

    await db.record_study_activity(user["id"])   # asking about a card counts as study
    msg: dict = {"role": "tutor", "reply": out["reply"], "new_items": out["new_items"]}
    if out.get("card_updates"):
        msg["card_updates"] = out["card_updates"]
    return {"message": msg, "lang": lang}


class TutorDrillRequest(BaseModel):
    skill: str


@app.post("/api/tutor/conversations/{conv_id}/drill")
@limiter.limit("20/minute;400/day")
async def tutor_drill(request: Request, conv_id: int, req: TutorDrillRequest,
                      user: dict = Depends(current_user)):
    """Start a practice drill on one skill the tutor just taught. Stores ONLY a
    tutor message — the learner never sees a 'drill me' prompt in the chat. The
    tutor's own drill question keeps the model in drill mode on follow-up turns."""
    skill = (req.skill or "").strip()[:80]
    if not skill:
        raise HTTPException(400, "skill required")
    conv = await db.get_tutor_conversation(user["id"], conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    lang = conv["lang"]

    access = await _resolve_gemini(user)            # meters 1 unit (shared-key users)

    learner_profile = await db.get_setting(user["id"], "learner_profile") or ""
    course = await db.get_active_course(user["id"], lang)
    level = course.get("level") or "A1" if course else "A1"

    # Vocab strategy by deck size. Small decks: hand the model the whole known list
    # (cheap, no embeddings). Large decks: embedding-snap a relevant subset to the
    # construction so a 2000-word deck never floods the prompt (we pass only a small
    # sample + the total count + the snapped palette).
    known_count = await db.count_known_words(user["id"], lang)
    cefr_stats = ""
    known_strings: list[str] | None = None
    vectors_provider = None
    if known_count <= tutor.SMALL_DECK_MAX:
        known_words = await db.get_known_words(user["id"], lang, limit=tutor.SMALL_DECK_MAX)
    else:
        deck = await db.get_known_words(user["id"], lang, limit=tutor.LARGE_DECK_VECTOR_CAP)
        known_words = deck[:tutor.LARGE_DECK_SAMPLE]    # strongest-first sample for the prompt
        known_strings = [(w.get("target_text") or "").strip() for w in deck]
        cefr_stats = await _known_cefr_stats(user["id"], lang, access.api_key)
        # Embed the deck lazily — only invoked if the cheap opener's answer leaned on
        # words the learner doesn't know (verify-then-snap), so most drills skip it.
        async def vectors_provider():
            return await _known_word_vectors(lang, deck, access.api_key,
                                             cap=tutor.LARGE_DECK_VECTOR_CAP)

    # Opener context = NORMAL chat only (skip any prior drills' turns).
    history = []
    for m in await db.get_tutor_messages(user["id"], conv_id):
        if m["drill_id"]:
            continue
        if m["role"] == "tutor":
            try:
                history.append({"role": "tutor", "text": json.loads(m["content"]).get("reply", "")})
            except (ValueError, TypeError):
                history.append({"role": "tutor", "text": m["content"]})
        else:
            history.append({"role": "user", "text": m["content"]})

    try:
        out = await tutor.start_drill(
            lang, skill, history,
            api_key=access.api_key, model=tutor.TUTOR_MODEL,
            level=level, learner_profile=learner_profile, known_words=known_words,
            deck_count=known_count, cefr_stats=cefr_stats,
            known_word_strings=known_strings, known_vectors_provider=vectors_provider,
        )
    except Exception as e:
        logger.error("Tutor drill failed lang=%s: %s", lang, e, exc_info=True)
        raise HTTPException(502, "The tutor couldn't start the drill — please try again.")

    # A blank opener (transient empty/blocked model response, even after the retry
    # in start_drill) must NOT be persisted — it would render as an empty drill
    # panel that stays broken on reload. Surface a clean retry instead.
    if not (out.get("reply") or "").strip():
        logger.warning("Tutor drill opener returned a blank reply lang=%s skill=%r", lang, skill)
        raise HTTPException(502, "The tutor couldn't start the drill — please try again.")

    payload = {k: out[k] for k in ("reply", "reply_en", "gloss", "corrections", "new_items", "points", "drill")}
    payload["drill"] = ""
    # The opener's own message id becomes the drill-group id; mark the conversation
    # as in an active drill so subsequent answers route through drill mode.
    msg_id = await db.add_tutor_message(user["id"], conv_id, "tutor",
                                        json.dumps(payload, ensure_ascii=False))
    await db.set_tutor_message_drill(user["id"], msg_id, msg_id, skill)
    await db.set_active_drill(user["id"], conv_id, msg_id)
    await db.record_study_activity(user["id"])
    return {"message": {"role": "tutor", **payload, "drill_id": msg_id, "drill_skill": skill},
            "drill_id": msg_id, "skill": skill}


@app.post("/api/tutor/conversations/{conv_id}/drill/end")
async def tutor_end_drill(conv_id: int, user: dict = Depends(current_user)):
    """End the active drill sub-session (learner-initiated). No LLM call — just
    clears the conversation's active-drill flag so further messages are normal
    chat again. The drill's turns stay stored (collapsed client-side) but are
    excluded from future chat context."""
    conv = await db.get_tutor_conversation(user["id"], conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    await db.set_active_drill(user["id"], conv_id, None)
    return {"success": True}


class LessonDrillRequest(BaseModel):
    construction: str
    plan_items: list[dict] = []   # [{english, target}] — the drill plan with reference translations
    answer: str | None = None
    turn: int = 1
    lang: str | None = None


@app.post("/api/lesson/drill")
@limiter.limit("60/minute;800/day")
async def lesson_drill(request: Request, req: LessonDrillRequest,
                       user: dict = Depends(current_user)):
    """One turn of an inline lesson construction-drill. Deterministic-first
    judging: exact match against the plan's expected answer = instant correct;
    LLM only called when the answer differs. 1 metered call/turn."""
    construction = (req.construction or "").strip()[:80]
    if not construction:
        raise HTTPException(400, "construction required")
    lang = req.lang if req.lang in translation.LANG_INFO else await _tutor_lang(user)
    access = await _resolve_gemini(user)

    known_words = await db.get_known_words(user["id"], lang, limit=tutor.SMALL_DECK_MAX)
    course = await db.get_active_course(user["id"], lang)
    level = course.get("level") or "A1" if course else "A1"
    answer = (req.answer or "").strip()[:500] or None

    try:
        out = await tutor.run_lesson_drill(
            lang, construction, answer=answer, plan_items=req.plan_items,
            api_key=access.api_key,
            level=level, known_words=known_words, turn=max(1, int(req.turn or 1)),
        )
    except Exception as e:
        logger.error("Lesson drill failed lang=%s: %s", lang, e, exc_info=True)
        raise HTTPException(502, "The drill couldn't continue — please try again.")

    if answer is not None:
        await db.record_study_activity(user["id"])
    return out


@app.get("/api/ruby")
@limiter.limit("300/minute")
async def ruby(request: Request, text: str, lang: str = "yue", user: dict = Depends(current_user)):
    """Tokenise `text` and return per-token romanization for ruby rendering.
    Returns [{text, roman, is_word}] — same data shape the reader uses internally.
    Empty `roman` means no annotation needed (Latin script or punctuation)."""
    text = (text or "").strip()[:500]
    if not text or lang not in translation.LANG_INFO:
        return []
    tokens = tokenizer.tokenize(text, lang)
    words = [t["text"] for t in tokens if t["is_word"]]
    rmap = tokenizer.romanize_words(words, lang) if words else {}
    return [{"text": t["text"], "roman": rmap.get(t["text"], "") if t["is_word"] else "", "is_word": t["is_word"]}
            for t in tokens]


class RubyBatchRequest(BaseModel):
    texts: list[str]
    lang: str = "yue"


# Languages whose messages get inline romanization ruby (non-Latin scripts).
# Mirrors the client-side RUBY_LANGS set in messages.html / tutor.html.
_RUBY_LANGS = {"yue", "cmn", "ko", "hi", "te", "ja", "bn", "ur", "ar", "ru", "fa", "uk", "el", "th", "he"}


_token_cache: dict[str, list[dict]] = {}
_TOKEN_CACHE_MAX = 2000


def _tokenize_map(texts: list[str], lang: str) -> dict:
    """Tokenise + romanise a set of texts → {original_text: [{text, roman, is_word}]}.
    Sync CPU work (jieba/pycantonese); call via asyncio.to_thread."""
    out: dict = {}
    if lang not in translation.LANG_INFO:
        return out
    for raw in texts:
        text = (raw or "").strip()[:500]
        if not text or raw in out:
            continue
        cache_key = f"{lang}:{raw}"
        cached = _token_cache.get(cache_key)
        if cached is not None:
            out[raw] = cached
            continue
        tokens = tokenizer.tokenize(text, lang)
        words = [t["text"] for t in tokens if t["is_word"]]
        rmap = tokenizer.romanize_words(words, lang) if words else {}
        result = [{"text": t["text"], "roman": rmap.get(t["text"], "") if t["is_word"] else "",
                     "is_word": t["is_word"]} for t in tokens]
        out[raw] = result
        if len(_token_cache) < _TOKEN_CACHE_MAX:
            _token_cache[cache_key] = result
    return out


@app.post("/api/ruby/batch")
@limiter.limit("60/minute")
async def ruby_batch(request: Request, req: RubyBatchRequest, user: dict = Depends(current_user)):
    """Tokenise many texts in one round trip (tutor chat renders a whole
    conversation at once — per-bubble GET /api/ruby was N requests).
    Returns {results: {<original text>: [{text, roman, is_word}, ...]}}."""
    if req.lang not in translation.LANG_INFO:
        return {"results": {}}

    def _tokens(text: str) -> list[dict]:
        tokens = tokenizer.tokenize(text, req.lang)
        words = [t["text"] for t in tokens if t["is_word"]]
        rmap = tokenizer.romanize_words(words, req.lang) if words else {}
        return [{"text": t["text"], "roman": rmap.get(t["text"], "") if t["is_word"] else "",
                 "is_word": t["is_word"]} for t in tokens]

    def _all() -> dict:
        results = {}
        for raw in req.texts[:80]:
            text = (raw or "").strip()[:500]
            if not text or raw in results:
                continue
            results[raw] = _tokens(text)
        return results

    # Tokenization is sync CPU work (jieba/pycantonese) — keep the event loop free.
    return {"results": await asyncio.to_thread(_all)}


@app.get("/api/tts")
@limiter.limit("120/minute")
async def tts(request: Request, text: str, lang: str = "yue", user: dict = Depends(current_user)):
    """On-demand TTS for the lesson player (and other UI). Returns MP3."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    if lang not in translation.LANG_INFO:
        lang = "yue"
    try:
        data = await audio.generate(text[:200], lang)
    except Exception:
        raise HTTPException(502, "TTS failed")
    return Response(content=data, media_type="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── Admin: dashboard ─────────────────────────────────────────────────────────

@app.get("/api/admin/dashboard")
async def admin_dashboard_stats(user: dict = Depends(current_admin)):
    import resource, shutil
    stats = await db.get_admin_dashboard_stats()
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    rss_mb = rusage.ru_maxrss / 1024
    total_ram_mb = None
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) / 1024
                    break
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_ram_mb = int(line.split()[1]) / 1024
                    break
    except OSError:
        pass
    cpu_count = os.cpu_count()
    disk = shutil.disk_usage("/")
    stats["server"] = {
        "rss_mb": round(rss_mb, 1),
        "total_ram_mb": round(total_ram_mb, 0) if total_ram_mb else None,
        "cpu_count": cpu_count,
        "disk_used_gb": round(disk.used / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        "uptime_seconds": int(time.time() - _SERVER_START),
        "pid": os.getpid(),
    }
    stats["plan_limits"] = PLAN_LIMITS
    return stats


# ── Admin: user management ────────────────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_list_users(user: dict = Depends(current_admin)):
    return {"users": await db.list_users()}


class PlanUpdate(BaseModel):
    plan: str


@app.put("/api/admin/users/{user_id}/plan")
async def admin_set_plan(user_id: int, req: PlanUpdate, user: dict = Depends(current_admin)):
    """Comp a friend to Pro (or revert to Free) without Stripe. Comped users have
    no stripe_customer_id, so subscription webhooks never touch them."""
    if req.plan not in PLAN_LIMITS:
        raise HTTPException(400, f"Unknown plan: {req.plan}")
    target = await db.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.get("stripe_customer_id"):
        raise HTTPException(
            409,
            "This user has a Stripe subscription; change their plan via the "
            "billing portal so it stays in sync.",
        )
    await db.set_user_plan(user_id, req.plan)
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


@app.post("/api/admin/reprocess-images")
async def admin_reprocess_images(
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_admin),
):
    """Re-run vision analysis on image messages that got empty suggestions."""
    rows = await db.get_unprocessed_image_messages()
    if not rows:
        return {"queued": 0, "message": "No unprocessed image messages found"}
    background_tasks.add_task(_reprocess_images_bg, rows)
    return {"queued": len(rows), "message": f"Reprocessing {len(rows)} image(s) in background"}


async def _reprocess_images_bg(rows: list[dict]) -> None:
    api_key = _SHARED_API_KEY
    if not api_key:
        logger.warning("reprocess-images: GEMINI_API_KEY not set, aborting")
        return
    ok_count = 0
    for row in rows:
        media_path = MEDIA_DIR / f"{row['media_id']}.jpg"
        if not media_path.exists():
            logger.warning("reprocess-images: media file missing for msg %d", row["msg_id"])
            continue
        image_bytes = media_path.read_bytes()
        langs: list[str] = []
        sender_id = row["sender_user_id"]
        other_id = row["user2_id"] if row["user1_id"] == sender_id else row["user1_id"]
        sender_lang = await db.get_setting(sender_id, "default_target_lang") or "yue"
        if sender_lang not in langs:
            langs.append(sender_lang)
        if other_id:
            recipient_lang = await db.get_setting(other_id, "default_target_lang") or "yue"
            if recipient_lang not in langs:
                langs.append(recipient_lang)
        try:
            vision = await asyncio.get_event_loop().run_in_executor(
                None, translation.analyze_image, image_bytes, langs, api_key
            )
        except Exception:
            logger.exception("reprocess-images: vision failed for msg %d", row["msg_id"])
            continue
        sugg = vision.get("suggestions") or {}
        has_content = any(
            (isinstance(v, dict) and (v.get("sender") or v.get("receiver")))
            or (isinstance(v, list) and v)
            for v in sugg.values()
        )
        if not has_content:
            logger.warning("reprocess-images: still no suggestions for msg %d", row["msg_id"])
            continue
        description = vision.get("description") or "Photo"
        descriptions = vision.get("descriptions") or {}
        analysis = {
            "type": "image",
            "url": row["analysis"].get("url", ""),
            "description": description,
            "descriptions": descriptions,
            "suggestions": sugg,
        }
        await db.update_image_message(row["msg_id"], description, analysis)
        ok_count += 1
        logger.info("reprocess-images: updated msg %d (%d/%d)", row["msg_id"], ok_count, len(rows))
    logger.info("reprocess-images: done — %d/%d updated", ok_count, len(rows))


# ── Feedback / bug reports ───────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    type: str = "bug"
    title: str
    description: str = ""


@app.post("/api/feedback")
@limiter.limit("10/minute;50/day")
async def submit_feedback(
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
):
    """Submit a bug report or feature request, optionally with a screenshot."""
    import io as _io
    form = await request.form()
    fb_type = str(form.get("type", "bug"))
    if fb_type not in ("bug", "feature"):
        fb_type = "bug"
    title = str(form.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "Title is required")
    description = str(form.get("description", "")).strip()
    screenshot_media_id = None
    screenshot_file = form.get("screenshot")
    if screenshot_file and hasattr(screenshot_file, "read"):
        raw = await screenshot_file.read()
        if raw and len(raw) <= _MAX_UPLOAD_BYTES and _PIL_OK:
            try:
                img = Image.open(_io.BytesIO(raw))
                img = _ImageOps.exif_transpose(img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                if max(img.size) > _MAX_IMAGE_DIM:
                    img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM), Image.LANCZOS)
                buf = _io.BytesIO()
                img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
                out_bytes = buf.getvalue()
                screenshot_media_id = _uuid.uuid4().hex
                (MEDIA_DIR / f"{screenshot_media_id}.jpg").write_bytes(out_bytes)
                await db.add_media_record(screenshot_media_id, user["id"], None, len(out_bytes))
            except Exception:
                pass
    feedback_id = await db.create_feedback(
        user["id"], fb_type, title, description, screenshot_media_id
    )
    background_tasks.add_task(_triage_feedback_bg, feedback_id, title, description)
    background_tasks.add_task(
        _notify_admins_feedback, user, fb_type, title, feedback_id,
    )
    return {"id": feedback_id, "message": "Thank you for your feedback!"}


async def _triage_feedback_bg(feedback_id: int, title: str, description: str) -> None:
    """Background: AI-triage a feedback report."""
    api_key = _SHARED_API_KEY
    if not api_key:
        return
    try:
        groups = await db.list_feedback_groups()
        existing_groups = [g["triage_group"] for g in groups]
        result = await asyncio.to_thread(
            translation.triage_feedback, title, description, existing_groups, api_key
        )
        await db.update_feedback_triage(
            feedback_id,
            result["triage_summary"],
            result["triage_group"],
            result["suggested_prompt"],
            result["priority"],
        )
    except Exception:
        pass


async def _notify_admins_feedback(
    submitter: dict, fb_type: str, title: str, feedback_id: int,
) -> None:
    """Push-notify all admins about a new feedback report."""
    try:
        admin_ids = await db.get_admin_user_ids()
        label = "Bug report" if fb_type == "bug" else "Feature request"
        username = submitter.get("username", "A user")
        for aid in admin_ids:
            if aid == submitter["id"]:
                continue
            await _send_push_to_user(
                aid,
                f"New {label.lower()}",
                f"{username}: {title}",
                url="/feedback",
                tag=f"feedback-{feedback_id}",
            )
    except Exception:
        logger.exception("_notify_admins_feedback failed")


async def _notify_user_feedback_status(report: dict, new_status: str) -> None:
    """Push-notify the submitter that their report's status changed."""
    try:
        labels = {
            "triaged": "has been triaged",
            "in_progress": "is being worked on",
            "resolved": "has been resolved",
            "closed": "has been closed",
        }
        label = labels.get(new_status, f"status → {new_status}")
        await _send_push_to_user(
            report["user_id"],
            "Feedback update",
            f'Your report "{report["title"]}" {label}.',
            url="/feedback",
            tag=f"feedback-{report['id']}",
        )
    except Exception:
        logger.exception("_notify_user_feedback_status failed")


@app.get("/api/feedback/mine")
async def my_feedback(user: dict = Depends(current_user)):
    return {"reports": await db.list_user_feedback(user["id"])}


@app.get("/api/admin/feedback")
async def admin_list_feedback(
    status: str = "",
    user: dict = Depends(current_admin),
):
    reports = await db.list_feedback(status=status or None)
    return {"reports": reports}


@app.get("/api/admin/feedback/{feedback_id}")
async def admin_get_feedback(feedback_id: int, user: dict = Depends(current_admin)):
    report = await db.get_feedback(feedback_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return report


class FeedbackStatusUpdate(BaseModel):
    status: str
    admin_notes: str = ""


@app.put("/api/admin/feedback/{feedback_id}/status")
async def admin_update_feedback_status(
    feedback_id: int,
    req: FeedbackStatusUpdate,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_admin),
):
    if req.status not in ("new", "triaged", "in_progress", "resolved", "closed"):
        raise HTTPException(400, "Invalid status")
    report = await db.get_feedback(feedback_id)
    await db.update_feedback_status(feedback_id, req.status, req.admin_notes)
    if report and report["user_id"] != user["id"]:
        background_tasks.add_task(
            _notify_user_feedback_status, report, req.status,
        )
    return {"success": True}


@app.get("/api/admin/feedback/groups")
async def admin_feedback_groups(user: dict = Depends(current_admin)):
    return {"groups": await db.list_feedback_groups()}


# ── Reader ────────────────────────────────────────────────────────────────────

class ReaderGenerateRequest(BaseModel):
    prompt: str = ""
    target_lang: str = "yue"
    difficulty: str = "B1"
    num_paragraphs: int = 4


class ReaderUrlRequest(BaseModel):
    url: str
    target_lang: str = "yue"
    difficulty: str = "B1"
    # True = the page is already in the target language → use it verbatim (no AI
    # rewrite). False = translate/adapt it to the target language at `difficulty`.
    in_target_language: bool = False


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
    sentences = await db.get_reader_sentences(user_id, text["id"])

    # For texts with stored sentence boundaries (URL/PDF imports), tokenize
    # each sentence individually so the frontend uses OUR boundaries instead
    # of re-deriving them from punctuation (which always drifts).
    # Verify the stored sentences reconstruct the content (old imports have
    # sentence_text set but from a different splitting — fall back for those).
    has_stored = False
    if sentences and sentences[0].get("sentence_text"):
        joined = "\n".join(s["sentence_text"] for s in sentences)
        has_stored = joined == text["content"]
    if has_stored:
        all_words: list[str] = []
        groups: list[list[dict]] = []
        for s in sentences:
            s_toks = tokenizer.tokenize(s["sentence_text"], text["target_lang"])
            all_words.extend(t["text"] for t in s_toks if t["is_word"])
            groups.append(s_toks)
        unique_words = list(dict.fromkeys(all_words))
        statuses = await db.get_word_statuses(user_id, unique_words, text["target_lang"])
        for g in groups:
            _annotate_tokens(g, statuses)
        tokens = [t for g in groups for t in g]
        rom_map = tokenizer.romanize_words(all_words, text["target_lang"])
    else:
        tokens = tokenizer.tokenize(text["content"], text["target_lang"])
        words = [t["text"] for t in tokens if t["is_word"]]
        unique_words = list(dict.fromkeys(words))
        statuses = await db.get_word_statuses(user_id, unique_words, text["target_lang"])
        _annotate_tokens(tokens, statuses)
        rom_map = tokenizer.romanize_words(words, text["target_lang"])

        # Stored translations may be indexed under old sentence boundaries
        # (e.g. a tokenizer fix changed how sentences split). Heal the stored
        # rows to the current boundaries so EVERY downstream reader (this
        # response, the windowed /preload, page-turn preloads) sees correct
        # indices — not just this in-memory response. Without persisting, the
        # /preload call the frontend fires right after returns raw stale-index
        # rows and re-pollutes the client cache, re-breaking alignment.
        if sentences:
            new_sents = tokenizer.split_sentences(tokens, text["target_lang"])
            # Stale = a cached row's text no longer matches the boundary at its
            # own index (a tokenizer fix shifted things). Normal lazy partial
            # caching is NOT stale, so we don't rewrite healthy readings.
            stale = any(
                s["sentence_idx"] >= len(new_sents)
                or s["sentence_text"] != new_sents[s["sentence_idx"]]
                for s in sentences
            )
            if stale:
                await db.resync_reader_sentences(text["id"], new_sents)
                sentences = await db.get_reader_sentences(user_id, text["id"])
            by_idx = {s["sentence_idx"]: s for s in sentences}
            sentences = [
                by_idx.get(i, {"sentence_idx": i, "sentence_text": st,
                               "translation": None, "has_audio": False})
                for i, st in enumerate(new_sents)
            ]
        # Authoritative sentence grouping: the client renders THESE groups
        # instead of re-splitting tokens in JS, so client/server boundaries
        # can't drift. Same splitter (and order/count) as `sentences` above.
        groups = tokenizer.split_token_sentences(tokens, text["target_lang"])

    preload_complete = bool(sentences) and all(
        s["translation"] and s["has_audio"] for s in sentences
    )
    all_vocab_added = bool(unique_words) and all(w in statuses for w in unique_words)
    resp = {
        **text,
        "tokens": tokens,
        "sentences": sentences,
        "preload_complete": preload_complete,
        "romanization": rom_map,
        "all_vocab_added": all_vocab_added,
        "sentence_groups": groups,
    }
    img_id = text.get("image_media_id")
    if img_id:
        resp["image_url"] = f"/api/media/{img_id}.jpg"
    return resp


@app.post("/api/reader/generate")
@limiter.limit("20/minute;100/day")
async def reader_generate(request: Request, req: ReaderGenerateRequest, user: dict = Depends(current_user)):
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    access = await _resolve_gemini(user)
    prompt = req.prompt.strip()
    if not prompt:
        existing = await db.list_reader_texts(user["id"], target_lang=req.target_lang)
        existing_titles = [t["title"] for t in existing[:30]]
        titles_hint = ""
        if existing_titles:
            titles_hint = (
                f"The user already has these stories: {', '.join(existing_titles[:20])}. "
                "Write something DIFFERENT — pick a fresh genre, topic, or setting."
            )
        prompt = (
            "Write something interesting and creative for a language learner. "
            "Pick a vivid scenario: a conversation, a short story, a diary entry, "
            "a news article, or a cultural anecdote. " + titles_hint
        )
    result = await translation.generate_reader_text(
        prompt, req.target_lang, req.difficulty,
        req.num_paragraphs,
        api_key=access.api_key, model=access.model_reader,
    )
    text_id = await db.create_reader_text(
        user["id"], result["title"], req.prompt or "(auto-generated)", result["content"], req.target_lang,
        difficulty=req.difficulty,
    )
    text = await db.get_reader_text(user["id"], text_id)
    return await _build_text_response(user["id"], text)


@app.post("/api/reader/generate-from-image")
@limiter.limit("10/minute;50/day")
async def reader_generate_from_image(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    """Generate a reader text inspired by an uploaded image."""
    import io as _io
    if not _PIL_OK:
        raise HTTPException(500, "Image processing unavailable")
    form = await request.form()
    target_lang = str(form.get("target_lang", "yue"))
    difficulty = str(form.get("difficulty", "B1"))
    num_paragraphs = int(form.get("num_paragraphs", 4))
    prompt = str(form.get("prompt", ""))
    if target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    access = await _resolve_gemini(user)
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image too large (max 20 MB)")
    try:
        img = Image.open(_io.BytesIO(raw))
        img = _ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > _MAX_IMAGE_DIM:
            img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        out_bytes = buf.getvalue()
    except Exception as exc:
        raise HTTPException(400, f"Could not process image: {exc}")
    # Save image to media dir for display in the reader
    media_id = _uuid.uuid4().hex
    (MEDIA_DIR / f"{media_id}.jpg").write_bytes(out_bytes)
    await db.add_media_record(media_id, user["id"], None, len(out_bytes))
    existing = await db.list_reader_texts(user["id"], target_lang=target_lang)
    existing_titles = [t["title"] for t in existing[:30]]
    result = await translation.generate_reader_text_from_image(
        out_bytes, target_lang, difficulty, num_paragraphs, prompt,
        existing_titles=existing_titles,
        api_key=access.api_key, model=access.model_reader,
    )
    stored_prompt = prompt or f"(image: {result.get('description', 'photo')})"
    text_id = await db.create_reader_text(
        user["id"], result["title"], stored_prompt, result["content"], target_lang,
        image_media_id=media_id, difficulty=difficulty,
    )
    text = await db.get_reader_text(user["id"], text_id)
    return await _build_text_response(user["id"], text)


async def _reading_from_source(
    user: dict, *, title: str, text: str, target_lang: str, difficulty: str,
    in_target_language: bool, stored_prompt: str,
) -> dict:
    """Turn extracted source text into a saved reading.

    If `in_target_language` the text is used verbatim (no AI — the whole point of
    the flag). Otherwise it's translated sentence-by-sentence into the target
    language (one metered LLM call per batch).
    """
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "Couldn't extract any readable text.")
    segments = None  # [{target, english}] when translated
    simplify = difficulty and difficulty != "original"
    if in_target_language and not simplify:
        content = text[:extract.MAX_TEXT_CHARS]
        final_title = (title or "").strip() or (content.split("\n", 1)[0][:60]) or "Imported reading"
    elif in_target_language and simplify:
        access = await _resolve_gemini(user)
        try:
            result = await translation.simplify_article(
                text, target_lang, difficulty,
                api_key=access.api_key, model=access.model_reader,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("simplify_article failed")
            raise HTTPException(502, f"Couldn't simplify the article: {exc}")
        segments = result["segments"]
        content = "\n".join(seg["target"] for seg in segments)
        final_title = (title or "").strip() or (content.split("\n", 1)[0][:60]) or "Imported reading"
    else:
        access = await _resolve_gemini(user)
        try:
            result = await translation.translate_article_to_reading(
                text, target_lang,
                api_key=access.api_key, model=access.model_reader,
                difficulty=difficulty if simplify else "",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("translate_article_to_reading failed")
            raise HTTPException(502, f"Couldn't translate the article: {exc}")
        segments = result["segments"]
        content = "\n".join(seg["target"] for seg in segments)
        final_title = (title or "").strip() or "Imported reading"
    text_id = await db.create_reader_text(
        user["id"], final_title[:120], stored_prompt, content, target_lang,
        difficulty=difficulty,
    )
    # One reader_sentence per source sentence — simple 1:1. The server sends
    # pre-grouped tokens so the frontend never re-derives sentence boundaries.
    if segments:
        for i, seg in enumerate(segments):
            await db.upsert_reader_sentence(
                text_id, i, seg["target"], seg["english"], None, None,
            )
    saved = await db.get_reader_text(user["id"], text_id)
    return await _build_text_response(user["id"], saved)


@app.post("/api/reader/generate-from-url")
@limiter.limit("10/minute;50/day")
async def reader_generate_from_url(request: Request, req: ReaderUrlRequest, user: dict = Depends(current_user)):
    """Create a reading from a pasted article URL."""
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    try:
        extracted = await extract.fetch_and_extract_url(req.url)
    except extract.ExtractError as exc:
        raise HTTPException(400, str(exc))
    return await _reading_from_source(
        user, title=extracted["title"], text=extracted["text"],
        target_lang=req.target_lang, difficulty=req.difficulty,
        in_target_language=req.in_target_language,
        stored_prompt=f"(URL: {req.url.strip()[:300]})",
    )


@app.post("/api/reader/generate-from-pdf")
@limiter.limit("10/minute;50/day")
async def reader_generate_from_pdf(request: Request, file: UploadFile = File(...), user: dict = Depends(current_user)):
    """Create a reading from an uploaded PDF."""
    form = await request.form()
    target_lang = str(form.get("target_lang", "yue"))
    difficulty = str(form.get("difficulty", "B1"))
    in_target = str(form.get("in_target_language", "false")).lower() == "true"
    if target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, "PDF too large (max 20 MB)")
    try:
        extracted = await asyncio.to_thread(extract.extract_pdf, raw)
    except extract.ExtractError as exc:
        raise HTTPException(400, str(exc))
    fname = (file.filename or "document.pdf").rsplit("/", 1)[-1]
    title = extracted["title"] or fname.rsplit(".", 1)[0]
    return await _reading_from_source(
        user, title=title, text=extracted["text"],
        target_lang=target_lang, difficulty=difficulty,
        in_target_language=in_target,
        stored_prompt=f"(PDF: {fname[:200]})",
    )


@app.get("/api/reader/texts")
async def reader_list_texts(user: dict = Depends(current_user)):
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    return {"texts": await db.list_reader_texts(user["id"], target_lang=lang)}


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
async def reader_preload(
    text_id: int,
    start: int = 0,
    count: int | None = None,
    user: dict = Depends(current_user),
):
    """Pre-generate translations + audio for a sentence window, skipping cached.

    By default (`count` unset) the WHOLE text is preloaded — used for short
    generated stories. For long imports the frontend passes `start`/`count` to
    preload only a window around the reader's current page (render-ahead), so a
    50+ sentence article doesn't fire 50 LLM/TTS calls on open. Returns all
    cached sentences + `total_sentences` and whether the WHOLE text is complete.
    """
    text = await db.get_reader_text(user["id"], text_id)
    if not text:
        raise HTTPException(404, "Text not found")
    access = await _resolve_gemini(user)

    existing = {s["sentence_idx"]: s for s in await db.get_reader_sentences(user["id"], text_id)}
    # Use stored sentence texts when available (URL/PDF imports store 1:1 with
    # source sentences). Otherwise derive from tokenization (AI stories).
    # Verify stored sentences reconstruct the content (old imports may not).
    use_stored = False
    if existing and next(iter(existing.values())).get("sentence_text"):
        total_stored = max(existing.keys()) + 1
        joined = "\n".join(existing[i]["sentence_text"] for i in range(total_stored) if i in existing)
        use_stored = joined == text["content"]
    if use_stored:
        total = max(existing.keys()) + 1
        sent_texts = [existing[i]["sentence_text"] for i in range(total)]
    else:
        tokens = tokenizer.tokenize(text["content"], text["target_lang"])
        sent_texts = tokenizer.split_sentences(tokens, text["target_lang"])
        total = len(sent_texts)
        # Heal stale cached rows to the current boundaries so the returned
        # sentences (read straight from the DB below) carry correct indices —
        # a tokenizer fix would otherwise leave old translations at wrong
        # indices and the frontend keys its cache by sentence_idx. Only rewrite
        # when actually stale (a cached row's text no longer matches its index);
        # normal lazy partial caching is left alone.
        stale = any(
            i >= len(sent_texts) or s["sentence_text"] != sent_texts[i]
            for i, s in existing.items()
        )
        if stale:
            await db.resync_reader_sentences(text_id, sent_texts)
            existing = {s["sentence_idx"]: s
                        for s in await db.get_reader_sentences(user["id"], text_id)}

    # Select the window to process. `count=None` → everything (legacy behaviour).
    lo = max(0, start)
    hi = total if count is None else min(total, lo + max(0, count))
    window = [(i, sent_texts[i]) for i in range(lo, hi)]

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
                    tr = await translation.translate_sentence(
                        sent_text, text["target_lang"],
                        api_key=access.api_key, model=access.model_translate,
                    )
                    trans_text = tr.get("english", "")
                    rom_text = tr.get("romanization") or rom_text
                except Exception:
                    trans_text = ""
            if need_audio:
                try:
                    audio_bytes = await audio.generate(sent_text, text["target_lang"])
                except Exception:
                    audio_bytes = None

        await db.upsert_reader_sentence(text_id, idx, sent_text, trans_text, audio_bytes, rom_text)

    await _asyncio.gather(*[process(i, s) for i, s in window])

    sentences = await db.get_reader_sentences(user["id"], text_id)
    complete = len(sentences) >= total and all(
        s["translation"] and s["has_audio"] for s in sentences
    )
    return {"sentences": sentences, "total_sentences": total, "preload_complete": complete}


@app.get("/api/reader/texts/{text_id}/sentences/{idx}/audio")
async def sentence_audio(text_id: int, idx: int, user: dict = Depends(current_user)):
    data = await db.get_sentence_audio(user["id"], text_id, idx)
    if not data:
        raise HTTPException(404, "Audio not ready")
    return Response(content=data, media_type="audio/mpeg")


class SentenceTranslateRequest(BaseModel):
    text: str
    target_lang: str = "yue"
    text_id: int | None = None
    sentence_idx: int | None = None


@app.post("/api/reader/translate-sentence")
@limiter.limit("60/minute")
async def reader_translate_sentence(
    request: Request,
    req: SentenceTranslateRequest,
    user: dict = Depends(current_user),
):
    """Translate a single reader sentence to English using a plain prose prompt."""
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    access = await _resolve_gemini(user)
    result = await translation.translate_sentence(
        req.text, req.target_lang,
        api_key=access.api_key, model=access.model_translate,
    )
    if req.text_id is not None and req.sentence_idx is not None:
        await db.upsert_reader_sentence(
            req.text_id, req.sentence_idx, req.text,
            translation=result.get("english"),
            romanization=result.get("romanization"),
        )
    return result


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
    text_id: int | None = None
    sentence_idx: int | None = None


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
    if req.text_id is not None and req.sentence_idx is not None:
        await db.upsert_reader_sentence(
            req.text_id, req.sentence_idx, req.text.strip(),
            audio_data=data,
        )
    return Response(content=data, media_type="audio/mpeg")


@app.post("/api/reader/translate-word")
@limiter.limit("120/minute;2000/day")
async def reader_translate_word(request: Request, req: ReaderTranslateWordRequest, user: dict = Depends(current_user)):
    if req.target_lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    # Romanization for the tap tooltip MUST match the reader ruby, which is the
    # offline oracle (tokenizer.romanize_words). Compute it once and prefer it
    # over the card's stored value or the LLM's (Gemini omits Thai tone marks
    # and uses a different scheme), so the same word never shows two spellings.
    oracle_rom = ""
    if translation.LANG_INFO[req.target_lang].get("romanization"):
        oracle_rom = tokenizer.romanize_text(req.word, req.target_lang)
    # Check if word is already in the user's deck.
    statuses = await db.get_word_statuses(user["id"], [req.word], req.target_lang)
    if req.word in statuses:
        # Word exists — find the card. First try exact match, then normalized/substring.
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
            if row is None:
                # Exact miss — get_word_statuses matched via normalization or CJK substring.
                # Find the shortest card whose normalized target_text matches or contains this token.
                # Shortest-first ensures we prefer the most atomic card over a longer phrase.
                norm_word = db._normalize_word(req.word)
                async with conn.execute(
                    "SELECT id, source_text, target_text, romanization, notes "
                    "FROM cards WHERE user_id=? AND target_lang=? "
                    "ORDER BY length(target_text) ASC",
                    (user["id"], req.target_lang),
                ) as cur2:
                    for r in await cur2.fetchall():
                        card_norm = db._normalize_word(r["target_text"])
                        if card_norm == norm_word or (norm_word and norm_word in card_norm):
                            row = r
                            break
        if row:
            resp: dict = {
                "source": "deck",
                "card_id": row["id"],
                "target_text": row["target_text"],
                "source_text": row["source_text"],
                "romanization": oracle_rom or row["romanization"],
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
                        resp["context_romanization"] = oracle_rom or ctx_candidate.get("romanization", "")
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
        "romanization": oracle_rom or candidate.get("romanization", ""),
        "notes": candidate.get("notes", ""),
        "priority": result.get("priority", 3),
        "suggested_labels": result.get("suggested_labels", []),
        "classifier": result.get("classifier", ""),
        "cefr_level": result.get("cefr_level"),
        "status": "new",
    }


# ── Reading comprehension quiz ────────────────────────────────────────────────

class ComprehensionRequest(BaseModel):
    text: str
    lang: str
    title: str = ""


class ComprehensionXpRequest(BaseModel):
    lang: str


_COMPREHENSION_XP = 10


@app.post("/api/reader/comprehension")
@limiter.limit("20/minute;100/day")
async def reader_comprehension(request: Request, req: ComprehensionRequest, user: dict = Depends(current_user)):
    if req.lang not in translation.LANG_INFO:
        raise HTTPException(400, "Unsupported language")
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text required")
    access = await _resolve_gemini(user)
    lang_name = translation.LANG_INFO[req.lang].get("full_name", translation.LANG_INFO[req.lang]["name"])
    snippet = text[:2000]
    prompt = (
        f"You are a reading comprehension quiz generator for {lang_name} learners.\n\n"
        f"Based on this {lang_name} text (title: \"{(req.title or 'untitled')[:80]}\"):\n"
        f"{snippet}\n\n"
        "Generate ONE multiple-choice comprehension question in English about a specific "
        "detail or fact from the text. The question must be answerable from the text alone.\n\n"
        "Return ONLY valid JSON (no markdown fences):\n"
        '{"question":"...","options":["A","B","C","D"],"correct":0}\n\n'
        "Rules:\n"
        "- question: a clear, specific question in English\n"
        "- options: exactly 4 English options, one correct and three plausible but wrong\n"
        "- correct: 0-based index of the correct answer (vary the position, not always 0)\n"
        "- Do NOT make the question about the title alone; use details from the body\n"
    )
    try:
        raw = await asyncio.to_thread(translation._call, prompt, access.api_key, access.model_reader)
        data = translation._parse_json(raw)
    except Exception as e:
        raise HTTPException(502, f"AI error: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("options"), list) or len(data["options"]) < 4:
        raise HTTPException(502, "Could not generate a valid question")
    correct = int(data.get("correct", 0))
    if not 0 <= correct < 4:
        correct = 0
    await db.record_study_activity(user["id"])
    return {
        "question": str(data.get("question", "")).strip(),
        "options": [str(o).strip() for o in data["options"][:4]],
        "correct": correct,
        "xp": _COMPREHENSION_XP,
    }


@app.post("/api/reader/comprehension/xp")
async def reader_comprehension_xp(req: ComprehensionXpRequest, user: dict = Depends(current_user)):
    lang = req.lang if req.lang in translation.LANG_INFO else "yue"
    await db.add_points(user["id"], lang, _COMPREHENSION_XP, "comprehension")
    await db.record_study_activity(user["id"])
    return {"xp": _COMPREHENSION_XP}


# ── Story sharing ────────────────────────────────────────────────────────────

class PublishStoryRequest(BaseModel):
    visibility: str = "public"

@app.put("/api/reader/texts/{text_id}/publish")
async def publish_story(text_id: int, req: PublishStoryRequest, user: dict = Depends(current_user)):
    if req.visibility not in ("private", "friends", "public"):
        raise HTTPException(400, "Invalid visibility")
    ok = await db.publish_story(user["id"], text_id, req.visibility)
    if not ok:
        raise HTTPException(404, "Story not found")
    return {"ok": True}

class RateStoryRequest(BaseModel):
    rating: int

@app.post("/api/reader/texts/{text_id}/rate")
async def rate_story(text_id: int, req: RateStoryRequest, user: dict = Depends(current_user)):
    if not 1 <= req.rating <= 5:
        raise HTTPException(400, "Rating must be 1-5")
    story = await db.get_story_public(text_id, user["id"])
    if not story:
        raise HTTPException(404, "Story not found or not accessible")
    await db.rate_story(user["id"], text_id, req.rating)
    return {"ok": True}

@app.get("/api/reader/community")
async def reader_community(
    request: Request,
    difficulty: str | None = None,
    min_rating: float | None = None,
    search: str | None = None,
    sort: str = "newest",
    length: str | None = None,
    lang: str | None = None,
    user: dict = Depends(current_user),
):
    stories = await db.list_community_stories(
        user["id"], target_lang=lang, difficulty=difficulty,
        min_rating=min_rating, search=search, sort=sort,
    )
    if length:
        thresholds = {"short": (0, 300), "medium": (300, 1000), "long": (1000, 999999)}
        lo, hi = thresholds.get(length, (0, 999999))
        stories = [s for s in stories if lo <= s.get("content_length", 0) < hi]
    return {"stories": stories}

@app.get("/api/reader/community/{text_id}")
async def reader_community_text(text_id: int, user: dict = Depends(current_user)):
    text = await db.get_story_public(text_id, user["id"])
    if not text:
        raise HTTPException(404, "Story not found or not accessible")
    sentences = await db.get_reader_sentences_public(text_id)
    has_stored = False
    if sentences and sentences[0].get("sentence_text"):
        joined = "\n".join(s["sentence_text"] for s in sentences)
        has_stored = joined == text["content"]
    if has_stored:
        all_words: list[str] = []
        groups: list[list[dict]] = []
        for s in sentences:
            s_toks = tokenizer.tokenize(s["sentence_text"], text["target_lang"])
            all_words.extend(t["text"] for t in s_toks if t["is_word"])
            groups.append(s_toks)
        unique_words = list(dict.fromkeys(all_words))
        statuses = await db.get_word_statuses(user["id"], unique_words, text["target_lang"])
        for g in groups:
            _annotate_tokens(g, statuses)
        tokens = [t for g in groups for t in g]
        rom_map = tokenizer.romanize_words(all_words, text["target_lang"])
    else:
        tokens = tokenizer.tokenize(text["content"], text["target_lang"])
        words = [t["text"] for t in tokens if t["is_word"]]
        unique_words = list(dict.fromkeys(words))
        statuses = await db.get_word_statuses(user["id"], unique_words, text["target_lang"])
        _annotate_tokens(tokens, statuses)
        rom_map = tokenizer.romanize_words(words, text["target_lang"])

        if sentences:
            new_sents = tokenizer.split_sentences(tokens, text["target_lang"])
            old_by_text: dict[str, dict] = {}
            for s in sentences:
                st = s.get("sentence_text", "")
                if st and st not in old_by_text:
                    old_by_text[st] = s
            reindexed = []
            for i, st in enumerate(new_sents):
                old = old_by_text.get(st)
                if old:
                    reindexed.append({**old, "sentence_idx": i})
                else:
                    reindexed.append({"sentence_idx": i, "sentence_text": st,
                                      "translation": None, "has_audio": False})
            sentences = reindexed
        # Authoritative grouping for the client (see _build_text_response).
        groups = tokenizer.split_token_sentences(tokens, text["target_lang"])

    preload_complete = bool(sentences) and all(
        s["translation"] and s["has_audio"] for s in sentences
    )
    rating_info = await db.get_story_rating(text_id)
    user_rating = await db.get_user_story_rating(user["id"], text_id)
    resp = {
        **text,
        "tokens": tokens,
        "sentences": sentences,
        "preload_complete": preload_complete,
        "romanization": rom_map,
        "avg_rating": rating_info["avg_rating"],
        "rating_count": rating_info["rating_count"],
        "user_rating": user_rating,
        "sentence_groups": groups,
    }
    img_id = text.get("image_media_id")
    if img_id:
        resp["image_url"] = f"/api/media/{img_id}.jpg"
    return resp


# ── Shared decks ─────────────────────────────────────────────────────────────

class CreateDeckRequest(BaseModel):
    name: str
    description: str = ""
    visibility: str = "public"
    card_ids: list[int] = []

@app.post("/api/decks")
async def create_deck(req: CreateDeckRequest, user: dict = Depends(current_user)):
    if not req.name.strip():
        raise HTTPException(400, "Name required")
    if req.visibility not in ("private", "friends", "public"):
        raise HTTPException(400, "Invalid visibility")
    if not req.card_ids:
        raise HTTPException(400, "Select at least one card")
    default_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    cards = await db.get_all_cards(user["id"])
    card_map = {c["id"]: c for c in cards}
    items = []
    langs: set[str] = set()
    for cid in req.card_ids:
        c = card_map.get(cid)
        if c:
            clang = c.get("target_lang") or default_lang
            langs.add(clang)
            items.append({
                "source_text": c["source_text"],
                "target_text": c["target_text"],
                "romanization": c.get("romanization"),
                "notes": c.get("notes"),
                "target_lang": clang,
            })
    if not items:
        raise HTTPException(400, "No valid cards found")
    # A deck holds a single language — keeps the deck (and importers' cards) coherent.
    if len(langs) > 1:
        raise HTTPException(400, "A deck can only contain cards from one language")
    deck_lang = next(iter(langs)) if langs else default_lang
    deck_id = await db.create_shared_deck(
        user["id"], req.name.strip(), req.description.strip(),
        deck_lang, req.visibility, items,
    )
    # Tag the creator's own cards with a "📦 {name}" label so they can study this
    # deck immediately (same label scheme importers get).
    label_id = await db.label_cards_for_deck(user["id"], req.name.strip(), req.card_ids)
    return {"id": deck_id, "label_id": label_id}

class UpdateDeckRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    visibility: str | None = None

@app.put("/api/decks/{deck_id}")
async def update_deck(deck_id: int, req: UpdateDeckRequest, user: dict = Depends(current_user)):
    if req.visibility and req.visibility not in ("private", "friends", "public"):
        raise HTTPException(400, "Invalid visibility")
    ok = await db.update_shared_deck(user["id"], deck_id, req.name, req.description, req.visibility)
    if not ok:
        raise HTTPException(404, "Deck not found")
    return {"ok": True}

@app.delete("/api/decks/{deck_id}")
async def delete_deck(deck_id: int, user: dict = Depends(current_user)):
    ok = await db.delete_shared_deck(user["id"], deck_id)
    if not ok:
        raise HTTPException(404, "Deck not found")
    return {"ok": True}

@app.get("/api/decks/mine")
async def my_decks(user: dict = Depends(current_user)):
    return {"decks": await db.list_my_decks(user["id"])}

@app.get("/api/decks/community")
async def community_decks(
    search: str | None = None,
    lang: str | None = None,
    sort: str | None = None,
    user: dict = Depends(current_user),
):
    return {"decks": await db.list_community_decks(user["id"], target_lang=lang, search=search, sort=sort)}

@app.get("/api/decks/{deck_id}")
async def get_deck(deck_id: int, user: dict = Depends(current_user)):
    deck = await db.get_shared_deck(deck_id, user["id"])
    if not deck:
        raise HTTPException(404, "Deck not found or not accessible")
    return deck

@app.post("/api/decks/{deck_id}/rate")
async def rate_deck(deck_id: int, request: Request, user: dict = Depends(current_user)):
    body = await request.json()
    rating = body.get("rating")
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        raise HTTPException(400, "Rating must be 1–5")
    deck = await db.get_shared_deck(deck_id, user["id"])
    if not deck:
        raise HTTPException(404, "Deck not found or not accessible")
    await db.rate_deck(user["id"], deck_id, rating)
    return await db.get_deck_rating(deck_id)

@app.post("/api/decks/{deck_id}/import")
async def import_deck(deck_id: int, user: dict = Depends(current_user)):
    deck = await db.get_shared_deck(deck_id, user["id"])
    if not deck:
        raise HTTPException(404, "Deck not found or not accessible")
    result = await db.import_deck(user["id"], deck_id)
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "Import failed"))
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Friends + Messaging
# ══════════════════════════════════════════════════════════════════════════════

class FriendRequestBody(BaseModel):
    username: str

class SendMessageBody(BaseModel):
    text: str
    mode: str = "native"   # "native" = user typed in English; "target" = typed in their target lang

class ConnectMessengerBody(BaseModel):
    page_id: str
    page_access_token: str
    page_name: str = ""


@app.get("/api/friends")
async def get_friends(user: dict = Depends(current_user)):
    return await db.get_friends(user["id"])


@app.get("/api/friends/search")
async def search_users(q: str, user: dict = Depends(current_user)):
    if len(q) < 2:
        return {"users": []}
    found = await db.get_user_by_username(q.strip())
    if not found or found["id"] == user["id"]:
        return {"users": []}
    return {"users": [{"id": found["id"], "username": found["username"]}]}


@app.post("/api/friends/request")
async def send_friend_request(body: FriendRequestBody, user: dict = Depends(current_user)):
    target = await db.get_user_by_username(body.username.strip())
    if not target:
        raise HTTPException(404, "User not found")
    result = await db.send_friend_request(user["id"], target["id"])
    if not result["ok"]:
        raise HTTPException(409, result.get("error", "conflict"))
    asyncio.create_task(_send_push_to_user(
        target["id"],
        title="👋 Friend Request",
        body=f"{user['username']} sent you a friend request",
        url="/messages",
        tag="friend-request",
    ))
    return {"ok": True}


@app.post("/api/friends/{friendship_id}/accept")
async def accept_friend_request(friendship_id: int, user: dict = Depends(current_user)):
    requester_id = await db.respond_friend_request(friendship_id, user["id"], accept=True)
    if not requester_id:
        raise HTTPException(404, "Request not found")
    asyncio.create_task(_send_push_to_user(
        requester_id,
        title="🤝 Friend Request Accepted",
        body=f"{user['username']} accepted your friend request",
        url="/messages",
        tag="friend-accepted",
    ))
    return {"ok": True}


@app.post("/api/friends/{friendship_id}/reject")
async def reject_friend_request(friendship_id: int, user: dict = Depends(current_user)):
    requester_id = await db.respond_friend_request(friendship_id, user["id"], accept=False)
    if not requester_id:
        raise HTTPException(404, "Request not found")
    return {"ok": True}


@app.delete("/api/friends/{other_user_id}")
async def remove_friend(other_user_id: int, user: dict = Depends(current_user)):
    await db.remove_friend(user["id"], other_user_id)
    return {"ok": True}


@app.get("/api/friends/leaderboard")
async def friends_leaderboard(user: dict = Depends(current_user)):
    data = await db.get_friends(user["id"])
    friends = data["friends"]
    user_ids = [user["id"]] + [f["user_id"] for f in friends]
    usernames = {user["id"]: user["username"]}
    for f in friends:
        usernames[f["user_id"]] = f["username"]

    import aiosqlite as _aiosqlite
    placeholders = ",".join("?" * len(user_ids))
    async with _aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            f"SELECT user_id, COALESCE(SUM(points), 0) FROM points_ledger "
            f"WHERE user_id IN ({placeholders}) GROUP BY user_id",
            user_ids,
        ) as cur:
            xp_map = {row[0]: row[1] async for row in cur}

    entries = []
    for uid in user_ids:
        streak = await db.get_streak(uid)
        entries.append({
            "user_id": uid,
            "username": usernames[uid],
            "xp": xp_map.get(uid, 0),
            "streak": streak,
            "is_me": uid == user["id"],
        })
    entries.sort(key=lambda x: -x["xp"])
    return {"leaderboard": entries}


@app.get("/api/notifications/counts")
async def notifications_counts(user: dict = Depends(current_user)):
    unread, pending = await asyncio.gather(
        db.get_total_unread(user["id"]),
        db.get_pending_friend_request_count(user["id"]),
    )
    return {"unread_messages": unread, "friend_requests": pending, "total": unread + pending}


@app.get("/api/push/vapid-public-key")
async def vapid_public_key(_user: dict = Depends(current_user)):
    return {"public_key": _VAPID_PUBLIC_KEY}


class PushSubscribeBody(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


@app.post("/api/push/subscribe")
async def push_subscribe(body: PushSubscribeBody, user: dict = Depends(current_user)):
    await db.add_push_subscription(user["id"], body.endpoint, body.p256dh, body.auth)
    return {"ok": True}


@app.delete("/api/push/subscribe")
async def push_unsubscribe(body: PushSubscribeBody, user: dict = Depends(current_user)):
    await db.remove_push_subscription(body.endpoint)
    return {"ok": True}


def _send_push_sync(endpoint: str, p256dh: str, auth_key: str, payload: str) -> tuple[bool, bool, str | None]:
    """Send one push. Returns (sent_ok, keep_subscription, error_message)."""
    try:
        from pywebpush import webpush
    except Exception as ex:
        msg = f"pywebpush not installed: {ex}"
        logging.error(msg)
        return False, True, msg
    try:
        # Apple's push service rejects the placeholder example.com sub; derive the
        # claims audience from the endpoint origin (pywebpush does aud itself).
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth_key}},
            data=payload,
            vapid_private_key=_VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{_VAPID_CLAIMS_EMAIL}"},
            ttl=86400,
        )
        return True, True, None
    except Exception as ex:
        resp = getattr(ex, "response", None)
        code = getattr(resp, "status_code", None)
        detail = ""
        try:
            detail = (resp.text or "")[:200] if resp is not None else ""
        except Exception:
            pass
        msg = f"{type(ex).__name__}: {ex}" + (f" [{code}] {detail}" if code else "")
        if code in (404, 410):
            return False, False, msg  # subscription is dead — drop it
        logging.warning("Push send failed: %s", msg, exc_info=True)
        return False, True, msg  # keep, might be transient


async def _send_push_to_user(user_id: int, title: str, body: str,
                              url: str = "/messages", tag: str = "default") -> dict:
    """Send to all of a user's subscriptions. Returns {sent, total, error}."""
    subs = await db.get_push_subscriptions(user_id)
    if not subs:
        return {"sent": 0, "total": 0, "error": "no subscriptions"}
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    dead: list[str] = []
    last_err: str | None = None
    for sub in subs:
        ok, keep, err = await asyncio.to_thread(
            _send_push_sync, sub["endpoint"], sub["p256dh"], sub["auth"], payload)
        if ok:
            sent += 1
        if err:
            last_err = err
        if not keep:
            dead.append(sub["endpoint"])
    for ep in dead:
        await db.remove_push_subscription(ep)
    return {"sent": sent, "total": len(subs), "error": None if sent else last_err}


@app.get("/api/conversations")
async def list_conversations(user: dict = Depends(current_user)):
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    convs = await db.list_conversations(user["id"])
    for c in convs:
        raw_trans = c.pop("last_translations", None)
        if raw_trans:
            try:
                trans = json.loads(raw_trans)
                c["last_text"] = trans.get(lang) or c.get("last_text")
            except Exception:
                pass
    return {"conversations": convs}


@app.post("/api/conversations")
async def open_conversation(user: dict = Depends(current_user), friend_user_id: int = 0):
    if not friend_user_id:
        raise HTTPException(400, "friend_user_id required")
    result = await db.get_or_create_conversation(user["id"], friend_user_id)
    return result


@app.get("/api/conversations/{conv_id}/messages")
async def get_messages(conv_id: int, before_id: int = 0,
                       user: dict = Depends(current_user)):
    msgs = await db.get_messages(conv_id, user["id"], limit=50,
                                 before_id=before_id or None)
    if not msgs:
        convs = await db.list_conversations(user["id"])
        if not any(c["id"] == conv_id for c in convs):
            raise HTTPException(403, "Not a participant")
    lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    msg_ids = [m["id"] for m in msgs]
    reactions_map = await db.get_reactions_for_messages(msg_ids, user["id"])

    # Resolve an API key once (only needed if on-demand translation is required)
    api_key = None

    result = []
    for m in msgs:
        is_mine = m["sender_user_id"] == user["id"]
        trans = json.loads(m["translations"]) if m.get("translations") else {}
        analysis = json.loads(m["analysis"]) if m.get("analysis") else {}
        display = trans.get(lang)

        # On-demand translation: if the viewer's language isn't cached
        # (e.g. recipient changed their target language after the message was
        # sent, or the translation was never generated), translate now and
        # persist so future loads are instant.
        if not display and not is_mine and m["original_text"] and m["original_lang"] != lang and not analysis.get("deleted"):
            if api_key is None:
                try:
                    access = await _resolve_gemini(user, meter=False)
                    api_key = access.api_key
                except Exception:
                    api_key = ""
            if api_key:
                try:
                    tr = await translation.translate_message(
                        m["original_text"], m["original_lang"], lang, api_key=api_key)
                    display = tr["translated"]
                    trans[lang] = display
                    if not analysis.get("reply_en") and m["original_lang"] != "en":
                        analysis["reply_en"] = tr.get("reply_en", "")
                    asyncio.create_task(
                        db.update_message_translations(m["id"], trans))
                except Exception:
                    pass

        if not display:
            display = trans.get(lang) or m["original_text"]

        result.append({
            "id": m["id"],
            "is_mine": is_mine,
            "display_text": display,
            "original_text": m["original_text"],
            "original_lang": m["original_lang"],
            "sender_name": m["sender_username"] or m.get("sender_name") or "Unknown",
            "sender_user_id": m["sender_user_id"],
            "created_at": m["created_at"],
            "analysis": analysis,
            "reactions": reactions_map.get(m["id"], {}),
        })

    # Attach inline romanization tokens (single round trip — no separate /api/ruby
    # fetch from the client, so romanization is always present and consistent).
    if lang in _RUBY_LANGS:
        texts: set[str] = set()
        for r in result:
            if r["analysis"].get("deleted") or r["analysis"].get("type") == "image":
                continue
            if r["display_text"]:
                texts.add(r["display_text"])
            for c in r["analysis"].get("corrections", []):
                if c.get("corrected"):
                    texts.add(c["corrected"])
        tmap = await asyncio.to_thread(_tokenize_map, list(texts), lang)
        for r in result:
            rt: dict = {}
            if r["display_text"] in tmap:
                rt[r["display_text"]] = tmap[r["display_text"]]
            for c in r["analysis"].get("corrections", []):
                ct = c.get("corrected")
                if ct in tmap:
                    rt[ct] = tmap[ct]
            r["tokens"] = rt
    return {"messages": result}


@app.post("/api/conversations/{conv_id}/messages")
async def send_message(conv_id: int, body: SendMessageBody,
                       user: dict = Depends(current_user)):
    # Verify participation and get other party's info
    convs = await db.list_conversations(user["id"])
    conv = next((c for c in convs if c["id"] == conv_id), None)
    if not conv:
        raise HTTPException(403, "Not a participant")

    access = await _resolve_gemini(user, meter=True)
    api_key = access.api_key
    sender_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    original_lang = "en" if body.mode == "native" else sender_lang

    translations: dict[str, str] = {}
    analysis: dict = {}

    if body.mode == "native":
        # Typed English → translate to sender's target lang; get nuance note + vocab
        tr = await translation.translate_message(body.text, "en", sender_lang, api_key=api_key)
        sender_display = tr["translated"]
        translations[sender_lang] = sender_display
        analysis["reply_en"] = body.text
        if tr.get("translation_failed"):
            analysis["translation_failed"] = True
        if tr.get("nuance_note"):
            analysis["nuance_note"] = tr["nuance_note"]
        if tr.get("explanation"):
            analysis["explanation"] = tr["explanation"]
        vocab = tr.get("vocab") or []
        if vocab:
            words = [v["target_text"] for v in vocab]
            statuses = await db.get_word_statuses(user["id"], words, sender_lang)
            vocab = [v for v in vocab if v["target_text"] not in statuses]
        if vocab:
            analysis["vocab"] = vocab
    else:
        # Already in target lang — grammar check + English meaning
        sender_display = body.text
        translations[sender_lang] = body.text
        msg_analysis = await translation.analyze_message(body.text, sender_lang, api_key=api_key)
        analysis["corrections"] = msg_analysis.get("corrections", [])
        analysis["reply_en"] = msg_analysis.get("reply_en", "")

    # What the other party sees (if in-app convo)
    if conv["type"] == "inapp":
        other_user_id = conv["other_user_id"]
        recipient_lang = await db.get_setting(other_user_id, "default_target_lang") or "yue"
        if recipient_lang != sender_lang:
            tr2 = await translation.translate_message(body.text, original_lang, recipient_lang,
                                                      api_key=api_key)
            translations[recipient_lang] = tr2["translated"]
        else:
            translations[recipient_lang] = sender_display

    msg_id = await db.add_message(
        conv_id, user["id"], body.text, original_lang, translations, analysis=analysis
    )

    tokens: dict = {}
    if sender_lang in _RUBY_LANGS:
        texts = [sender_display] + [c.get("corrected", "")
                                    for c in analysis.get("corrections", [])]
        texts += [v.get("target_text", "") for v in analysis.get("vocab", [])]
        tokens = await asyncio.to_thread(_tokenize_map, [t for t in texts if t], sender_lang)

    # Push notification to the other party (in-app conversations only)
    if conv["type"] == "inapp":
        other_user_id = conv["other_user_id"]
        preview = translations.get(recipient_lang, sender_display)[:80]
        asyncio.create_task(_send_push_to_user(
            other_user_id,
            title=f"💬 {user['username']}",
            body=preview,
            url="/messages",
            tag=f"msg-{conv_id}",
        ))

    return {"ok": True, "display_text": sender_display, "msg_id": msg_id,
            "analysis": analysis, "tokens": tokens}


@app.post("/api/messages/{msg_id}/retry-translate")
@limiter.limit("10/minute")
async def retry_translate_message(msg_id: int, request: Request,
                                  user: dict = Depends(current_user)):
    """Re-attempt translation for a message that failed."""
    msg = await db.get_message(msg_id, user["id"])
    if not msg:
        raise HTTPException(404, "Message not found")
    if msg["sender_user_id"] != user["id"]:
        raise HTTPException(403, "Not your message")
    if msg["original_lang"] != "en":
        raise HTTPException(400, "Only English messages can be retranslated")

    access = await _resolve_gemini(user, meter=True)
    sender_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"

    tr = await translation.translate_message(
        msg["original_text"], "en", sender_lang, api_key=access.api_key)
    if tr.get("translation_failed"):
        raise HTTPException(502, "Translation failed again — try later")

    translations = msg["translations"]
    translations[sender_lang] = tr["translated"]
    analysis = msg["analysis"]
    analysis.pop("translation_failed", None)
    analysis["reply_en"] = msg["original_text"]
    if tr.get("nuance_note"):
        analysis["nuance_note"] = tr["nuance_note"]
    if tr.get("explanation"):
        analysis["explanation"] = tr["explanation"]
    vocab = tr.get("vocab") or []
    if vocab:
        words = [v["target_text"] for v in vocab]
        statuses = await db.get_word_statuses(user["id"], words, sender_lang)
        vocab = [v for v in vocab if v["target_text"] not in statuses]
    if vocab:
        analysis["vocab"] = vocab

    await db.update_message_analysis(msg_id, translations, analysis)

    tokens: dict = {}
    display = tr["translated"]
    if sender_lang in _RUBY_LANGS:
        texts = [display] + [v.get("target_text", "") for v in vocab]
        tokens = await asyncio.to_thread(_tokenize_map, [t for t in texts if t], sender_lang)

    return {"ok": True, "display_text": display, "analysis": analysis, "tokens": tokens}


@app.post("/api/conversations/{conv_id}/image")
@limiter.limit("10/minute")
async def send_image_message(conv_id: int, request: Request,
                             file: UploadFile = File(...),
                             user: dict = Depends(current_user)):
    """Accept an image upload, downscale it, save to disk, store as a message."""
    import io as _io
    if not _PIL_OK:
        raise HTTPException(500, "Image processing unavailable (Pillow not installed)")

    convs = await db.list_conversations(user["id"])
    conv = next((c for c in convs if c["id"] == conv_id), None)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image too large (max 20 MB)")

    try:
        img = Image.open(_io.BytesIO(raw))
        img = _ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > _MAX_IMAGE_DIM:
            img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        out_bytes = buf.getvalue()
    except Exception as exc:
        raise HTTPException(400, f"Could not process image: {exc}")

    media_id = _uuid.uuid4().hex
    (MEDIA_DIR / f"{media_id}.jpg").write_bytes(out_bytes)
    await db.add_media_record(media_id, user["id"], conv_id, len(out_bytes))

    url = f"/api/media/{media_id}.jpg"

    # Collect distinct target languages for phrase suggestions
    langs_set: list[str] = []
    sender_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    if sender_lang not in langs_set:
        langs_set.append(sender_lang)
    if conv.get("type") == "inapp":
        recipient_lang = await db.get_setting(conv["other_user_id"], "default_target_lang") or "yue"
        if recipient_lang not in langs_set:
            langs_set.append(recipient_lang)

    # Run vision analysis synchronously (1–3 s); fall back gracefully on failure
    vision: dict = {"description": "Photo", "suggestions": {}}
    try:
        api_key = _SHARED_API_KEY
        if api_key:
            vision = await asyncio.get_event_loop().run_in_executor(
                None, translation.analyze_image, out_bytes, langs_set, api_key
            )
        else:
            logger.warning("image vision skipped: GEMINI_API_KEY (shared key) not set")
    except Exception:
        logger.exception("image vision analysis failed conv=%s langs=%s", conv_id, langs_set)

    description = vision.get("description") or "Photo"
    descriptions = vision.get("descriptions") or {}
    # Store per-language descriptions in the translations field so each user
    # sees the preview in their own target language in the conversation list.
    # Prefix with 📷 to match the original_text format.
    translations_for_msg = {lang: f"📷 {d}" for lang, d in descriptions.items() if d}
    analysis = {
        "type": "image",
        "url": url,
        "description": description,
        "descriptions": descriptions,
        "suggestions": vision.get("suggestions") or {},
    }
    msg_id = await db.add_message(
        conv_id, user["id"], f"📷 {description}", "en", translations_for_msg,
        analysis=analysis,
    )
    await db.record_study_activity(user["id"])

    if conv.get("type") == "inapp":
        # Use recipient's target-language description in their push notification
        notif_body = descriptions.get(recipient_lang) or description
        asyncio.create_task(_send_push_to_user(
            conv["other_user_id"],
            title=f"📷 {user['username']}",
            body=notif_body,
            url="/messages",
            tag=f"msg-{conv_id}",
        ))

    return {"ok": True, "url": url, "msg_id": msg_id, "description": description,
            "descriptions": descriptions, "suggestions": analysis["suggestions"]}


@app.get("/api/media/{media_id}")
async def serve_media(media_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}\.jpg", media_id):
        raise HTTPException(404)
    path = MEDIA_DIR / media_id
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.post("/api/conversations/{conv_id}/read")
async def mark_read(conv_id: int, user: dict = Depends(current_user)):
    await db.mark_conversation_read(conv_id, user["id"])
    return {"ok": True}


@app.get("/api/conversations/start/{friend_user_id}")
async def start_or_get_conv_with_friend(friend_user_id: int, user: dict = Depends(current_user)):
    """Get or create an in-app conversation with a friend."""
    result = await db.get_or_create_conversation(user["id"], friend_user_id)
    return result


@app.delete("/api/messages/{msg_id}")
async def delete_message(msg_id: int, user: dict = Depends(current_user)):
    """Delete/unsend a message. Only the sender can delete their own messages."""
    analysis = await db.delete_message(msg_id, user["id"])
    if analysis is None:
        raise HTTPException(404, "Message not found or not yours")
    if analysis.get("type") == "image":
        url = analysis.get("url", "")
        media_id = url.rsplit("/", 1)[-1].replace(".jpg", "") if url else ""
        if media_id:
            media_path = MEDIA_DIR / f"{media_id}.jpg"
            media_path.unlink(missing_ok=True)
    return {"ok": True}


_ALLOWED_REACTIONS = {"❤️", "😂", "😮", "😢", "👍", "🔥"}


@app.post("/api/messages/{msg_id}/reactions/{emoji}")
async def toggle_reaction(msg_id: int, emoji: str, user: dict = Depends(current_user)):
    if emoji not in _ALLOWED_REACTIONS:
        raise HTTPException(400, "Emoji not allowed")
    added = await db.toggle_reaction(msg_id, user["id"], emoji)
    reactions = await db.get_reactions_for_messages([msg_id], user["id"])
    return {"added": added, "reactions": reactions.get(msg_id, {})}


# ── Facebook Messenger integration ────────────────────────────────────────────

@app.get("/api/messenger/status")
async def messenger_status(user: dict = Depends(current_user)):
    account = await db.get_messenger_account(user["id"])
    if account:
        return {"connected": True, "page_name": account["page_name"], "page_id": account["page_id"]}
    return {"connected": False, "fb_app_id": _FB_APP_ID}


@app.post("/api/messenger/connect")
async def connect_messenger(body: ConnectMessengerBody, user: dict = Depends(current_user)):
    """Manually connect with a page access token (from Graph API Explorer or OAuth callback)."""
    if not body.page_id or not body.page_access_token:
        raise HTTPException(400, "page_id and page_access_token required")
    await db.upsert_messenger_account(user["id"], body.page_id,
                                      body.page_access_token, body.page_name or None)
    return {"ok": True}


@app.delete("/api/messenger/disconnect")
async def disconnect_messenger(user: dict = Depends(current_user)):
    await db.delete_messenger_account(user["id"])
    return {"ok": True}


# ── Social connect (Facebook) — identity + mutual-app-friend discovery ─────────
# NOTE: friend discovery uses Graph `/me/friends`, which returns ONLY friends who
# also use this app AND granted `user_friends`. Until Meta App Review approves
# `user_friends`, this works only for the app's own developers/testers. The OAuth
# + matching plumbing is in place so enabling it later is just a review away.

_FB_OAUTH_SCOPES = "public_profile,user_friends"
_oauth_states: dict[str, tuple[int, float]] = {}  # CSRF state → (user_id, expiry)


def _fb_redirect_uri() -> str:
    return f"{email_utils.APP_URL}/api/social/facebook/callback"


@app.get("/api/social/facebook/login")
async def facebook_login(user: dict = Depends(current_user)):
    """Return the Facebook OAuth dialog URL (frontend redirects the browser to it)."""
    if not _FB_APP_ID or not _FB_APP_SECRET:
        raise HTTPException(400, "Facebook integration is not configured.")
    now = time.time()
    for k in [k for k, (_, exp) in _oauth_states.items() if exp < now]:
        _oauth_states.pop(k, None)
    state = secrets.token_urlsafe(24)
    _oauth_states[state] = (user["id"], now + 600)
    from urllib.parse import urlencode
    params = urlencode({
        "client_id": _FB_APP_ID,
        "redirect_uri": _fb_redirect_uri(),
        "state": state,
        "scope": _FB_OAUTH_SCOPES,
        "response_type": "code",
    })
    return {"url": f"https://www.facebook.com/v19.0/dialog/oauth?{params}"}


@app.get("/api/social/facebook/callback")
async def facebook_callback(code: str = "", state: str = "", error: str = "",
                            user: dict = Depends(current_user)):
    """OAuth redirect target. Verifies CSRF state, exchanges the code, stores the
    connection, then bounces back to the messages page."""
    if error or not code:
        return RedirectResponse("/messages?fb=error", status_code=302)
    entry = _oauth_states.pop(state, None)
    if not entry or entry[0] != user["id"] or entry[1] < time.time():
        return RedirectResponse("/messages?fb=error", status_code=302)
    token_resp = await _messenger.exchange_code_for_token(
        _FB_APP_ID, _FB_APP_SECRET, _fb_redirect_uri(), code)
    token = token_resp.get("access_token")
    if not token:
        return RedirectResponse("/messages?fb=error", status_code=302)
    me = await _messenger.get_me(token)
    fb_id = me.get("id")
    if not fb_id:
        return RedirectResponse("/messages?fb=error", status_code=302)
    await db.upsert_social_account(
        user["id"], "facebook", fb_id, crypto.encrypt(token), me.get("name"))
    return RedirectResponse("/messages?fb=connected", status_code=302)


@app.get("/api/social/facebook/status")
async def facebook_status(user: dict = Depends(current_user)):
    acct = await db.get_social_account(user["id"], "facebook")
    return {
        "connected": bool(acct),
        "display_name": acct["display_name"] if acct else None,
        "configured": bool(_FB_APP_ID and _FB_APP_SECRET),
    }


@app.delete("/api/social/facebook/disconnect")
async def facebook_disconnect(user: dict = Depends(current_user)):
    await db.delete_social_account(user["id"], "facebook")
    return {"ok": True}


@app.get("/api/social/facebook/friend-suggestions")
async def facebook_friend_suggestions(user: dict = Depends(current_user)):
    """Friends-of-this-user who also use the app (mapped from Facebook), minus
    anyone already a friend / pending. Each: {user_id, username}."""
    acct = await db.get_social_account(user["id"], "facebook")
    if not acct:
        raise HTTPException(400, "Facebook not connected")
    try:
        token = crypto.decrypt(acct["access_token"])
    except Exception:
        raise HTTPException(400, "Stored Facebook token is invalid — please reconnect.")
    fb_friends = await _messenger.get_app_friends(token)
    fb_ids = [f["id"] for f in fb_friends if f.get("id")]
    matched = await db.get_users_by_social_ids("facebook", fb_ids, user["id"])
    rel = await db.get_friends(user["id"])
    excluded = ({f["user_id"] for f in rel["friends"]}
                | {f["user_id"] for f in rel["sent"]}
                | {f["user_id"] for f in rel["received"]})
    suggestions = [{"user_id": m["user_id"], "username": m["username"]}
                   for m in matched if m["user_id"] not in excluded]
    return {"suggestions": suggestions, "fb_friend_count": len(fb_ids)}


@app.get("/api/messenger/webhook")
async def messenger_webhook_verify(
    request: Request,
    hub_mode: str = "",
    hub_verify_token: str = "",
    hub_challenge: str = "",
):
    """Meta webhook verification handshake."""
    mode = request.query_params.get("hub.mode", hub_mode)
    token = request.query_params.get("hub.verify_token", hub_verify_token)
    challenge = request.query_params.get("hub.challenge", hub_challenge)
    if mode == "subscribe" and token == _FB_WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    raise HTTPException(403, "Verification failed")


@app.post("/api/messenger/webhook")
async def messenger_webhook(request: Request):
    """Receive Messenger events from Meta."""
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if _FB_APP_SECRET and not _messenger.verify_signature(body, sig, _FB_APP_SECRET):
        raise HTTPException(403, "Bad signature")

    data = await request.json()
    if data.get("object") != "page":
        return {"ok": True}

    for entry in data.get("entry", []):
        page_id = str(entry.get("id", ""))
        page_user = await db.get_user_by_messenger_page(page_id)
        if not page_user:
            continue
        user_id = page_user["id"]
        page_token = page_user["page_access_token"]
        target_lang = await db.get_setting(user_id, "default_target_lang") or "yue"
        api_key = _SHARED_API_KEY  # webhook uses server key

        for event in entry.get("messaging", []):
            sender_psid = event.get("sender", {}).get("id", "")
            if sender_psid == page_id:
                continue  # echo of our own send
            msg = event.get("message", {})
            text = msg.get("text", "").strip()
            if not text:
                continue

            # Get sender's name
            profile = await _messenger.get_user_profile(page_token, sender_psid)
            sender_name = profile.get("name", sender_psid[:8])

            conv_id = await db.get_or_create_platform_conversation(
                user_id, "messenger", sender_psid
            )
            # Translate incoming message to user's target lang + nuance note
            tr = {"translated": text, "nuance_note": "", "reply_en": text}
            if api_key:
                tr = await translation.translate_message(text, "en", target_lang, api_key=api_key)
            translations = {target_lang: tr["translated"]}
            analysis: dict = {"reply_en": text}
            if tr.get("nuance_note"):
                analysis["nuance_note"] = tr["nuance_note"]
            await db.add_message(
                conv_id,
                sender_user_id=None,
                original_text=text,
                original_lang="en",
                translations=translations,
                sender_platform_id=sender_psid,
                sender_name=sender_name,
                analysis=analysis,
            )

    return {"ok": True}


@app.post("/api/conversations/{conv_id}/messenger-reply")
async def messenger_reply(conv_id: int, body: SendMessageBody,
                          user: dict = Depends(current_user)):
    """Send a reply to a Messenger conversation from the app."""
    convs = await db.list_conversations(user["id"])
    conv = next((c for c in convs if c["id"] == conv_id), None)
    if not conv or conv.get("type") != "messenger":
        raise HTTPException(400, "Not a Messenger conversation")

    account = await db.get_messenger_account(user["id"])
    if not account:
        raise HTTPException(400, "Messenger not connected")

    access = await _resolve_gemini(user, meter=True)
    api_key = access.api_key
    sender_lang = await db.get_setting(user["id"], "default_target_lang") or "yue"
    original_lang = "en" if body.mode == "native" else sender_lang

    # What user sees (their target lang)
    translations: dict[str, str] = {}
    if body.mode == "native":
        sender_display = await translation.translate_simple(
            body.text, "en", sender_lang, api_key=api_key
        )
        translations[sender_lang] = sender_display
    else:
        sender_display = body.text
        translations[sender_lang] = body.text

    # What gets sent to Messenger (translate back to English)
    if original_lang != "en":
        sent_text = await translation.translate_simple(
            body.text, original_lang, "en", api_key=api_key
        )
    else:
        sent_text = body.text

    psid = conv.get("platform_thread_id") or conv.get("name")
    await _messenger.send_message(account["page_access_token"], psid, sent_text)
    msg_id = await db.add_message(
        conv_id, user["id"], body.text, original_lang, translations,
        sent_text=sent_text,
    )
    return {"ok": True, "display_text": sender_display, "msg_id": msg_id}
