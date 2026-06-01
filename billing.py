"""Stripe wrapper: hosted Checkout, Customer Portal, and webhook verification.

We never handle card data — Checkout and the Portal are Stripe-hosted pages.
The app only stores the resulting customer id and subscription state.

The Stripe SDK is synchronous; callers run these in a worker thread
(asyncio.to_thread) so the event loop isn't blocked on network I/O.
"""

import os

import stripe

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_PRO = os.getenv("STRIPE_PRICE_PRO")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def is_configured() -> bool:
    """True when both an API key and a Pro price id are set."""
    return bool(STRIPE_SECRET_KEY and STRIPE_PRICE_PRO)


def create_checkout_session(
    *,
    customer_id: str | None,
    customer_email: str | None,
    client_reference_id: str,
    success_url: str,
    cancel_url: str,
):
    """Create a subscription Checkout session for the Pro plan.

    Passes the user id as client_reference_id so the webhook can map the
    resulting Stripe customer back to our user. Reuses an existing customer
    when we already have one, otherwise Stripe creates one from the email.
    """
    params: dict = {
        "mode": "subscription",
        "line_items": [{"price": STRIPE_PRICE_PRO, "quantity": 1}],
        "client_reference_id": client_reference_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "allow_promotion_codes": True,
    }
    if customer_id:
        params["customer"] = customer_id
    elif customer_email:
        params["customer_email"] = customer_email
    return stripe.checkout.Session.create(**params)


def get_active_subscription_id(customer_id: str) -> str | None:
    """Return the first active subscription ID for a customer, or None."""
    subs = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
    items = subs.get("data") if isinstance(subs, dict) else list(subs.auto_paging_iter())
    return items[0]["id"] if items else None


def cancel_subscription(sub_id: str):
    """Set cancel_at_period_end=True — access continues until period end."""
    return stripe.Subscription.modify(sub_id, cancel_at_period_end=True)


def resume_subscription(sub_id: str):
    """Clear cancel_at_period_end — subscription resumes as normal."""
    return stripe.Subscription.modify(sub_id, cancel_at_period_end=False)


def create_portal_session(*, customer_id: str, return_url: str):
    """Create a Customer Portal session for managing/canceling a subscription."""
    return stripe.billing_portal.Session.create(
        customer=customer_id, return_url=return_url
    )


def construct_event(payload: bytes, sig_header: str):
    """Verify a webhook signature against the raw body and return the event.

    Raises stripe.error.SignatureVerificationError (or ValueError) on failure.
    """
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
