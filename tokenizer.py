import re

# Each token: {"text": str, "is_word": bool}
Token = dict

# Matches sentence-ending punctuation that is NOT inside quotes.
# CJK: 。！？; Latin: . ! ?; Devanagari danda: । ॥; also newlines.
_SENTENCE_ENDERS = re.compile(r'[。！？.!?।॥\n]')

# Punctuation that must never be swallowed inside a word token. Includes the
# middle-dot family (· U+00B7, ・ U+30FB, ‧ U+2027, • U+2022) used to list
# individual syllables (詩·史·試·時·市·事) — splitting on them lets each character
# become its own token and get its own romanization.
_INLINE_PUNCT = re.compile(r'([。！？、，：；…⋯！？「」『』【】《》〈〉·・‧•\n])')
# Opening/closing quote characters — we avoid splitting mid-quote.
_OPEN_QUOTES = set('"«「『')
_CLOSE_QUOTES = set('"»」』')

# Word characters for alphabetic, space-delimited scripts. Includes Latin (with
# accents + apostrophe), Devanagari and Telugu (consonants AND combining vowel
# marks, which are not .isalpha()), and Hangul (precomposed syllables + Jamo).
# Used to tokenise any non-CJK script where words are separated by spaces.
# Devanagari range deliberately skips U+0964–U+0965 (danda / double danda) so
# those stay as sentence punctuation rather than getting glued onto a word.
_ALPHA = r"a-zA-ZÀ-ÿ'ऀ-ॣ०-ॿఀ-౿가-힣ᄀ-ᇿ㄰-㆏"
_ALPHA_RE = re.compile(rf"[{_ALPHA}]")


def split_sentences(tokens: list[Token]) -> list[str]:
    """Group tokens into sentence strings, split on sentence-ending punctuation.

    Does not split inside quoted speech — a sentence-ender found while inside
    quotes is absorbed into the current buffer rather than flushing it.
    For Latin scripts a period must be followed by whitespace or end-of-text
    (to avoid splitting on abbreviations or decimal numbers).
    """
    sentences = []
    buf: list[str] = []
    quote_depth = 0

    for i, tok in enumerate(tokens):
        text = tok["text"]
        # Track quote depth from non-word tokens (punctuation)
        if not tok["is_word"]:
            for ch in text:
                if ch in _OPEN_QUOTES:
                    quote_depth += 1
                elif ch in _CLOSE_QUOTES:
                    quote_depth = max(0, quote_depth - 1)

        buf.append(text)

        if not tok["is_word"] and quote_depth == 0 and _SENTENCE_ENDERS.search(text):
            # For a numeric period (e.g. "1.5"), don't split.
            if '.' in text and '。' not in text:
                if re.search(r'\d\.\d', text):
                    continue
            s = "".join(buf).strip()
            if s:
                sentences.append(s)
            buf = []

    if buf:
        s = "".join(buf).strip()
        if s:
            sentences.append(s)
    return sentences


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
    if lang in ("hi", "te"):
        try:
            from indic_transliteration import sanscript
            src = sanscript.DEVANAGARI if lang == "hi" else sanscript.TELUGU
            for word in words:
                if word not in result:
                    rom = sanscript.transliterate(word, src, sanscript.IAST)
                    if rom:
                        result[word] = rom
        except Exception:
            pass
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
    if lang in ("yue", "cmn"):
        return _tokenize_cjk(text, lang)
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


def _tokenize_latin(text: str) -> list[Token]:
    """Tokenise a space-delimited alphabetic script (Latin, Devanagari, Hangul…)."""
    tokens = []
    for m in re.finditer(rf"[{_ALPHA}]+|[^{_ALPHA}]+", text):
        word = m.group()
        is_word = bool(_ALPHA_RE.match(word))
        tokens.append({"text": word, "is_word": is_word})
    return tokens
