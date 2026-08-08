import re

# Each token: {"text": str, "is_word": bool}
Token = dict

# Matches sentence-ending punctuation that is NOT inside quotes.
# CJK: 。！？; Latin: . ! ?; Devanagari danda: । ॥; also newlines.
_SENTENCE_ENDERS = re.compile(r'[。！？.!?।॥۔؟\n]')

# Punctuation that must never be swallowed inside a word token. Includes the
# middle-dot family (· U+00B7, ・ U+30FB, ‧ U+2027, • U+2022) used to list
# individual syllables (詩·史·試·時·市·事) — splitting on them lets each character
# become its own token and get its own romanization.
_INLINE_PUNCT = re.compile(r'([。！？、，：；…⋯！？「」『』【】《》〈〉·・‧•۔؟،؛\n])')
# Opening/closing quote characters — we avoid splitting mid-quote. Directional
# pairs only: a straight " is the same code point for open and close, so
# depth-tracking it only ever increments and sentence-splitting would stall
# forever inside the first quote. Untracked straight quotes simply don't
# suppress splitting (matching the reader frontend's prior behaviour).
_OPEN_QUOTES = set('“«「『')
_CLOSE_QUOTES = set('”»」』')

# Word characters for alphabetic, space-delimited scripts. Includes Latin (with
# accents + apostrophe), Devanagari and Telugu (consonants AND combining vowel
# marks, which are not .isalpha()), and Hangul (precomposed syllables + Jamo).
# Used to tokenise any non-CJK script where words are separated by spaces.
# Devanagari range deliberately skips U+0964–U+0965 (danda / double danda) so
# those stay as sentence punctuation rather than getting glued onto a word.
_ALPHA = r"a-zA-ZÀ-ÿĀ-ſƀ-ɏḀ-ỿ'ऀ-ॣ०-ॿఀ-౿가-힣ᄀ-ᇿ㄰-㆏ঀ-৿ؐ-ؚؠ-ۓە-ۿﭐ-﷿ﹰ-﻿Ѐ-ӿͰ-Ͽά-ωϐ-Ͽἀ-῾ְ-תก-๎"
_ALPHA_RE = re.compile(rf"[{_ALPHA}]")

# Latin letters proper — Basic Latin, Latin-1/Extended-A/B (accents, ligatures)
# and Latin Extended Additional (Vietnamese). Everything else that is a letter
# belongs to some other script.
_LATIN_LETTER_RE = re.compile(r"[A-Za-zÀ-ɏḀ-ỿ]")


def script_class(text: str) -> str:
    """Which broad script a string is written in: ``"native"`` if it contains any
    non-Latin letter (CJK, kana, Hangul, Devanagari, Telugu, Bengali, Arabic,
    Cyrillic, Greek, Thai, Hebrew…), ``"latin"`` if it has Latin letters only,
    and ``""`` when it has no letters at all (digits/punctuation, or empty) and
    so cannot be judged.

    Deliberately language-agnostic: it answers "is this the target script, or is
    this English?" for every non-Latin language at once, and is a no-op for
    Latin-script targets, where French and English are both ``"latin"`` and no
    amount of inspection can tell an answer from a foil.
    """
    has_latin = False
    for ch in text or "":
        if not ch.isalpha():
            continue
        if _LATIN_LETTER_RE.match(ch):
            has_latin = True
        else:
            return "native"
    return "latin" if has_latin else ""


_AR_CHAR = {
    'ا': 'a', 'ب': 'b', 'ت': 't', 'ث': 'th', 'ج': 'j', 'ح': 'ḥ', 'خ': 'kh',
    'د': 'd', 'ذ': 'dh', 'ر': 'r', 'ز': 'z', 'س': 's', 'ش': 'sh', 'ص': 'ṣ',
    'ض': 'ḍ', 'ط': 'ṭ', 'ظ': 'ẓ', 'ع': "'", 'غ': 'gh', 'ف': 'f', 'ق': 'q',
    'ك': 'k', 'ل': 'l', 'م': 'm', 'ن': 'n', 'ه': 'h', 'و': 'w', 'ي': 'y',
    'ة': 'a', 'ى': 'a', 'ء': "'", 'آ': 'aa', 'أ': 'a', 'إ': 'i', 'ؤ': "'",
    'ئ': "'",
    'َ': 'a', 'ُ': 'u', 'ِ': 'i',
    'ً': 'an', 'ٌ': 'un', 'ٍ': 'in',
    'ّ': '', 'ْ': '',
    'پ': 'p', 'چ': 'ch', 'ژ': 'zh', 'گ': 'g',
    'ک': 'k', 'ی': 'y', 'ں': 'n', 'ے': 'e', 'ۓ': 'e', 'ہ': 'h', 'ھ': 'h',
}


