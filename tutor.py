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
import logging

import embeddings
import tokenizer
from learning import _lang_preamble, _registry_block, _word_list_block
from translation import LANG_INFO, _call, _call_stream, _parse_json

logger = logging.getLogger(__name__)

TUTOR_MODEL = "gemini-2.5-flash"   # better conversational quality than -lite, still cheap

# Sentinel separating the streamed prose answer from the trailing JSON extras in
# the card-ask streaming format (see build_card_ask_prompt(streaming=True)).
STREAM_META = "<<<META>>>"

HISTORY_LIMIT = 20      # most recent messages serialized into the prompt
MAX_CORRECTIONS = 3
MAX_NEW_ITEMS = 4       # a little headroom for multiple ways to say an asked-for phrase
MAX_POINT_ITEMS = 3     # ≤3 awards/message, 1–3 points each
LESSON_DRILL_TURNS = 4  # how many phrases an inline lesson construction-drill poses

# Construction-drill vocab snapping (embedding-anchored, verify-fail fallback only).
SNAP_THRESHOLD = 0.62   # cosine ≥ this ⇒ a known word can stand in for a missed word
MAX_PALETTE = 12        # known substitutes handed to a regenerated drill
# Deck-size strategy: at/under SMALL_DECK_MAX we just hand the model the whole
# known-words list (cheap, no embeddings); above it we embedding-snap a relevant
# subset and pass only a small sample + the total count (so a 2000-word deck never
# floods the prompt). LARGE_DECK_VECTOR_CAP bounds how many words we vectorise.
SMALL_DECK_MAX = 150
LARGE_DECK_VECTOR_CAP = 1500
LARGE_DECK_SAMPLE = 40


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
    drill_skill: str = "",
    practiced: list[str] | None = None,
) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])

    # Drill bullets: in a manual drill sub-session (`drill_skill` set) the learner
    # ends the drill themselves, so the tutor must KEEP posing phrases and never
    # wrap up on its own. Otherwise the normal "offer a Drill button" behavior.
    if drill_skill:
        drill_section = (
            f"• YOU ARE MID-DRILL on the construction \"{drill_skill}\". Your previous "
            f"message posed an English phrase; the learner's new message is their attempt. "
            f"JUDGE it via `corrections` (confirm the natural {name} version + name the "
            f"construction in `construction`), give a SHORT encouragement, then pose the "
            f"NEXT concrete English phrase exercising the SAME construction, built from "
            f"words the learner already KNOWS (vary the vocabulary each turn). Do NOT wrap "
            f"up, recap, or leave the drill on your own — the learner ends it themselves. "
            f"Keep `drill` empty. The English phrase-to-translate is the ONE exception to "
            f"the no-English-in-reply rule.\n"
        )
    else:
        drill_section = (
            f"• Set `drill` to a SHORT construction/skill label ONLY when THIS turn surfaced "
            f"a GENERALIZABLE construction worth practicing — either you just taught one, or "
            f"a correction above exposed one the learner could drill (use the SAME label as "
            f"that correction's `construction`). Leave it \"\" for ordinary chat or a one-off "
            f"vocab word. This unlocks a 'Drill' button.\n"
        )

    practiced_block = ""
    if practiced:
        practiced_block = (
            f"── ALREADY PRACTICED (in drills this chat) ──\n{', '.join(practiced)}\n"
            f"The learner has drilled these constructions; weave them in naturally and "
            f"don't re-offer a drill for them.\n\n"
        )

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
        f"• ENGLISH = A REQUEST FOR WORDS. Whenever the learner drops into English for "
        f"something they can't yet say in {name} — a whole sentence, a single word "
        f"they slipped in, or an explicit 'how do you say …?' — you MUST hand them the "
        f"natural {name} way to say it. Offer 1–3 idiomatic options when they genuinely "
        f"differ (register, nuance, formality), and put EACH option in `new_items` with "
        f"a one-line note on when to use it. These asked-for/groped-for translations are "
        f"REQUIRED and are EXEMPT from the selectivity limit below. Acknowledge their "
        f"meaning and model the {name} expression in your reply.\n"
        f"• OTHER `new_items` (things you volunteer, not asked for) = be VERY selective: "
        f"at most 2, empty is normal. Only high-frequency, immediately reusable words or "
        f"short set phrases CENTRAL to this exchange. Never proper nouns, niche/literary "
        f"words, full sentences, or words included just because they appeared.\n"
        f"• CORRECT THE CONSTRUCTION, NOT JUST THE WORDS. When you correct a grammar "
        f"slip, identify the underlying CONSTRUCTION / form at play (e.g. 'comparative "
        f"with 過', 'possessive with de', '-er present tense', 'if…then…') and name it "
        f"in the correction's `construction` field. Make `explanation` about the RULE "
        f"(how the form works in general), not just this one instance, so the fix "
        f"generalizes. Leave `construction` \"\" for pure vocab/spelling slips.\n"
        f"{drill_section}"
        f"• `reply_en` = a faithful, natural English translation of your WHOLE reply "
        f"(the learner reveals it only when stuck). `gloss` = a word-by-word English "
        f"gloss of EVERY distinct {name} word in your reply (content AND function "
        f"words) so they can decode it piece by piece.\n"
        f"• Award points ONLY when the learner correctly USES a word/structure from the "
        f"lists — 1 for a word, 2–3 for a full structure or an impressive sentence. "
        f"Never for English, and never for a word you just handed them.\n"
        f"• Keep replies SHORT (1–3 sentences) so the conversation stays brisk.\n\n"
        f"{profile}{deck}{concepts}{weak}{practiced_block}"
        f"{_history_block(history or [])}"
        f"Learner's new message: {user_msg.strip()}\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        f'  "reply": "<entirely in {name}>",\n'
        f'  "reply_en": "<natural English translation of the whole reply>",\n'
        f'  "gloss": {{"<{name} word>":"<English>", ...}},\n'
        '  "corrections": [{"quote":"<what the learner wrote>","corrected":"<natural version>","construction":"<the form/construction, e.g. comparative with 過; empty for pure vocab slips>","explanation":"<short English: the RULE, not just this case>"}],\n'
        '  "new_items": [{"target_text":"<native word/phrase worth saving>","english":"<gloss>","notes":"<usage/etymology, optional>"}],\n'
        '  "points": [{"concept":"<the word/structure used>","points":1,"reason":"<short English>"}],\n'
        '  "drill": "<short construction/skill label if a generalizable pattern surfaced, else empty>"\n'
        '}\n'
        'corrections/new_items/points may be empty arrays. EXCEPTION: if the learner '
        'asked how to say something or used English for a word they lack, `new_items` '
        f'MUST contain the {name} rendering(s). Otherwise do NOT put a word in `new_items` '
        'that the learner already used or that appears in their known-words list — only '
        'genuinely new expressions you are teaching.'
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
            "construction": (c.get("construction") or "").strip()[:60],
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

    drill = (parsed.get("drill") or "").strip()[:60] if isinstance(parsed.get("drill"), str) else ""

    card_updates = None
    raw_cu = parsed.get("card_updates")
    if isinstance(raw_cu, dict):
        cu: dict[str, str] = {}
        for field in ("notes_append", "notes", "romanization", "source_text"):
            val = (raw_cu.get(field) or "").strip()
            if val:
                cu[field] = val[:600]
        # `notes_append` (additive) wins over `notes` (replace) if the model sent
        # both — never wipe the learner's existing notes when an append was offered.
        if "notes_append" in cu:
            cu.pop("notes", None)
        if cu:
            card_updates = cu

    return {"reply": reply, "reply_en": reply_en, "gloss": gloss,
            "corrections": corrections, "new_items": new_items, "points": points,
            "drill": drill, "card_updates": card_updates}


