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
    if script_type == "tonal":
        return _build_tonal_lesson_content(lesson, lang)
    if script_type == "simple_alphabet":
        return _build_simple_alphabet_lesson_content(lesson, taught, lang)
    if script_type == "abjad":
        return _build_abjad_lesson_content(lesson, taught, lang)
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


# ── Tonal engine (Cantonese yue / Mandarin cmn) ───────────────────────────────
# Teaches the PHONOLOGICAL system: syllable structure, tones, initials, finals.
# No character literacy is assumed — romanisation (jyutping / pinyin) is the
# notation being learned. Native characters appear only as audio/prompt sources.
# hide_roman on exercises prevents inline ruby (romanisation IS what's tested).

# Data constructors ─────────────────────────────────────────────────────────────
# tone definition: number, description, Chao contour string
TN = lambda n, desc, contour: {"number": n, "desc": desc, "contour": contour}
# tone pair word: char, romanisation, audio text for TTS, English meaning, tone#
TP = lambda char, roman, audio, meaning, tone_num: {
    "char": char, "roman": roman, "audio": audio, "meaning": meaning, "tone_num": tone_num}
# initial: symbol label, representative char, romanisation, audio, meaning, note
TI = lambda sym, char, roman, audio, meaning, note="": {
    "sym": sym, "char": char, "roman": roman, "audio": audio, "meaning": meaning, "note": note}
# final: char, romanisation, audio, meaning, note
TF = lambda char, roman, audio, meaning, note="": {
    "char": char, "roman": roman, "audio": audio, "meaning": meaning, "note": note}


# ── Cantonese (yue) tonal data ─────────────────────────────────────────────────
_YUE_TONE_DEFS = [
    TN(1, "high level",  "55"),
    TN(2, "high rising", "25"),
    TN(3, "mid level",   "33"),
    TN(4, "low falling", "21"),
    TN(5, "low rising",  "23"),
    TN(6, "low level",   "22"),
]

# Minimal set 1: si1–si6 (詩·史·試·時·市·事)
_YUE_PAIRS_SI = [
    TP("詩", "si1", "詩", "poem",     1),
    TP("史", "si2", "史", "history",  2),
    TP("試", "si3", "試", "to try",   3),
    TP("時", "si4", "時", "time",     4),
    TP("市", "si5", "市", "market",   5),
    TP("事", "si6", "事", "matter",   6),
]

# Minimal set 2: fu1–fu6 (夫·虎·副·扶·婦·父)
_YUE_PAIRS_FU = [
    TP("夫", "fu1", "夫", "husband",    1),
    TP("虎", "fu2", "虎", "tiger",      2),
    TP("副", "fu3", "副", "deputy",     3),
    TP("扶", "fu4", "扶", "to support", 4),
    TP("婦", "fu5", "婦", "woman",      5),
    TP("父", "fu6", "父", "father",     6),
]

_YUE_INITIALS = [
    TI("ng-", "我",  "ngo5",   "我",  "I / me",        "Like 'ng' in 'sing' — but at the START of a syllable"),
    TI("gw-", "瓜",  "gwaa1",  "瓜",  "melon",          "'g' with rounded lips"),
    TI("kw-", "礦",  "kwong3", "礦",  "mine / mineral", "Aspirated rounded 'k'"),
    TI("j-",  "一",  "jat1",   "一",  "one",            "Like English 'y' — NOT French 'j'"),
    TI("z-",  "字",  "zi6",    "字",  "character",      "A buzzy 'dz' sound"),
    TI("c-",  "次",  "ci3",    "次",  "time / occasion","Aspirated 'ts'"),
]

_YUE_FINALS = [
    TF("啊",  "aa3",    "啊",   "ah! (exclamation)",    "Long 'aa' — like 'father'"),
    TF("飛",  "fei1",   "飛",   "to fly",               "-ei diphthong"),
    TF("好",  "hou2",   "好",   "good",                 "-ou diphthong — like 'low'"),
    TF("香",  "hoeng1", "香",   "fragrant",             "'oe' + -ng — a rounded 'e'"),
    TF("入",  "jap6",   "入",   "to enter",             "Final -p (unreleased stop)"),
    TF("八",  "baat3",  "八",   "eight",                "Final -t (unreleased stop)"),
    TF("北",  "bak1",   "北",   "north",                "Final -k (unreleased stop)"),
    TF("心",  "sam1",   "心",   "heart",                "Final -m"),
    TF("安",  "on1",    "安",   "peace",                "Final -n"),
    TF("生",  "saang1", "生",   "born / raw",           "Long 'aa' + final -ng"),
]

_CANTONESE_TRACK = {
    "script_type": "tonal",
    "title": "Cantonese Sounds",
    "units": [
        {"title": "How Cantonese Sounds Work",
         "objective": "Syllable structure and the jyutping romanisation system",
         "lessons": [
            {"title": "Syllables & Jyutping", "type": "info",
             "intro": (
                 "Cantonese syllables follow a fixed pattern: INITIAL + FINAL + TONE. "
                 "The INITIAL is an opening consonant (optional). The FINAL is a vowel "
                 "core plus an optional coda (nasal or unreleased stop). The TONE is one "
                 "of SIX distinct pitches — they change meaning entirely:\n\n"
                 "詩·史·試·時·市·事 are all 'si', differing only in tone.\n\n"
                 "Jyutping romanises Cantonese with a digit at the end for the tone: "
                 "si1 = poem, si6 = matter. You'll train your ear on all six tones first."
             )},
         ]},
        {"title": "The Six Tones",
         "objective": "Hear and distinguish all six Cantonese tones",
         "lessons": [
            {"title": "Tones 1, 2 & 3", "type": "tones",
             "tone_defs": _YUE_TONE_DEFS[:3],
             "pairs": _YUE_PAIRS_SI[:3],
             "intro": (
                 "The high and mid tones — differ in movement:\n"
                 "• Tone 1: stays high and flat — 詩 si1 (poem)\n"
                 "• Tone 2: rises — 史 si2 (history)\n"
                 "• Tone 3: stays mid and flat — 試 si3 (to try)"
             )},
            {"title": "Tones 4, 5 & 6", "type": "tones",
             "tone_defs": _YUE_TONE_DEFS[3:],
             "pairs": _YUE_PAIRS_SI[3:],
             "intro": (
                 "The low tones — all start low, but move differently:\n"
                 "• Tone 4: falls — 時 si4 (time)\n"
                 "• Tone 5: rises from low — 市 si5 (market)\n"
                 "• Tone 6: stays low and flat — 事 si6 (matter)"
             )},
            {"title": "All Six Tones", "type": "tones",
             "tone_defs": _YUE_TONE_DEFS,
             "pairs": _YUE_PAIRS_FU,
             "intro": (
                 "Consolidate all six tones on a fresh minimal set: "
                 "夫·虎·副·扶·婦·父 (fu1–fu6). "
                 "Listen for the overall pitch shape — high/mid vs low, flat vs moving."
             )},
         ]},
        {"title": "Key Sounds",
         "objective": "Initials and finals that are new for English speakers",
         "lessons": [
            {"title": "Tricky Initials", "type": "initials",
             "initials": _YUE_INITIALS,
             "intro": (
                 "Most initials map to familiar English sounds. These six need extra attention:\n"
                 "• ng- opens a syllable (in English it only ends words)\n"
                 "• gw- and kw- are labialised velars (rounded lips + back of tongue)\n"
                 "• j- sounds like English 'y' (not French 'j')\n"
                 "• z- and c- are affricate sounds ('dz' and aspirated 'ts')"
             )},
            {"title": "Finals & Endings", "type": "finals",
             "finals": _YUE_FINALS,
             "intro": (
                 "Cantonese finals are rich: long vowels (aa), diphthongs (ei, ou), the "
                 "rounded vowel oe, and three unreleased final stops (-p, -t, -k) that "
                 "cut a syllable short. There are also three nasal endings: -m, -n, -ng."
             )},
         ]},
    ],
}


