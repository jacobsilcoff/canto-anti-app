"""Tokenizer + romanization tests, focused on the non-Latin alphabetic scripts
(Korean Hangul, Hindi Devanagari) added alongside the CJK and Latin paths."""
import tokenizer


def _words(text, lang):
    return [t["text"] for t in tokenizer.tokenize(text, lang) if t["is_word"]]


# ── Korean (Hangul) ───────────────────────────────────────────────────────────

def test_korean_splits_on_spaces():
    assert _words("저는 한국어를 배워요", "ko") == ["저는", "한국어를", "배워요"]


def test_korean_sentence_split():
    sents = tokenizer.split_sentences(tokenizer.tokenize("저는 좋아요. 당신은요?", "ko"))
    assert sents == ["저는 좋아요.", "당신은요?"]


def test_korean_romanization():
    rom = tokenizer.romanize_words(["물", "사랑"], "ko")
    assert rom["물"] == "mul"
    assert rom["사랑"] == "sarang"


# ── Hindi (Devanagari) ────────────────────────────────────────────────────────

def test_hindi_splits_on_spaces():
    assert _words("मैं हिंदी सीखता हूँ", "hi") == ["मैं", "हिंदी", "सीखता", "हूँ"]


def test_hindi_keeps_combining_vowel_marks_in_word():
    # 'किताब' = क + ि (matra, not .isalpha()) + त + ा + ब — must stay one token.
    assert _words("किताब", "hi") == ["किताब"]


def test_hindi_danda_is_not_part_of_word():
    words = _words("मैं हूँ।", "hi")
    assert words == ["मैं", "हूँ"]   # danda (।) excluded


def test_hindi_sentence_split_on_danda():
    sents = tokenizer.split_sentences(tokenizer.tokenize("मैं ठीक हूँ। तुम कैसे हो?", "hi"))
    assert sents == ["मैं ठीक हूँ।", "तुम कैसे हो?"]


def test_hindi_romanization_iast():
    rom = tokenizer.romanize_words(["नमस्ते", "पानी"], "hi")
    assert rom["नमस्ते"] == "namaste"
    assert rom["पानी"] == "pānī"


# ── Latin unaffected ──────────────────────────────────────────────────────────

def test_latin_still_tokenizes():
    assert _words("Bonjour le monde", "fr") == ["Bonjour", "le", "monde"]


def test_latin_has_no_offline_romanization():
    assert tokenizer.romanize_words(["bonjour"], "fr") == {}
