"""AI tutor chat — conversational practice in the target language.

One LLM call per learner message (history serialized into a single prompt, same
plumbing as translation/learning — no SDK chat sessions). The tutor:
  • replies MOSTLY in the target language, calibrated to what the learner knows
    (their SRS deck + course concept registry are in the prompt);
  • responds to the learner's MEANING first, then corrects gently;
  • encourages circumlocution with known words, then teaches the proper
    expression through examples / related words / etymology;
  • awards small points when the learner correctly uses known material.

The reply is a structured JSON side-channel the UI renders as correction cards,
"Add to deck" chips, and point toasts. Same trust model as lessons — *liberal in
what you SHOW, strict in what you STORE*: romanization on new_items is always
recomputed by the offline oracle (never the model), arrays are clipped, point
values clamped, malformed entries dropped. A JSON-parse failure degrades to a
plain-text reply rather than an error bubble.
"""
import asyncio
import json

import tokenizer
from learning import _lang_preamble, _registry_block, _word_list_block
from translation import LANG_INFO, _call, _parse_json

TUTOR_MODEL = "gemini-2.5-flash"   # better conversational quality than -lite, still cheap

HISTORY_LIMIT = 20      # most recent messages serialized into the prompt
MAX_CORRECTIONS = 3
MAX_NEW_ITEMS = 5
MAX_POINT_ITEMS = 3     # ≤3 awards/message, 1–3 points each


def _history_block(history: list[dict]) -> str:
    if not history:
        return "This is the first message of the conversation.\n"
    lines = []
    for m in history[-HISTORY_LIMIT:]:
        who = "Learner" if m.get("role") == "user" else "Tutor"
        lines.append(f'{who}: {(m.get("text") or "").strip()}')
    return "── CONVERSATION SO FAR ──\n" + "\n".join(lines) + "\n"


def build_tutor_prompt(
    target_lang: str,
    user_msg: str,
    history: list[dict] | None = None,
    *,
    level: str = "A1",
    learner_profile: str = "",
    known_words: list[dict] | None = None,
    concept_registry: list[dict] | None = None,
    weak_concepts: list[dict] | None = None,
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]

    profile = f"── LEARNER BACKGROUND ──\n{learner_profile.strip()}\n\n" if learner_profile.strip() else ""
    deck = ""
    if known_words:
        deck = (f"── WORDS THE LEARNER KNOWS (their flashcard deck) ──\n"
                f"{_word_list_block(known_words)}\n\n")
    concepts = ""
    if concept_registry:
        concepts = f"── COURSE CONCEPTS TAUGHT ──\n{_registry_block(concept_registry)}\n\n"
    weak = ""
    if weak_concepts:
        weak = (f"── STRUGGLING WITH ──\n{_word_list_block(weak_concepts)}\n"
                f"Gently work these into the conversation when natural.\n\n")

    return (
        f"You are a warm, witty {name} conversation partner and tutor chatting with an "
        f"English-speaking learner (level {level}).\n\n"
        f"{_lang_preamble(info)}"
        f"── HOW TO TUTOR ──\n"
        f"• Write your `reply` ENTIRELY in {name}. No English in the reply itself — "
        f"English lives only in the structured fields below. Calibrate vocabulary and "
        f"grammar to the learner's level and the word lists below; if you must use a "
        f"word they're unlikely to know, keep it short and add it to `new_items`.\n"
        f"• Be a real conversation partner, NOT a quiz bot. Ask OPEN-ENDED questions "
        f"(almost never yes/no), react with genuine curiosity and personality, tell "
        f"tiny stories, drop the occasional fun cultural tidbit, and gently push the "
        f"learner to say MORE than they think they can. Keep changing the topic so it "
        f"never feels like a form to fill out.\n"
        f"• Respond to the learner's MEANING first — never stall. If they made mistakes "
        f"but got the idea across, run with it and put the fix in `corrections` (NOT in "
        f"the reply).\n"
        f"• If they grope for something they can't say, applaud the workaround, then "
        f"teach a natural way to say it via `new_items` (a short example, a related "
        f"word, or a memorable origin when it helps).\n"
        f"• `reply_en` = a faithful, natural English translation of your WHOLE reply "
        f"(the learner reveals it only when stuck). `gloss` = a word-by-word English "
        f"gloss of EVERY distinct {name} word in your reply (content AND function "
        f"words) so they can decode it piece by piece.\n"
        f"• Award points ONLY when the learner correctly USES a word/structure from the "
        f"lists — 1 for a word, 2–3 for a full structure or an impressive sentence. "
        f"Never for English, and never for a word you just handed them.\n"
        f"• Keep replies SHORT (1–3 sentences) so the conversation stays brisk.\n\n"
        f"{profile}{deck}{concepts}{weak}"
        f"{_history_block(history or [])}"
        f"Learner's new message: {user_msg.strip()}\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        f'  "reply": "<entirely in {name}>",\n'
        f'  "reply_en": "<natural English translation of the whole reply>",\n'
        f'  "gloss": {{"<{name} word>":"<English>", ...}},\n'
        '  "corrections": [{"quote":"<what the learner wrote>","corrected":"<natural version>","explanation":"<short English why>"}],\n'
        '  "new_items": [{"target_text":"<native word/phrase worth saving>","english":"<gloss>","notes":"<usage/etymology, optional>"}],\n'
        '  "points": [{"concept":"<the word/structure used>","points":1,"reason":"<short English>"}]\n'
        '}\n'
        'corrections/new_items/points may be empty arrays. Do NOT put a word in '
        '`new_items` that the learner already used or that appears in their known-words '
        'list — only genuinely new expressions you are teaching.'
    )


