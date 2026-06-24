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

import embeddings
import tokenizer
from learning import _lang_preamble, _registry_block, _word_list_block
from translation import LANG_INFO, _call, _parse_json

TUTOR_MODEL = "gemini-2.5-flash"   # better conversational quality than -lite, still cheap

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
        for field in ("notes", "romanization", "source_text"):
            val = (raw_cu.get(field) or "").strip()
            if val:
                cu[field] = val[:600]
        if cu:
            card_updates = cu

    return {"reply": reply, "reply_en": reply_en, "gloss": gloss,
            "corrections": corrections, "new_items": new_items, "points": points,
            "drill": drill, "card_updates": card_updates}


async def _run(prompt: str, target_lang: str, *, api_key: str, model: str,
               user_msg: str = "", known_texts: set[str] | None = None) -> dict:
    """Call the model, parse JSON (one retry), normalize. Never hard-fails: a
    malformed side-channel degrades to a plain-text reply."""
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
) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])

    profile = f"── LEARNER BACKGROUND ──\n{learner_profile.strip()}\n\n" if learner_profile.strip() else ""
    deck = ""
    if known_words:
        deck = (f"── WORDS THE LEARNER ALREADY KNOWS (their deck) ──\n"
                f"{_word_list_block(known_words)}\n"
                f"Prefer these in your example sentences; don't re-teach them as new_items.\n\n")

    return (
        f"You are a warm, expert {name} tutor helping an English-speaking learner "
        f"(level {level}) who is reviewing a flashcard and has a question about it.\n\n"
        f"{_lang_preamble(info)}"
        f"── HOW TO ANSWER ──\n"
        f"• ANSWER THE QUESTION the learner actually asked — directly, clearly, concisely. "
        f"Write your EXPLANATION in ENGLISH (this is study help, NOT conversation practice — "
        f"the learner is decoding a card and needs to understand). Write only the actual "
        f"{name} words and example sentences in {name} script, each followed by its "
        f"romanization and a short English gloss in parentheses.\n"
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
        f"explanation would help the learner remember, put it in the notes! Specifically:\n"
        f"• When the learner asks about the card, asks to break it down, asks why something "
        f"works a certain way, or asks for a mnemonic → ALWAYS suggest a `notes` update that "
        f"captures the key insight in a concise note they'll see next time they review.\n"
        f"• When the learner asks to update the card or add a note → ALWAYS include card_updates.\n"
        f"• When you spot wrong/missing romanization → suggest `romanization` fix.\n"
        f"• When the translation is wrong or misleading → suggest `source_text` fix.\n"
        f"• When existing notes are empty or sparse and your explanation adds real value → "
        f"suggest enriched `notes` (etymology, mnemonic, usage tip, component breakdown).\n"
        f"The notes field should be concise (a helpful reminder, not an essay). If the card "
        f"already has good notes and the learner isn't asking about anything note-worthy, "
        f"then omit card_updates (null).\n\n"
        f"{profile}{deck}{_card_block(card, name)}"
        f"{_history_block(history or [])}"
        f"Learner's question: {question.strip()}\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        f'  "reply": "<your answer; English explanation is fine, {name} examples in {name} script>",\n'
        '  "new_items": [{"target_text":"<native word/phrase worth saving>","english":"<gloss>","notes":"<usage, optional>"}],\n'
        '  "card_updates": {"notes":"<concise note for the card>","romanization":"<fixed romanization>","source_text":"<fixed translation>"}\n'
        '}\n'
        'new_items may be an empty array. Do NOT put a word in new_items that already appears in '
        'the learner\'s known-words list. card_updates: include only the fields you want to change '
        '(notes and/or romanization and/or source_text). Omit card_updates entirely (or null) only '
        'when there is genuinely nothing useful to add to the card.'
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
    return await _run(prompt, target_lang, api_key=api_key, model=model,
                      user_msg=question, known_texts=known_texts)


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
# with vocabulary the learner knows, judging the previous attempt. Same trust model
# as the rest of tutor — romanization recomputed by the oracle, fields normalized,
# never hard-fails. One call per turn (start = no answer; then judge-and-advance).

# ── Inline lesson construction-drill ──────────────────────────────────────────
# A tight, LLM-graded drill embedded in the lesson player. To make it STRUCTURALLY
# impossible for one phrase's answer to leak into another phrase's feedback, the two
# jobs are split across separate LLM calls that never share context:
#   1. PLAN  (opener, answer=None): generate ALL the English phrases up front, in one
#      call. This call produces NO feedback.
#   2. JUDGE (each turn): grade ONE answer against ONE phrase. The call is given ONLY
#      the phrase being judged — never the next one — so it physically cannot preview
#      or leak the next phrase's answer into the current feedback.
# The plan round-trips through the client (sent back each turn); the server picks the
# next phrase deterministically from the plan, so the judge call never poses anything.
# Same trust model as the rest of tutor — romanization recomputed by the oracle,
# fields normalized, never hard-fails. Same total call count as before (1 + N).

# Shared judging rubric — used by the judge-only call.
_DRILL_VERDICT_RUBRIC = (
    "Decide if the answer is correct (accept any natural, correct rendering — don't "
    "nitpick word choice that means the same thing). There are THREE verdicts:\n"
    "1. PERFECT — flawless. `correct: true`, `corrected` = the EXACT string the learner "
    "wrote (copy it verbatim), `note` = brief confirmation of what they used (e.g. "
    "'Good — 本 is the correct classifier for books').\n"
    "2. ACCEPTED WITH A MINOR FLAW — correct except for a missing/wrong diacritic or "
    "accent (e.g. 'francaise' for 'française', 'cafe' for 'café') or a small typo (one "
    "swapped/missing letter) where the intent is unambiguous. Still `correct: true` (the "
    "learner clearly knows the word/form), but `corrected` = the POLISHED proper form "
    "(e.g. 'française'), and you MUST add a `note` naming the fix (e.g. 'Almost perfect — "
    "add the cedilla: française'). NEVER leave the note empty in this case.\n"
    "3. WRONG — a real error. `correct: false`. Gender/number agreement errors are NOT "
    "minor — they are real grammar mistakes (e.g. 'Elle est content' must be 'contente'; "
    "'les chat' must be 'les chats') — mark them WRONG. `corrected` = the FULL corrected "
    "sentence (the complete, natural translation of the phrase you were asked to judge; "
    "NEVER just a word or fragment — the learner sees it side-by-side with what they "
    "wrote). `note` = (1) name the specific error category (e.g. 'Incorrect conjugation "
    "of verb mettre', 'Wrong gender agreement'), (2) state the rule in one sentence (e.g. "
    "'mettre → je mets (not met) — first-person singular adds -s').\n"
    "CRITICAL: base your `note` ONLY on characters and words the learner actually wrote — "
    "never reference a particle, classifier, or word that does not appear in their answer.\n"
)


def build_lesson_drill_plan_prompt(
    target_lang: str,
    construction: str,
    *,
    level: str,
    known_words: list[dict] | None,
    n: int,
) -> str:
    """PLAN call: generate ALL the drill's English phrases up front. No judging here,
    so no answer/feedback can be produced — the phrases are the only output."""
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    deck = (f"── WORDS THE LEARNER KNOWS ──\n{_word_list_block(known_words)}\n\n"
            if known_words else "")
    return (
        f"You are designing a tight PRACTICE DRILL embedded in a {name} lesson "
        f"(learner level {level}). The skill being drilled is the CONSTRUCTION/form: "
        f"\"{construction}\".\n\n"
        f"Produce EXACTLY {n} short, concrete ENGLISH phrases for the learner to translate "
        f"into {name}, each one exercising \"{construction}\". Requirements:\n"
        f"• Every phrase must be translatable using words the learner already KNOWS "
        f"(use the list below); introduce a new word only if the construction truly needs it.\n"
        f"• VARY the vocabulary across the {n} phrases — don't reuse the same nouns/verbs.\n"
        f"• Keep each phrase short and level-appropriate ({level}). Order them easiest first.\n"
        f"• These are ENGLISH prompts only — do NOT translate them, do NOT include any "
        f"{name} text or answers.\n\n"
        f"{deck}"
        f"Return ONLY valid JSON, no other text — exactly {n} phrases:\n"
        '{\n'
        '  "phrases": ["<English phrase>", "<English phrase>", "..."]\n'
        '}\n'
    )


def build_lesson_drill_judge_prompt(
    target_lang: str,
    construction: str,
    phrase: str,
    answer: str,
    *,
    level: str,
    known_words: list[dict] | None,
) -> str:
    """JUDGE call: grade ONE answer against ONE phrase. Given only the phrase being
    judged — never any other phrase — so it cannot leak the next phrase's answer."""
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    return (
        f"You are a {name} tutor grading ONE answer in a practice drill (learner level "
        f"{level}). The skill being drilled is the construction: \"{construction}\".\n\n"
        f"{_lang_preamble(info)}"
        f"You asked the learner to translate this English phrase into {name}:\n"
        f"  \"{phrase.strip()}\"\n"
        f"Their answer was:\n"
        f"  \"{answer.strip()}\"\n\n"
        f"Judge THIS answer and nothing else. {_DRILL_VERDICT_RUBRIC}"
        f"If there is another equally natural {name} translation OF THIS SAME phrase, you "
        f"may put it in `alternative` with a one-sentence `alt_note`; omit `alternative` "
        f"when there is no meaningful variant or when the answer is wrong. The ONLY {name} "
        f"text you produce is inside `corrected`/`alternative`. Be encouraging but concise.\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        '  "correct": true,\n'
        '  "corrected": "<perfect: verbatim; flaw/wrong: the polished/correct form>",\n'
        '  "note": "<wrong: \'Error: [specific]. Rule: [one sentence]\'; minor flaw: name the fix (REQUIRED); perfect: confirm what they used>",\n'
        '  "alternative": "<another natural translation of THIS phrase; omit if none or if wrong>",\n'
        '  "alt_note": "<when to prefer corrected vs alternative; omit if no alternative>"\n'
        '}\n'
    )


def _normalize_drill_feedback(parsed: dict, target_lang: str) -> dict | None:
    has_rom = bool(LANG_INFO[target_lang].get("romanization"))

    def rom(s: str) -> str:
        return tokenizer.romanize_text(s, target_lang) if (has_rom and s) else ""

    corrected = (parsed.get("corrected") or "").strip()
    if not corrected:
        return None
    correct = bool(parsed.get("correct"))
    # Defense in depth: an `alternative` is only meaningful for an accepted answer.
    # Drop it on a wrong verdict (it adds nothing and was the historical leak vector).
    alternative = (parsed.get("alternative") or "").strip() if correct else ""
    return {
        "correct":           correct,
        "corrected":         corrected,
        "corrected_roman":   rom(corrected),
        "note":              (parsed.get("note") or "").strip(),
        "alternative":       alternative,
        "alternative_roman": rom(alternative) if alternative else "",
        "alt_note":          (parsed.get("alt_note") or "").strip() if alternative else "",
    }


def _normalize_drill_plan(parsed: dict, n: int) -> list[str]:
    raw = parsed.get("phrases") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for p in raw:
        if isinstance(p, str) and p.strip():
            out.append(p.strip()[:200])
        if len(out) >= n:
            break
    return out


async def _drill_call(prompt: str, api_key: str, model: str) -> dict:
    """Call + parse-with-one-retry; returns {} on failure (never hard-fails)."""
    for _ in range(2):
        raw = await asyncio.to_thread(lambda: _call(prompt, api_key, model))
        try:
            parsed = _parse_json(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


async def run_lesson_drill(
    target_lang: str,
    construction: str,
    *,
    answer: str | None = None,
    phrases: list[str] | None = None,
    api_key: str,
    model: str = TUTOR_MODEL,
    level: str = "A1",
    known_words: list[dict] | None = None,
    turn: int = 1,
    max_turns: int = LESSON_DRILL_TURNS,
) -> dict:
    """One turn of an inline lesson construction-drill.

    PLAN  (answer=None): generate all `max_turns` phrases; return the first + the full
      plan so the client can carry it forward.
    JUDGE (answer given): grade the answer to `phrases[turn-1]` and advance to
      `phrases[turn]` from the client-supplied plan — the judge call never poses a
      phrase, so the next phrase cannot leak into this turn's feedback.
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
            return {"feedback": None, "phrase": "", "phrases": [], "done": True}
        return {"feedback": None, "phrase": plan[0], "phrases": plan, "done": False}

    # ── JUDGE ──
    plan = [p for p in (phrases or []) if isinstance(p, str) and p.strip()]
    judged = plan[turn - 1] if 0 < turn <= len(plan) else ""
    prompt = build_lesson_drill_judge_prompt(
        target_lang, construction, judged, answer,
        level=level, known_words=known_words,
    )
    feedback = _normalize_drill_feedback(await _drill_call(prompt, api_key, model), target_lang)
    # Next phrase comes straight from the plan — deterministic, no LLM generation.
    if plan and turn < len(plan) and turn < max_turns:
        return {"feedback": feedback, "phrase": plan[turn], "done": False}
    return {"feedback": feedback, "phrase": "", "done": True}
