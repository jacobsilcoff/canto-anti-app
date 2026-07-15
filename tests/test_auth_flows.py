"""Tests for self-service signup, email verification, and password reset flows."""
import os

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("DB_PATH", "/tmp/test_auth_cards.db")
os.environ.setdefault("API_KEY_ENC_KEY", Fernet.generate_key().decode())
os.environ.setdefault("RESEND_API_KEY", "test-key")
os.environ.setdefault("FROM_EMAIL", "noreply@test.example")
os.environ.setdefault("APP_URL", "http://localhost")

import auth
import db
import main


@pytest.mark.parametrize("stored", [None, "", "not-a-hash", "zz$11", "00$00"])
def test_verify_password_fails_closed_for_malformed_hash(stored):
    assert auth.verify_password("password", stored) is False


@pytest.mark.asyncio
async def test_messenger_webhook_verification_is_reachable_without_login(
    client, monkeypatch,
):
    monkeypatch.setattr(main, "_FB_WEBHOOK_VERIFY_TOKEN", "verify-me")
    res = await client.get(
        "/api/messenger/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-123",
        },
    )
    assert res.status_code == 200
    assert res.text == "challenge-123"


@pytest.mark.asyncio
async def test_messenger_webhook_requires_valid_signature_without_login(
    client, monkeypatch,
):
    import hashlib
    import hmac

    secret = "app-secret"
    monkeypatch.setattr(main, "_FB_APP_SECRET", secret)
    body = b'{"object":"ignored"}'

    unsigned = await client.post("/api/messenger/webhook", content=body)
    assert unsigned.status_code == 403

    signature = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256,
    ).hexdigest()
    signed = await client.post(
        "/api/messenger/webhook",
        content=body,
        headers={"X-Hub-Signature-256": signature},
    )
    assert signed.status_code == 200
    assert signed.json() == {"ok": True}


@pytest.mark.asyncio
async def test_comprehension_xp_claim_is_single_use(fresh_db):
    user = await db.get_user_by_username("admin")
    token = main._issue_comprehension_claim(user["id"], "yue")
    request = main.ComprehensionXpRequest(lang="yue", claim_token=token)

    result = await main.reader_comprehension_xp(request, user)
    assert result == {"xp": main._COMPREHENSION_XP}
    assert await db.get_points_total(user["id"], "yue") == main._COMPREHENSION_XP

    with pytest.raises(main.HTTPException) as exc:
        await main.reader_comprehension_xp(request, user)
    assert exc.value.status_code == 400


@pytest_asyncio.fixture(autouse=True)
async def disable_rate_limits():
    """Disable slowapi rate limiting for all auth flow tests."""
    original = main.limiter.enabled
    main.limiter.enabled = False
    yield
    main.limiter.enabled = original
    main.limiter._storage.reset()


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    await db.bootstrap_admin("admin", auth.hash_password("adminpass1"))


@pytest_asyncio.fixture
async def client(fresh_db):
    """ASGI test client with a fresh database."""
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _authenticate_test_client(client):
    """Attach a plain test session; production login cookies are Secure."""
    import time

    user = await db.get_user_by_username("admin")
    token = "test-ui-shell-session"
    await db.create_session(token, user["id"], time.time() + 3600)
    client.cookies.set("session", token)
    return user


@pytest.mark.asyncio
async def test_app_bootstrap_combines_shared_startup_data(client):
    await _authenticate_test_client(client)

    res = await client.get("/api/bootstrap")

    assert res.status_code == 200
    assert "no-store" in res.headers["cache-control"]
    payload = res.json()
    assert set(payload) == {
        "me", "settings", "languages", "streak", "due", "billing",
        "notifications",
    }
    assert payload["me"]["username"] == "admin"
    assert isinstance(payload["languages"], list)

    home = await client.get("/")
    assert home.status_code == 200
    assert home.text.count('/static/app-shell.js?v=') == 1


@pytest.mark.asyncio
async def test_shared_shell_is_loaded_once_across_primary_pages(client):
    await _authenticate_test_client(client)

    for path in ("/", "/cards", "/reader", "/tutor", "/messages", "/browse", "/settings"):
        res = await client.get(path)
        assert res.status_code == 200, path
        assert res.text.count('/static/app-shell.js?v=') == 1, path
        if path != "/tutor":
            assert res.text.count('class="tab shell-more-tab"') == 1, path

    home = await client.get("/")
    assert 'class="more-btn"' not in home.text