MAX_GLOSS = 40          # word-for-word gloss entries kept per reply


def _strip_for_match(s: str) -> str:
    """Loose key for 'did the learner already use this word': drop spaces and
    punctuation, casefold. Works for both CJK (no spaces) and spaced scripts."""
    return "".join(ch for ch in (s or "").casefold() if ch.isalnum())


def _normalize(parsed: dict, target_lang: str, raw: str,
               user_msg: str = "", known_texts: set[str] | None = None) -> dict:
    """Strict, deterministic clean-up of the model's structured reply.

    `user_msg` / `known_texts` are used to drop `new_items` the learner already
    used in this message or already has in their deck — those aren't new words."""
    has_rom = bool(LANG_INFO[target_lang].get("romanization"))

    def rom(s: str) -> str:
        return tokenizer.romanize_text(s, target_lang) if (has_rom and s) else ""

    reply = (parsed.get("reply") or "").strip() or raw.strip()
    reply_en = (parsed.get("reply_en") or "").strip()

    # Word-for-word gloss for the reply (English on tap). Native key → English.
    gloss = {}
    raw_gloss = parsed.get("gloss")
    if isinstance(raw_gloss, dict):
        for k, v in raw_gloss.items():
            k = (k or "").strip()
            v = (v or "").strip() if isinstance(v, str) else ""
            if k and v and k not in gloss:
                gloss[k] = v
            if len(gloss) >= MAX_GLOSS:
                break

    # Words the learner already used (this message) or already knows (deck) —
    # used to suppress redundant new_items.
    used = _strip_for_match(user_msg)
    known_keys = {_strip_for_match(t) for t in (known_texts or set())}
    known_keys.discard("")

    def _already_has(target: str) -> bool:
        key = _strip_for_match(target)
        if not key:
            return False
        if key in known_keys:
            return True
        # The learner "used" it if the (punctuation-stripped) word is in their message.
        return key in used

    # Filter first, clip after — malformed entries shouldn't consume slots.
    corrections = []
    for c in (parsed.get("corrections") or []):
        if len(corrections) >= MAX_CORRECTIONS:
            break
        if not isinstance(c, dict):
            continue
        corrected = (c.get("corrected") or "").strip()
        if not corrected:
            continue
        corrections.append({
            "quote":       (c.get("quote") or "").strip(),
            "corrected":   corrected,
            "corrected_roman": rom(corrected),
            "explanation": (c.get("explanation") or "").strip(),
        })

    new_items = []
    for it in (parsed.get("new_items") or []):
        if len(new_items) >= MAX_NEW_ITEMS:
            break
        if not isinstance(it, dict):
            continue
        target = (it.get("target_text") or "").strip()
        english = (it.get("english") or "").strip()
        if not target or not english:
            continue
        if _already_has(target):          # learner already used or knows it
            continue
        new_items.append({
            "target_text":  target,
            "english":      english,
            "romanization": rom(target),     # oracle, never the model
            "notes":        (it.get("notes") or "").strip(),
        })

    points = []
    for p in (parsed.get("points") or []):
        if len(points) >= MAX_POINT_ITEMS:
            break
        if not isinstance(p, dict):
            continue
        try:
            val = int(p.get("points") or 0)
        except (TypeError, ValueError):
            continue
        if val <= 0:
            continue
        points.append({
            "concept": (p.get("concept") or "").strip(),
            "points":  max(1, min(3, val)),
            "reason":  (p.get("reason") or "").strip(),
        })

    return {"reply": reply, "reply_en": reply_en, "gloss": gloss,
            "corrections": corrections, "new_items": new_items, "points": points}


async def respond(
    target_lang: str,
    user_msg: str,
    history: list[dict] | None = None,
    *,
    api_key: str,
    model: str = TUTOR_MODEL,
    level: str = "A1",
    learner_profile: str = "",
    known_words: list[dict] | None = None,
    concept_registry: list[dict] | None = None,
    weak_concepts: list[dict] | None = None,
) -> dict:
    """One LLM call → normalized structured reply. On JSON-parse failure, retry
    once; then degrade to a plain-text reply (the chat must never hard-fail on a
    malformed side-channel)."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = build_tutor_prompt(
        target_lang, user_msg, history,
        level=level, learner_profile=learner_profile,
        known_words=known_words, concept_registry=concept_registry,
        weak_concepts=weak_concepts,
    )
    raw = ""
    parsed = None
    for _ in range(2):                       # one retry on malformed JSON
        raw = await asyncio.to_thread(lambda: _call(prompt, api_key, model))
        try:
            parsed = _parse_json(raw)
            break
        except (ValueError, TypeError):
            parsed = None
    if not isinstance(parsed, dict):
        parsed = {}                          # graceful fallback: raw text as reply
    known_texts = {(w.get("target_text") or "").strip() for w in (known_words or [])}
    out = _normalize(parsed, target_lang, raw, user_msg=user_msg, known_texts=known_texts)
    out["_raw_prompt"] = prompt
    out["_raw_response"] = raw or ""
    return out