async def _run(prompt: str, target_lang: str, *, api_key: str, model: str,
               user_msg: str = "", known_texts: set[str] | None = None,
               thinking_budget: int | None = None) -> dict:
    """Call the model, parse JSON (one retry), normalize. Never hard-fails: a
    malformed side-channel degrades to a plain-text reply. Pass `thinking_budget=0`
    to disable the model's thinking pass (faster, for low-reasoning Q&A)."""
    raw = ""
    parsed = None
    for _ in range(2):                       # one retry on malformed JSON
        raw = await asyncio.to_thread(
            lambda: _call(prompt, api_key, model, thinking_budget=thinking_budget))
        try:
            parsed = _parse_json(raw)
            break
        except (ValueError, TypeError):
            parsed = None
    if not isinstance(parsed, dict):
        parsed = {}                          # graceful fallback: raw text as reply
    out = _normalize(parsed, target_lang, raw, user_msg=user_msg, known_texts=known_texts)
    out["_raw_prompt"] = prompt
    out["_raw_response"] = raw or ""
    return out


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
    drill_skill: str = "",
    practiced: list[str] | None = None,
) -> dict:
    """One LLM call → normalized structured reply. Pass `drill_skill` to run a
    drill-continuation turn (judge + pose the next phrase, no auto-end)."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = build_tutor_prompt(
        target_lang, user_msg, history,
        level=level, learner_profile=learner_profile,
        known_words=known_words, concept_registry=concept_registry,
        weak_concepts=weak_concepts, drill_skill=drill_skill, practiced=practiced,
    )
    known_texts = {(w.get("target_text") or "").strip() for w in (known_words or [])}
    return await _run(prompt, target_lang, api_key=api_key, model=model,
                      user_msg=user_msg, known_texts=known_texts)


# ── Contextual "ask about this card" (ephemeral study Q&A) ─────────────────────
# A focused, English-allowed Q&A about the specific flashcard the learner is
# looking at. Unlike `respond` (conversation practice, reply must be in-target),
# this is study help: the tutor MAY explain in English but writes every example
# in the target script. No stored conversation — history (if any) lives client-side.

def _card_block(card: dict | None, name: str) -> str:
    if not card:
        return ""
    tgt = (card.get("target_text") or "").strip()
    src = (card.get("source_text") or "").strip()
    rom = (card.get("romanization") or "").strip()
    notes = (card.get("notes") or "").strip()
    status = (card.get("status") or "").strip()
    line = tgt or "(blank)"
    if rom:
        line += f" [{rom}]"
    if src:
        line += f" — “{src}”"
    parts = [f"word/phrase: {line}"]
    if notes:
        parts.append(f"learner's saved notes: {notes}")
    if status:
        parts.append(f"the learner's progress: {status}")
    return ("── THE FLASHCARD THE LEARNER IS STUDYING ──\n"
            + "\n".join(parts) + "\n\n")


def build_card_ask_prompt(
    target_lang: str,
    question: str,
    card: dict | None,
    history: list[dict] | None = None,
    *,
    level: str = "A1",
    learner_profile: str = "",
    known_words: list[dict] | None = None,
    streaming: bool = False,
) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])

    profile = f"── LEARNER BACKGROUND ──\n{learner_profile.strip()}\n\n" if learner_profile.strip() else ""
    deck = ""
    if known_words:
        deck = (f"── WORDS THE LEARNER ALREADY KNOWS (their deck) ──\n"
                f"{_word_list_block(known_words)}\n"
                f"Prefer these in your example sentences; don't re-teach them as new_items.\n\n")

    base = (
        f"You are a warm, expert {name} tutor helping an English-speaking learner "
        f"(level {level}) who is reviewing a flashcard and has a question about it.\n\n"
        f"{_lang_preamble(info)}"
        f"── HOW TO ANSWER ──\n"
        f"• ANSWER THE QUESTION the learner actually asked — directly, clearly, concisely. "
        f"This is study help, NOT conversation practice — the learner is decoding a card and "
        f"needs to UNDERSTAND, so do NOT reply in {name}. Write your EXPLANATION in the SAME "
        f"language the learner asked their question in (English by default; if they wrote their "
        f"question in another language, answer in that language). The ONLY {name} that appears "
        f"in your reply is the actual {name} words and example sentences, each followed by its "
        f"romanization and a short gloss in parentheses.\n"
        f"• Be concrete and useful: explain the underlying RULE or pattern (not just this one "
        f"instance), give 1–2 SHORT example sentences in {name}, and mention a closely related "
        f"word, contrast, or memory/etymology hook when it genuinely helps.\n"
        f"• Calibrate to the learner's level and known words above; build examples from words "
        f"they already know where possible.\n"
        f"• If you mention {name} words or phrases worth saving to their deck, list them in "
        f"`new_items` (at most 4; skip anything already in their deck/known list). Empty is fine.\n"
        f"• Keep it focused — a few sentences, not an essay.\n\n"
        f"── SUGGESTING CARD UPDATES ──\n"
        f"You can suggest changes to THIS flashcard via `card_updates`. Be PROACTIVE — if your "
        f"explanation would help the learner remember, offer to save it to the card. Notes are "
        f"ADDITIVE by default — prefer `notes_append` (a short snippet ADDED to the existing "
        f"notes) over `notes` (which REPLACES them entirely). Specifically:\n"
        f"• When the learner asks about the card, asks to break it down, asks why something "
        f"works a certain way, or asks for a mnemonic → suggest a `notes_append` snippet that "
        f"captures the key insight, so it's added to whatever notes are already there.\n"
        f"• When the learner asks to update the card or add a note → ALWAYS include card_updates "
        f"(use `notes_append` unless they explicitly want to rewrite/replace the note).\n"
        f"• Use `notes` (full replacement) ONLY when the existing notes contain an actual ERROR "
        f"worth removing, or the learner explicitly asks to rewrite them. Otherwise NEVER send "
        f"`notes` — append instead, so you don't wipe what's already there.\n"
        f"• When you spot wrong/missing romanization → suggest `romanization` fix.\n"
        f"• When the translation is wrong or misleading → suggest `source_text` fix.\n"
        f"Keep the note snippet concise (a helpful reminder, not an essay). If the card already "
        f"covers everything and the learner isn't asking about anything note-worthy, omit "
        f"card_updates (null).\n\n"
        f"{profile}{deck}{_card_block(card, name)}"
        f"{_history_block(history or [])}"
        f"Learner's question: {question.strip()}\n\n"
    )

    extras_note = (
        'new_items may be an empty array. Do NOT put a word in new_items that already appears in '
        'the learner\'s known-words list. card_updates: include only the fields you want to change. '
        'Prefer `notes_append` over `notes`; send at most ONE of them (never both). Omit '
        'card_updates entirely (or {}) when there is genuinely nothing useful to add to the card.'
    )

    if streaming:
        # Streamed format: the prose answer comes FIRST (forwarded to the learner
        # token-by-token), THEN a sentinel, THEN a single JSON object with the
        # structured extras only (parsed server-side after the stream finishes).
        return base + (
            f"OUTPUT FORMAT — follow it EXACTLY, in this order:\n"
            f"1. Your answer to the learner as plain prose ONLY (no JSON, no markdown headings, "
            f"no field labels). English explanation, {name} examples in {name} script with "
            f"romanization + gloss in parentheses. This is the ONLY part the learner reads.\n"
            f"2. Then the marker on its own line — exactly: {STREAM_META}\n"
            f"3. Then ONE JSON object and nothing after it (use {STREAM_META} followed by {{}} if "
            f"there are no extras):\n"
            '   {"new_items": [{"target_text":"<native>","english":"<gloss>","notes":"<usage, optional>"}],'
            ' "card_updates": {"notes_append":"<short note to ADD>","notes":"<replacement, ONLY to fix an error>",'
            '"romanization":"<fixed>","source_text":"<fixed translation>"}}\n'
            f"CRITICAL: never write the words \"new_items\" or \"card_updates\" in the prose part — "
            f"they appear ONLY inside the JSON after {STREAM_META}. Do not label or announce the "
            f"JSON; just output the marker then the object. {extras_note}"
        )

    return base + (
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        f'  "reply": "<your answer; English explanation is fine, {name} examples in {name} script>",\n'
        '  "new_items": [{"target_text":"<native word/phrase worth saving>","english":"<gloss>","notes":"<usage, optional>"}],\n'
        '  "card_updates": {"notes_append":"<short note to ADD to existing notes>","notes":"<full replacement, ONLY to fix an error>","romanization":"<fixed romanization>","source_text":"<fixed translation>"}\n'
        '}\n'
        + extras_note
    )


async def ask_about_card(
    target_lang: str,
    question: str,
    card: dict | None = None,
    history: list[dict] | None = None,
    *,
    api_key: str,
    model: str = TUTOR_MODEL,
    level: str = "A1",
    learner_profile: str = "",
    known_words: list[dict] | None = None,
) -> dict:
    """One LLM call → focused answer about a specific card. Ephemeral (no storage).
    Returns the same normalized shape as `respond`; corrections/points/drill are
    naturally empty here (the prompt only asks for reply + new_items)."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = build_card_ask_prompt(
        target_lang, question, card, history,
        level=level, learner_profile=learner_profile, known_words=known_words,
    )
    known_texts = {(w.get("target_text") or "").strip() for w in (known_words or [])}
    # Study Q&A is factual/low-reasoning — disable the thinking pass so the
    # pop-over answers in ~1–2s instead of stalling for several seconds.
    return await _run(prompt, target_lang, api_key=api_key, model=model,
                      user_msg=question, known_texts=known_texts, thinking_budget=0)