# ── Mandarin (cmn) tonal data ──────────────────────────────────────────────────
_CMN_TONE_DEFS = [
    TN(1, "flat (high)", "55"),
    TN(2, "rising",      "35"),
    TN(3, "dipping",     "214"),
    TN(4, "falling",     "51"),
]

# Minimal set 1: mā·má·mǎ·mà (妈·麻·马·骂)
_CMN_PAIRS_MA = [
    TP("妈", "mā",  "妈", "mom",      1),
    TP("麻", "má",  "麻", "hemp",     2),
    TP("马", "mǎ",  "马", "horse",    3),
    TP("骂", "mà",  "骂", "to scold", 4),
]

# Minimal set 2: tāng·táng·tǎng·tàng (汤·糖·躺·烫)
_CMN_PAIRS_TANG = [
    TP("汤", "tāng", "汤", "soup",         1),
    TP("糖", "táng", "糖", "sugar / candy", 2),
    TP("躺", "tǎng", "躺", "to lie down",  3),
    TP("烫", "tàng", "烫", "scalding hot", 4),
]

_CMN_INITIALS = [
    TI("zh", "找", "zhǎo", "找", "to look for",  "Retroflex — tongue curled back (hard 'j')"),
    TI("z",  "走", "zǒu",  "走", "to walk",      "NOT retroflex — flat 'dz'"),
    TI("ch", "吃", "chī",  "吃", "to eat",       "Retroflex 'ch' — tongue curled back"),
    TI("sh", "是", "shì",  "是", "to be",        "Retroflex 'sh' — tongue curled back"),
    TI("r",  "人", "rén",  "人", "person",       "Retroflex — a buzzy, curled 'r'"),
    TI("x",  "小", "xiǎo", "小", "small",        "Palatal 'sh' — tongue forward"),
    TI("q",  "七", "qī",   "七", "seven",        "Aspirated palatal 'ch' — tongue forward"),
    TI("j",  "家", "jiā",  "家", "home / family","Unaspirated palatal — like 'j' in 'jeep'"),
]

_CMN_FINALS = [
    TF("鱼", "yú",    "鱼",  "fish",          "Rounded front vowel — like French 'u'"),
    TF("哦", "ò",     "哦",  "oh (particle)", "Pure round 'o'"),
    TF("爱", "ài",    "爱",  "love",          "-ai diphthong"),
    TF("好", "hǎo",   "好",  "good",          "-ao diphthong — like 'how'"),
    TF("安", "ān",    "安",  "peace",         "Final -n"),
    TF("生", "shēng", "生",  "born / raw",    "Final -ng"),
    TF("儿", "ér",    "儿",  "child",         "Rhotacized 'er' — like 'her'"),
]

_MANDARIN_TRACK = {
    "script_type": "tonal",
    "title": "Mandarin Sounds",
    "units": [
        {"title": "How Mandarin Sounds Work",
         "objective": "Syllable structure and the pinyin romanisation system",
         "lessons": [
            {"title": "Syllables & Pinyin", "type": "info",
             "intro": (
                 "Mandarin syllables follow the pattern: INITIAL + FINAL + TONE. "
                 "There are four tones (plus an unstressed neutral tone on some particles):\n\n"
                 "• mā (Tone 1): high and flat\n"
                 "• má (Tone 2): rises\n"
                 "• mǎ (Tone 3): dips then rises\n"
                 "• mà (Tone 4): falls sharply\n\n"
                 "妈·麻·马·骂 are all 'ma' — only the tone distinguishes them. "
                 "Pinyin puts tone marks over the vowel (ā á ǎ à)."
             )},
         ]},
        {"title": "The Four Tones",
         "objective": "Recognise and distinguish all four Mandarin tones",
         "lessons": [
            {"title": "Tones 1 & 2", "type": "tones",
             "tone_defs": _CMN_TONE_DEFS[:2],
             "pairs": [_CMN_PAIRS_MA[0], _CMN_PAIRS_MA[1],
                       _CMN_PAIRS_TANG[0], _CMN_PAIRS_TANG[1]],
             "intro": (
                 "• Tone 1: stay HIGH and flat — mā 妈 (mom), tāng 汤 (soup)\n"
                 "• Tone 2: RISE — má 麻 (hemp), táng 糖 (sugar)"
             )},
            {"title": "Tones 3 & 4", "type": "tones",
             "tone_defs": _CMN_TONE_DEFS[2:],
             "pairs": [_CMN_PAIRS_MA[2], _CMN_PAIRS_MA[3],
                       _CMN_PAIRS_TANG[2], _CMN_PAIRS_TANG[3]],
             "intro": (
                 "• Tone 3: DIP down then up — mǎ 马 (horse), tǎng 躺 (lie down)\n"
                 "• Tone 4: FALL sharply — mà 骂 (to scold), tàng 烫 (scalding)"
             )},
            {"title": "All Four Tones", "type": "tones",
             "tone_defs": _CMN_TONE_DEFS,
             "pairs": _CMN_PAIRS_MA + _CMN_PAIRS_TANG,
             "intro": (
                 "Distinguish all four tones across both sets — 妈·麻·马·骂 and 汤·糖·躺·烫. "
                 "Focus on the overall pitch shape: flat high / rising / dip-rise / sharp fall."
             )},
         ]},
        {"title": "Key Sounds",
         "objective": "Initials and finals that differ from English",
         "lessons": [
            {"title": "Retroflex & Palatal Initials", "type": "initials",
             "initials": _CMN_INITIALS,
             "intro": (
                 "Mandarin has three sibilant series — each distinct:\n"
                 "• zh / ch / sh / r: RETROFLEX — tongue curled back\n"
                 "• z / c / s: flat sibilants — tongue flat, no curl\n"
                 "• j / q / x: PALATAL — tongue near the hard palate, lips spread\n"
                 "These are often confused by learners. Drill the contrasts carefully."
             )},
            {"title": "Key Finals", "type": "finals",
             "finals": _CMN_FINALS,
             "intro": (
                 "Key Mandarin finals to notice: ü (written 'u' after j/q/x/y) is a "
                 "rounded front vowel like French 'u'. The -n vs -ng distinction is important. "
                 "The 'er' final is rhotacized — unique to Mandarin."
             )},
         ]},
    ],
}


