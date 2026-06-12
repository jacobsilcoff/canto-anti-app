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
        f"• Set `drill` to a SHORT construction/skill label ONLY when THIS turn surfaced "
        f"a GENERALIZABLE construction worth practicing — either you just taught one, or "
        f"a correction above exposed one the learner could drill (use the SAME label as "
        f"that correction's `construction`). Leave it \"\" for ordinary chat, a one-off "
        f"vocab word, or when you are already mid-drill. This unlocks a 'Drill' button.\n"
        f"• DRILL MODE: when your OWN previous message posed an English phrase for the "
        f"learner to translate, their new message is their attempt — judge it via "
        f"`corrections`, confirm the natural version, then pose the NEXT short English "
        f"phrase exercising the SAME construction. Build each phrase from words the "
        f"learner already KNOWS (vary the vocabulary using their deck/known list above; "
        f"if the construction needs a word they don't have, pick a known substitute or "
        f"teach ONE simple word via `new_items`). Keep going until they've correctly "
        f"produced 3–4 examples of the construction, THEN give a one-line recap and "
        f"return to normal conversation. The English phrase-to-translate is the ONE "
        f"exception to the no-English-in-reply rule.\n"
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

    return {"reply": reply, "reply_en": reply_en, "gloss": gloss,
            "corrections": corrections, "new_items": new_items, "points": points,
            "drill": drill}


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
) -> dict:
    """One LLM call → normalized structured reply."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = build_tutor_prompt(
        target_lang, user_msg, history,
        level=level, learner_profile=learner_profile,
        known_words=known_words, concept_registry=concept_registry,
        weak_concepts=weak_concepts,
    )
    known_texts = {(w.get("target_text") or "").strip() for w in (known_words or [])}
    return await _run(prompt, target_lang, api_key=api_key, model=model,
                      user_msg=user_msg, known_texts=known_texts)


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
    name = info["name"]
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
    out = await _drill_opener(
        target_lang, skill, history, api_key=api_key, model=model, level=level,
        learner_profile=learner_profile, known_words=known_words, large=large,
        deck_count=deck_count, cefr_stats=cefr_stats,
    )
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

    return await _drill_opener(
        target_lang, skill, history, api_key=api_key, model=model, level=level,
        learner_profile=learner_profile, known_words=known_words, large=True,
        deck_count=deck_count, cefr_stats=cefr_stats, palette=palette, avoid=misses,
    )


# ── Inline lesson construction-drill ──────────────────────────────────────────
# A tight, LLM-graded drill embedded in the lesson player: each turn poses ONE
# English phrase to translate into the target language, exercising one construction
# with vocabulary the learner knows, judging the previous attempt. Same trust model
# as the rest of tutor — romanization recomputed by the oracle, fields normalized,
# never hard-fails. One call per turn (start = no answer; then judge-and-advance).

def build_lesson_drill_prompt(
    target_lang: str,
    construction: str,
    history: list[dict],
    answer: str | None,
    *,
    level: str,
    known_words: list[dict] | None,
    turn: int,
    max_turns: int,
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    deck = (f"── WORDS THE LEARNER KNOWS ──\n{_word_list_block(known_words)}\n\n"
            if known_words else "")
    convo = ""
    if history:
        lines = []
        for h in history:
            who = "You posed" if h.get("role") == "tutor" else "Learner answered"
            lines.append(f"{who}: {(h.get('text') or '').strip()}")
        convo = "── DRILL SO FAR ──\n" + "\n".join(lines) + "\n\n"
    if answer is not None:
        state = (f"The learner's answer to your last English phrase: \"{answer.strip()}\". "
                 f"Judge it (this is attempt #{turn} of ~{max_turns}), then either pose the "
                 f"NEXT English phrase or, if they've done ~{max_turns}, set done=true and "
                 f"pose nothing more.\n\n")
    else:
        state = "Begin the drill: pose the FIRST English phrase. There is no answer to judge yet.\n\n"
    return (
        f"You are a {name} tutor running a tight PRACTICE DRILL embedded in a lesson "
        f"(learner level {level}). The skill being drilled is the CONSTRUCTION/form: "
        f"\"{construction}\".\n\n"
        f"{_lang_preamble(info)}"
        f"Each turn you pose EXACTLY ONE short, concrete English phrase for the learner to "
        f"translate into {name}, all exercising \"{construction}\". Build every phrase from "
        f"words the learner already KNOWS (use the list below); introduce a new word only if "
        f"the construction truly needs it. Vary the vocabulary across turns. Do NOT translate "
        f"the phrase for them.\n"
        f"When judging an answer: decide if it's correct, give the natural {name} version, and "
        f"a one-line English note about the RULE (how the construction works in general), not "
        f"just this instance. Be encouraging.\n\n"
        f"{deck}{convo}{state}"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        '  "feedback": {"correct": true, "corrected": "<the natural target-language answer; '
        'empty on the very first turn>", "note": "<short English rule; empty on the first turn>"},\n'
        f'  "phrase": "<the next English phrase to translate, or empty when done>",\n'
        f'  "reply": "<one short {name} encouragement/lead-in>",\n'
        f'  "reply_en": "<English translation of reply>",\n'
        f'  "gloss": {{"<{name} word>":"<English>", ...}},\n'
        '  "done": false\n'
        '}\n'
    )


def _normalize_lesson_drill(parsed: dict, target_lang: str) -> dict:
    has_rom = bool(LANG_INFO[target_lang].get("romanization"))

    def rom(s: str) -> str:
        return tokenizer.romanize_text(s, target_lang) if (has_rom and s) else ""

    fb_raw = parsed.get("feedback") if isinstance(parsed.get("feedback"), dict) else {}
    corrected = (fb_raw.get("corrected") or "").strip()
    feedback = None
    if corrected:
        feedback = {
            "correct":        bool(fb_raw.get("correct")),
            "corrected":      corrected,
            "corrected_roman": rom(corrected),
            "note":           (fb_raw.get("note") or "").strip(),
        }

    gloss: dict[str, str] = {}
    raw_gloss = parsed.get("gloss")
    if isinstance(raw_gloss, dict):
        for k, v in raw_gloss.items():
            k = (k or "").strip()
            v = (v or "").strip() if isinstance(v, str) else ""
            if k and v and k not in gloss:
                gloss[k] = v
            if len(gloss) >= MAX_GLOSS:
                break

    return {
        "feedback":  feedback,
        "phrase":    (parsed.get("phrase") or "").strip(),
        "reply":     (parsed.get("reply") or "").strip(),
        "reply_en":  (parsed.get("reply_en") or "").strip(),
        "gloss":     gloss,
        "done":      bool(parsed.get("done")),
    }


async def run_lesson_drill(
    target_lang: str,
    construction: str,
    history: list[dict] | None = None,
    answer: str | None = None,
    *,
    api_key: str,
    model: str = TUTOR_MODEL,
    level: str = "A1",
    known_words: list[dict] | None = None,
    turn: int = 1,
    max_turns: int = LESSON_DRILL_TURNS,
) -> dict:
    """One turn of an inline lesson construction-drill: pose the first phrase
    (`answer=None`) or judge `answer` and advance. Parse-with-one-retry, normalize,
    never hard-fail."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = build_lesson_drill_prompt(
        target_lang, construction, history or [], answer,
        level=level, known_words=known_words, turn=turn, max_turns=max_turns,
    )
    parsed = None
    for _ in range(2):
        raw = await asyncio.to_thread(lambda: _call(prompt, api_key, model))
        try:
            parsed = _parse_json(raw)
            break
        except (ValueError, TypeError):
            parsed = None
    if not isinstance(parsed, dict):
        parsed = {}
    return _normalize_lesson_drill(parsed, target_lang)
