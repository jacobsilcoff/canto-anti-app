import os

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")
# Fixed encryption key so crypto doesn't try to persist a key file.
os.environ.setdefault("API_KEY_ENC_KEY", Fernet.generate_key().decode())

import auth
import crypto
import db
import main
import translation


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    admin_id = await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))
    return admin_id


# ── crypto ──────────────────────────────────────────────────────────────────

def test_crypto_roundtrip():
    token = crypto.encrypt("AIzaSecret123")
    assert token != "AIzaSecret123"  # ciphertext, not plaintext
    assert crypto.decrypt(token) == "AIzaSecret123"


def test_crypto_distinct_tokens():
    # Fernet embeds a random IV, so the same plaintext encrypts differently.
    assert crypto.encrypt("x") != crypto.encrypt("x")


# ── can_use_shared_key ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_can_use_shared_key(fresh_db):
    admin = await db.get_user(fresh_db)
    assert admin["can_use_shared_key"] == 1


@pytest.mark.asyncio
async def test_new_user_cannot_use_shared_key_by_default(fresh_db):
    uid = await db.create_user("friend", auth.hash_password("password123"))
    user = await db.get_user(uid)
    assert user["can_use_shared_key"] == 0


@pytest.mark.asyncio
async def test_set_user_shared_key(fresh_db):
    uid = await db.create_user("friend", auth.hash_password("password123"))
    await db.set_user_shared_key(uid, True)
    assert (await db.get_user(uid))["can_use_shared_key"] == 1
    await db.set_user_shared_key(uid, False)
    assert (await db.get_user(uid))["can_use_shared_key"] == 0


# ── resolver ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_uses_own_key_and_models(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    uid = await db.create_user("friend", auth.hash_password("password123"))
    await db.set_setting(uid, "gemini_api_key", crypto.encrypt("user-own-key"))
    await db.set_setting(uid, "model_translate", "gemini-2.5-pro")
    user = await db.get_user(uid)

    access = await main._resolve_gemini(user)
    assert access.api_key == "user-own-key"
    assert access.model_translate == "gemini-2.5-pro"
    # Unset model falls back to the default.
    assert access.model_reader == translation.DEFAULT_MODEL


@pytest.mark.asyncio
async def test_resolve_free_user_gets_shared_key_and_is_metered(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    uid = await db.create_user("friend", auth.hash_password("password123"))
    user = await db.get_user(uid)
    access = await main._resolve_gemini(user)
    assert access.api_key == "shared-key"
    # The call was metered against the free monthly allowance.
    assert await db.get_usage(uid) == 1


@pytest.mark.asyncio
async def test_resolve_blocks_free_user_over_quota(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    uid = await db.create_user("friend", auth.hash_password("password123"))
    user = await db.get_user(uid)
    # Exhaust the free allowance.
    for _ in range(main.PLAN_LIMITS["free"]):
        await db.increment_usage(uid)
    with pytest.raises(main.HTTPException) as exc:
        await main._resolve_gemini(user)
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_resolve_unmetered_does_not_consume_quota(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    uid = await db.create_user("friend", auth.hash_password("password123"))
    user = await db.get_user(uid)
    await main._resolve_gemini(user, meter=False)
    assert await db.get_usage(uid) == 0


@pytest.mark.asyncio
async def test_resolve_granted_friend_is_unlimited(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    uid = await db.create_user("friend", auth.hash_password("password123"))
    await db.set_user_shared_key(uid, True)
    user = await db.get_user(uid)
    for _ in range(main.PLAN_LIMITS["free"] + 5):
        await main._resolve_gemini(user)
    # Granted friends bypass the quota — nothing is metered.
    assert await db.get_usage(uid) == 0


@pytest.mark.asyncio
async def test_resolve_shared_key_uses_fixed_model(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    uid = await db.create_user("friend", auth.hash_password("password123"))
    await db.set_user_shared_key(uid, True)
    # Even if a model is stored, the shared key ignores it.
    await db.set_setting(uid, "model_translate", "gemini-2.5-pro")
    user = await db.get_user(uid)

    access = await main._resolve_gemini(user)
    assert access.api_key == "shared-key"
    assert access.model_translate == translation.DEFAULT_MODEL


@pytest.mark.asyncio
async def test_resolve_admin_uses_shared_key(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    admin = await db.get_user(fresh_db)
    access = await main._resolve_gemini(admin)
    assert access.api_key == "shared-key"


@pytest.mark.asyncio
async def test_resolve_admin_uses_own_models_on_shared_key(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    # Admin's personal model choices apply to their own calls on the shared key.
    await db.set_setting(fresh_db, "model_translate", "gemini-2.5-pro")
    await db.set_setting(fresh_db, "model_reader", "gemini-2.5-flash")
    admin = await db.get_user(fresh_db)

    access = await main._resolve_gemini(admin)
    assert access.api_key == "shared-key"
    assert access.model_translate == "gemini-2.5-pro"
    assert access.model_reader == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_resolve_friend_uses_admin_shared_models(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_SHARED_API_KEY", "shared-key")
    # Friends-on-shared-key models are configured by the admin, separate from
    # the admin's own model choices.
    await db.set_setting(fresh_db, "shared_model_translate", "gemini-2.5-flash")
    await db.set_setting(fresh_db, "shared_model_reader", "gemini-2.5-pro")
    uid = await db.create_user("friend", auth.hash_password("password123"))
    await db.set_user_shared_key(uid, True)
    user = await db.get_user(uid)

    access = await main._resolve_gemini(user)
    assert access.api_key == "shared-key"
    assert access.model_translate == "gemini-2.5-flash"
    assert access.model_reader == "gemini-2.5-pro"


# ── model validation ──────────────────────────────────────────────────────────

def test_valid_model_allowlist():
    assert main._valid_model("gemini-2.5-pro") == "gemini-2.5-pro"
    assert main._valid_model("evil-expensive-model") == translation.DEFAULT_MODEL
    assert main._valid_model(None) == translation.DEFAULT_MODEL