@pytest.mark.asyncio
async def test_cards_page_endpoint_returns_a_bounded_page(client):
    user = await _authenticate_test_client(client)
    for i in range(5):
        await db.create_card(
            user["id"], f"Page source {i}", f"頁面 {i}", f"jyut {i}",
            target_lang="yue",
        )

    res = await client.get(
        "/api/cards/page",
        params={"limit": 2, "search": "Page source", "lang": "yue"},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["total"] == 5
    assert len(payload["cards"]) == 2
    assert payload["has_more"] is True


@pytest.mark.asyncio
async def test_message_event_signal_is_published_to_connected_user():
    import asyncio

    queue = asyncio.Queue(maxsize=1)
    main._message_event_queues[42] = {queue}
    try:
        main._publish_message_event(42)
        event = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert event["type"] == "messages"
    finally:
        main._message_event_queues.pop(42, None)


# ── Turnstile CAPTCHA ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_turnstile_disabled_passes(monkeypatch):
    """No secret configured → verification is a no-op (dev/local signup works)."""
    monkeypatch.setattr(main, "_TURNSTILE_SECRET_KEY", "")
    assert await main._verify_turnstile("", None) is True
    assert await main._verify_turnstile("anything", "1.2.3.4") is True


@pytest.mark.asyncio
async def test_verify_turnstile_requires_token_when_enabled(monkeypatch):
    """Secret configured but no token → fail closed (no network call needed)."""
    monkeypatch.setattr(main, "_TURNSTILE_SECRET_KEY", "secret-xyz")
    assert await main._verify_turnstile("", "1.2.3.4") is False


@pytest.mark.asyncio
async def test_register_rejects_bad_captcha(client, monkeypatch):
    """When CAPTCHA is enabled, a registration missing a token is refused before
    any account is created."""
    monkeypatch.setattr(main, "_TURNSTILE_SECRET_KEY", "secret-xyz")
    res = await client.post("/api/register", json={
        "email": "bot@example.com",
        "username": "bot",
        "display_name": "Bot",
        "password": "securepass1",
    })
    assert res.status_code == 400
    assert "captcha" in res.json()["detail"].lower()
    assert await db.get_user_by_email("bot@example.com") is None


# ── Registration ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_success(client, monkeypatch):
    sent = []

    async def capture_send(to, token, app_name):
        sent.append((to, token))

    monkeypatch.setattr("email_utils.send_verification", capture_send)

    res = await client.post("/api/register", json={
        "email": "alice@example.com",
        "username": "alice",
        "display_name": "Alice Test",
        "password": "securepass1",
    })
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert len(sent) == 1
    assert sent[0][0] == "alice@example.com"


@pytest.mark.asyncio
async def test_register_duplicate_email(client, monkeypatch):
    async def noop(*a, **kw): pass
    monkeypatch.setattr("email_utils.send_verification", noop)

    payload = {
        "email": "bob@example.com",
        "username": "bob1",
        "display_name": "Bob",
        "password": "securepass1",
    }
    await client.post("/api/register", json=payload)
    res = await client.post("/api/register", json={**payload, "username": "bob2"})
    assert res.status_code == 409
    assert "email" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_register_duplicate_username(client, monkeypatch):
    async def noop(*a, **kw): pass
    monkeypatch.setattr("email_utils.send_verification", noop)

    payload = {
        "email": "carol@example.com",
        "username": "carol",
        "display_name": "Carol",
        "password": "securepass1",
    }
    await client.post("/api/register", json=payload)
    res = await client.post("/api/register", json={**payload, "email": "carol2@example.com"})
    assert res.status_code == 409
    assert "username" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_register_weak_password(client):
    res = await client.post("/api/register", json={
        "email": "dave@example.com",
        "username": "dave",
        "display_name": "Dave",
        "password": "short",
    })
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_register_invalid_username(client):
    res = await client.post("/api/register", json={
        "email": "eve@example.com",
        "username": "e",  # too short
        "display_name": "Eve",
        "password": "securepass1",
    })
    assert res.status_code == 400


# ── Email verification ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_email_flow(client, monkeypatch):
    token_holder = []

    async def capture_send(to, token, app_name):
        token_holder.append(token)

    monkeypatch.setattr("email_utils.send_verification", capture_send)

    await client.post("/api/register", json={
        "email": "frank@example.com",
        "username": "frank",
        "display_name": "Frank",
        "password": "securepass1",
    })

    # Login before verification should be blocked.
    res = await client.post("/api/login", json={"username": "frank@example.com", "password": "securepass1"})
    assert res.status_code == 403
    assert res.json()["code"] == "email_not_verified"

    # Verify via token.
    token = token_holder[0]
    res = await client.get(f"/verify-email?token={token}", follow_redirects=False)
    assert res.status_code == 302
    assert "verified=1" in res.headers["location"]

    # Now login should succeed.
    res = await client.post("/api/login", json={"username": "frank@example.com", "password": "securepass1"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_verify_invalid_token(client):
    res = await client.get("/verify-email?token=notavalidtoken", follow_redirects=False)
    assert res.status_code == 302
    assert "invalid_token" in res.headers["location"]


@pytest.mark.asyncio
async def test_verify_missing_token(client):
    res = await client.get("/verify-email", follow_redirects=False)
    assert res.status_code == 302
    assert "invalid_token" in res.headers["location"]


# ── Login with email or username ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_with_email(client):
    await db.create_user(
        "greta", auth.hash_password("password123"),
        email="greta@example.com", display_name="Greta",
        email_verified=True,
    )
    res = await client.post("/api/login", json={"username": "greta@example.com", "password": "password123"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_login_with_username(client):
    await db.create_user(
        "henry", auth.hash_password("password123"),
        email="henry@example.com", email_verified=True,
    )
    res = await client.post("/api/login", json={"username": "henry", "password": "password123"})
    assert res.status_code == 200


# ── Password reset ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_forgot_password_flow(client, monkeypatch):
    token_holder = []

    async def capture_reset(to, token, app_name):
        token_holder.append(token)

    monkeypatch.setattr("email_utils.send_password_reset", capture_reset)

    uid = await db.create_user(
        "irene", auth.hash_password("oldpass123"),
        email="irene@example.com", email_verified=True,
    )

    res = await client.post("/api/forgot-password", json={"email": "irene@example.com"})
    assert res.status_code == 200
    assert len(token_holder) == 1

    token = token_holder[0]

    # Reset the password.
    res = await client.post("/api/reset-password", json={"token": token, "password": "newpass456"})
    assert res.status_code == 200

    # Old password should no longer work.
    res = await client.post("/api/login", json={"username": "irene", "password": "oldpass123"})
    assert res.status_code == 401

    # New password should work.
    res = await client.post("/api/login", json={"username": "irene", "password": "newpass456"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_forgot_password_nonexistent_email_returns_200(client):
    """No email enumeration — unknown addresses always return 200."""
    res = await client.post("/api/forgot-password", json={"email": "nobody@example.com"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_reset_password_invalid_token(client):
    res = await client.post("/api/reset-password", json={"token": "badtoken", "password": "newpass456"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_expired_token(client, monkeypatch):
    import datetime

    token_holder = []

    async def capture_reset(to, token, app_name):
        token_holder.append(token)

    monkeypatch.setattr("email_utils.send_password_reset", capture_reset)

    uid = await db.create_user(
        "jake", auth.hash_password("oldpass123"),
        email="jake@example.com", email_verified=True,
    )

    await client.post("/api/forgot-password", json={"email": "jake@example.com"})
    token = token_holder[0]

    # Force-expire the token by setting expiry in the past.
    past = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=2)).isoformat()
    await db.set_reset_token(uid, token, past)

    res = await client.post("/api/reset-password", json={"token": token, "password": "newpass456"})
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reset_password_weak(client, monkeypatch):
    token_holder = []

    async def capture_reset(to, token, app_name):
        token_holder.append(token)

    monkeypatch.setattr("email_utils.send_password_reset", capture_reset)

    uid = await db.create_user(
        "kim", auth.hash_password("oldpass123"),
        email="kim@example.com", email_verified=True,
    )
    await client.post("/api/forgot-password", json={"email": "kim@example.com"})
    token = token_holder[0]

    res = await client.post("/api/reset-password", json={"token": token, "password": "short"})
    assert res.status_code == 400


# ── Resend verification ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resend_verification(client, monkeypatch):
    sent = []

    async def capture_send(to, token, app_name):
        sent.append(token)

    monkeypatch.setattr("email_utils.send_verification", capture_send)

    # Register (first send).
    await client.post("/api/register", json={
        "email": "leo@example.com",
        "username": "leo",
        "display_name": "Leo",
        "password": "securepass1",
    })
    assert len(sent) == 1

    # Resend — should send a second email with a new token.
    res = await client.post("/api/resend-verification", json={"email": "leo@example.com"})
    assert res.status_code == 200
    assert len(sent) == 2
    assert sent[0] != sent[1]


@pytest.mark.asyncio
async def test_resend_verification_unknown_email_silent(client):
    """Resend for unknown email must succeed silently (no enumeration)."""
    res = await client.post("/api/resend-verification", json={"email": "nobody@example.com"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_resend_does_not_send_if_already_verified(client, monkeypatch):
    sent = []

    async def capture_send(to, token, app_name):
        sent.append(token)

    monkeypatch.setattr("email_utils.send_verification", capture_send)

    await db.create_user(
        "mary", auth.hash_password("password123"),
        email="mary@example.com", email_verified=True,
    )

    res = await client.post("/api/resend-verification", json={"email": "mary@example.com"})
    assert res.status_code == 200
    # Already verified — no email should be sent.
    assert len(sent) == 0