_EL_CHAR = {
    'α': 'a', 'β': 'v', 'γ': 'g', 'δ': 'd', 'ε': 'e', 'ζ': 'z', 'η': 'i',
    'θ': 'th', 'ι': 'i', 'κ': 'k', 'λ': 'l', 'μ': 'm', 'ν': 'n', 'ξ': 'x',
    'ο': 'o', 'π': 'p', 'ρ': 'r', 'σ': 's', 'ς': 's', 'τ': 't', 'υ': 'y',
    'φ': 'f', 'χ': 'ch', 'ψ': 'ps', 'ω': 'o',
    'ά': 'a', 'έ': 'e', 'ή': 'i', 'ί': 'i', 'ό': 'o', 'ύ': 'y', 'ώ': 'o',
    'ϊ': 'i', 'ϋ': 'y', 'ΐ': 'i', 'ΰ': 'y',
}

_HE_CHAR = {
    'א': "'", 'ב': 'v', 'ג': 'g', 'ד': 'd', 'ה': 'h', 'ו': 'v', 'ז': 'z',
    'ח': 'ch', 'ט': 't', 'י': 'y', 'כ': 'kh', 'ך': 'kh', 'ל': 'l', 'מ': 'm',
    'ם': 'm', 'נ': 'n', 'ן': 'n', 'ס': 's', 'ע': "'", 'פ': 'f', 'ף': 'f',
    'צ': 'ts', 'ץ': 'ts', 'ק': 'k', 'ר': 'r', 'ש': 'sh', 'ת': 't',
}


def _arabic_romanize(word: str) -> str:
    out: list[str] = []
    for ch in word:
        if ch in _AR_CHAR:
            out.append(_AR_CHAR[ch])
    return ''.join(out)


def _greek_romanize(word: str) -> str:
    out: list[str] = []
    for ch in word.lower():
        if ch in _EL_CHAR:
            out.append(_EL_CHAR[ch])
    return ''.join(out)


def _hebrew_romanize(word: str) -> str:
    out: list[str] = []
    for ch in word:
        if ch in _HE_CHAR:
            out.append(_HE_CHAR[ch])
    return ''.join(out)


def split_sentences(tokens: list[Token], lang: str | None = None) -> list[str]:
    """Sentence strings — a thin wrapper over :func:`split_token_sentences`."""
    return ["".join(t["text"] for t in g) for g in split_token_sentences(tokens, lang)]


