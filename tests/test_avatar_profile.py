"""Integration tests for profile avatar upload/serve + greeting-name plumbing."""
import io
import os

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import AsyncClient, ASGITransport
from PIL import Image

os.environ.setdefault("DB_PATH", "/tmp/test_avatar_cards.db")
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
    main.MEDIA_DIR = tmp_path / "media"
    main.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    await db.init()


async def _login(ac, username):
    await db.create_user(username, auth.hash_password("password1"),
                         display_name="Firstname Lastname")
    res = await ac.post("/api/login", json={"username": username, "password": "password1"})
    assert res.status_code == 200, res.text


def _png_bytes(size=(1000, 500), color=(200, 30, 30)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="PNG")
    return b.getvalue()


@pytest_asyncio.fixture
async def client(fresh_db):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_me_returns_display_name(client):
    await _login(client, "alice")
    me = (await client.get("/api/me")).json()
    assert me["display_name"] == "Firstname Lastname"
    assert me["avatar_url"] is None


@pytest.mark.asyncio
async def test_avatar_upload_serve_and_delete(client):
    await _login(client, "bob")
    # upload a wide image -> square-cropped 400x400 jpg
    res = await client.post("/api/profile/avatar",
                            files={"file": ("pic.png", _png_bytes(), "image/png")})
    assert res.status_code == 200, res.text
    url = res.json()["avatar_url"]
    assert url.startswith("/api/media/") and url.endswith(".jpg")

    # /api/me + /api/profile now expose it
    assert (await client.get("/api/me")).json()["avatar_url"] == url
    assert (await client.get("/api/profile")).json()["avatar_url"] == url

    # served bytes are a 400x400 JPEG
    served = await client.get(url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(served.content))
    assert img.size == (400, 400)

    # uploading a second one replaces the file (old file removed)
    old_media = url.rsplit("/", 1)[1]
    res2 = await client.post("/api/profile/avatar",
                             files={"file": ("pic2.png", _png_bytes(color=(0, 0, 200)), "image/png")})
    new_url = res2.json()["avatar_url"]
    assert new_url != url
    assert not (main.MEDIA_DIR / old_media).exists()

    # delete clears the field and removes the file
    res3 = await client.delete("/api/profile/avatar")
    assert res3.status_code == 200
    assert (await client.get("/api/me")).json()["avatar_url"] is None
    assert not (main.MEDIA_DIR / new_url.rsplit("/", 1)[1]).exists()


@pytest.mark.asyncio
async def test_avatar_shows_in_conversation_list(client):
    await _login(client, "carol")
    carol = await db.get_user_by_username("carol")
    dave_id = await db.create_user("dave", auth.hash_password("password1"))
    # dave uploads via db directly
    await db.add_media_record("d" * 32, dave_id, None, 10)
    await db.update_user_profile(dave_id, avatar_media_id="d" * 32)
    await db.get_or_create_conversation(carol["id"], dave_id)

    convs = (await client.get("/api/conversations")).json()["conversations"]
    dave_conv = [c for c in convs if c.get("other_user_id") == dave_id][0]
    assert dave_conv["avatar_url"] == "/api/media/" + "d" * 32 + ".jpg"
