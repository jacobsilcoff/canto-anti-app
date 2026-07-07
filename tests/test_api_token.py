"""Integration tests for the long-lived API token (Even glasses / MentraOS plugin auth)."""
import os

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("DB_PATH", "/tmp/test_api_token_cards.db")
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
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()


@pytest_asyncio.fixture
async def client(fresh_db):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        yield ac


async def _login(ac, username="alice"):
    await db.create_user(username, auth.hash_password("password1"))
    res = await ac.post("/api/login", json={"username": username, "password": "password1"})
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_generate_and_authenticate(client):
    await _login(client)
    # Profile reports no token yet.
    assert (await client.get("/api/profile")).json()["has_api_token"] is False

    token = (await client.post("/api/profile/api-token")).json()["token"]
    assert token
    assert (await client.get("/api/profile")).json()["has_api_token"] is True

    # A fresh client (no session cookie) can call the API with the bearer token.
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        # No auth -> 401.
        assert (await bare.get("/api/cards/due-count")).status_code == 401
        # Bearer token -> authenticated.
        res = await bare.get("/api/cards/due-count",
                             headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200, res.text
        assert "count" in res.json()


@pytest.mark.asyncio
async def test_regenerate_replaces_old_token(client):
    await _login(client)
    old = (await client.post("/api/profile/api-token")).json()["token"]
    new = (await client.post("/api/profile/api-token")).json()["token"]
    assert old != new

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        assert (await bare.get("/api/cards/due-count",
                               headers={"Authorization": f"Bearer {old}"})).status_code == 401
        assert (await bare.get("/api/cards/due-count",
                               headers={"Authorization": f"Bearer {new}"})).status_code == 200


@pytest.mark.asyncio
async def test_revoke(client):
    await _login(client)
    token = (await client.post("/api/profile/api-token")).json()["token"]
    assert (await client.delete("/api/profile/api-token")).status_code == 200
    assert (await client.get("/api/profile")).json()["has_api_token"] is False

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        assert (await bare.get("/api/cards/due-count",
                               headers={"Authorization": f"Bearer {token}"})).status_code == 401