def split_token_sentences(tokens: list[Token], lang: str | None = None) -> list[list[Token]]:
    """Group tokens into sentences (a list of token groups).

    THE single source of truth for sentence boundaries. ``split_sentences``
    (strings) is a thin wrapper over this, and the reader frontend renders the
    token groups it produces (``sentence_groups``) instead of re-deriving
    boundaries in JS — so client and server can never drift (a past class of
    off-by-N bugs where the two splitters disagreed on empty newline sentences,
    Arabic enders, or quote characters).

    Does not split inside quoted speech — a sentence-ender found while inside
    quotes is absorbed into the current buffer rather than flushing it. Each
    sentence is edge-trimmed of whitespace-only tokens and whitespace-only
    groups are dropped, so a newline paragraph break is never its own sentence.

    Thai writes no sentence-ending punctuation and uses spaces, so for ``th`` a
    whitespace run also flushes; Greek uses ';'/U+037E where others use '?'.
    """
    enders = _SENTENCE_ENDERS
    split_on_space = lang == "th"
    if lang == "el":
        enders = re.compile(r'[。！？.!?।॥۔؟;;\n]')

    groups: list[list[Token]] = []
    buf: list[Token] = []
    quote_depth = 0

    def flush() -> None:
        nonlocal buf
        a, b = 0, len(buf)
        while a < b and not buf[a]["is_word"] and buf[a]["text"].strip() == "":
            a += 1
        while b > a and not buf[b - 1]["is_word"] and buf[b - 1]["text"].strip() == "":
            b -= 1
        trimmed = buf[a:b]
        if not trimmed:
            buf = []
            return
        # Strip whitespace at the very edges (a token like '। ' or '. ' carries
        # the ender plus a trailing space) so the joined group text equals the
        # stripped sentence string — keeping split_sentences identical to before
        # and matching the cached/stored sentence_text. Copy the edge token so
        # the shared flat token list is untouched.
        if not trimmed[0]["is_word"]:
            ls = trimmed[0]["text"].lstrip()
            if ls != trimmed[0]["text"]:
                trimmed[0] = {**trimmed[0], "text": ls}
        if not trimmed[-1]["is_word"]:
            rs = trimmed[-1]["text"].rstrip()
            if rs != trimmed[-1]["text"]:
                trimmed[-1] = {**trimmed[-1], "text": rs}
        groups.append(trimmed)
        buf = []

    for tok in tokens:
        text = tok["text"]
        # Track quote depth from non-word tokens (punctuation)
        if not tok["is_word"]:
            for ch in text:
                if ch in _OPEN_QUOTES:
                    quote_depth += 1
                elif ch in _CLOSE_QUOTES:
                    quote_depth = max(0, quote_depth - 1)

        buf.append(tok)

        if not tok["is_word"] and quote_depth == 0:
            is_ender = bool(enders.search(text))
            # Thai: a whitespace run separates sentences (no period/!/? is used).
            if split_on_space and text.strip() == "":
                is_ender = True
            if is_ender:
                # For a numeric period (e.g. "1.5"), don't split.
                if '.' in text and '。' not in text:
                    if re.search(r'\d\.\d', text):
                        continue
                flush()

    flush()
    return groups


def romanize_text(text: str, lang: str) -> str:
    """Romanise a whole target-language string for display hints (jyutping/IAST/…).

    Returns an empty string for Latin-script languages or when no romaniser
    exists. Use this rather than trusting AI-generated romanisation.
    """
    if not text:
        return ""
    words = [t["text"] for t in tokenize(text, lang) if t["is_word"]]
    if not words:
        return ""
    rmap = romanize_words(words, lang)
    if not rmap:
        return ""
    return " ".join(rmap.get(w, w) for w in words).strip()


# ── Thai tonal romanization ─────────────────────────────────────────────────
# ROYIN romanization + tone diacritics (à=low, â=falling, á=high, ǎ=rising).
# Per-syllable tone detection via pythainlp so multi-syllable words are correct.
import re as _re

_TH_TONE_MARKS = {"l": "̀", "f": "̂", "h": "́", "r": "̌"}

# Thai characters that pythainlp's ROYIN engine sometimes fails to romanize,
# leaving them as-is in the output.  Map to their Latin equivalent or empty.
_TH_LEAKED_CHAR: dict[str, str] = {
    "ะ": "",     # ะ sara a — already captured by surrounding romanization
    "ั": "a",    # ั mai han akat → short a
    "ิ": "i",    # ิ sara i
    "ี": "i",    # ี sara ii
    "ึ": "ue",   # ึ sara ue
    "ื": "ue",   # ื sara uee
    "ุ": "u",    # ุ sara u
    "ู": "u",    # ู sara uu
    "็": "o",    # ็ mai taikhu → inherent vowel (leaks in ก็ → ko)
    "่": "",     # ่ mai ek (tone mark — handled by tone_detector)
    "้": "",     # ้ mai tho
    "๊": "",     # ๊ mai tri
    "๋": "",     # ๋ mai chattawa
    "์": "",     # ์ thanthakhat (silencer)
    "ํ": "n",    # ํ nikhahit
}