def _extract_card_ask_extras(text: str) -> dict:
    """Tolerantly pull `new_items` / `card_updates` out of the streamed tail,
    whatever shape the model used: the `<<<META>>>` + JSON contract, a bare JSON
    object, or labelled `new_items: [...]` / `card_updates: {...}` blocks (the
    common drift on weak models). Returns {} on anything unparseable."""
    if not text:
        return {}
    t = text.replace(STREAM_META, " ").strip()
    if not t:
        return {}
    # Best case: a single JSON object carrying the keys.
    try:
        parsed = _parse_json(t)
        if isinstance(parsed, dict) and ("new_items" in parsed or "card_updates" in parsed):
            return parsed
    except (ValueError, TypeError):
        pass
    # Fallback: labelled blocks. Strip markdown emphasis/backticks first.
    import re
    cleaned = t.replace("*", "").replace("`", "")
    out: dict = {}
    m = re.search(r"new_items\s*[:=]?\s*(\[.*?\])", cleaned, re.S)
    if m:
        try:
            out["new_items"] = json.loads(m.group(1))
        except (ValueError, TypeError):
            pass
    m = re.search(r"card_updates\s*[:=]?\s*(\{.*\})", cleaned, re.S)
    if m:
        try:
            out["card_updates"] = json.loads(m.group(1))
        except (ValueError, TypeError):
            pass
    return out


