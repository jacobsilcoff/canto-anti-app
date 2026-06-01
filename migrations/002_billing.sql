-- Subscription billing + monthly AI usage metering.
-- Existing rows default to the free plan; admins and granted-friend accounts
-- bypass quotas in application code regardless of plan.

ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free';
ALTER TABLE users ADD COLUMN stripe_customer_id TEXT;
ALTER TABLE users ADD COLUMN subscription_status TEXT;
ALTER TABLE users ADD COLUMN subscription_period_end TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_stripe_customer
  ON users (stripe_customer_id) WHERE stripe_customer_id IS NOT NULL;

-- One row per user per calendar month (period = 'YYYY-MM'). A new month is a
-- new key starting at 0, so the quota resets automatically with no cron job.
CREATE TABLE IF NOT EXISTS usage_counters (
    user_id  INTEGER NOT NULL,
    period   TEXT NOT NULL,
    ai_calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, period)
);
