"""Tests for deterministic option hygiene on graded multiple-choice items.

Both guards exist because a prompt is a request, not a guarantee: the author is
TOLD to keep every option in one language and to make foils differ in meaning,
and it periodically does neither. These are the backstops, and they run on the
same principle as the rest of `_assemble_drill` — we would rather drop an item
than ship one the learner can beat without knowing the language.

Two real failures from a Cantonese course motivate them:

  1. "What does 唔該借個位。 mean?" — the English answer sat among three
     native-script foils, so the only English option was visibly the answer.
  2. A quick_check offering 唔該借支筆。 / 唔該借枝筆。 — 支 and 枝 are both
     zi1, both correct classifiers for a pen, and nothing distinguished them.
"""
import os

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import grammar_lessons
import learning
import tokenizer


def _rom(lang):
    return lambda s: tokenizer.romanize_text(s, lang) if s else ""


# ── tokenizer.script_class ───────────────────────────────────────────────────

def test_script_class_separates_english_from_native_scripts():
    assert tokenizer.script_class("May I borrow a seat?") == "latin"
    assert tokenizer.script_class("我借個位。") == "native"
    assert tokenizer.script_class("こんにちは") == "native"
    assert tokenizer.script_class("안녕하세요") == "native"
    assert tokenizer.script_class("नमस्ते") == "native"
    assert tokenizer.script_class("Привет") == "native"
    assert tokenizer.script_class("Γειά") == "native"
    assert tokenizer.script_class("สวัสดี") == "native"
    assert tokenizer.script_class("مرحبا") == "native"


def test_script_class_treats_accented_latin_as_latin():
    """A Latin-script target must not read as 'native' — otherwise the guard
    would drop every English foil in a French or Vietnamese lesson."""
    for s in ("le garçon", "l'été", "Grüße", "português", "tiếng Việt", "łódź"):
        assert tokenizer.script_class(s) == "latin", s


def test_script_class_is_empty_when_there_are_no_letters():
    """Unjudgeable, so callers keep the option rather than guess."""
    assert tokenizer.script_class("42") == ""
    assert tokenizer.script_class("") == ""
    assert tokenizer.script_class("—?!") == ""
    # Romanisation is Latin letters + digits, never 'native'.
    assert tokenizer.script_class("m4 goi1 ze3 zi1 bat1") == "latin"


# ── Guard 1: options must share the answer's script ──────────────────────────

def test_recognition_drops_native_script_foils():
    """Bug 1. The English answer among native-script foils is a free point."""
    ex = learning._assemble_drill(
        {"kind": "recognition", "concept": "k", "target": "唔該借個位。",
         "gloss": "May I borrow a seat?",
         "distractors": ["我借個位。", "請借個位。", "我要個位。"]},
        "yue", {"k": "grammar"}, _rom("yue"))
    assert ex is None


def test_recognition_keeps_english_foils():
    ex = learning._assemble_drill(
        {"kind": "recognition", "concept": "k", "target": "唔該借個位。",
         "gloss": "May I borrow a seat?",
         "distractors": ["Where is the seat?", "This seat is taken.",
                         "I need two seats."]},
        "yue", {"k": "grammar"}, _rom("yue"))
    assert ex is not None
    assert ex["options"][ex["answer"]] == "May I borrow a seat?"
    assert len(ex["options"]) == 4
    assert all(tokenizer.script_class(o) == "latin" for o in ex["options"])


def test_production_drops_english_foils():
    """The mirror image: native answer, English foil sticks out just as badly."""
    ex = learning._assemble_drill(
        {"kind": "production", "concept": "k", "gloss": "May I borrow a seat?",
         "target": "唔該借個位。",
         "distractors": ["I borrow a seat.", "我要個位。"]},
        "yue", {"k": "grammar"}, _rom("yue"))
    assert ex is not None
    assert ex["options"][ex["answer"]] == "唔該借個位。"
    assert "I borrow a seat." not in ex["options"]
    assert "我要個位。" in ex["options"]


def test_listening_drops_english_foils():
    ex = learning._assemble_drill(
        {"kind": "listening", "concept": "k", "target": "水", "gloss": "water",
         "distractors": ["he", "I", "火"]},
        "yue", {"k": "vocab"}, _rom("yue"))
    assert ex is not None
    assert set(ex["options"]) == {"水", "火"}


def test_script_guard_is_a_noop_for_latin_targets():
    """French and English are both 'latin'; nothing can be filtered, and the
    guard must not start eating perfectly good French foils."""
    ex = learning._assemble_drill(
        {"kind": "production", "concept": "k", "gloss": "I speak",
         "target": "je parle",
         "distractors": ["je parles", "tu parle", "je parlent"]},
        "fr", {"k": "grammar"}, _rom("fr"))
    assert ex is not None
    assert len(ex["options"]) == 4
    assert ex["options"][ex["answer"]] == "je parle"