# ── Tonal lesson builder ──────────────────────────────────────────────────────

def _tone_label(t: dict) -> str:
    return f"Tone {t['number']} — {t['desc']}"


def _build_tonal_lesson_content(lesson: dict, lang: str) -> dict:
    """Build {segments:[...]} content for a tonal-script-type Foundations lesson."""
    ltype = lesson["type"]

    if ltype == "info":
        return {"segments": [{"teach": {"intro": lesson["intro"], "items": []}, "exercises": []}]}

    if ltype == "tones":
        tone_defs = lesson["tone_defs"]
        pairs     = lesson["pairs"]
        labels    = [_tone_label(t) for t in tone_defs]
        roman_pool = [p["roman"] for p in pairs]

        exs = []
        # hear audio → pick tone name
        for p in pairs:
            t_idx = next(i for i, t in enumerate(tone_defs) if t["number"] == p["tone_num"])
            opts, ans = _options(labels[t_idx], labels)
            exs.append({
                "type": "choice", "instruction": "What tone do you hear?",
                "audio": p["audio"], "hide_roman": True,
                "options": opts, "answer": ans,
                "tip": f"{p['roman']} — {p['meaning']}",
            })
        # hear audio → pick correct romanisation
        for p in pairs:
            opts, ans = _options(p["roman"], roman_pool)
            exs.append({
                "type": "choice", "instruction": "Which romanisation matches what you hear?",
                "audio": p["audio"], "hide_roman": True,
                "options": opts, "answer": ans,
                "tip": p["meaning"],
            })

        random.shuffle(exs)
        tone_desc = {t["number"]: _tone_label(t) for t in tone_defs}
        teach = {
            "intro": lesson.get("intro", ""),
            "items": [{"target": p["char"], "target_roman": p["roman"],
                       "gloss": p["meaning"], "note": tone_desc.get(p["tone_num"], ""),
                       "audio": p["audio"]} for p in pairs],
        }
        return {"segments": [{"teach": teach, "exercises": exs}]}

    if ltype == "initials":
        items    = lesson["initials"]
        sym_pool = list(dict.fromkeys(i["sym"] for i in items))
        roman_pool = [i["roman"] for i in items]

        exs = []
        for item in items:
            opts, ans = _options(item["sym"], sym_pool)
            exs.append({
                "type": "choice", "instruction": "What initial consonant do you hear?",
                "audio": item["audio"], "hide_roman": True,
                "options": opts, "answer": ans,
                "tip": f"{item['roman']} — {item['meaning']}",
            })
        for item in items:
            opts, ans = _options(item["roman"], roman_pool)
            exs.append({
                "type": "choice", "instruction": "Which syllable did you hear?",
                "audio": item["audio"], "hide_roman": True,
                "options": opts, "answer": ans,
                "tip": item["meaning"],
            })

        random.shuffle(exs)
        teach = {
            "intro": lesson.get("intro", ""),
            "items": [{"target": i["char"], "target_roman": i["roman"],
                       "gloss": i["meaning"], "note": i.get("note", ""),
                       "audio": i["audio"]} for i in items],
        }
        return {"segments": [{"teach": teach, "exercises": exs}]}

    if ltype == "finals":
        items      = lesson["finals"]
        roman_pool = [f["roman"] for f in items]
        char_pool  = [f["char"]  for f in items]

        exs = []
        for item in items:
            opts, ans = _options(item["roman"], roman_pool)
            exs.append({
                "type": "choice", "instruction": "Which romanisation matches what you hear?",
                "audio": item["audio"], "hide_roman": True,
                "options": opts, "answer": ans,
                "tip": item.get("note", item["meaning"]),
            })
        for item in items[:5]:
            opts, ans = _options(item["char"], char_pool)
            exs.append({
                "type": "listening", "instruction": "What did you hear?", "hide_roman": True,
                "audio": item["audio"], "audio_roman": item["roman"],
                "options": opts, "options_roman": ["" for _ in opts], "answer": ans,
            })

        random.shuffle(exs)
        teach = {
            "intro": lesson.get("intro", ""),
            "items": [{"target": f["char"], "target_roman": f["roman"],
                       "gloss": f["meaning"], "note": f.get("note", ""),
                       "audio": f["audio"]} for f in items],
        }
        return {"segments": [{"teach": teach, "exercises": exs}]}

    return {"segments": [{"teach": {"intro": "", "items": []}, "exercises": []}]}


FOUNDATIONS["yue"] = _CANTONESE_TRACK
FOUNDATIONS["cmn"] = _MANDARIN_TRACK


# ── Simple alphabet engine (Cyrillic, kana, …) ──────────────────────────────
# For scripts where each character maps to a sound and words are just sequences
# of characters — no composition math (unlike Hangul), no matras (unlike abugida).
# Decomposition = set of characters. Exercise types: graphemes, words.

def decompose_simple(text: str) -> set[str]:
    return {ch for ch in text if not ch.isspace()}


SL = lambda s, r, a=None, n="": {"symbol": s, "roman": r, "audio": a or s, "note": n, "kind": "letter"}


