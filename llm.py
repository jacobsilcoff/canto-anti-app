"""Provider-pluggable LLM dispatcher for the lesson pipeline.

One async entry point — `call(prompt, *, model, gemini_key, anthropic_key)` —
routes by model id: a `claude-*` model goes to the Anthropic SDK, everything
else falls through to the existing Gemini path (`translation._call`). Both return
raw text; callers parse JSON with `translation._parse_json`, so the rest of the
pipeline (planner/author) is provider-agnostic.

Claude runs on a server-side shared key (admin-billed) — see `_SHARED_ANTHROPIC_KEY`
in main.py. This is the A/B switch for comparing lesson quality against Gemini Pro
on the identical prompt + deterministic assembler.
"""
import asyncio

import translation

# Output cap. Lessons (teach blocks + ~10 drills) are the large case; the planner
# reply is tiny. 16k keeps a full author response from truncating while staying
# well under Claude's limits.
_MAX_TOKENS = 16384

_anthropic_clients: dict[str, object] = {}


def is_claude(model: str | None) -> bool:
    return (model or "").startswith("claude")


def _get_anthropic(api_key: str):
    """Return a cached Anthropic client for the given key (lazy import so the
    dependency is only needed when a Claude model is actually used)."""
    client = _anthropic_clients.get(api_key)
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        _anthropic_clients[api_key] = client
    return client


def _call_anthropic(prompt: str, api_key: str, model: str) -> str:
    msg = _get_anthropic(api_key).messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()


async def call(prompt: str, *, model: str, gemini_key: str,
               anthropic_key: str | None = None) -> str:
    """Run one completion on the provider implied by `model`, off the event loop.

    Raises RuntimeError if a Claude model is requested without an Anthropic key.
    """
    if is_claude(model):
        if not anthropic_key:
            raise RuntimeError(
                "A Claude model was selected but ANTHROPIC_API_KEY is not configured."
            )
        return await asyncio.to_thread(_call_anthropic, prompt, anthropic_key, model)
    return await asyncio.to_thread(translation._call, prompt, gemini_key, model)


async def call_with_image(prompt: str, image_bytes: bytes, *, model: str,
                          gemini_key: str,
                          anthropic_key: str | None = None) -> str:
    """Run a completion with one JPEG attachment.

    Textbook structure/planning always uses the Gemini reader model, so that is
    the current multimodal path. Keeping this beside ``call`` prevents textbook
    code from reaching into the provider module directly.
    """
    if is_claude(model):
        # No current caller selects Claude for source planning. Falling back to
        # text is safer than silently constructing a provider-specific payload.
        return await call(prompt, model=model, gemini_key=gemini_key,
                          anthropic_key=anthropic_key)
    return await asyncio.to_thread(
        translation._call_with_image, prompt, image_bytes, gemini_key, model)