def _th_clean_rom(rom: str) -> str | None:
    """Replace any leaked Thai characters in romanization output."""
    if not rom:
        return None
    out: list[str] = []
    for ch in rom:
        if "฀" <= ch <= "๿":
            out.append(_TH_LEAKED_CHAR.get(ch, ""))
        else:
            out.append(ch)
    cleaned = "".join(out)
    return cleaned if cleaned else None


def _th_add_tone(rom: str, tone: str) -> str:
    if tone == "m" or tone not in _TH_TONE_MARKS:
        return rom
    m = _re.search(r"[aeiou]", rom)
    if not m:
        return rom
    p = m.end()
    return rom[:p] + _TH_TONE_MARKS[tone] + rom[p:]


def _th_has_thai(s: str) -> bool:
    return any("฀" <= c <= "๿" for c in s)


def _th_base_len(s: str) -> int:
    """Character count ignoring combining diacritics (tone marks)."""
    import unicodedata
    return sum(1 for c in s if unicodedata.category(c) != "Mn")


# Low-class sonorants after which a leading ห/อ is silent (ห นำ / อ นำ),
# e.g. หมา→'ma', หนา→'na'. Before anything else (a vowel or a non-sonorant
# consonant) the ห is a real /h/ initial — e.g. หาร, หาม, หก.
_TH_SONORANTS = set("งญณนมยรลวฬ")


def _th_fix_leading_h(syl: str, rom: str) -> str:
    """Restore the /h/ that pythainlp's royin engine drops on ห-initial syllables.

    royin systematically misreads ห as a silent leading consonant even when it
    is the pronounced initial: หาร→'an', หาม→'am', หก→'ok' (but หา→'ha',
    หาน→'han'). ห is only truly silent before a low sonorant (หมา→'ma'); in
    every other ห-initial syllable it is /h/, so prepend it when royin omitted
    it.
    """
    if syl.startswith("ห") and len(syl) > 1 and rom and not rom.startswith("h"):
        if syl[1] not in _TH_SONORANTS:
            return "h" + rom
    return rom


def _th_tonal_romanize(word, romanize_fn, tone_fn, syl_fn):
    """ROYIN romanization with tone diacritics for a Thai word."""
    syls = None
    if syl_fn:
        # Syllable splitting is best-effort: pythainlp's default engine needs
        # python-crfsuite, which we don't ship. A failure here must degrade to
        # whole-word romanization, not bubble up and blank the ruby.
        try:
            syls = syl_fn(word)
        except Exception:
            syls = None
    if syls and len(syls) > 1:
        syl_parts = []
        for s in syls:
            rom = _th_clean_rom(romanize_fn(s, engine="royin"))
            if rom:
                rom = _th_fix_leading_h(s, rom)
                syl_parts.append(_th_add_tone(rom, tone_fn(s)))
        syl_result = "".join(syl_parts) if syl_parts else None
        whole_raw = romanize_fn(word, engine="royin") or ""
        whole = _th_clean_rom(whole_raw)
        if syl_result and whole and syl_result != whole:
            # Per-syllable gives per-syllable tones (better), but can drop
            # consonants (e.g. หาร→'an'). Whole-word is often more accurate
            # but can drop vowels (e.g. ส้ม→'tm'). Heuristic: a notably
            # shorter syllable result means lost consonants → use whole;
            # otherwise keep syllable for its tones.
            if not _th_has_thai(whole_raw) and _th_base_len(syl_result) < _th_base_len(whole):
                return _th_add_tone(whole, tone_fn(word))
        return syl_result
    rom = _th_clean_rom(romanize_fn(word, engine="royin"))
    if not rom:
        return None
    rom = _th_fix_leading_h(word, rom)
    return _th_add_tone(rom, tone_fn(word))