def _build_simple_alphabet_lesson_content(lesson: dict, taught: list[dict], lang: str) -> dict:
    ltype = lesson["type"]
    taught_romans = [g["roman"] for g in taught]

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

    if ltype == "words":
        known = {g["symbol"] for g in taught}
        valid = [(w, m) for (w, m) in lesson["words"] if decompose_simple(w) <= known][:6]
        if not valid:
            valid = lesson["words"][:4]
        word_pool = [w for w, _ in valid]
        roman_pool = [_romanize(w, lang) for w in word_pool]
        exs = [_read_word(w, roman_pool, m, lang) for w, m in valid]
        for w, _ in valid[:3]:
            exs.append(_listen_word(w, word_pool, lang))
        random.shuffle(exs)
        teach = {"intro": lesson.get("intro", "Words you can now read:"),
                 "items": [_teach_item_word(w, m, lang) for w, m in valid]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    return {"segments": [{"teach": {"intro": "", "items": []}, "exercises": []}]}


# ── Russian / Cyrillic track ─────────────────────────────────────────────────
_RU_VOWELS = [
    SL("А", "a"), SL("О", "o"), SL("У", "u"), SL("Э", "e"), SL("И", "i"), SL("Ы", "y", n="No English equivalent — say 'i' with your tongue pulled back"),
]
_RU_CONS_1 = [
    SL("М", "m"), SL("Н", "n"), SL("К", "k"), SL("Т", "t"), SL("Д", "d"), SL("С", "s"),
]
_RU_CONS_2 = [
    SL("Л", "l"), SL("Р", "r"), SL("П", "p"), SL("Б", "b"), SL("В", "v"), SL("Г", "g"),
]
_RU_CONS_3 = [
    SL("Ж", "zh", n="Like 's' in 'pleasure'"), SL("Ш", "sh"), SL("З", "z"),
    SL("Ф", "f"), SL("Х", "kh", n="Like 'ch' in Scottish 'loch'"),
    SL("Ц", "ts"), SL("Ч", "ch"),
]
_RU_SPECIAL = [
    SL("Й", "y", n="Short 'y' — only after vowels"),
    SL("Я", "ya"), SL("Ё", "yo"), SL("Ю", "yu"), SL("Е", "ye"),
    SL("Щ", "shch", n="Like 'sh' + 'ch' run together"),
    SL("Ь", "'", n="Soft sign — softens the previous consonant"),
    SL("Ъ", "", n="Hard sign — prevents softening (rare)"),
]

_RU_WORDS_1 = [("мама", "mom"), ("дом", "house"), ("кот", "cat"),
               ("сон", "dream"), ("нос", "nose"), ("там", "there")]
_RU_WORDS_2 = [("вода", "water"), ("рука", "hand"), ("луна", "moon"),
               ("папа", "dad"), ("губа", "lip"), ("пара", "pair")]
_RU_WORDS_3 = [("жук", "beetle"), ("шум", "noise"), ("час", "hour"),
               ("зуб", "tooth"), ("фон", "background"), ("цвет", "color")]

_CYRILLIC_TRACK = {
    "script_type": "simple_alphabet",
    "title": "Read Cyrillic",
    "units": [
        {"title": "Cyrillic Basics", "objective": "How the script works and first letters", "lessons": [
            {"title": "How Cyrillic Works", "type": "info",
             "intro": "Russian is written in Cyrillic — a true alphabet where each letter "
                      "represents a sound. Some letters look like Latin but sound different "
                      "(Р = 'r', С = 's', Н = 'n'). There are 33 letters total. "
                      "Let's start with the vowels."},
            {"title": "Vowels А О У Э И Ы", "type": "graphemes", "graphemes": _RU_VOWELS,
             "intro": "The six basic vowels. Ы has no English equivalent — "
                      "try saying 'i' with your tongue pulled back."},
        ]},
        {"title": "First Consonants", "objective": "Core consonants and first words", "lessons": [
            {"title": "Consonants М Н К Т Д С", "type": "graphemes", "graphemes": _RU_CONS_1,
             "intro": "These consonants are straightforward — most have English equivalents."},
            {"title": "Your First Words", "type": "words", "words": _RU_WORDS_1},
            {"title": "Consonants Л Р П Б В Г", "type": "graphemes", "graphemes": _RU_CONS_2,
             "intro": "Note: Р is 'r' (not 'p'!), В is 'v' (not 'b'!), Б is 'b'."},
            {"title": "More Words", "type": "words", "words": _RU_WORDS_2},
        ]},
        {"title": "More Consonants", "objective": "The remaining consonants", "lessons": [
            {"title": "Consonants Ж Ш З Ф Х Ц Ч", "type": "graphemes", "graphemes": _RU_CONS_3,
             "intro": "These include sounds less common in English — pay attention to Ж (zh) and Х (kh)."},
            {"title": "Reading Practice", "type": "words", "words": _RU_WORDS_3},
        ]},
        {"title": "Special Letters", "objective": "Soft vowels, signs, and full reading", "lessons": [
            {"title": "Й Я Ё Ю Е Щ Ь Ъ", "type": "graphemes", "graphemes": _RU_SPECIAL,
             "intro": "Я (ya), Ё (yo), Ю (yu), Е (ye) are 'yotated' vowels — they add a 'y' "
                      "before the vowel sound. Ь (soft sign) and Ъ (hard sign) modify pronunciation "
                      "but have no sound of their own."},
        ]},
    ],
}

FOUNDATIONS["ru"] = _CYRILLIC_TRACK


# ── Bengali / Bangla track (abugida) ────────────────────────────────────────
_BN_VOWELS = [IV("অ", "o"), IV("আ", "a"), IV("ই", "i"), IV("ঈ", "ii"),
              IV("উ", "u"), IV("ঊ", "uu"), IV("এ", "e"), IV("ও", "o")]
_BN_CONS_1 = [IC("ক", "ko"), IC("গ", "go"), IC("ন", "no"),
              IC("ম", "mo"), IC("র", "ro"), IC("ল", "lo")]
_BN_CONS_2 = [IC("ত", "to"), IC("দ", "do"), IC("প", "po"),
              IC("ব", "bo"), IC("স", "sho"), IC("হ", "ho")]
_BN_MATRAS = [IM("া", "a", "কা"), IM("ি", "i", "কি"), IM("ী", "ii", "কী"),
              IM("ু", "u", "কু"), IM("ূ", "uu", "কূ"), IM("ে", "e", "কে"), IM("ো", "o", "কো")]

_BN_WORDS_1 = [("কলম", "pen"), ("মন", "mind"), ("নল", "tube"),
               ("কমল", "lotus"), ("মরন", "death")]
_BN_WORDS_2 = [("নাম", "name"), ("কাম", "work"), ("পানি", "water"),
               ("দিন", "day"), ("রাত", "night"), ("বন", "forest")]

_BENGALI_TRACK = {
    "script_type": "abugida",
    "title": "Read Bengali",
    "units": [
        {"title": "How Bengali Script Works", "objective": "How the script works + the vowels", "lessons": [
            {"title": "How Bengali Works", "type": "info",
             "intro": "Bengali is written in its own script — an abugida. Each consonant carries a "
                      "built-in 'o' sound (ক = 'ko'). A vowel SIGN (matra/kar) attached to a consonant "
                      "changes that vowel; full vowel LETTERS are used at the start of a word. "
                      "Learn the letters and you can read almost anything."},
            {"title": "Vowels", "type": "graphemes", "graphemes": _BN_VOWELS,
             "intro": "These are the independent vowel letters (used at the start of a word or standalone)."},
        ]},
        {"title": "First Consonants", "objective": "Core consonants and your first words", "lessons": [
            {"title": "Consonants ক গ ন ম র ল", "type": "graphemes", "graphemes": _BN_CONS_1,
             "intro": "Each consonant already includes an 'o': ক = 'ko', ন = 'no'. Read them aloud."},
            {"title": "Your First Words", "type": "words", "words": _BN_WORDS_1},
            {"title": "Consonants ত দ প ব স হ", "type": "graphemes", "graphemes": _BN_CONS_2},
        ]},
        {"title": "Vowel Signs", "objective": "Matras/Kar change a consonant's vowel", "lessons": [
            {"title": "Vowel Signs (Kar)", "type": "matras", "graphemes": _BN_MATRAS,
             "intro": "A vowel sign (kar) attaches to a consonant and replaces its built-in 'o'. "
                      "ক + া → কা (ka), ক + ি → কি (ki). Build a few."},
            {"title": "More Words", "type": "words", "words": _BN_WORDS_2},
        ]},
    ],
}

FOUNDATIONS["bn"] = _BENGALI_TRACK


# ── Japanese / Kana track (simple_alphabet) ──────────────────────────────────
# Hiragana first (primary syllabary), then katakana (foreign words, emphasis).
# Japanese kana are syllabaries — each symbol = one mora (syllable).
# No composition needed: あ = 'a', か = 'ka', etc.

_JA_HIRA_VOWELS = [
    SL("あ", "a"), SL("い", "i"), SL("う", "u"), SL("え", "e"), SL("お", "o"),
]
_JA_HIRA_K = [SL("か", "ka"), SL("き", "ki"), SL("く", "ku"), SL("け", "ke"), SL("こ", "ko")]
_JA_HIRA_S = [SL("さ", "sa"), SL("し", "shi"), SL("す", "su"), SL("せ", "se"), SL("そ", "so")]
_JA_HIRA_T = [SL("た", "ta"), SL("ち", "chi"), SL("つ", "tsu"), SL("て", "te"), SL("と", "to")]
_JA_HIRA_N = [SL("な", "na"), SL("に", "ni"), SL("ぬ", "nu"), SL("ね", "ne"), SL("の", "no")]
_JA_HIRA_H = [SL("は", "ha"), SL("ひ", "hi"), SL("ふ", "fu"), SL("へ", "he"), SL("ほ", "ho")]
_JA_HIRA_M = [SL("ま", "ma"), SL("み", "mi"), SL("む", "mu"), SL("め", "me"), SL("も", "mo")]
_JA_HIRA_R = [SL("ら", "ra"), SL("り", "ri"), SL("る", "ru"), SL("れ", "re"), SL("ろ", "ro")]
_JA_HIRA_YWN = [SL("や", "ya"), SL("ゆ", "yu"), SL("よ", "yo"),
                SL("わ", "wa"), SL("を", "wo"), SL("ん", "n", n="Standalone nasal — the only kana without a vowel")]

_JA_KATA_VOWELS = [SL("ア", "a"), SL("イ", "i"), SL("ウ", "u"), SL("エ", "e"), SL("オ", "o")]
_JA_KATA_K = [SL("カ", "ka"), SL("キ", "ki"), SL("ク", "ku"), SL("ケ", "ke"), SL("コ", "ko")]
_JA_KATA_S = [SL("サ", "sa"), SL("シ", "shi"), SL("ス", "su"), SL("セ", "se"), SL("ソ", "so")]
_JA_KATA_T = [SL("タ", "ta"), SL("チ", "chi"), SL("ツ", "tsu"), SL("テ", "te"), SL("ト", "to")]
_JA_KATA_N = [SL("ナ", "na"), SL("ニ", "ni"), SL("ヌ", "nu"), SL("ネ", "ne"), SL("ノ", "no")]
_JA_KATA_H = [SL("ハ", "ha"), SL("ヒ", "hi"), SL("フ", "fu"), SL("ヘ", "he"), SL("ホ", "ho")]
_JA_KATA_MR = [SL("マ", "ma"), SL("ミ", "mi"), SL("ム", "mu"), SL("メ", "me"), SL("モ", "mo"),
               SL("ラ", "ra"), SL("リ", "ri"), SL("ル", "ru"), SL("レ", "re"), SL("ロ", "ro")]
_JA_KATA_YWN = [SL("ヤ", "ya"), SL("ユ", "yu"), SL("ヨ", "yo"),
                SL("ワ", "wa"), SL("ヲ", "wo"), SL("ン", "n")]

_JA_HIRA_WORDS_1 = [("あい", "love"), ("かき", "persimmon"), ("いけ", "pond"),
                    ("あき", "autumn"), ("いか", "squid"), ("えき", "station")]
_JA_HIRA_WORDS_2 = [("さけ", "sake"), ("すし", "sushi"), ("した", "below"),
                    ("そと", "outside"), ("つき", "moon"), ("くち", "mouth")]
_JA_HIRA_WORDS_3 = [("はな", "flower"), ("にく", "meat"), ("ほね", "bone"),
                    ("ひと", "person"), ("ねこ", "cat"), ("なに", "what")]
_JA_HIRA_WORDS_4 = [("まち", "town"), ("もも", "peach"), ("むし", "insect"),
                    ("みち", "road"), ("めし", "rice"), ("やま", "mountain")]
_JA_HIRA_WORDS_5 = [("りんご", "apple"), ("よる", "night"), ("わたし", "I/me"),
                    ("れきし", "history"), ("ろく", "six")]

_JA_KATA_WORDS = [("アメリカ", "America"), ("コーヒー", "coffee"), ("テスト", "test"),
                  ("タクシー", "taxi"), ("ホテル", "hotel"), ("メニュー", "menu")]

_HIRAGANA_KATAKANA_TRACK = {
    "script_type": "simple_alphabet",
    "title": "Read Japanese Kana",
    "units": [
        {"title": "Hiragana: Vowels", "objective": "The five vowel sounds and their hiragana", "lessons": [
            {"title": "How Kana Works", "type": "info",
             "intro": "Japanese uses two syllabaries: hiragana (ひらがな) for native words and "
                      "katakana (カタカナ) for foreign/loan words. Each symbol represents one "
                      "mora (syllable). There are only 46 basic characters in each set — "
                      "very regular. Let's start with hiragana."},
            {"title": "Vowels あ い う え お", "type": "graphemes", "graphemes": _JA_HIRA_VOWELS,
             "intro": "The five vowels are the foundation of every kana row. "
                      "Every other hiragana is a consonant + one of these vowels."},
        ]},
        {"title": "Hiragana: K & S rows", "objective": "か行 and さ行", "lessons": [
            {"title": "K-row か き く け こ", "type": "graphemes", "graphemes": _JA_HIRA_K},
            {"title": "First Words", "type": "words", "words": _JA_HIRA_WORDS_1},
            {"title": "S-row さ し す せ そ", "type": "graphemes", "graphemes": _JA_HIRA_S,
             "intro": "Note: し is 'shi' (not 'si') — an irregular romanization."},
            {"title": "T-row た ち つ て と", "type": "graphemes", "graphemes": _JA_HIRA_T,
             "intro": "Note: ち = 'chi' and つ = 'tsu' — irregular romanizations."},
            {"title": "Reading Practice", "type": "words", "words": _JA_HIRA_WORDS_2},
        ]},
        {"title": "Hiragana: N & H rows", "objective": "な行 and は行", "lessons": [
            {"title": "N-row な に ぬ ね の", "type": "graphemes", "graphemes": _JA_HIRA_N},
            {"title": "H-row は ひ ふ へ ほ", "type": "graphemes", "graphemes": _JA_HIRA_H,
             "intro": "Note: ふ is 'fu' (not 'hu') — the lips are rounded but not quite an 'f'."},
            {"title": "More Words", "type": "words", "words": _JA_HIRA_WORDS_3},
        ]},
        {"title": "Hiragana: M & R rows", "objective": "ま行 and ら行", "lessons": [
            {"title": "M-row ま み む め も", "type": "graphemes", "graphemes": _JA_HIRA_M},
            {"title": "R-row ら り る れ ろ", "type": "graphemes", "graphemes": _JA_HIRA_R,
             "intro": "The Japanese 'r' is a light tap — between English 'r', 'l', and 'd'."},
            {"title": "Reading Practice", "type": "words", "words": _JA_HIRA_WORDS_4},
        ]},
        {"title": "Hiragana: Y, W & N", "objective": "The remaining hiragana", "lessons": [
            {"title": "や ゆ よ わ を ん", "type": "graphemes", "graphemes": _JA_HIRA_YWN,
             "intro": "The final hiragana group. ん is special — it's the only kana without a vowel (a standalone nasal). "
                      "を is the object particle (pronounced 'o' in modern Japanese)."},
            {"title": "Full Hiragana Practice", "type": "words", "words": _JA_HIRA_WORDS_5},
        ]},
        {"title": "Katakana", "objective": "The katakana syllabary for foreign words", "lessons": [
            {"title": "What is Katakana?", "type": "info",
             "intro": "Katakana (カタカナ) is the second syllabary — same sounds as hiragana, "
                      "but different shapes. It's used for foreign loan words, onomatopoeia, "
                      "and emphasis. Each katakana has a hiragana twin: あ = ア, か = カ, etc."},
            {"title": "Vowels ア イ ウ エ オ", "type": "graphemes", "graphemes": _JA_KATA_VOWELS},
            {"title": "K-row カ キ ク ケ コ", "type": "graphemes", "graphemes": _JA_KATA_K},
            {"title": "S-row サ シ ス セ ソ", "type": "graphemes", "graphemes": _JA_KATA_S},
            {"title": "T-row タ チ ツ テ ト", "type": "graphemes", "graphemes": _JA_KATA_T},
            {"title": "N-row ナ ニ ヌ ネ ノ", "type": "graphemes", "graphemes": _JA_KATA_N},
            {"title": "H-row ハ ヒ フ ヘ ホ", "type": "graphemes", "graphemes": _JA_KATA_H},
            {"title": "M & R rows", "type": "graphemes", "graphemes": _JA_KATA_MR},
            {"title": "ヤ ユ ヨ ワ ヲ ン", "type": "graphemes", "graphemes": _JA_KATA_YWN},
            {"title": "Katakana Words", "type": "words", "words": _JA_KATA_WORDS},
        ]},
    ],
}

FOUNDATIONS["ja"] = _HIRAGANA_KATAKANA_TRACK


# ── Abjad engine (Arabic script — ar, ur, fa) ────────────────────────────────
# Arabic script is an abjad: letters represent consonants; short vowels are
# optional diacritics. Letters change shape by position (isolated/initial/medial/
# final). The engine teaches the isolated form + sound, then words.
# Right-to-left rendering is handled by CSS (.script-arabic { direction: rtl }).

AL = lambda s, r, name, a=None, n="": {"symbol": s, "roman": r, "audio": a or s, "note": n, "kind": "letter", "name": name}


def _build_abjad_lesson_content(lesson: dict, taught: list[dict], lang: str) -> dict:
    ltype = lesson["type"]
    taught_romans = [g["roman"] for g in taught]

    if ltype == "info":
        return {"segments": [{"teach": {"intro": lesson["intro"], "items": []}, "exercises": []}]}

    if ltype == "graphemes":
        graphemes = lesson["graphemes"]
        roman_pool = [g["roman"] for g in graphemes] + taught_romans
        exs = [_grapheme_to_sound(g, roman_pool) for g in graphemes]
        if len(graphemes) >= 3:
            exs.append(_grapheme_match(graphemes))
        random.shuffle(exs)
        items = []
        for g in graphemes:
            item = _teach_item_grapheme(g)
            if g.get("name"):
                item["note"] = g["name"] + ((" — " + g["note"]) if g.get("note") else "")
            items.append(item)
        teach = {"intro": lesson.get("intro", ""), "items": items}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    if ltype == "words":
        known = {g["symbol"] for g in taught}
        valid = [(w, m) for (w, m) in lesson["words"] if decompose_simple(w) <= known][:6]
        if not valid:
            valid = lesson["words"][:4]
        word_pool = [w for w, _ in valid]
        roman_pool = [_romanize(w, lang) for w in word_pool]
        exs = [_read_word(w, roman_pool, m, lang) for w, m in valid]
        for w, _ in valid[:3]:
            exs.append(_listen_word(w, word_pool, lang))
        random.shuffle(exs)
        teach = {"intro": lesson.get("intro", "Words you can now read:"),
                 "items": [_teach_item_word(w, m, lang) for w, m in valid]}
        return {"segments": [{"teach": teach, "exercises": exs}]}

    return {"segments": [{"teach": {"intro": "", "items": []}, "exercises": []}]}


# ── Arabic (ar) ──────────────────────────────────────────────────────────────
_AR_LETTERS_1 = [
    AL("ا", "a", "alif", n="A tall vertical stroke — carrier for vowels"),
    AL("ب", "b", "baa"), AL("ت", "t", "taa"), AL("ث", "th", "thaa", n="Like 'th' in 'think'"),
    AL("ن", "n", "nun"), AL("ي", "y", "yaa"),
]
_AR_LETTERS_2 = [
    AL("ج", "j", "jiim"), AL("ح", "ḥ", "haa", n="Deep breathy 'h' from the throat"),
    AL("خ", "kh", "khaa", n="Like 'ch' in Scottish 'loch'"),
    AL("د", "d", "daal"), AL("ذ", "dh", "dhaal", n="Like 'th' in 'this'"),
    AL("ر", "r", "raa"),
]
_AR_LETTERS_3 = [
    AL("س", "s", "siin"), AL("ش", "sh", "shiin"),
    AL("ص", "ṣ", "ṣaad", n="Emphatic 's' — tongue pressed to roof"),
    AL("ض", "ḍ", "ḍaad", n="Emphatic 'd' — unique to Arabic"),
    AL("ز", "z", "zaay"), AL("ع", "'", "ayn", n="A deep guttural vowel — no English equivalent"),
]
_AR_LETTERS_4 = [
    AL("ط", "ṭ", "ṭaa", n="Emphatic 't'"), AL("ظ", "ẓ", "ẓaa", n="Emphatic 'z'"),
    AL("غ", "gh", "ghayn", n="Like French 'r' — gargled"),
    AL("ف", "f", "faa"), AL("ق", "q", "qaaf", n="Deep 'k' from the back of the throat"),
    AL("ك", "k", "kaaf"),
]
_AR_LETTERS_5 = [
    AL("ل", "l", "laam"), AL("م", "m", "miim"),
    AL("ه", "h", "haa", n="Light 'h' — different from ح"),
    AL("و", "w", "waaw"), AL("ة", "a", "taa marbuuta", n="Feminine ending — 't' when linked"),
]

_AR_WORDS_1 = [("باب", "door"), ("بنت", "girl"), ("بيت", "house")]
_AR_WORDS_2 = [("جبل", "mountain"), ("درس", "lesson"), ("رجل", "man")]
_AR_WORDS_3 = [("كتاب", "book"), ("قلم", "pen"), ("فيل", "elephant")]

_ARABIC_TRACK = {
    "script_type": "abjad",
    "title": "Read Arabic",
    "units": [
        {"title": "Arabic Script Basics", "objective": "How Arabic script works", "lessons": [
            {"title": "How Arabic Works", "type": "info",
             "intro": "Arabic is written RIGHT TO LEFT in a connected (cursive) script. "
                      "There are 28 letters, each representing a consonant. Short vowels "
                      "(a, i, u) are usually NOT written — experienced readers infer them from "
                      "context. Letters change shape depending on their position in a word "
                      "(isolated, initial, medial, final), but the core shape is recognizable. "
                      "Let's learn the letters by their isolated forms and sounds."},
            {"title": "Letters ا ب ت ث ن ي", "type": "graphemes", "graphemes": _AR_LETTERS_1,
             "intro": "The first group includes alif (the vowel carrier) and letters with dots below or above."},
        ]},
        {"title": "More Letters", "objective": "Building your Arabic letter inventory", "lessons": [
            {"title": "Letters ج ح خ د ذ ر", "type": "graphemes", "graphemes": _AR_LETTERS_2,
             "intro": "This group includes the throat sounds ح (deep h) and خ (kh), plus the 'th' sound ذ."},
            {"title": "First Words", "type": "words", "words": _AR_WORDS_1},
            {"title": "Letters س ش ص ض ز ع", "type": "graphemes", "graphemes": _AR_LETTERS_3,
             "intro": "The emphatic letters (ص, ض) are pronounced with the tongue pressed to "
                      "the roof of the mouth. ع (ayn) is a uniquely Arabic guttural sound."},
            {"title": "More Words", "type": "words", "words": _AR_WORDS_2},
        ]},
        {"title": "Completing the Alphabet", "objective": "The remaining letters", "lessons": [
            {"title": "Letters ط ظ غ ف ق ك", "type": "graphemes", "graphemes": _AR_LETTERS_4,
             "intro": "More emphatic letters (ط, ظ) and sounds from the back of the throat (غ, ق)."},
            {"title": "Letters ل م ه و ة", "type": "graphemes", "graphemes": _AR_LETTERS_5,
             "intro": "The final group. ة (taa marbuuta) marks feminine nouns — it sounds like 'a' at the end of a word."},
            {"title": "Reading Practice", "type": "words", "words": _AR_WORDS_3},
        ]},
    ],
}

FOUNDATIONS["ar"] = _ARABIC_TRACK


# ── Urdu (ur) — Arabic script + extra letters ────────────────────────────────
# Urdu uses a modified Arabic script (Nastaliq style) with additional letters
# for sounds not in Arabic. The base letters are shared.
_UR_LETTERS_1 = [
    AL("ا", "a", "alif", n="Vowel carrier, like Arabic"),
    AL("ب", "b", "be"), AL("پ", "p", "pe", n="Added for Urdu — 'p' (not in Arabic)"),
    AL("ت", "t", "te"), AL("ٹ", "ṭ", "ṭe", n="Retroflex 't' — tongue curled back"),
    AL("ن", "n", "nun"),
]
_UR_LETTERS_2 = [
    AL("ج", "j", "jiim"), AL("چ", "ch", "che", n="Added for Urdu — 'ch'"),
    AL("د", "d", "daal"), AL("ڈ", "ḍ", "ḍaal", n="Retroflex 'd'"),
    AL("ر", "r", "re"), AL("ڑ", "ṛ", "ṛe", n="Retroflex flap 'r'"),
]
_UR_LETTERS_3 = [
    AL("س", "s", "siin"), AL("ش", "sh", "shiin"),
    AL("ک", "k", "kaaf"), AL("گ", "g", "gaaf", n="Added for Urdu — 'g'"),
    AL("ل", "l", "laam"), AL("م", "m", "miim"),
]
_UR_LETTERS_4 = [
    AL("ھ", "h", "do-chashmi he", n="Aspiration marker — makes consonants breathy"),
    AL("و", "w/o", "waaw"), AL("ی", "y/i", "chhoṭi ye"),
    AL("ے", "e", "baṛi ye", n="Final 'e' sound"),
    AL("ہ", "h", "gol he"), AL("ں", "n", "nun ghunna", n="Nasal 'n'"),
]

_UR_WORDS_1 = [("باب", "chapter"), ("پانی", "water"), ("نام", "name")]
_UR_WORDS_2 = [("کتاب", "book"), ("دن", "day"), ("رات", "night")]

_URDU_TRACK = {
    "script_type": "abjad",
    "title": "Read Urdu",
    "units": [
        {"title": "Urdu Script Basics", "objective": "How Urdu script works", "lessons": [
            {"title": "How Urdu Works", "type": "info",
             "intro": "Urdu uses a modified Arabic script written RIGHT TO LEFT in the Nastaliq "
                      "calligraphic style. It has all 28 Arabic letters plus extra letters for "
                      "sounds specific to Urdu/Hindi (پ p, ٹ ṭ, چ ch, ڈ ḍ, ڑ ṛ, گ g). "
                      "Like Arabic, short vowels are usually not written. "
                      "Let's learn the letters by their sounds."},
            {"title": "Letters ا ب پ ت ٹ ن", "type": "graphemes", "graphemes": _UR_LETTERS_1,
             "intro": "Starting with the basics — note پ (pe) is unique to Urdu, not found in Arabic."},
        ]},
        {"title": "More Letters", "objective": "Building your Urdu letter inventory", "lessons": [
            {"title": "Letters ج چ د ڈ ر ڑ", "type": "graphemes", "graphemes": _UR_LETTERS_2,
             "intro": "This group includes retroflex sounds (ٹ, ڈ, ڑ) from the Indo-Aryan sound system."},
            {"title": "First Words", "type": "words", "words": _UR_WORDS_1},
            {"title": "Letters س ش ک گ ل م", "type": "graphemes", "graphemes": _UR_LETTERS_3},
            {"title": "More Words", "type": "words", "words": _UR_WORDS_2},
        ]},
        {"title": "Special Letters", "objective": "The remaining Urdu letters", "lessons": [
            {"title": "ھ و ی ے ہ ں", "type": "graphemes", "graphemes": _UR_LETTERS_4,
             "intro": "These include the aspiration marker ھ (do-chashmi he), the nasal ں, "
                      "and two forms of 'ye' — ی (chhoṭi ye) and ے (baṛi ye)."},
        ]},
    ],
}

FOUNDATIONS["ur"] = _URDU_TRACK


# ── Farsi / Persian (fa) — Arabic script + extra letters ────────────────────
# Farsi uses Arabic script with 4 extra letters (پ چ ژ گ) and different
# pronunciation for some shared letters.
_FA_LETTERS_1 = [
    AL("ا", "a", "alef", n="Vowel carrier"),
    AL("ب", "b", "be"), AL("پ", "p", "pe", n="Persian addition — 'p'"),
    AL("ت", "t", "te"), AL("ث", "s", "se", n="Pronounced 's' in Persian (not 'th')"),
    AL("ن", "n", "nun"),
]
_FA_LETTERS_2 = [
    AL("ج", "j", "jim"), AL("چ", "ch", "che", n="Persian addition — 'ch'"),
    AL("خ", "kh", "khe", n="Like 'ch' in 'loch'"),
    AL("د", "d", "daal"), AL("ر", "r", "re"),
    AL("ز", "z", "ze"),
]
_FA_LETTERS_3 = [
    AL("س", "s", "sin"), AL("ش", "sh", "shin"),
    AL("ف", "f", "fe"), AL("ق", "gh", "ghaaf", n="In Persian, ق is pronounced 'gh' (not 'q')"),
    AL("ک", "k", "kaaf"), AL("گ", "g", "gaaf", n="Persian addition — 'g'"),
]
_FA_LETTERS_4 = [
    AL("ل", "l", "laam"), AL("م", "m", "mim"),
    AL("ه", "h", "he"), AL("و", "v/u", "vaav", n="'v' as a consonant, 'u/o' as a vowel"),
    AL("ی", "y/i", "ye"), AL("ژ", "zh", "zhe", n="Persian addition — like 's' in 'pleasure'"),
]

_FA_WORDS_1 = [("نان", "bread"), ("آب", "water"), ("نام", "name")]
_FA_WORDS_2 = [("کتاب", "book"), ("گل", "flower"), ("شب", "night")]

_FARSI_TRACK = {
    "script_type": "abjad",
    "title": "Read Farsi",
    "units": [
        {"title": "Persian Script Basics", "objective": "How Persian script works", "lessons": [
            {"title": "How Persian Script Works", "type": "info",
             "intro": "Persian (Farsi) uses a modified Arabic script written RIGHT TO LEFT. "
                      "It adds four letters not in Arabic: پ (p), چ (ch), ژ (zh), گ (g). "
                      "Some Arabic letters are pronounced differently in Persian — for example, "
                      "ث is 's' (not 'th') and ق is 'gh' (not 'q'). "
                      "Like Arabic, short vowels are usually omitted in writing."},
            {"title": "Letters ا ب پ ت ث ن", "type": "graphemes", "graphemes": _FA_LETTERS_1,
             "intro": "The first group — note پ (pe) is unique to Persian, not found in Arabic."},
        ]},
        {"title": "More Letters", "objective": "Building your Persian letter inventory", "lessons": [
            {"title": "Letters ج چ خ د ر ز", "type": "graphemes", "graphemes": _FA_LETTERS_2,
             "intro": "This group includes چ (che, Persian addition) and the throat sound خ (khe)."},
            {"title": "First Words", "type": "words", "words": _FA_WORDS_1},
            {"title": "Letters س ش ف ق ک گ", "type": "graphemes", "graphemes": _FA_LETTERS_3,
             "intro": "Note: ق is pronounced 'gh' in modern Persian, not 'q' as in Arabic."},
            {"title": "More Words", "type": "words", "words": _FA_WORDS_2},
        ]},
        {"title": "Remaining Letters", "objective": "Complete the Persian alphabet", "lessons": [
            {"title": "Letters ل م ه و ی ژ", "type": "graphemes", "graphemes": _FA_LETTERS_4,
             "intro": "The final group. و (vaav) serves double duty as both 'v' (consonant) and "
                      "'u'/'o' (vowel). ژ (zhe) is the rarest Persian addition."},
        ]},
    ],
}

FOUNDATIONS["fa"] = _FARSI_TRACK