async def stream_card_ask(
    target_lang: str,
    question: str,
    card: dict | None = None,
    history: list[dict] | None = None,
    *,
    api_key: str,
    model: str = TUTOR_MODEL,
    level: str = "A1",
    learner_profile: str = "",
    known_words: list[dict] | None = None,
):
    """Streaming variant of `ask_about_card`. Yields NDJSON event strings (each
    newline-terminated): `{"type":"delta","text":...}` for prose as it's generated,
    then one `{"type":"final","reply":...,"new_items":[...],"card_updates":{...}}`
    with the cleaned reply + parsed/normalized extras.

    The model is told to emit the prose answer first, then `STREAM_META`, then the
    JSON extras. Weak models don't always emit the marker — they may instead print
    the structured part as visible `new_items:` / `card_updates:` labels — so the
    boundary detector ALSO stops at those bare label tokens, and the extractor
    (`_extract_card_ask_extras`) tolerates marker / labelled / raw-JSON shapes.
    The `final` event carries the cleaned `reply` so the client can replace any
    stray label text that slipped through before the boundary was recognised."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = build_card_ask_prompt(
        target_lang, question, card, history,
        level=level, learner_profile=learner_profile, known_words=known_words,
        streaming=True,
    )
    known_texts = {(w.get("target_text") or "").strip() for w in (known_words or [])}

    def _delta(text: str) -> str:
        return json.dumps({"type": "delta", "text": text}, ensure_ascii=False) + "\n"

    # Tokens that mark the start of the structured tail. `new_items`/`card_updates`
    # are extremely unlikely in a genuine prose answer about a flashcard, so we
    # treat the earliest of them (or the real marker) as the prose/meta boundary.
    stop_tokens = (STREAM_META, "new_items", "card_updates")

    def _earliest_stop(s: str) -> int:
        best = -1
        for tok in stop_tokens:
            i = s.find(tok)
            if i != -1 and (best == -1 or i < best):
                best = i
        return best

    mode = None            # None=undecided, "prose", "json"
    pending = ""           # un-emitted prose tail (may hold a partial stop token)
    full_reply = ""
    meta_buf = ""          # everything from the boundary onward (the extras)
    json_buf = ""          # whole-output buffer in the JSON fallback path
    in_meta = False
    hold = max(len(t) for t in stop_tokens) - 1

    stream_err = None
    try:
        async for chunk in _call_stream(prompt, api_key, model, thinking_budget=0):
            if mode == "json":
                json_buf += chunk
                continue
            if in_meta:
                meta_buf += chunk
                continue
            pending += chunk
            if mode is None:                       # decide prose vs JSON-fallback
                stripped = pending.lstrip()
                if not stripped:
                    continue
                if stripped[0] == "{":
                    mode = "json"
                    json_buf = pending
                    pending = ""
                    continue
                mode = "prose"
            # prose: stop at the earliest boundary token; emit everything ahead of it
            i = _earliest_stop(pending)
            if i != -1:
                emit = pending[:i]
                if emit:
                    full_reply += emit
                    yield _delta(emit)
                meta_buf = pending[i:]             # keep the token; the extractor strips it
                in_meta = True
                pending = ""
                continue
            if len(pending) > hold:                # hold back chars that could start a token
                cut = len(pending) - hold
                emit, pending = pending[:cut], pending[cut:]
                if emit:
                    full_reply += emit
                    yield _delta(emit)
    except Exception as e:
        # A mid/late stream failure must NOT discard an answer the learner already
        # saw — capture it and finalize with whatever prose we have. Only re-raise
        # (→ route emits an error event) if nothing usable streamed.
        stream_err = e
        logger.warning("card-ask stream interrupted: %s", e)

    # Stream ended — flush remaining prose (no boundary ever arrived).
    if mode != "json" and not in_meta and pending:
        full_reply += pending
        yield _delta(pending)

    if stream_err is not None and not full_reply.strip() and not (mode == "json" and json_buf.strip()):
        raise stream_err                            # genuine failure, no content

    # Parse the structured extras and normalize via the shared oracle path.
    extras = {}
    try:
        if mode == "json":
            parsed = None
            try:
                parsed = _parse_json(json_buf)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                reply = (parsed.get("reply") or "").strip()
                if reply:
                    full_reply = reply
                    yield _delta(reply)
                extras = parsed
        else:
            extras = _extract_card_ask_extras(meta_buf)
    except Exception as e:
        logger.warning("card-ask extras parse failed: %s", e)
        extras = {}

    full_reply = full_reply.strip()
    try:
        norm = _normalize(
            {"reply": full_reply, "new_items": extras.get("new_items"),
             "card_updates": extras.get("card_updates")},
            target_lang, raw=full_reply, user_msg=question, known_texts=known_texts,
        )
        final = {"type": "final", "reply": norm["reply"],
                 "new_items": norm["new_items"], "card_updates": norm["card_updates"]}
    except Exception as e:                           # never fail the answer over extras
        logger.warning("card-ask normalize failed: %s", e)
        final = {"type": "final", "reply": full_reply, "new_items": [], "card_updates": None}
    yield json.dumps(final, ensure_ascii=False) + "\n"


def build_drill_prompt(
    target_lang: str,
    skill: str,
    history: list[dict] | None = None,
    *,
    level: str = "A1",
    learner_profile: str = "",
    known_words: list[dict] | None = None,
    large: bool = False,
    palette: list[str] | None = None,
    avoid: list[str] | None = None,
    deck_count: int = 0,
    cefr_stats: str = "",
) -> str:
    """Kick off a focused practice drill on one generalizable skill — the tutor
    poses ONE English phrase to translate. Separate prompt so the learner never
    sees a 'drill me' instruction in the chat (the button calls this directly).

    Tiered by deck size: SMALL decks (`large=False`) pass `known_words` wholesale.
    LARGE decks (`large=True`) pass only a `known_words` SAMPLE + a CEFR profile +
    `deck_count` (never 1000s of words), and ask for `expected_words` so the caller
    can verify the answer uses familiar vocab. `palette` (known substitutes) and
    `avoid` (words the learner doesn't know) are only set on a verify-fail REGEN."""
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    bg = f"── LEARNER BACKGROUND ──\n{learner_profile.strip()}\n\n" if learner_profile.strip() else ""
    if not known_words:
        deck = ""
    elif large:
        prof = (f"The learner knows ~{deck_count} words"
                + (f" (CEFR spread — {cefr_stats})" if cefr_stats else "") + ". ")
        deck = (f"── THE LEARNER'S VOCABULARY ──\n{prof}Here is a small sample of words "
                f"they know (NOT the full list):\n{_word_list_block(known_words)}\n\n")
    else:
        deck = f"── WORDS THE LEARNER KNOWS ──\n{_word_list_block(known_words)}\n\n"
    palette_block = ""
    if palette:
        palette_block = (
            f"── PREFER THESE KNOWN WORDS ──\n"
            f"These fit the construction and the learner knows them — build the drill "
            f"phrase from them so the only new thing is the construction:\n"
            f"{', '.join(palette)}\n\n"
        )
    avoid_block = ""
    if avoid:
        avoid_block = (
            f"── AVOID THESE WORDS ──\n"
            f"The learner does NOT know these — don't require them. Use a known word "
            f"instead, or if the construction truly needs a new word, teach it in "
            f"`new_items` (with its English):\n{', '.join(avoid)}\n\n"
        )
    expected_line = (
        f'  "expected_words": ["<the {name} content words a correct answer to your phrase '
        f'should contain>"],\n' if large else ""
    )
    return (
        f"You are a {name} tutor running a quick, encouraging PRACTICE DRILL with an "
        f"English-speaking learner (level {level}).\n\n"
        f"{_lang_preamble(info)}"
        f"The learner just tapped 'Drill' to practice this CONSTRUCTION/form: "
        f"\"{skill}\". This is the first of 3–4 short examples you'll walk them "
        f"through, all exercising the SAME construction with different vocabulary.\n\n"
        f"── HOW TO START ──\n"
        f"• In ONE short message: a friendly one-line lead-in IN {name}, then pose "
        f"EXACTLY ONE concrete English phrase for the learner to translate into {name} "
        f"that exercises \"{skill}\". Do NOT translate it for them.\n"
        f"• Build the phrase from words the learner already KNOWS (prefer any listed "
        f"palette/sample) so the ONLY new thing they practice is the construction. If it "
        f"needs a word they lack, teach ONE simple word via `new_items`.\n"
        f"• Keep it level-appropriate. The English phrase-to-translate is the only "
        f"English allowed in `reply`.\n"
        f"• `reply_en` = English translation of your lead-in (NOT the answer to the "
        f"drill). `gloss` = word-by-word gloss of the {name} words in your lead-in.\n\n"
        f"{bg}{deck}{palette_block}{avoid_block}"
        f"{_history_block(history or [])}"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        f'  "reply": "<{name} lead-in + the ONE English phrase to translate>",\n'
        f'  "reply_en": "<English translation of the lead-in>",\n'
        f'  "gloss": {{"<{name} word>":"<English>", ...}},\n'
        f'{expected_line}'
        '  "corrections": [],\n'
        '  "new_items": [],\n'
        '  "points": [],\n'
        '  "drill": ""\n'
        '}\n'
    )


def _expected_words(out: dict) -> list[str]:
    """Pull the opener's self-reported answer content words from the raw response."""
    try:
        parsed = _parse_json(out.get("_raw_response") or "")
    except (ValueError, TypeError):
        return []
    ws = parsed.get("expected_words") if isinstance(parsed, dict) else None
    if not isinstance(ws, list):
        return []
    seen, res = set(), []
    for w in ws:
        if isinstance(w, str) and w.strip():
            key = _strip_for_match(w)
            if key and key not in seen:
                seen.add(key)
                res.append(w.strip())
    return res[:12]


async def _drill_opener(target_lang, skill, history, *, api_key, model, level,
                        learner_profile, known_words, large, deck_count=0,
                        cefr_stats="", palette=None, avoid=None) -> dict:
    prompt = build_drill_prompt(
        target_lang, skill, history, level=level, learner_profile=learner_profile,
        known_words=known_words, large=large, palette=palette, avoid=avoid,
        deck_count=deck_count, cefr_stats=cefr_stats,
    )
    return await _run(prompt, target_lang, api_key=api_key, model=model)


async def _snap_misses(target_lang: str, misses: list[str],
                       known_vectors: dict[str, list[float]], api_key: str) -> list[str]:
    """Embed the unknown answer-words and return known substitutes (nearest ≥ threshold)."""
    if not misses or not known_vectors:
        return []
    mvecs = await embeddings.embed(misses, api_key)
    known_items = list(known_vectors.items())
    palette: list[str] = []
    for mv in mvecs:
        label, score = embeddings.nearest(mv, known_items)
        if score >= SNAP_THRESHOLD and label and label not in palette:
            palette.append(label)
    return palette[:MAX_PALETTE]


async def start_drill(
    target_lang: str,
    skill: str,
    history: list[dict] | None = None,
    *,
    api_key: str,
    model: str = TUTOR_MODEL,
    level: str = "A1",
    learner_profile: str = "",
    known_words: list[dict] | None = None,
    deck_count: int = 0,
    cefr_stats: str = "",
    known_word_strings: list[str] | None = None,
    known_vectors_provider=None,
) -> dict:
    """Open a construction drill, with embeddings as a verify-fail fallback.

    SMALL deck: caller passes the full `known_words` list and no `known_word_strings`
      → a single opener call, NO embeddings.
    LARGE deck: caller passes `known_word_strings` (the FULL known set, for cheap
      string verification), a `known_words` SAMPLE, a `cefr_stats` profile, and a
      `known_vectors_provider` (async, embeds the deck — invoked ONLY on a miss). We
      run a cheap opener, verify its `expected_words` against the known set, and only
      when it leaned on unknown vocab do we embed-snap those misses to known
      substitutes and regenerate the opener avoiding them. So most large-deck drills
      still cost zero embedding calls."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")

    large = bool(known_word_strings)

    async def opener(**extra):
        # Retry once on a BLANK reply — an empty opener (transient model hiccup /
        # safety stop) would otherwise render as an empty drill panel and get
        # persisted, leaving the learner a contentless box.
        o = await _drill_opener(
            target_lang, skill, history, api_key=api_key, model=model, level=level,
            learner_profile=learner_profile, known_words=known_words, large=large,
            deck_count=deck_count, cefr_stats=cefr_stats, **extra,
        )
        if not (o.get("reply") or "").strip():
            o = await _drill_opener(
                target_lang, skill, history, api_key=api_key, model=model, level=level,
                learner_profile=learner_profile, known_words=known_words, large=large,
                deck_count=deck_count, cefr_stats=cefr_stats, **extra,
            )
        return o

    out = await opener()
    if not large:
        return out

    known_set = {_strip_for_match(w) for w in known_word_strings}
    known_set.discard("")
    misses = [w for w in _expected_words(out) if _strip_for_match(w) not in known_set]
    if not misses or not known_vectors_provider:
        return out                          # verification passed → no embeddings

    try:
        vectors = await known_vectors_provider()
        palette = await _snap_misses(target_lang, misses, vectors, api_key)
    except Exception:
        return out                          # embedding failed → keep the cheap opener

    regen = await opener(palette=palette, avoid=misses)
    # If the regen came back blank, keep the (non-blank) first opener rather than
    # shipping an empty drill.
    return regen if (regen.get("reply") or "").strip() else out


# ── Inline lesson construction-drill ──────────────────────────────────────────
# A tight, LLM-graded drill embedded in the lesson player: each turn poses ONE
# English phrase to translate into the target language, exercising one construction
# with vocabulary the learner knows. Architecture: the PLAN call generates
# target-language sentences that use the construction + their English renderings
# (the target is a reference example, NOT a match oracle). Judging is ALWAYS one
# liberal LLM call graded from the English meaning — any valid translation passes,
# even without the construction (an `alt` then shows the construction-using form).
# A mechanical same-answer override catches the judge marking wrong an answer that
# equals its own correction. If the LLM call fails, the turn is SKIPPED (not marked
# right or wrong) — the user sees "couldn't check" and moves on. Same trust model
# as the rest of tutor — romanization recomputed by the oracle, never hard-fails.


def build_lesson_drill_plan_prompt(
    target_lang: str,
    construction: str,
    *,
    level: str,
    known_words: list[dict] | None,
    n: int,
) -> str:
    """PLAN call: invent target-language sentences that USE the construction, then
    back-translate each to English. The target is a reference example (it uses the
    construction) — NOT a strict answer key; judging is done from the English."""
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    deck = (f"── WORDS THE LEARNER KNOWS ──\n{_word_list_block(known_words)}\n\n"
            if known_words else "")
    return (
        f"You are designing a tight PRACTICE DRILL inside a {name} lesson "
        f"(learner level {level}). The skill being practised is the construction/form: "
        f"\"{construction}\".\n\n"
        f"Write EXACTLY {n} short, natural {name} sentences that each USE "
        f"\"{construction}\", then give the English translation of each. Requirements:\n"
        f"• Build each sentence from words the learner already KNOWS (list below); "
        f"introduce a new word only if the construction truly needs it.\n"
        f"• Each {name} sentence must be ONE natural, standard rendering — no slashes, "
        f"no parenthetical optional letters, no listed alternatives.\n"
        f"• VARY the vocabulary across the {n} sentences — don't reuse the same nouns/verbs.\n"
        f"• Keep each sentence short and level-appropriate ({level}); order them easiest first.\n"
        f"• The English is what the learner will see and translate back into {name}.\n\n"
        f"{deck}"
        "Return ONLY valid JSON, no other text:\n"
        '{\n'
        '  "items": [\n'
        '    {"target": "<' + name + ' sentence using the construction>", '
        '"english": "<its English translation>"},\n'
        '    ...\n'
        '  ]\n'
        '}\n'
    )


def build_lesson_drill_judge_prompt(
    target_lang: str,
    construction: str,
    phrase: str,
    answer: str,
    reference: str,
    *,
    level: str,
) -> str:
    """JUDGE call: decide whether the learner's translation of `phrase` is
    acceptable. Graded from the English meaning — NOT by matching `reference`
    (which is just one example that happens to use the construction). Liberal:
    any correct, natural translation passes, even if it doesn't use the
    construction; in that case we still surface a construction-using version."""
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    ref = (f"One {name} sentence that uses this construction is: \"{reference.strip()}\". "
           f"This is only an example — the learner does NOT need to match it.\n\n"
           if reference.strip() else "")
    return (
        f"You are a {name} tutor. A learner (level {level}) translated this English "
        f"phrase into {name}:\n"
        f"  English: \"{phrase.strip()}\"\n"
        f"  Learner wrote: \"{answer.strip()}\"\n\n"
        f"The lesson is practising the construction \"{construction}\". "
        f"{ref}"
        f"Judge ONLY whether the learner's sentence is a correct, natural {name} "
        f"translation of the English. Be liberal: accept ANY valid translation, even if "
        f"it does not use the construction or differs from the example. Ignore "
        f"capitalization, trailing punctuation (.!?), and apostrophe style. Accept a minor "
        f"typo (one swapped/missing letter) when the intent is clear, but note it. Do NOT "
        f"require optional gender/number/formality variants — one valid form is enough.\n\n"
        f"Return ONLY valid JSON:\n"
        '{\n'
        '  "correct": true or false,\n'
        '  "corrected": "<empty if the answer is correct and clean; the answer with typos '
        'fixed if correct but slightly off; the correct translation if wrong>",\n'
        f'  "alt": "<empty UNLESS the answer is correct but does not use the construction; '
        f'then one natural {name} sentence that does>",\n'
        '  "note": "<one short sentence: the error and rule if wrong; the construction tip '
        'if you filled alt; empty otherwise>"\n'
        '}\n'
    )


def _norm_for_compare(s: str) -> str:
    """Normalize a string for mechanical same-answer comparison: casefold,
    strip trailing/leading punctuation, collapse whitespace, and unify
    apostrophe/quote variants to ASCII '."""
    import unicodedata
    s = unicodedata.normalize("NFC", s).casefold().strip()
    s = s.rstrip(".!?。！？،؟…")
    _APOSTROPHES = str.maketrans({
        "’": "'", "‘": "'", "ʼ": "'",
        "`": "'", "´": "'", "′": "'",
    })
    s = s.translate(_APOSTROPHES)
    return " ".join(s.split())


def _rom(s: str, target_lang: str) -> str:
    has_rom = bool(LANG_INFO[target_lang].get("romanization"))
    return tokenizer.romanize_text(s, target_lang) if (has_rom and s) else ""


def _build_feedback(correct: bool, corrected: str, note: str,
                    target_lang: str, alt: str = "") -> dict:
    return {
        "correct":         correct,
        "corrected":       corrected,
        "corrected_roman": _rom(corrected, target_lang),
        "alt":             alt,
        "alt_roman":       _rom(alt, target_lang),
        "note":            note,
    }


def _normalize_drill_plan(parsed: dict, n: int) -> list[dict]:
    """Extract items from the plan response as {"english", "target"} dicts.
    `target` is a reference rendering that uses the construction (for feedback,
    not strict matching). Tolerates the old `expected` key and a bare `phrases`
    list (English-only)."""
    if not isinstance(parsed, dict):
        return []
    # New format: {"items": [{"target": ..., "english": ...}]}
    raw = parsed.get("items")
    if isinstance(raw, list):
        out: list[dict] = []
        for item in raw:
            if isinstance(item, dict):
                eng = (item.get("english") or "").strip()[:200]
                tgt = (item.get("target") or item.get("expected") or "").strip()[:200]
                if eng:
                    out.append({"english": eng, "target": tgt})
            if len(out) >= n:
                break
        return out
    # Fallback: old {"phrases": [...]} format (English-only, no reference)
    raw = parsed.get("phrases")
    if isinstance(raw, list):
        out = []
        for p in raw:
            if isinstance(p, str) and p.strip():
                out.append({"english": p.strip()[:200], "target": ""})
            if len(out) >= n:
                break
        return out
    return []


async def _drill_call(prompt: str, api_key: str, model: str) -> dict:
    """Call + parse-with-one-retry; returns {} on ANY failure (never hard-fails).

    Catches the API call too — not just parse errors — so a transient hiccup
    (empty/safety-filtered response → `.text` is None → AttributeError, a 429/500,
    a quota error) degrades to {} instead of propagating into a 502 that kills the
    whole drill. The caller turns {} into a graceful "accepted" fallback so the
    drill always continues.
    """
    for attempt in range(2):
        try:
            raw = await asyncio.to_thread(lambda: _call(prompt, api_key, model))
            parsed = _parse_json(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception as e:
            logger.warning("drill LLM call/parse failed (attempt %d): %s", attempt + 1, e)
    return {}


async def run_lesson_drill(
    target_lang: str,
    construction: str,
    *,
    answer: str | None = None,
    plan_items: list[dict] | None = None,
    api_key: str,
    model: str = TUTOR_MODEL,
    level: str = "A1",
    known_words: list[dict] | None = None,
    turn: int = 1,
    max_turns: int = LESSON_DRILL_TURNS,
) -> dict:
    """One turn of an inline lesson construction-drill.

    PLAN  (answer=None): generate target-lang sentences that use the
      construction + their English translations; return the first phrase +
      the full plan so the client can carry it forward.
    JUDGE (answer given): one LLM call grades the learner's translation from
      the English alone. The plan's reference `target` is passed as context
      (an example that uses the construction) but is NOT a match-oracle.
      If the LLM call fails, returns feedback=None so the client can show
      "couldn't check" and let the user skip.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")

    # ── PLAN ──
    if answer is None:
        prompt = build_lesson_drill_plan_prompt(
            target_lang, construction, level=level,
            known_words=known_words, n=max_turns,
        )
        plan = _normalize_drill_plan(await _drill_call(prompt, api_key, model), max_turns)
        if not plan:
            return {"feedback": None, "phrase": "", "plan_items": [], "done": True}
        return {
            "feedback": None,
            "phrase": plan[0]["english"],
            "plan_items": plan,
            "done": False,
        }

    # ── JUDGE ──
    items = plan_items or []
    item = items[turn - 1] if 0 < turn <= len(items) else {}
    phrase = item.get("english", "")
    reference = item.get("target", item.get("expected", ""))

    llm_result = await _drill_call(
        build_lesson_drill_judge_prompt(
            target_lang, construction, phrase, answer, reference,
            level=level,
        ), api_key, model,
    )
    if not llm_result:
        feedback = None
    else:
        correct = bool(llm_result.get("correct", llm_result.get("acceptable")))
        corrected = (llm_result.get("corrected") or "").strip()
        alt = (llm_result.get("alt") or "").strip()
        note = (llm_result.get("note") or "").strip()
        if not correct and _norm_for_compare(answer) == _norm_for_compare(corrected):
            correct = True
            note = ""
        if not correct and not corrected:
            corrected = reference
        feedback = _build_feedback(correct, corrected, note, target_lang, alt)

    # Next phrase from the plan
    next_phrase = ""
    done = True
    if items and turn < len(items) and turn < max_turns:
        next_phrase = items[turn].get("english", "")
        done = False

    return {"feedback": feedback, "phrase": next_phrase, "done": done}
