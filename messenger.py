"""Facebook Messenger integration: send/receive messages via the Graph API."""
import hashlib
import hmac
import httpx

GRAPH = "https://graph.facebook.com/v19.0"


def verify_signature(payload: bytes, signature_header: str, app_secret: str) -> bool:
    """Verify X-Hub-Signature-256 from Meta webhook."""
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header[7:], expected)


async def send_message(page_access_token: str, recipient_psid: str, text: str) -> bool:
    """Send a text message to a Messenger user. Returns True on success."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{GRAPH}/me/messages",
            params={"access_token": page_access_token},
            json={
                "recipient": {"id": recipient_psid},
                "message": {"text": text},
                "messaging_type": "RESPONSE",
            },
        )
    return r.status_code == 200


async def get_user_profile(page_access_token: str, psid: str) -> dict:
    """Fetch first_name + last_name for a Messenger PSID."""
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(
            f"{GRAPH}/{psid}",
            params={"fields": "name", "access_token": page_access_token},
        )
    if r.status_code == 200:
        return r.json()
    return {}


async def exchange_code_for_token(
    app_id: str, app_secret: str, redirect_uri: str, code: str
) -> dict:
    """Exchange OAuth code for a short-lived user token."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
    return r.json()


async def get_managed_pages(user_token: str) -> list[dict]:
    """Return pages the user manages (for picking which page to connect)."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{GRAPH}/me/accounts",
            params={"access_token": user_token, "fields": "id,name,access_token"},
        )
    data = r.json()
    return data.get("data", [])


async def get_me(user_token: str) -> dict:
    """Fetch the authenticated user's basic profile {id, name}."""
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(
            f"{GRAPH}/me",
            params={"fields": "id,name", "access_token": user_token},
        )
    if r.status_code == 200:
        return r.json()
    return {}


async def get_app_friends(user_token: str) -> list[dict]:
    """Return the user's friends who ALSO use this app (the only friend data Meta
    exposes — requires the `user_friends` permission, granted via App Review or
    for app testers/devs). Each: {id, name}. Paginates through all results."""
    friends: list[dict] = []
    url = f"{GRAPH}/me/friends"
    params = {"access_token": user_token, "fields": "id,name", "limit": "200"}
    async with httpx.AsyncClient(timeout=10) as client:
        for _ in range(20):  # hard page cap, just in case
            r = await client.get(url, params=params)
            if r.status_code != 200:
                break
            data = r.json()
            friends.extend(data.get("data", []))
            nxt = (data.get("paging") or {}).get("next")
            if not nxt:
                break
            url, params = nxt, None  # `next` is a fully-formed URL
    return friends