def romanize_words(words: list[str], lang: str) -> dict[str, str]:
    """Return a mapping of word text → romanization string for the given words.

    Produces output for scripts with an offline romaniser (yue, cmn, ko, hi).
    Returns an empty dict for Latin-script languages or on failure.
    """
    result: dict[str, str] = {}
    if lang == "ko":
        try:
            from korean_romanizer.romanizer import Romanizer
            for word in words:
                if word not in result:
                    rom = Romanizer(word).romanize()
                    if rom:
                        result[word] = rom
        except Exception:
            pass
        return result
    if lang in ("hi", "te", "bn"):
        try:
            from indic_transliteration import sanscript
            src = sanscript.DEVANAGARI if lang == "hi" else (sanscript.BENGALI if lang == "bn" else sanscript.TELUGU)
            for word in words:
                if word not in result:
                    rom = sanscript.transliterate(word, src, sanscript.IAST)
                    if rom:
                        result[word] = rom
        except Exception:
            pass
        return result
    if lang == "ja":
        try:
            import pykakasi
            kks = pykakasi.kakasi()
            for word in words:
                if word not in result:
                    items = kks.convert(word)
                    rom = "".join(item["hepburn"] for item in items)
                    if rom:
                        result[word] = rom
        except Exception:
            pass
        return result
    if lang == "ru":
        try:
            from transliterate import translit
            for word in words:
                if word not in result:
                    rom = translit(word, "ru", reversed=True)
                    if rom:
                        result[word] = rom
        except Exception:
            pass
        return result
    if lang in ("ar", "ur", "fa"):
        for word in words:
            if word not in result:
                rom = _arabic_romanize(word)
                if rom:
                    result[word] = rom
        return result
    if lang == "uk":
        try:
            from transliterate import translit
            for word in words:
                if word not in result:
                    rom = translit(word, "uk", reversed=True)
                    if rom:
                        result[word] = rom
        except Exception:
            pass
        return result
    if lang == "el":
        for word in words:
            if word not in result:
                rom = _greek_romanize(word)
                if rom:
                    result[word] = rom
        return result
    if lang == "he":
        for word in words:
            if word not in result:
                rom = _hebrew_romanize(word)
                if rom:
                    result[word] = rom
        return result
    if lang == "th":
        try:
            from pythainlp.transliterate import romanize as th_romanize
        except Exception:
            return result  # no romanizer available at all
        # Tone detection + syllable tokenization are optional enhancements:
        # if either is missing (older pythainlp), still emit base romanization
        # so ruby never disappears entirely — just without tone diacritics.
        try:
            from pythainlp.util import tone_detector as th_tone
        except Exception:
            th_tone = lambda s: "m"  # mid tone = no diacritic
        try:
            from pythainlp.tokenize import syllable_tokenize as _th_syl_raw
            # The default ("han_solo") engine requires python-crfsuite, which we
            # don't ship; "dict" splits syllables without that dependency.
            def th_syl(w):
                return _th_syl_raw(w, engine="dict")
        except Exception:
            th_syl = None
        for word in words:
            if word not in result:
                try:
                    rom = _th_tonal_romanize(word, th_romanize, th_tone, th_syl)
                except Exception:
                    rom = None
                if rom:
                    result[word] = rom
        return result
    if lang == "yue":
        try:
            import pycantonese

            def _jyut(s: str) -> str:
                # Romanize one word on its own. Romanizing each word individually
                # (rather than one big join) keeps single characters working —
                # a batched join can re-segment '詩史試時市事' into chunks whose
                # keys never match the individual characters we asked about.
                roms = [rom for _seg, rom in pycantonese.characters_to_jyutping(s) if rom]
                return " ".join(roms)

            for word in words:
                if word in result:
                    continue
                rom = _jyut(word)
                if rom:
                    result[word] = rom
        except Exception:
            pass
    elif lang == "cmn":
        try:
            from pypinyin import pinyin, Style
            for word in words:
                if word not in result:
                    p = pinyin(word, style=Style.TONE)
                    joined = " ".join(s[0] for s in p if s)
                    if joined:
                        result[word] = joined
        except Exception:
            pass
    return result


def tokenize(text: str, lang: str) -> list[Token]:
    """Split text into word and non-word tokens for the reader view."""
    if lang in ("yue", "cmn", "ja"):
        return _tokenize_cjk(text, lang)
    if lang == "th":
        return _tokenize_thai(text)
    return _tokenize_latin(text)


def phrase_words(target_text: str, lang: str) -> list[str]:
    """Return the distinct word-tokens inside a phrase (in order).

    A single-word card returns a list of length 1.
    Used to detect phrase cards and enumerate their constituents for
    automatic atomic-card creation.
    """
    seen: set[str] = set()
    result: list[str] = []
    for tok in tokenize(target_text, lang):
        if tok["is_word"] and tok["text"] not in seen:
            seen.add(tok["text"])
            result.append(tok["text"])
    return result