# ── Guard 2: options must not be homophones of the answer ────────────────────

def test_production_drops_homophone_foils():
    """支 and 枝 are both zi1 — a foil that sounds identical to the answer is a
    second correct answer, not a distractor."""
    rom = _rom("yue")
    assert rom("唔該借支筆。") == rom("唔該借枝筆。")
    ex = learning._assemble_drill(
        {"kind": "production", "concept": "k", "gloss": "May I borrow a pen?",
         "target": "唔該借支筆。",
         "distractors": ["唔該借枝筆。", "我要枝筆。"]},
        "yue", {"k": "grammar"}, rom)
    assert ex is not None
    assert "唔該借枝筆。" not in ex["options"]
    assert set(ex["options"]) == {"唔該借支筆。", "我要枝筆。"}


def test_listening_still_drops_homophones():
    """The original CJK-only guard's behaviour, preserved through the rewrite.
    Note 個/嗰 — the pair the old comment cited — are go3 and go2, a real tone
    contrast and a perfectly good listening drill. 支/枝 are the homophones."""
    ex = learning._assemble_drill(
        {"kind": "listening", "concept": "k", "target": "支", "gloss": "classifier",
         "distractors": ["枝"]},
        "yue", {"k": "grammar"}, _rom("yue"))
    assert ex is None


def test_listening_keeps_a_tone_contrast():
    ex = learning._assemble_drill(
        {"kind": "listening", "concept": "k", "target": "個", "gloss": "classifier",
         "distractors": ["嗰"]},
        "yue", {"k": "grammar"}, _rom("yue"))
    assert ex is not None
    assert set(ex["options"]) == {"個", "嗰"}


def test_homophone_guard_is_a_noop_without_a_romaniser():
    """French parle/parles/parlent are homophones, but there is no romanisation
    oracle for a Latin-script language, so the guard must stay out of the way —
    they are distinguished in writing, which is what a cloze tests."""
    ex = learning._assemble_drill(
        {"kind": "production", "concept": "k", "gloss": "I speak",
         "target": "je parle", "distractors": ["je parles", "je parlent"]},
        "fr", {"k": "grammar"}, _rom("fr"))
    assert ex is not None
    assert len(ex["options"]) == 3


# ── quick_check: same hygiene as a graded drill ──────────────────────────────

def test_quick_check_drops_homophone_options():
    """Bug 2. Both options are correct, so there is no check to make."""
    block = grammar_lessons._clean_block(
        {"type": "quick_check", "question": "How would you ask 'May I borrow a pen?'",
         "options": ["唔該借支筆。", "唔該借枝筆。"],
         "answer": "唔該借支筆。", "why": "polite request"},
        _rom("yue"))
    assert block is None


def test_quick_check_drops_cross_script_options():
    block = grammar_lessons._clean_block(
        {"type": "quick_check", "question": "Which is the polite form?",
         "options": ["唔該借個位。", "Excuse me, may I borrow a seat?"],
         "answer": "唔該借個位。", "why": "唔該 marks a polite request"},
        _rom("yue"))
    assert block is None


def test_quick_check_keeps_a_genuine_contrast():
    block = grammar_lessons._clean_block(
        {"type": "quick_check", "question": "Which asks politely?",
         "options": ["唔該借個位。", "我要個位。"],
         "answer": "唔該借個位。", "why": "唔該 marks a polite request"},
        _rom("yue"))
    assert block is not None
    assert block["type"] == "quick_check"
    assert block["options"][block["answer"]] == "唔該借個位。"
    assert len(block["options"]) == 2


def test_quick_check_dedupes_on_normalised_form():
    """'唔該借個位。' and '唔該借個位' differ only by terminal punctuation — one
    option, not two, and with no foil left there is nothing to check."""
    block = grammar_lessons._clean_block(
        {"type": "quick_check", "question": "Which is correct?",
         "options": ["唔該借個位。", "唔該借個位"],
         "answer": "唔該借個位。", "why": "polite"},
        _rom("yue"))
    assert block is None


def test_quick_check_answer_key_survives_shuffling():
    """The model gives us the answer STRING; we place and key it ourselves."""
    for _ in range(30):
        block = grammar_lessons._clean_block(
            {"type": "quick_check", "question": "Which asks politely?",
             "options": ["唔該借個位。", "我要個位。", "我借個位。"],
             "answer": "我要個位。", "why": "…"},
            _rom("yue"))
        assert block["options"][block["answer"]] == "我要個位。"


