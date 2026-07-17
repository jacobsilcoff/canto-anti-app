"""Integration coverage for the Even Hub G2 Bearer-token bridge."""
import os
import time

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DB_PATH", "/tmp/test_even_hub_cards.db")
os.environ.setdefault("API_KEY_ENC_KEY", Fernet.generate_key().decode())

import auth
import db
import main


@pytest_asyncio.fixture(autouse=True)
async def disable_rate_limits():
    original = main.limiter.enabled
    main.limiter.enabled = False
    yield
    main.limiter.enabled = original
    main.limiter._storage.reset()


@pytest_asyncio.fixture
async def client(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    user_id = await db.create_user("glasses", auth.hash_password("password1"))
    session = "even-hub-test-session"
    await db.create_session(session, user_id, time.time() + 3600)
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        ac.cookies.set("session", session)
        yield ac


@pytest.mark.asyncio
async def test_generate_rotate_and_revoke_token(client):
    profile = (await client.get("/api/profile")).json()
    assert profile["has_even_hub_token"] is False

    first = (await client.post("/api/profile/even-hub-token")).json()["token"]
    second = (await client.post("/api/profile/even-hub-token")).json()["token"]
    assert first != second

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        old = await bare.get(
            "/api/cards/due-count", headers={"Authorization": f"Bearer {first}"}
        )
        current = await bare.get(
            "/api/cards/due-count", headers={"Authorization": f"Bearer {second}"}
        )
        assert old.status_code == 401
        assert current.status_code == 200

    assert (await client.delete("/api/profile/even-hub-token")).status_code == 200
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        revoked = await bare.get(
            "/api/cards/due-count", headers={"Authorization": f"Bearer {second}"}
        )
        assert revoked.status_code == 401


@pytest.mark.asyncio
async def test_even_hub_cors_preflight_bypasses_cookie_auth(client):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        res = await bare.options(
            "/api/cards/due",
            headers={
                "Origin": "https://hub.evenrealities.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "*"
    assert "Authorization" in res.headers["access-control-allow-headers"]