def _tokenize_cjk(text: str, lang: str) -> list[Token]:
    if lang == "yue":
        try:
            import pycantonese
            words = pycantonese.segment(text)
            return _words_to_tokens(words)
        except Exception:
            pass
    elif lang == "cmn":
        try:
            import jieba
            words = list(jieba.cut(text))
            return _words_to_tokens(words)
        except Exception:
            pass
    elif lang == "ja":
        try:
            import pykakasi
            kks = pykakasi.kakasi()
            # pykakasi duplicates/mangles tokens around \n characters.
            # Avoid the bug by tokenizing each line separately.
            all_tokens: list[Token] = []
            for i, line in enumerate(text.split("\n")):
                if i > 0:
                    all_tokens.append({"text": "\n", "is_word": False})
                if line:
                    items = kks.convert(line)
                    words = [item["orig"] for item in items]
                    all_tokens.extend(_words_to_tokens(words))
            return all_tokens
        except Exception:
            pass
    return _char_tokenize(text)


def _words_to_tokens(words: list[str]) -> list[Token]:
    tokens = []
    for w in words:
        if _is_cjk_word(w):
            # Split on any embedded punctuation so sentence-enders are separate tokens.
            parts = _INLINE_PUNCT.split(w)
            if len(parts) == 1:
                tokens.append({"text": w, "is_word": True})
            else:
                for part in parts:
                    if not part:
                        continue
                    tokens.append({"text": part, "is_word": _is_cjk_word(part)})
        else:
            # Non-CJK segments (punctuation, spaces, numbers) are non-interactive.
            tokens.append({"text": w, "is_word": False})
    return tokens


def _is_cjk_word(s: str) -> bool:
    return bool(s.strip()) and any(
        '一' <= c <= '鿿' or '㐀' <= c <= '䶿'
        or '豈' <= c <= '﫿'
        or '぀' <= c <= 'ゟ'
        or '゠' <= c <= 'ヿ'
        for c in s
    )


def _char_tokenize(text: str) -> list[Token]:
    """Fallback: each CJK char is its own token; group non-CJK runs."""
    tokens = []
    buf = ""
    for ch in text:
        if _is_cjk_word(ch):
            if buf:
                tokens.append({"text": buf, "is_word": False})
                buf = ""
            tokens.append({"text": ch, "is_word": True})
        else:
            buf += ch
    if buf:
        tokens.append({"text": buf, "is_word": False})
    return tokens


def _is_thai(ch: str) -> bool:
    return 'ก' <= ch <= '๎'


def _tokenize_thai(text: str) -> list[Token]:
    """Tokenise Thai using pythainlp dictionary segmentation (no spaces)."""
    try:
        from pythainlp.tokenize import word_tokenize
        words = word_tokenize(text, engine="newmm")
        tokens = []
        for w in words:
            is_word = bool(w.strip()) and any(_is_thai(c) for c in w)
            tokens.append({"text": w, "is_word": is_word})
        return tokens
    except Exception:
        tokens = []
        buf = ""
        for ch in text:
            if _is_thai(ch):
                if buf and not any(_is_thai(c) for c in buf):
                    tokens.append({"text": buf, "is_word": False})
                    buf = ""
                buf += ch
            else:
                if buf and any(_is_thai(c) for c in buf):
                    tokens.append({"text": buf, "is_word": True})
                    buf = ""
                buf += ch
        if buf:
            tokens.append({"text": buf, "is_word": any(_is_thai(c) for c in buf)})
        return tokens


def _tokenize_latin(text: str) -> list[Token]:
    """Tokenise a space-delimited alphabetic script (Latin, Devanagari, Hangul…)."""
    tokens = []
    for m in re.finditer(rf"[{_ALPHA}]+|[^{_ALPHA}]+", text):
        word = m.group()
        is_word = bool(_ALPHA_RE.match(word))
        tokens.append({"text": word, "is_word": is_word})
    return tokens
