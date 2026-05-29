import re

# Each token: {"text": str, "is_word": bool}
Token = dict

_SENTENCE_ENDERS = re.compile(r"[。！？\n!?]")


def split_sentences(tokens: list[Token]) -> list[str]:
    """Group tokens into sentence strings, split on sentence-ending punctuation."""
    sentences = []
    buf: list[str] = []
    for tok in tokens:
        buf.append(tok["text"])
        if not tok["is_word"] and _SENTENCE_ENDERS.search(tok["text"]):
            s = "".join(buf).strip()
            if s:
                sentences.append(s)
            buf = []
    if buf:
        s = "".join(buf).strip()
        if s:
            sentences.append(s)
    return sentences


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
            tokens.append({"text": w, "is_word": True})
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
