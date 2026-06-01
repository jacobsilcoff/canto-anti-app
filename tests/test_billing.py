import os

import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


@pytest_asyncio.fixture
async def member(fresh_db):
    """A non-admin user on the default (free) plan."""
    return await db.create_user("alice", auth.hash_password("pw-12345678"), email="alice@example.com")


@pytest.mark.asyncio
async def test_new_user_defaults_to_free_plan(member):
    user = await db.get_user(member)
    assert user["plan"] == "free"
    assert user["stripe_customer_id"] is None


@pytest.mark.asyncio
async def test_usage_starts_at_zero(member):
    assert await db.get_usage(member) == 0


@pytest.mark.asyncio
async def test_increment_usage_accumulates(member):
    assert await db.increment_usage(member) == 1
    assert await db.increment_usage(member) == 2
    assert await db.get_usage(member) == 2


@pytest.mark.asyncio
async def test_usage_is_per_user(fresh_db, member):
    admin_id = fresh_db
    await db.increment_usage(member)
    await db.increment_usage(member)
    assert await db.get_usage(member) == 2
    assert await db.get_usage(admin_id) == 0


@pytest.mark.asyncio
async def test_set_stripe_customer_and_lookup(member):
    await db.set_stripe_customer(member, "cus_test_123")
    user = await db.get_user_by_stripe_customer("cus_test_123")
    assert user is not None
    assert user["id"] == member


@pytest.mark.asyncio
async def test_set_plan_by_customer(member):
    await db.set_stripe_customer(member, "cus_test_456")
    await db.set_plan_by_customer("cus_test_456", "pro", "active", "2026-07-01T00:00:00+00:00")
    user = await db.get_user(member)
    assert user["plan"] == "pro"
    assert user["subscription_status"] == "active"
    assert user["subscription_period_end"] == "2026-07-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_cancel_reverts_to_free(member):
    await db.set_stripe_customer(member, "cus_test_789")
    await db.set_plan_by_customer("cus_test_789", "pro", "active", None)
    await db.set_plan_by_customer("cus_test_789", "free", "canceled", None)
    user = await db.get_user(member)
    assert user["plan"] == "free"
    assert user["subscription_status"] == "canceled"
