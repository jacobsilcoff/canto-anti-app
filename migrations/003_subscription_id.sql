-- Track the active Stripe subscription ID and pending-cancellation flag so
-- the app can call Stripe to cancel/resume without a live API lookup.
ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT;
ALTER TABLE users ADD COLUMN cancel_at_period_end INTEGER NOT NULL DEFAULT 0;
