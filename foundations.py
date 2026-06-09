"""Foundations module — teaching the writing/sound SYSTEM (script + sounds),
separate from the vocab course (IDEAS item 43).

Design goals:
- Generalizable: a declarative per-language registry (FOUNDATIONS) describes the
  units; per-script-TYPE engines (decomposer + exercise builders) are the only
  script-specific code, shared by every language of that type. Adding a language
  = add its data, reusing an engine.
- Reuses the segmented-lesson player: build_units() returns ordinary course units
  whose lessons already carry pre-built `content` = {"segments": [...]}, using the
  existing exercise types (choice/listening/match) plus a new "block_build".
- Curated/deterministic: the inventory is finite & factual; romanisation comes
  from korean-romanizer, never the AI.

Currently implemented: Korean / Hangul (script_type "alphabetic"). The structure
generalises to abugida/tonal/etc. by adding data + (if a new script type) an
engine.
"""
import random

# ── Hangul jamo engine ────────────────────────────────────────────────────────
# Compatibility-jamo orderings used by the Unicode syllable composition formula.
_CHO = ["ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
_JUNG = ["ㅏ", "ㅐ", "ㅑ", "ㅒ", "ㅓ", "ㅔ", "ㅕ", "ㅖ", "ㅗ", "ㅘ", "ㅙ", "ㅚ", "ㅛ", "ㅜ", "ㅝ", "ㅞ", "ㅟ", "ㅠ", "ㅡ", "ㅢ", "ㅣ"]
_JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
# Compound jamo → their base components (so word validation works on base letters).
_SPLIT = {
    "ㅘ": "ㅗㅏ", "ㅙ": "ㅗㅐ", "ㅚ": "ㅗㅣ", "ㅝ": "ㅜㅓ", "ㅞ": "ㅜㅔ", "ㅟ": "ㅜㅣ", "ㅢ": "ㅡㅣ",
    "ㄳ": "ㄱㅅ", "ㄵ": "ㄴㅈ", "ㄶ": "ㄴㅎ", "ㄺ": "ㄹㄱ", "ㄻ": "ㄹㅁ", "ㄼ": "ㄹㅂ",
    "ㄽ": "ㄹㅅ", "ㄾ": "ㄹㅌ", "ㄿ": "ㄹㅍ", "ㅀ": "ㄹㅎ", "ㅄ": "ㅂㅅ", "ㄲ": "ㄱㄱ", "ㅆ": "ㅅㅅ",
}

_CHO_IDX = {j: i for i, j in enumerate(_CHO)}
_JUNG_IDX = {j: i for i, j in enumerate(_JUNG)}
_JONG_IDX = {j: i for i, j in enumerate(_JONG)}
# Consonants commonly used as a final (받침).
_FINAL_CONS = {"ㄱ", "ㄴ", "ㄷ", "ㄹ", "ㅁ", "ㅂ", "ㅇ"}


def compose_syllable(cho: str, jung: str, jong: str = "") -> str:
    """Compose a Hangul syllable block from base jamo."""
    ci, ji = _CHO_IDX[cho], _JUNG_IDX[jung]
    ki = _JONG.index(jong) if jong in _JONG else 0
    return chr(0xAC00 + (ci * 21 + ji) * 28 + ki)


def _split_compound(j: str) -> str:
    return _SPLIT.get(j, j)


def decompose_hangul(text: str) -> set[str]:
    """Return the set of BASE jamo used to write `text` (compounds split)."""
    out: set[str] = set()
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            s = code - 0xAC00
            cho, jung, jong = _CHO[s // 28 // 21], _JUNG[(s // 28) % 21], _JONG[s % 28]
            for j in (cho, jung, jong):
                if j:
                    out.update(_split_compound(j))
        elif ch.strip():
            out.add(ch)
    return out


def _romanize_ko(text: str) -> str:
    try:
        from korean_romanizer.romanizer import Romanizer
        return Romanizer(text).romanize()
    except Exception:
        return ""


def _romanize(text: str, lang: str) -> str:
    """Offline romanisation for a Foundations target, dispatched by language.
    Korean uses korean-romanizer; Indic scripts reuse the reader's tokenizer
    oracle (indic-transliteration). Never the AI."""
    if lang == "ko":
        return _romanize_ko(text)
    try:
        import tokenizer
        return tokenizer.romanize_text(text, lang)
    except Exception:
        return ""


# ── Curated Hangul content ────────────────────────────────────────────────────
# Each grapheme: (symbol, romanised sound, representative syllable to voice, note)
V = lambda s, r, a, n="": {"symbol": s, "roman": r, "audio": a, "note": n, "kind": "vowel"}
C = lambda s, r, a, n="": {"symbol": s, "roman": r, "audio": a, "note": n, "kind": "consonant"}

_VOWELS_1 = [V("ㅏ", "a", "아"), V("ㅓ", "eo", "어"), V("ㅗ", "o", "오"),
             V("ㅜ", "u", "우"), V("ㅡ", "eu", "으"), V("ㅣ", "i", "이")]
_CONS_1 = [C("ㄱ", "g", "가"), C("ㄴ", "n", "나"), C("ㄷ", "d", "다"),
           C("ㄹ", "r/l", "라"), C("ㅁ", "m", "마")]
_CONS_2 = [C("ㅂ", "b", "바"), C("ㅅ", "s", "사"),
           C("ㅇ", "(silent)", "아", "Silent at the start of a block; an 'ng' sound at the end."),
           C("ㅈ", "j", "자"), C("ㅎ", "h", "하")]
_CONS_3 = [C("ㅊ", "ch", "차"), C("ㅋ", "k", "카"), C("ㅌ", "t", "타"), C("ㅍ", "p", "파")]
_VOWELS_2 = [V("ㅑ", "ya", "야"), V("ㅕ", "yeo", "여"), V("ㅛ", "yo", "요"),
             V("ㅠ", "yu", "유"), V("ㅐ", "ae", "애"), V("ㅔ", "e", "에")]
# Compound vowels — written (and typed) as two basic vowels.
_VOWELS_COMPOUND = [V("ㅘ", "wa", "와", "ㅗ + ㅏ"), V("ㅝ", "wo", "워", "ㅜ + ㅓ"),
                    V("ㅚ", "oe", "외", "ㅗ + ㅣ"), V("ㅟ", "wi", "위", "ㅜ + ㅣ"),
                    V("ㅢ", "ui", "의", "ㅡ + ㅣ")]

# Candidate words per lesson (English meaning). Validated at build time against
# the letters taught so far — any using an untaught letter is dropped.
_WORDS_AFTER_CONS_1 = [("나", "I/me"), ("너", "you"), ("나무", "tree"), ("다리", "leg"),
                       ("머리", "head"), ("어디", "where"), ("고기", "meat"), ("나라", "country")]
_WORDS_AFTER_CONS_2 = [("우리", "we"), ("바다", "sea"), ("하나", "one"), ("어머니", "mother"),
                       ("아버지", "father"), ("사람", "person"), ("머리", "head"), ("바지", "trousers")]
_WORDS_AFTER_CONS_3 = [("코", "nose"), ("커피", "coffee"), ("치마", "skirt"),
                       ("토마토", "tomato"), ("우표", "stamp"), ("기차", "train")]
_WORDS_AFTER_VOWELS_2 = [("안녕", "hi"), ("네", "yes"), ("우유", "milk"), ("여자", "woman"),
                         ("야구", "baseball"), ("새", "bird"), ("개", "dog")]

# The Korean Foundations track: an ordered list of units, each a list of lessons.
_HANGUL_TRACK = {
    "script_type": "alphabetic",
    "title": "Read Hangul",
    "units": [
        {"title": "Hangul Basics", "objective": "Learn how Hangul works and the first vowels", "lessons": [
            {"title": "How Hangul Works", "type": "info",
             "intro": "Korean is written in Hangul — an alphabet, but the letters are grouped into "
                      "square syllable blocks (e.g. ㅎ+ㅏ+ㄴ → 한). It's very regular: once you know the "
                      "letters, you can read almost anything. Let's start with the vowels."},
            {"title": "First Vowels", "type": "graphemes", "graphemes": _VOWELS_1},
            {"title": "Building Blocks", "type": "blocks_info", "vowels": _VOWELS_1,
             "intro": "Every block needs a consonant + a vowel. The letter ㅇ is silent at the "
                      "start, so a vowel can stand alone with it: ㅇ+ㅏ → 아 (a). Try building a few."},
        ]},
        {"title": "Consonants", "objective": "The core consonants and your first words", "lessons": [
            {"title": "Consonants ㄱㄴㄷㄹㅁ", "type": "graphemes", "graphemes": _CONS_1, "blocks": True},
            {"title": "Your First Words", "type": "words", "words": _WORDS_AFTER_CONS_1},
            {"title": "Consonants ㅂㅅㅇㅈㅎ", "type": "graphemes", "graphemes": _CONS_2, "blocks": True},
            {"title": "More Words", "type": "words", "words": _WORDS_AFTER_CONS_2},
            {"title": "Consonants ㅊㅋㅌㅍ", "type": "graphemes", "graphemes": _CONS_3, "blocks": True},
            {"title": "Reading Practice", "type": "words", "words": _WORDS_AFTER_CONS_3},
        ]},
        {"title": "More Vowels", "objective": "Y-vowels, combined vowels, full blocks", "lessons": [
            {"title": "Y-Vowels & ㅐㅔ", "type": "graphemes", "graphemes": _VOWELS_2, "blocks": True},
            {"title": "Everyday Words", "type": "words", "words": _WORDS_AFTER_VOWELS_2},
            {"title": "Combined Vowels & Full Blocks", "type": "compound_vowels", "vowels": _VOWELS_COMPOUND},
        ]},
    ],
}

FOUNDATIONS = {"ko": _HANGUL_TRACK}


def has_foundations(lang: str) -> bool:
    return lang in FOUNDATIONS


# ── Exercise builders (deterministic) ─────────────────────────────────────────

def _options(correct: str, pool: list[str], n: int = 4) -> tuple[list[str], int]:
    seen, distractors = {correct}, []
    for p in pool:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p); distractors.append(p)
    random.shuffle(distractors)
    opts = [correct] + distractors[: max(0, n - 1)]
    random.shuffle(opts)
    return opts, opts.index(correct)


# Foundations is the READING track, so romanisation must never be shown inline —
# it's the very thing the learner is being tested on (a spoiler). `hide_roman`
# tells the player to tuck romanisation into a tap/hover tooltip instead of ruby.
def _grapheme_to_sound(g: dict, roman_pool: list[str]) -> dict:
    opts, ans = _options(g["roman"], roman_pool)
    return {
        "type": "choice", "instruction": "What sound does this letter make?",
        "prompt": g["symbol"], "prompt_lang": "target", "audio": g["audio"],
        "options": opts, "answer": ans, "tip": g.get("note", ""), "hide_roman": True,
    }


def _grapheme_match(graphemes: list[dict]) -> dict:
    return {
        "type": "match", "instruction": "Match each letter to its sound", "hide_roman": True,
        "pairs": [{"target": g["symbol"], "target_roman": "", "english": g["roman"]} for g in graphemes[:5]],
    }


def _block_build(target: str, cons_pool: list[dict], vowel_pool: list[dict]) -> dict:
    """A syllable-assembly exercise: the learner taps jamo (like a Korean
    keyboard) and they auto-compose into the block. We just supply the available
    consonants + vowels; the frontend composes + grades against `target`."""
    return {
        "type": "block_build", "instruction": "Spell the syllable you hear",
        "audio": target, "roman": _romanize_ko(target), "target": target, "hide_roman": True,
        "consonants": [c["symbol"] for c in cons_pool],
        "vowels": [v["symbol"] for v in vowel_pool],
    }


def _read_word(word: str, roman_pool: list[str], meaning: str, lang: str = "ko") -> dict:
    roman = _romanize(word, lang)
    opts, ans = _options(roman, roman_pool)
    return {
        "type": "choice", "instruction": "How do you read this?",
        "prompt": word, "prompt_lang": "target", "audio": word, "hide_roman": True,
        "options": opts, "answer": ans, "tip": (f"means: {meaning}" if meaning else ""),
    }


def _listen_word(word: str, word_pool: list[str], lang: str = "ko") -> dict:
    opts, ans = _options(word, word_pool)
    return {
        "type": "listening", "instruction": "What did you hear?", "hide_roman": True,
        "audio": word, "audio_roman": _romanize(word, lang),
        "options": opts, "options_roman": [_romanize(o, lang) for o in opts], "answer": ans,
    }


# ── Lesson/segment assembly ───────────────────────────────────────────────────

def _teach_item_grapheme(g: dict) -> dict:
    return {"target": g["symbol"], "target_roman": "", "gloss": f"“{g['roman']}” sound",
            "note": g.get("note", ""), "audio": g["audio"]}


def _teach_item_word(word: str, meaning: str, lang: str = "ko") -> dict:
    return {"target": word, "target_roman": _romanize(word, lang), "gloss": meaning,
            "note": "", "audio": word}


def _build_hangul_lesson_content(lesson: dict, taught: list[dict]) -> dict:
    """Build the {segments:[...]} content for one Hangul Foundations lesson.
    `taught` is the list of graphemes known BEFORE this lesson (for distractors/words)."""
    ltype = lesson["type"]
    taught_romans = [g["roman"] for g in taught]
    taught_vowels = [g for g in taught if g["kind"] == "vowel"]
    taught_cons = [g for g in taught if g["kind"] == "consonant"]

    if ltype == "info":
        return {"segments": [{"teach": {"intro": lesson["intro"], "items": []}, "exercises": []}]}

    if ltype == "blocks_info":
        vowels = lesson["vowels"]
        ieung = C("ㅇ", "(silent)", "아")
        exs = []
        for v in vowels[:4]:
            exs.append(_block_build(compose_syllable("ㅇ", v["symbol"]), [ieung], vowels))
        teach = {"intro": lesson["intro"],
                 "items": [{"target": compose_syllable("ㅇ", v["symbol"]),
                            "target_roman": v["roman"], "gloss": f"ㅇ + {v['symbol']}",
                            "note": "", "audio": compose_syllable("ㅇ", v["symbol"])} for v in vowels[:4]]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    if ltype == "graphemes":
        graphemes = lesson["graphemes"]
        roman_pool = [g["roman"] for g in graphemes] + taught_romans
        exs = [_grapheme_to_sound(g, roman_pool) for g in graphemes]
        if len(graphemes) >= 3:
            exs.append(_grapheme_match(graphemes))
        # block-building drills, if this lesson teaches consonants and we have vowels
        if lesson.get("blocks"):
            new_cons = [g for g in graphemes if g["kind"] == "consonant"]
            new_vowels = [g for g in graphemes if g["kind"] == "vowel"]
            cons_pool = (taught_cons + new_cons) or [C("ㅇ", "(silent)", "아")]
            vowel_pool = taught_vowels + new_vowels
            final_caps = [g for g in cons_pool if g["symbol"] in _FINAL_CONS]
            targets: list[str] = []
            # consonant + vowel
            for c in (new_cons or cons_pool)[:2]:
                if vowel_pool:
                    targets.append(compose_syllable(c["symbol"], random.choice(vowel_pool)["symbol"]))
            for v in new_vowels[:2]:
                targets.append(compose_syllable(random.choice(cons_pool)["symbol"], v["symbol"]))
            # consonant + vowel + final (받침)
            if final_caps and vowel_pool:
                for _ in range(2):
                    targets.append(compose_syllable(
                        random.choice(cons_pool)["symbol"], random.choice(vowel_pool)["symbol"],
                        random.choice(final_caps)["symbol"]))
            for t in dict.fromkeys(targets):
                exs.append(_block_build(t, cons_pool, vowel_pool))
        random.shuffle(exs)
        teach = {"intro": "", "items": [_teach_item_grapheme(g) for g in graphemes]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    if ltype == "compound_vowels":
        compounds = lesson["vowels"]
        roman_pool = [g["roman"] for g in compounds] + taught_romans
        exs = [_grapheme_to_sound(g, roman_pool) for g in compounds]
        # 4-jamo blocks: consonant + compound vowel + final. The learner types the
        # compound vowel's two components, so the keyboard is the basic vowels.
        basic_vowels = _VOWELS_1
        cons_pool = taught_cons or [C("ㅇ", "(silent)", "아")]
        final_caps = [g for g in cons_pool if g["symbol"] in _FINAL_CONS]
        for cv in compounds[:3]:
            c = random.choice(cons_pool)
            exs.append(_block_build(compose_syllable(c["symbol"], cv["symbol"]), cons_pool, basic_vowels))
            if final_caps:
                f = random.choice(final_caps)
                exs.append(_block_build(compose_syllable(c["symbol"], cv["symbol"], f["symbol"]), cons_pool, basic_vowels))
        random.shuffle(exs)
        teach = {"intro": "Some vowels combine two sounds — type both letters and they merge into one. "
                          "A full block can have up to four letters (e.g. 관 = ㄱ+ㅗ+ㅏ+ㄴ).",
                 "items": [{"target": g["symbol"], "target_roman": "", "gloss": f"“{g['roman']}” sound",
                            "note": g.get("note", ""), "audio": g["audio"]} for g in compounds]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    if ltype == "words":
        known = {g["symbol"] for g in taught}
        valid = [(w, m) for (w, m) in lesson["words"] if decompose_hangul(w) <= known]
        valid = valid[:6]
        word_pool = [w for w, _ in valid]
        roman_pool = [_romanize_ko(w) for w in word_pool]
        exs = []
        for w, m in valid:
            exs.append(_read_word(w, roman_pool, m))
        for w, _ in valid[:3]:
            exs.append(_listen_word(w, word_pool))
        random.shuffle(exs)
        teach = {"intro": "Words you can now read with the letters you know:",
                 "items": [_teach_item_word(w, m) for w, m in valid]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    return {"segments": [{"teach": {"intro": "", "items": []}, "exercises": []}]}


# ── Abugida engine (Devanagari, Telugu, …) ────────────────────────────────────
# Indic scripts are ALREADY decomposed at the code-point level: a syllable like
# कि (ki) is stored as क (consonant) + ि (vowel-sign/matra) — two code points.
# So decomposition is plain character iteration, and composition is plain string
# concatenation — far simpler than the Hangul jamo math above.

def decompose_indic(text: str) -> set[str]:
    """Return the set of code points used to write `text` (spaces ignored).
    Because Indic stores consonant + matra as separate code points, this already
    splits a syllable into its taught units. Words using an untaught sign
    (incl. virama → conjuncts) won't validate, which conveniently keeps early
    word lessons to simple, non-conjunct syllables."""
    return {ch for ch in text if not ch.isspace()}


# Grapheme constructors for abugida data. audio = a representative pronounceable
# unit (the letter itself for vowels/consonants; a demo syllable for a matra,
# which can't be voiced alone).
IV = lambda s, r, n="": {"symbol": s, "roman": r, "audio": s, "note": n, "kind": "vowel"}
IC = lambda s, r, n="": {"symbol": s, "roman": r, "audio": s, "note": n, "kind": "consonant"}
IM = lambda s, r, demo, n="": {"symbol": s, "roman": r, "audio": demo, "note": n, "kind": "matra"}


def _blend_build(target: str, cons_syms: list[str], matra_syms: list[str], lang: str) -> dict:
    """Abugida syllable-assembly: tap a consonant then a vowel-sign; they
    CONCATENATE (compose='concat' on the client) into the akshara. Graded by
    string equality against `target`."""
    return {
        "type": "block_build", "compose": "concat",
        "instruction": "Build the syllable you hear", "hide_roman": True,
        "audio": target, "roman": _romanize(target, lang), "target": target,
        "consonants": cons_syms, "vowels": matra_syms,
    }


def _build_abugida_lesson_content(lesson: dict, taught: list[dict], lang: str) -> dict:
    """Build the {segments:[...]} content for one abugida Foundations lesson."""
    ltype = lesson["type"]
    taught_romans = [g["roman"] for g in taught]
    taught_cons = [g for g in taught if g["kind"] == "consonant"]

    if ltype == "info":
        return {"segments": [{"teach": {"intro": lesson["intro"], "items": []}, "exercises": []}]}

    if ltype == "graphemes":
        graphemes = lesson["graphemes"]
        roman_pool = [g["roman"] for g in graphemes] + taught_romans
        exs = [_grapheme_to_sound(g, roman_pool) for g in graphemes]
        if len(graphemes) >= 3:
            exs.append(_grapheme_match(graphemes))
        random.shuffle(exs)
        teach = {"intro": lesson.get("intro", ""), "items": [_teach_item_grapheme(g) for g in graphemes]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    if ltype == "matras":
        # Teach vowel-signs by blending them onto a known consonant.
        matras = lesson["graphemes"]
        cons_pool = taught_cons or [IC("क", "ka")]
        cons_syms = [c["symbol"] for c in cons_pool]
        matra_syms = [m["symbol"] for m in matras]
        roman_pool = [m["roman"] for m in matras] + taught_romans
        exs = [_grapheme_to_sound(m, roman_pool) for m in matras]
        # A blend per matra (consonant + that sign) + a couple of bare-consonant
        # (inherent-vowel) syllables for contrast.
        base = cons_pool[0]["symbol"]
        for m in matras:
            exs.append(_blend_build(base + m["symbol"], cons_syms, matra_syms, lang))
        for c in cons_pool[:2]:
            exs.append(_blend_build(c["symbol"], cons_syms, matra_syms, lang))
        random.shuffle(exs)
        teach = {"intro": lesson.get("intro", ""),
                 "items": [{"target": base + m["symbol"], "target_roman": _romanize(base + m["symbol"], lang),
                            "gloss": f"{base} + {m['symbol']} = “{m['roman']}”", "note": m.get("note", ""),
                            "audio": base + m["symbol"]} for m in matras]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    if ltype == "words":
        known = {g["symbol"] for g in taught}
        valid = [(w, m) for (w, m) in lesson["words"] if decompose_indic(w) <= known][:6]
        word_pool = [w for w, _ in valid]
        roman_pool = [_romanize(w, lang) for w in word_pool]
        exs = [_read_word(w, roman_pool, m, lang) for w, m in valid]
        for w, _ in valid[:3]:
            exs.append(_listen_word(w, word_pool, lang))
        random.shuffle(exs)
        teach = {"intro": "Words you can now read with the letters you know:",
                 "items": [_teach_item_word(w, m, lang) for w, m in valid]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    return {"segments": [{"teach": {"intro": "", "items": []}, "exercises": []}]}


# ── Dispatch + build ──────────────────────────────────────────────────────────

def _lesson_taught_graphemes(lesson: dict) -> list[dict]:
    """Graphemes a lesson adds to the learner's known set (for word validation).
    Both `graphemes` and `matras` lessons carry a `graphemes` list."""
    if lesson["type"] in ("graphemes", "matras"):
        return lesson.get("graphemes", [])
    return []


def _build_lesson_content(lesson: dict, taught: list[dict], script_type: str, lang: str) -> dict:
    if script_type == "abugida":
        return _build_abugida_lesson_content(lesson, taught, lang)
    return _build_hangul_lesson_content(lesson, taught)


def build_units(lang: str) -> list[dict]:
    """Return Foundations units (with lessons carrying pre-built `content`) to be
    prepended to a course for `lang`. Empty list if the language has no track."""
    track = FOUNDATIONS.get(lang)
    if not track:
        return []
    script_type = track.get("script_type", "alphabetic")
    taught: list[dict] = []
    units = []
    for u in track["units"]:
        lessons = []
        for lsn in u["lessons"]:
            content = _build_lesson_content(lsn, list(taught), script_type, lang)
            lessons.append({"title": lsn["title"], "objective": "", "content": content})
            taught += _lesson_taught_graphemes(lsn)
        units.append({"title": u["title"], "theme": "foundations",
                      "objective": u.get("objective", ""), "lessons": lessons})
    return units


# ── Hindi / Devanagari track (abugida) ────────────────────────────────────────
_HI_VOWELS = [IV("अ", "a"), IV("आ", "aa"), IV("इ", "i"), IV("ई", "ii"),
              IV("उ", "u"), IV("ऊ", "uu"), IV("ए", "e"), IV("ओ", "o")]
_HI_CONS_1 = [IC("क", "ka"), IC("ग", "ga"), IC("न", "na"),
              IC("म", "ma"), IC("र", "ra"), IC("ल", "la")]
_HI_CONS_2 = [IC("त", "ta"), IC("द", "da"), IC("प", "pa"),
              IC("ब", "ba"), IC("स", "sa"), IC("ह", "ha")]
_HI_CONS_3 = [IC("च", "cha"), IC("ज", "ja"), IC("य", "ya"),
              IC("व", "va"), IC("श", "sha"), IC("ट", "ṭa")]
# Matras (vowel signs). The inherent vowel is 'a'; a matra replaces it.
_HI_MATRAS = [IM("ा", "aa", "का"), IM("ि", "i", "कि"), IM("ी", "ii", "की"),
              IM("ु", "u", "कु"), IM("ू", "uu", "कू"), IM("े", "e", "के"), IM("ो", "o", "को")]

_HI_WORDS_1 = [("कल", "yesterday/tomorrow"), ("नल", "tap"), ("मन", "mind"),
               ("कम", "less"), ("गरम", "warm"), ("नरम", "soft"), ("कमल", "lotus"), ("मगर", "but")]
_HI_WORDS_2 = [("नाम", "name"), ("काम", "work"), ("पानी", "water"), ("दिन", "day"),
               ("रात", "night"), ("नाक", "nose"), ("माला", "garland"), ("बस", "bus")]
_HI_WORDS_3 = [("चाय", "tea"), ("जल", "water"), ("वन", "forest"),
               ("शाम", "evening"), ("समय", "time"), ("नया", "new")]

_DEVANAGARI_TRACK = {
    "script_type": "abugida",
    "title": "Read Hindi",
    "units": [
        {"title": "How Devanagari Works", "objective": "How the script works + the vowels", "lessons": [
            {"title": "How Devanagari Works", "type": "info",
             "intro": "Hindi is written in Devanagari — an abugida. Each consonant carries a "
                      "built-in “a” sound (क = “ka”). A vowel SIGN (matra) attached to a consonant "
                      "changes that vowel; full vowel LETTERS are used at the start of a word. It's "
                      "very regular — learn the letters and you can read almost anything."},
            {"title": "Vowels", "type": "graphemes", "graphemes": _HI_VOWELS,
             "intro": "These are the independent vowel letters (used at the start of a word)."},
        ]},
        {"title": "First Consonants", "objective": "Core consonants and your first words", "lessons": [
            {"title": "Consonants क ग न म र ल", "type": "graphemes", "graphemes": _HI_CONS_1,
             "intro": "Each consonant already includes an “a”: क = “ka”, न = “na”. Read them aloud."},
            {"title": "Your First Words", "type": "words", "words": _HI_WORDS_1},
            {"title": "Consonants त द प ब स ह", "type": "graphemes", "graphemes": _HI_CONS_2},
        ]},
        {"title": "Vowel Signs", "objective": "Matras change a consonant's vowel", "lessons": [
            {"title": "Matras (Vowel Signs)", "type": "matras", "graphemes": _HI_MATRAS,
             "intro": "A matra attaches to a consonant and replaces its built-in “a”. "
                      "क + ा → का (kaa), क + ि → कि (ki). Build a few."},
            {"title": "More Words", "type": "words", "words": _HI_WORDS_2},
        ]},
        {"title": "More Letters", "objective": "The rest of the core consonants", "lessons": [
            {"title": "Consonants च ज य व श ट", "type": "graphemes", "graphemes": _HI_CONS_3},
            {"title": "Reading Practice", "type": "words", "words": _HI_WORDS_3},
        ]},
    ],
}


# ── Telugu track (abugida) ────────────────────────────────────────────────────
_TE_VOWELS = [IV("అ", "a"), IV("ఆ", "aa"), IV("ఇ", "i"), IV("ఈ", "ii"),
              IV("ఉ", "u"), IV("ఊ", "uu"), IV("ఎ", "e"), IV("ఒ", "o")]
# TTS workaround: the Telugu neural voice barely articulates a BARE independent
# vowel (mean ≈ −40 dB — effectively silent), while consonants/syllables are fine.
# Appending a visarga (ః, a soft /h/ release) makes the clip audible (≈ −5 dB) and
# the pronunciation is still essentially the pure vowel ("a" → "ah"). Display +
# romanisation stay the bare letter; only the spoken `audio` changes.
_TE_VISARGA = "ః"
for _tv in _TE_VOWELS:
    _tv["audio"] = _tv["symbol"] + _TE_VISARGA
_TE_CONS_1 = [IC("క", "ka"), IC("గ", "ga"), IC("న", "na"),
              IC("మ", "ma"), IC("ర", "ra"), IC("ల", "la")]
_TE_CONS_2 = [IC("త", "ta"), IC("ద", "da"), IC("ప", "pa"),
              IC("బ", "ba"), IC("స", "sa"), IC("హ", "ha")]
_TE_MATRAS = [IM("ా", "aa", "కా"), IM("ి", "i", "కి"), IM("ీ", "ii", "కీ"),
              IM("ు", "u", "కు"), IM("ూ", "uu", "కూ"), IM("ె", "e", "కె"), IM("ొ", "o", "కొ")]

_TE_WORDS_1 = [("కమల", "lotus"), ("మన", "our")]
_TE_WORDS_2 = [("నీరు", "water"), ("పులి", "tiger"), ("కాకి", "crow"),
               ("పాము", "snake"), ("మామ", "uncle"), ("కమల", "lotus")]

_TELUGU_TRACK = {
    "script_type": "abugida",
    "title": "Read Telugu",
    "units": [
        {"title": "How Telugu Works", "objective": "How the script works + the vowels", "lessons": [
            {"title": "How Telugu Works", "type": "info",
             "intro": "Telugu is written in its own script — an abugida. Each consonant carries a "
                      "built-in “a” sound (క = “ka”). A vowel SIGN attached to a consonant changes "
                      "that vowel; full vowel LETTERS appear at the start of a word."},
            {"title": "Vowels", "type": "graphemes", "graphemes": _TE_VOWELS,
             "intro": "These are the independent vowel letters."},
        ]},
        {"title": "First Consonants", "objective": "Core consonants", "lessons": [
            {"title": "Consonants క గ న మ ర ల", "type": "graphemes", "graphemes": _TE_CONS_1,
             "intro": "Each consonant already includes an “a”: క = “ka”, న = “na”."},
            {"title": "Your First Words", "type": "words", "words": _TE_WORDS_1},
            {"title": "Consonants త ద ప బ స హ", "type": "graphemes", "graphemes": _TE_CONS_2},
        ]},
        {"title": "Vowel Signs", "objective": "Signs change a consonant's vowel", "lessons": [
            {"title": "Vowel Signs", "type": "matras", "graphemes": _TE_MATRAS,
             "intro": "A vowel sign attaches to a consonant and replaces its built-in “a”. "
                      "క + ా → కా (kaa), క + ి → కి (ki). Build a few."},
            {"title": "Reading Practice", "type": "words", "words": _TE_WORDS_2},
        ]},
    ],
}

FOUNDATIONS["hi"] = _DEVANAGARI_TRACK
FOUNDATIONS["te"] = _TELUGU_TRACK
