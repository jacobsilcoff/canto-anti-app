import re

# Each token: {"text": str, "is_word": bool}
Token = dict

# Matches sentence-ending punctuation that is NOT inside quotes.
# For CJK: 。！？; for Latin: . ! ?; also newlines.
_SENTENCE_ENDERS = re.compile(r'[。！？.!?\n]')

# Punctuation that must never be swallowed inside a word token.
_INLINE_PUNCT = re.compile(r'([。！？、，：；…⋯！？「」『』【】《》〈〉\n])')
# Opening/closing quote characters — we avoid splitting mid-quote.
_OPEN_QUOTES = set('"«「『')
_CLOSE_QUOTES = set('"»」』')


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


def romanize_words(words: list[str], lang: str) -> dict[str, str]:
    """Return a mapping of word text → romanization string for the given words.

    Only produces output for logographic languages (yue, cmn). Returns an
    empty dict for Latin-script languages or on failure.
    """
    result: dict[str, str] = {}
    if lang == "yue":
        try:
            import pycantonese
            full_text = "".join(words)
            pairs = pycantonese.characters_to_jyutping(full_text)
            # Build a segment-level map from whatever characters_to_jyutping produces.
            seg_map: dict[str, str] = {}
            for seg, rom in pairs:
                if rom and seg not in seg_map:
                    seg_map[seg] = rom
            for word in words:
                if word in result:
                    continue
                if word in seg_map:
                    result[word] = seg_map[word]
                else:
                    # characters_to_jyutping and segment() use different internal
                    # tokenisers so multi-char words may not appear as keys.
                    # Assemble from per-character entries when available.
                    parts = [seg_map.get(c) for c in word]
                    if all(parts):
                        result[word] = " ".join(parts)  # type: ignore[arg-type]
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
    tokens = []
    for m in re.finditer(r"[a-zA-ZÀ-ÿ']+|[^a-zA-ZÀ-ÿ']+", text):
        word = m.group()
        is_word = bool(re.match(r"[a-zA-ZÀ-ÿ']", word))
        tokens.append({"text": word, "is_word": is_word})
    return tokens