# ── Cloze fillers get the same treatment ─────────────────────────────────────

def test_free_cloze_drops_homophone_fillers():
    ex = grammar_lessons._free_cloze(
        "k", "唔該借___筆。", "May I borrow a pen?", "支", ["支", "枝"], _rom("yue"))
    assert ex is not None
    assert ex["options"] == ["支"]


def test_free_cloze_keeps_distinct_fillers():
    ex = grammar_lessons._free_cloze(
        "k", "我食___。", "I eat rice.", "飯", ["飯", "麵", "水"], _rom("yue"))
    assert ex is not None
    assert set(ex["options"]) == {"飯", "麵", "水"}
    assert ex["options"][ex["answer"]] == "飯"


def test_free_cloze_without_a_romaniser_is_unchanged():
    """Called with no oracle (the legacy signature) it must behave as before."""
    ex = grammar_lessons._free_cloze(
        "k", "je ___ français.", "I speak French.", "parle",
        ["parle", "parles", "parlent"])
    assert ex is not None
    assert len(ex["options"]) == 3
    assert ex["options"][ex["answer"]] == "parle"


# ── The drill has to be in the language being taught ─────────────────────────
# A model fed an English-heavy source (a phrasebook chapter, a romanization-only
# textbook page) sometimes authors a whole drill in ENGLISH: "What does this
# mean? / Please include some coins in the change." with that same sentence
# among the options. The learner answers it without reading anything, in a
# course they are taking to learn Cantonese.

def test_a_recognition_drill_written_in_english_is_dropped():
    ex = learning._assemble_drill(
        {"kind": "recognition", "concept": "k",
         "target": "Please include some coins in the change.",
         "gloss": "Please include some coins in the change.",
         "distractors": ["I have no coins.", "Do you have coins?"]},
        "yue", {"k": "vocab"}, _rom("yue"),
    )
    assert ex is None


def test_a_production_drill_whose_answer_is_english_is_dropped():
    ex = learning._assemble_drill(
        {"kind": "production", "concept": "k", "gloss": "thank you",
         "target": "thank you very much", "distractors": ["good morning", "goodbye"]},
        "yue", {"k": "vocab"}, _rom("yue"),
    )
    assert ex is None


def test_a_real_cantonese_drill_still_assembles():
    ex = learning._assemble_drill(
        {"kind": "production", "concept": "k", "gloss": "thank you",
         "target": "唔該", "distractors": ["早晨", "再見"]},
        "yue", {"k": "vocab"}, _rom("yue"),
    )
    assert ex is not None and "唔該" in ex["options"]


def test_a_latin_script_language_is_never_judged_by_script():
    """French and English are both Latin — nothing here can tell them apart, so
    the check must be a no-op rather than a source of false drops."""
    ex = learning._assemble_drill(
        {"kind": "production", "concept": "k", "gloss": "thank you",
         "target": "merci", "distractors": ["bonjour", "au revoir"]},
        "fr", {"k": "vocab"}, _rom("fr"),
    )
    assert ex is not None


def test_an_option_that_repeats_the_prompt_is_dropped():
    """The language-agnostic half of the check: whatever the language, a prompt
    that IS one of the options gives the answer away without being read."""
    assert learning.drill_is_sane(
        {"type": "choice", "prompt_lang": "target", "prompt": "Je mange du pain",
         "options": ["Je mange du pain", "I eat bread"], "answer": 0}, "fr") is False


def test_typed_and_tile_drills_are_checked_too():
    assert learning.drill_is_sane(
        {"type": "type_answer", "prompt": "thank you", "answer": "thank you"},
        "yue") is False
    assert learning.drill_is_sane(
        {"type": "word_bank", "answer_tokens": ["thank", "you"], "audio": "thank you"},
        "yue") is False
    assert learning.drill_is_sane(
        {"type": "word_bank", "answer_tokens": ["我", "食飯"], "audio": "我食飯"},
        "yue") is True


def test_a_quick_check_written_in_english_is_dropped():
    """A quick_check is a drill in teach clothing and fails the same way."""
    assert learning.block_is_sane(
        {"type": "quick_check", "question": "Which is right?",
         "options": ["Please give me coins", "I have no coins"], "answer": 0},
        "yue") is False
    assert learning.block_is_sane({"type": "prose", "text": "English is fine here"},
                                  "yue") is True


def test_self_managed_drills_are_left_alone():
    """AI Speak and the mini-games carry no stored answer to inspect."""
    assert learning.drill_is_sane({"type": "construction_drill",
                                   "construction": "-er present"}, "yue") is True
