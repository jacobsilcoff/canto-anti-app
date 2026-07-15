"""Integration tests for profile avatar upload/serve + greeting-name plumbing."""
import io
import os

import pytest
import pytest_asyncio
import aiosqlite
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
    async with aiosqlite.connect(db.DB_PATH) as conn:
        old_id = old_media.removesuffix(".jpg")
        assert (await (await conn.execute(
            "SELECT COUNT(*) FROM media WHERE id=?", (old_id,)
        )).fetchone())[0] == 0

    # delete clears the field and removes the file
    res3 = await client.delete("/api/profile/avatar")
    assert res3.status_code == 200
    assert (await client.get("/api/me")).json()["avatar_url"] is None
    assert not (main.MEDIA_DIR / new_url.rsplit("/", 1)[1]).exists()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        assert (await (await conn.execute(
            "SELECT COUNT(*) FROM media"
        )).fetchone())[0] == 0


def test_image_normalizer_rejects_excessive_pixel_count(monkeypatch):
    monkeypatch.setattr(main, "_MAX_IMAGE_PIXELS", 99)
    with pytest.raises(ValueError, match="dimensions"):
        main._normalize_uploaded_image(_png_bytes(size=(10, 10)))


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


async def _with_avatar(username, media_id):
    uid = await db.create_user(username, auth.hash_password("password1"))
    await db.add_media_record(media_id, uid, None, 10)
    await db.update_user_profile(uid, avatar_media_id=media_id)
    return uid


@pytest.mark.asyncio
async def test_avatar_shows_in_friends_search_leaderboard_and_league(client):
    await _login(client, "erin")
    erin = await db.get_user_by_username("erin")
    frank_id = await _with_avatar("frank", "f" * 32)
    expected_url = "/api/media/" + "f" * 32 + ".jpg"

    # search surfaces frank's avatar
    search = (await client.get("/api/friends/search?q=frank")).json()["users"]
    assert search == [{"id": frank_id, "username": "frank", "avatar_url": expected_url}]

    # friend request + accept -> friends list carries the avatar both ways
    result = await db.send_friend_request(erin["id"], frank_id)
    assert result["ok"], result
    sent = (await db.get_friends(erin["id"]))["sent"]
    friendship_id = [f for f in sent if f["user_id"] == frank_id][0]["id"]
    await db.respond_friend_request(friendship_id, frank_id, accept=True)

    friends_resp = (await client.get("/api/friends")).json()
    frank_entry = [f for f in friends_resp["friends"] if f["user_id"] == frank_id][0]
    assert frank_entry["avatar_url"] == expected_url

    # leaderboard: both erin (no avatar) and frank (has one) render correctly
    lb = (await client.get("/api/friends/leaderboard")).json()["leaderboard"]
    lb_by_id = {e["user_id"]: e for e in lb}
    assert lb_by_id[frank_id]["avatar_url"] == expected_url
    assert lb_by_id[erin["id"]]["avatar_url"] is None

    # weekly league: same avatar plumbing
    league = (await client.get("/api/league")).json()["league"]
    league_by_id = {r["user_id"]: r for r in league}
    assert league_by_id[frank_id]["avatar_url"] == expected_url
    assert league_by_id[erin["id"]]["avatar_url"] is None
