"""Tests for the unit-plan-first / unified micro-lesson authoring (learning.py).

These cover the DETERMINISTIC assembly + validation — no LLM calls. The contract
that matters most: WE own the answer key (option[answer] is always the intended
correct answer), and romanization is recomputed by the offline oracle.
"""
import json
import os
import types

import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db
import learning


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


# ── Assembly: answer keys are correct by construction ────────────────────────

_CONCEPTS = [
    {"kind": "grammar", "key": "articles", "label": "el / la", "gloss": "the (m/f)"},
    {"kind": "vocab", "key": "mesa", "label": "la mesa", "gloss": "the table"},
]


def _correct_option(ex):
    """The option the grader will accept as correct."""
    if ex["type"] in ("choice", "listening"):
        return ex["options"][ex["answer"]]
    if ex["type"] == "word_bank":
        return ex["answer_tokens"]
    return None


def test_assemble_answer_keys_are_correct():
    authored = {
        "intro": "intro line",
        "teach": [{"type": "prose", "text": "Spanish has gendered articles."}],
        "drills": [
            {"kind": "recognition", "concept": "mesa", "target": "la mesa",
             "gloss": "the table", "distractors": ["the book", "the door"]},
            {"kind": "production", "concept": "articles", "gloss": "the book",
             "target": "el libro", "distractors": ["la libro", "el mesa"]},
            {"kind": "listening", "concept": "mesa", "target": "la mesa",
             "gloss": "the table", "distractors": ["el libro", "la puerta"]},
            {"kind": "match", "concept": "articles",
             "pairs": [{"target": "el libro", "english": "the book"},
                       {"target": "la mesa", "english": "the table"}]},
        ],
    }
    content = learning.assemble_lesson("es", _CONCEPTS, authored)
    segs = content["segments"]
    # Flat legacy shape wraps as one step; the grammar concept adds an "AI Speak"
    # construction-drill segment at the end.
    assert len(segs) == 2
    assert segs[1]["speak"] is True
    ex = {e["type"]: e for e in segs[0]["exercises"]}
    # Recognition: prompt is the target, correct option is its English gloss.
    assert ex["choice"]["prompt"] == "la mesa" or ex["choice"]["prompt"] == "the book"
    # Every choice/listening exercise's keyed option is the intended correct one.
    by_concept = {e.get("concept_key"): e for e in segs[0]["exercises"]}
    recog = next(e for e in segs[0]["exercises"]
                 if e["type"] == "choice" and e["prompt"] == "la mesa")
    assert recog["options"][recog["answer"]] == "the table"
    prod = next(e for e in segs[0]["exercises"]
                if e["type"] == "choice" and e["prompt_lang"] == "english")
    assert prod["options"][prod["answer"]] == "el libro"
    listen = next(e for e in segs[0]["exercises"] if e["type"] == "listening")
    assert listen["options"][listen["answer"]] == "la mesa"
    assert listen["audio"] == "la mesa"


def test_assemble_drops_invalid_drills():
    authored = {
        "teach": [],
        "drills": [
            {"kind": "recognition", "concept": "mesa", "target": "la mesa"},   # no gloss
            {"kind": "cloze", "concept": "articles", "sentence": "no blank here",
             "answer": "el", "distractors": ["la"]},                           # no ___
            {"kind": "reorder", "concept": "articles", "sentence": "x",
             "tokens": ["solo"]},                                              # <2 tokens
            {"kind": "match", "concept": "articles",
             "pairs": [{"target": "el libro", "english": "the book"}]},        # <2 pairs
        ],
    }
    content = learning.assemble_lesson("es", _CONCEPTS, authored)
    # All authored drills are invalid → dropped. (A construction_drill is auto-added
    # for grammar concepts; it's not an authored/graded drill, so exclude it here.)
    graded = [e for e in content["segments"][0]["exercises"] if e["type"] != "construction_drill"]
    assert graded == []


def test_grammar_flag_tracks_concept_kind():
    authored = {"teach": [], "drills": [
        {"kind": "recognition", "concept": "articles", "target": "el libro",
         "gloss": "the book", "distractors": ["the table", "the door"]},
        {"kind": "recognition", "concept": "mesa", "target": "la mesa",
         "gloss": "the table", "distractors": ["the book", "the door"]},
    ]}
    exs = learning.assemble_lesson("es", _CONCEPTS, authored)["segments"][0]["exercises"]
    flags = {e["concept_key"]: e["grammar"] for e in exs}
    assert flags["articles"] is True
    assert flags["mesa"] is False


def test_romanization_recomputed_for_logographic():
    # For a romanized language (Cantonese) every target gets ruby from the oracle,
    # never the model. The reorder drill carries answer_roman; choice carries roman.
    concepts = [{"kind": "vocab", "key": "hello", "label": "你好", "gloss": "hello"}]
    authored = {"teach": [], "drills": [
        {"kind": "recognition", "concept": "hello", "target": "你好",
         "gloss": "hello", "distractors": ["goodbye", "thanks"]},
        {"kind": "reorder", "concept": "hello", "sentence": "你好",
         "tokens": ["你", "好"]},
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    recog = next(e for e in exs if e["type"] == "choice")
    reorder = next(e for e in exs if e["type"] == "word_bank")
    assert recog["prompt_roman"]              # jyutping computed
    assert reorder["answer_roman"]


def test_reorder_tokens_reordered_from_sentence():
    # The model returned tokens in wrong order ("香港人你係") but sentence correct
    # ("你係香港人"). Assembly must reorder tokens to match the sentence.
    concepts = [{"kind": "vocab", "key": "hk_person", "label": "香港人", "gloss": "Hong Kong person"}]
    authored = {"teach": [], "drills": [
        {"kind": "reorder", "concept": "hk_person", "sentence": "你係香港人",
         "tokens": ["香港人", "你", "係"]},   # wrong order from model
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    wb = next(e for e in exs if e["type"] == "word_bank")
    assert wb["answer_tokens"] == ["你", "係", "香港人"]


def test_reorder_dropped_when_tokens_dont_tile_sentence():
    # If the model's tokens can't tile the sentence exactly, drop the drill.
    concepts = [{"kind": "vocab", "key": "test", "label": "你好", "gloss": "hello"}]
    authored = {"teach": [], "drills": [
        {"kind": "reorder", "concept": "test", "sentence": "你好嗎",
         "tokens": ["你", "好"]},   # missing 嗎 — can't tile sentence
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    assert exs == []


def test_reorder_glossary_filtered_to_real_tokens():
    # The reorder glossary keeps only {token: gloss} for tokens that are actually
    # tiles and have a non-empty gloss — stray/blank entries are dropped.
    concepts = [{"kind": "vocab", "key": "iam", "label": "我係", "gloss": "I am"}]
    authored = {"teach": [], "drills": [
        {"kind": "reorder", "concept": "iam", "sentence": "我係學生",
         "tokens": ["我", "係", "學生"],
         "glossary": [
             {"token": "我", "gloss": "I"},
             {"token": "係", "gloss": "am"},
             {"token": "唔", "gloss": "not"},     # not a tile → dropped
             {"token": "學生", "gloss": ""},       # blank gloss → dropped
         ]},
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    wb = next(e for e in exs if e["type"] == "word_bank")
    assert wb["glossary"] == {"我": "I", "係": "am"}


def test_vocab_glossary_includes_all_words_and_concepts():
    # Glosses are hidden behind hover/tap, so we keep EVERY word the model offers
    # plus the concepts being introduced — no known-word filtering.
    concepts = [{"kind": "vocab", "key": "eat", "label": "食", "gloss": "eat"}]
    authored = {
        "teach": [], "drills": [],
        "vocab_glossary": {"我": "I", "喺": "at/in", "食": "consume(LLM)"},
    }
    vg = learning.assemble_lesson("yue", concepts, authored)["vocab_glossary"]
    assert vg["我"] == "I"           # simple word kept (not filtered)
    assert vg["喺"] == "at/in"
    assert vg["食"] == "eat"         # concept label wins over the LLM's gloss


def test_vocab_glossary_does_not_augment_word_bank_tiles():
    # Tile glosses come ONLY from the drill's own `glossary` (focused helper words),
    # never the broad vocab_glossary — so tiles don't get over-glossed.
    concepts = [{"kind": "vocab", "key": "eat", "label": "食", "gloss": "eat"}]
    authored = {
        "teach": [], "drills": [
            {"kind": "reorder", "concept": "eat", "sentence": "我食飯",
             "tokens": ["我", "食", "飯"],
             "glossary": [{"token": "飯", "gloss": "rice/meal"}]},
        ],
        "vocab_glossary": {"我": "I", "飯": "rice/meal", "食": "eat"},
    }
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    wb = next(e for e in exs if e["type"] == "word_bank")
    assert wb["glossary"] == {"飯": "rice/meal"}   # only the drill's own glossary


def test_french_cloze_uses_conjugation_oracle():
    # When the model tags a cloze with verb+person, grammar.py is authoritative:
    # the correct option must be the engine's paradigm cell, regardless of order.
    concepts = [{"kind": "grammar", "key": "present_er", "label": "-er present",
                 "gloss": "present tense of -er verbs"}]
    authored = {"teach": [], "drills": [
        {"kind": "cloze", "concept": "present_er",
         "sentence": "Nous ___ le français.", "answer": "parlons",
         "gloss": "We speak French.", "verb": "parler", "person": "nous",
         "distractors": ["parle", "parles"]},
    ]}
    segs = learning.assemble_lesson("fr", concepts, authored)["segments"]
    # The cloze, plus a construction_drill for the grammar concept in its own
    # final "AI Speak" segment.
    cloze = next(e for e in segs[0]["exercises"] if e["type"] == "choice")
    assert cloze["options"][cloze["answer"]] == "parlons"
    all_ex = [e for s in segs for e in s["exercises"]]
    assert any(e["type"] == "construction_drill" for e in all_ex)


# ── Drill validation: distractor hygiene + reorder backtracking ──────────────

def test_recognition_drops_disguised_duplicate_distractors():
    # "The table." normalizes to the same thing as the correct gloss "the table"
    # — keeping it would make two options arguably correct.
    concepts = [{"kind": "vocab", "key": "mesa", "label": "la mesa", "gloss": "the table"}]
    authored = {"teach": [], "drills": [
        {"kind": "recognition", "concept": "mesa", "target": "la mesa",
         "gloss": "the table",
         "distractors": ["The table.", "a table", "the book", "the door"]},
    ]}
    exs = learning.assemble_lesson("es", concepts, authored)["segments"][0]["exercises"]
    recog = exs[0]
    norm = [o.casefold().rstrip(".") for o in recog["options"]]
    assert norm.count("the table") == 1          # disguised dupes removed
    assert "a table" not in norm                 # article-stripped dupe removed
    assert recog["options"][recog["answer"]] == "the table"


def test_listening_drops_homophone_distractors(monkeypatch):
    # Two characters that romanize identically are undecidable by ear, so the
    # homophonous distractor must go. Romanization is stubbed so the test doesn't
    # depend on the pycantonese dictionary's exact tones.
    fake = {"個": "go3", "嗰": "go3", "你": "nei5", "好": "hou2"}
    monkeypatch.setattr(learning.tokenizer, "romanize_text",
                        lambda s, lang: fake.get(s, s))
    concepts = [{"kind": "vocab", "key": "go3", "label": "個", "gloss": "classifier"}]
    authored = {"teach": [], "drills": [
        {"kind": "listening", "concept": "go3", "target": "個",
         "gloss": "classifier", "distractors": ["嗰", "你", "好"]},
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    listen = exs[0]
    assert "嗰" not in listen["options"]
    assert listen["options"][listen["answer"]] == "個"


def test_listening_dropped_when_all_distractors_homophonous(monkeypatch):
    # If every distractor sounds identical to the answer, the drill is undecidable
    # → fewer than 2 options → dropped entirely.
    monkeypatch.setattr(learning.tokenizer, "romanize_text",
                        lambda s, lang: "go3")
    concepts = [{"kind": "vocab", "key": "go3", "label": "個", "gloss": "classifier"}]
    authored = {"teach": [], "drills": [
        {"kind": "listening", "concept": "go3", "target": "個",
         "gloss": "classifier", "distractors": ["嗰"]},
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    assert exs == []


def test_reorder_backtracks_when_short_token_shadows_long():
    # Tokens 你 and 你好 both match at position 0 of 你好你 — a greedy walk that
    # consumes 你 first dead-ends; backtracking must find 你好+你.
    concepts = [{"kind": "vocab", "key": "t", "label": "你好", "gloss": "hello"}]
    authored = {"teach": [], "drills": [
        {"kind": "reorder", "concept": "t", "sentence": "你好你",
         "tokens": ["你", "你好"]},
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    wb = next(e for e in exs if e["type"] == "word_bank")
    assert wb["answer_tokens"] == ["你好", "你"]


def test_order_tokens_backtracking_unit():
    f = learning._order_tokens_from_sentence
    assert f("你好你", ["你", "你好"]) == ["你好", "你"]
    assert f("aabaa", ["aa", "b", "aa"]) == ["aa", "b", "aa"]
    assert f("你好嗎", ["你", "好"]) is None        # can't tile → drop


def test_cloze_prompt_roman_preserves_blank():
    concepts = [{"kind": "vocab", "key": "p", "label": "佢", "gloss": "he/she"}]
    authored = {"teach": [], "drills": [
        {"kind": "cloze", "concept": "p", "sentence": "___係香港人",
         "answer": "佢", "gloss": "He is a Hong Konger.",
         "distractors": ["我", "你"]},
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    cloze = exs[0]
    assert "___" in cloze["prompt_roman"]


def test_registry_block_caps_length():
    registry = [{"key": f"k{i}", "label": f"l{i}", "gloss": "g"} for i in range(200)]
    block = learning._registry_block(registry)
    assert "k199" in block                  # newest kept
    assert "k0" not in block                # oldest dropped
    assert "omitted" in block


# ── Spiral review ─────────────────────────────────────────────────────────────

def test_review_concepts_flag_kinds_but_lesson_registers_only_batch():
    # A review grammar concept's drills must carry grammar=True even though the
    # concept isn't in the lesson's new batch.
    batch = [{"kind": "vocab", "key": "mesa", "label": "la mesa", "gloss": "the table"}]
    review = [{"kind": "grammar", "key": "articles", "label": "el / la", "gloss": "the (m/f)"}]
    authored = {"teach": [], "drills": [
        {"kind": "recognition", "concept": "mesa", "target": "la mesa",
         "gloss": "the table", "distractors": ["the book", "the door"]},
        {"kind": "recognition", "concept": "articles", "target": "el libro",
         "gloss": "the book", "distractors": ["the table", "the door"]},
    ]}
    exs = learning.assemble_lesson("es", batch + review, authored)["segments"][0]["exercises"]
    flags = {e["concept_key"]: e["grammar"] for e in exs}
    assert flags["mesa"] is False
    assert flags["articles"] is True


def test_review_block_in_prompt():
    review = [{"kind": "vocab", "key": "casa", "label": "casa", "gloss": "house"}]
    prompt = learning._build_lesson_prompt("es", _CONCEPTS, [], None, review)
    assert "REVIEW (warm-up)" in prompt
    assert "casa" in prompt
    # Lessons are always authored at max depth; review lessons get the larger budget.
    assert "12–16" in prompt
    # Review drills open the lesson as step 1's warm-up (C2).
    assert "START of step 1" in prompt
    # Without review: no section, the standard (non-review) max budget.
    prompt2 = learning._build_lesson_prompt("es", _CONCEPTS, [], None, None)
    assert "REVIEW (warm-up)" not in prompt2
    assert "10–14" in prompt2


def test_lesson_prompt_asks_for_steps():
    prompt = learning._build_lesson_prompt("es", _CONCEPTS, [], None, None)
    assert "3–4 BITE-SIZED STEPS" in prompt
    assert '"steps"' in prompt
    assert "quick_check" in prompt


# ── Multi-step assembly (A1/A2/C1) ────────────────────────────────────────────

def test_steps_become_segments():
    authored = {
        "intro": "intro line",
        "steps": [
            {"title": "Warm-up", "teach": [], "drills": [
                {"kind": "recognition", "concept": "mesa", "target": "la mesa",
                 "gloss": "the table", "distractors": ["the book", "the door"]},
            ]},
            {"title": "The pattern",
             "teach": [{"type": "prose", "text": "Articles agree in gender."}],
             "drills": [
                {"kind": "production", "concept": "articles", "gloss": "the book",
                 "target": "el libro", "distractors": ["la libro", "el mesa"]},
             ]},
            {"title": "Empty step", "teach": [], "drills": []},   # dropped
        ],
    }
    segs = learning.assemble_lesson("es", _CONCEPTS, authored)["segments"]
    # 2 surviving steps + the AI Speak segment for the grammar concept.
    assert [s["title"] for s in segs] == ["Warm-up", "The pattern", "AI Speak"]
    # Intro lands on the first segment only.
    assert segs[0]["teach"]["intro"] == "intro line"
    assert segs[1]["teach"]["intro"] == ""
    assert segs[0]["exercises"][0]["type"] == "choice"
    assert segs[1]["teach"]["blocks"][0]["type"] == "prose"
    # The AI Speak segment has no teach and one construction drill.
    assert segs[2]["speak"] is True and segs[2]["teach"] is None
    assert [e["type"] for e in segs[2]["exercises"]] == ["construction_drill"]
    assert segs[2]["exercises"][0]["construction"] == "el / la"


def test_step_dedup_spans_steps():
    # The same recognition answer in two different steps is still a duplicate.
    drill = {"kind": "recognition", "concept": "mesa", "target": "la mesa",
             "gloss": "the table", "distractors": ["the book", "the door"]}
    authored = {"steps": [
        {"title": "A", "teach": [], "drills": [dict(drill)]},
        {"title": "B", "teach": [], "drills": [dict(drill)]},
    ]}
    concepts = [{"kind": "vocab", "key": "mesa", "label": "la mesa", "gloss": "the table"}]
    segs = learning.assemble_lesson("es", concepts, authored)["segments"]
    all_ex = [e for s in segs for e in s["exercises"]]
    assert len(all_ex) == 1


def test_quick_check_block_validated_and_keyed():
    # quick_check: we place + shuffle the answer ourselves; the stored index is
    # correct by construction. An answer missing from options is inserted.
    concepts = [{"kind": "vocab", "key": "mesa", "label": "la mesa", "gloss": "the table"}]
    authored = {"steps": [{"title": "T", "teach": [
        {"type": "quick_check", "question": "Which means 'my books'?",
         "options": ["mis libros", "mi libros"], "answer": "mis libros",
         "why": "plural agrees"},
        {"type": "quick_check", "question": "Pick one",
         "options": ["b", "c"], "answer": "a", "why": ""},          # answer inserted
        {"type": "quick_check", "question": "No foils",
         "options": ["only"], "answer": "only", "why": ""},         # dropped
        {"type": "quick_check", "question": "", "options": ["a", "b"],
         "answer": "a", "why": ""},                                  # no question → dropped
    ], "drills": []}]}
    blocks = learning.assemble_lesson("es", concepts, authored)["segments"][0]["teach"]["blocks"]
    qcs = [b for b in blocks if b["type"] == "quick_check"]
    assert len(qcs) == 2
    assert qcs[0]["options"][qcs[0]["answer"]] == "mis libros"
    assert qcs[0]["why"] == "plural agrees"
    assert qcs[1]["options"][qcs[1]["answer"]] == "a"
    assert set(qcs[1]["options"]) == {"a", "b", "c"}


def test_no_speak_segment_for_vocab_only_lesson():
    concepts = [{"kind": "vocab", "key": "mesa", "label": "la mesa", "gloss": "the table"}]
    authored = {"steps": [{"title": "T", "teach": [], "drills": [
        {"kind": "recognition", "concept": "mesa", "target": "la mesa",
         "gloss": "the table", "distractors": ["the book", "the door"]},
    ]}]}
    segs = learning.assemble_lesson("es", concepts, authored)["segments"]
    assert len(segs) == 1
    assert not any(s.get("speak") for s in segs)


def test_pick_review_concepts_prefers_weak_then_rotates():
    import main
    registry = [{"kind": "vocab", "key": f"k{i}", "label": f"l{i}", "gloss": "g"}
                for i in range(5)]
    batch = [registry[0]]                       # k0 excluded (being taught now)
    mastery = [
        {"concept_key": "k3", "correct": 1, "total": 5},   # weak → must be picked
        {"concept_key": "k2", "correct": 5, "total": 5},   # strong
        {"concept_key": "k4", "correct": 2, "total": 2},   # too few attempts
    ]
    picked = main._pick_review_concepts(registry, batch, mastery, lesson_num=7)
    keys = [c["key"] for c in picked]
    assert len(keys) == 2
    assert "k3" in keys                          # weak concept always included
    assert "k0" not in keys                      # batch excluded
    # Rotation: different lesson numbers pick different filler concepts.
    other = main._pick_review_concepts(registry, batch, mastery, lesson_num=8)
    assert [c["key"] for c in other] != keys or len(registry) <= 3


def test_pick_review_concepts_empty_registry():
    import main
    assert main._pick_review_concepts([], [], [], 1) == []


# ── Plan concept dedup (main._filter_new_concepts) ───────────────────────────

def test_filter_new_concepts_drops_registry_dupes():
    import main
    registry = [
        {"kind": "vocab", "key": "hello", "label": "你好", "gloss": "hello"},
        {"kind": "grammar", "key": "copula", "label": "係", "gloss": "to be"},
    ]
    plan = [
        {"kind": "vocab", "key": "hello", "label": "哈囉", "gloss": "hi"},        # dup key
        {"kind": "vocab", "key": "hello2", "label": "你好", "gloss": "hello"},    # dup vocab label
        {"kind": "vocab", "key": "thanks", "label": "多謝", "gloss": "thanks"},   # new
        {"kind": "vocab", "key": "thanks", "label": "唔該", "gloss": "thanks"},   # dup within plan
        {"kind": "grammar", "key": "aspect", "label": "係", "gloss": "..."},      # grammar may share label
    ]
    out = main._filter_new_concepts(plan, registry)
    assert [c["key"] for c in out] == ["thanks", "aspect"]


# ── Active chapter persistence ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_plan_roundtrip(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    assert await db.get_active_plan(cid) is None
    # active_plan now holds the in-progress CHAPTER ({title, objective, summary}).
    chapter = {"title": "Greetings", "objective": "Greet people", "summary": "bonjour, salut"}
    await db.set_active_plan(cid, chapter)
    got = await db.get_active_plan(cid)
    assert got["title"] == "Greetings"
    assert got["summary"] == "bonjour, salut"
    await db.set_active_plan(cid, None)
    assert await db.get_active_plan(cid) is None


# ── Next-lesson planner (just-in-time) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_next_lesson_normalizes_spec(monkeypatch):
    # The planner reply is normalized: a valid chapter_action survives, target_items
    # are cleaned, and missing scope/focus default sensibly.
    async def fake_call(prompt, **kw):
        return json.dumps({
            "chapter_action": "new",
            "chapter": {"title": "Regular -er verbs", "objective": "Conjugate -er verbs", "summary": "the core"},
            "skill": {"kind": "grammar", "key": "present_er", "label": "-er present", "gloss": "present tense"},
            "target_items": [
                {"label": "parler", "gloss": "to speak"},
                {"label": "  ", "gloss": "blank dropped"},
                {"label": "manger", "gloss": "to eat"},
            ],
            "rationale": "core verbs first",
        })
    monkeypatch.setattr(learning.llm, "call", fake_call)
    spec = await learning.plan_next_lesson("fr", "A1", api_key="x")
    assert spec["chapter_action"] == "new"
    assert spec["chapter"]["title"] == "Regular -er verbs"
    assert spec["skill"]["kind"] == "grammar"
    assert spec["scope"] == "broad"          # default
    assert spec["focus"] == "new"            # default
    assert [i["label"] for i in spec["target_items"]] == ["parler", "manger"]  # blank dropped


@pytest.mark.asyncio
async def test_plan_next_lesson_defaults_action_without_chapter(monkeypatch):
    # An invalid/missing chapter_action defaults to "new" when no chapter is open.
    async def fake_call(prompt, **kw):
        return json.dumps({"skill": {"kind": "vocab", "key": "food", "label": "nourriture", "gloss": "food"}})
    monkeypatch.setattr(learning.llm, "call", fake_call)
    spec = await learning.plan_next_lesson("fr", "A1", api_key="x", current_chapter=None)
    assert spec["chapter_action"] == "new"


def test_concepts_from_spec_grammar_keeps_items():
    import main
    spec = {
        "skill": {"kind": "grammar", "key": "present_er", "label": "-er present", "gloss": "rule"},
        "target_items": [{"label": "parler", "gloss": "to speak"}, {"label": "manger", "gloss": "to eat"}],
    }
    concepts = main._concepts_from_spec(spec)
    assert len(concepts) == 1
    assert concepts[0]["kind"] == "grammar"
    assert concepts[0]["key"] == "present_er"
    # The forms ride along as `items` (coverage), not separate registry rows.
    assert [i["label"] for i in concepts[0]["items"]] == ["parler", "manger"]


def test_concepts_from_spec_vocab_registers_each_item():
    import main
    spec = {
        "skill": {"kind": "vocab", "key": "food_theme", "label": "food", "gloss": "food words"},
        "target_items": [{"label": "pomme", "gloss": "apple"}, {"label": "pain", "gloss": "bread"}],
    }
    concepts = main._concepts_from_spec(spec)
    assert [c["label"] for c in concepts] == ["pomme", "pain"]
    assert all(c["kind"] == "vocab" for c in concepts)
    assert all(c["key"] for c in concepts)   # keys synthesized from gloss/label


def test_brief_block_exceptions_focus():
    block = learning._brief_block({"focus": "exceptions", "scope": "narrow", "title": "Spelling changes"})
    assert "EXCEPTIONS lesson" in block
    # No brief → empty.
    assert learning._brief_block(None) == ""


# ── End-to-end orchestration: plan → chapter open/close → register ───────────

@pytest.mark.asyncio
async def test_author_next_lesson_opens_then_closes_chapter(fresh_db, monkeypatch):
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    course = {"id": cid, "target_lang": "fr", "level": "A1"}
    access = types.SimpleNamespace(api_key="x", anthropic_key=None,
                                   model_reader="gemini-2.5-flash-lite")

    # Avoid the live CEFR-backfill LLM call.
    async def no_cefr(*a, **k):
        return ""
    monkeypatch.setattr(main, "_known_cefr_stats", no_cefr)

    # Planner: lesson 1 opens chapter "Greetings"; lesson 2 opens a new chapter,
    # which must close the first into a unit.
    specs = iter([
        {"chapter_action": "new",
         "chapter": {"title": "Greetings", "objective": "Greet", "summary": "hi/bye"},
         "skill": {"kind": "vocab", "key": "greet", "label": "salut", "gloss": "hi"},
         "scope": "broad", "focus": "new",
         "target_items": [{"label": "salut", "gloss": "hi"}, {"label": "bonjour", "gloss": "hello"}],
         "rationale": "", "_raw_prompt": "P", "_raw_response": "R"},
        {"chapter_action": "new",
         "chapter": {"title": "Numbers", "objective": "Count", "summary": "1-10"},
         "skill": {"kind": "vocab", "key": "num", "label": "un", "gloss": "one"},
         "scope": "broad", "focus": "new",
         "target_items": [{"label": "un", "gloss": "one"}],
         "rationale": "", "_raw_prompt": "P", "_raw_response": "R"},
    ])

    async def fake_plan(*a, **k):
        return dict(next(specs))
    monkeypatch.setattr(main.learning, "plan_next_lesson", fake_plan)

    async def fake_author(target_lang, concepts, *a, **k):
        # Minimal but valid: one assembled drill so the no-exercise guard passes.
        authored = {"teach": [], "drills": [
            {"kind": "recognition", "concept": concepts[0]["key"],
             "target": concepts[0]["label"], "gloss": concepts[0]["gloss"],
             "distractors": ["x", "y"]},
        ]}
        content = learning.assemble_lesson(target_lang, concepts, authored)
        return {"title": "T", "objective": "O", "summary": "S", "content": content,
                "_raw_prompt": "AP", "_raw_response": "AR"}
    monkeypatch.setattr(main.learning, "author_lesson", fake_author)

    # Lesson 1 → opens the chapter, registers the vocab items.
    lid1 = await main._author_next_lesson(course, access, "gemini-2.5-flash-lite", uid)
    chapter = await db.get_active_plan(cid)
    assert chapter["title"] == "Greetings"
    reg = (await db.get_next_lesson_context(cid))["concept_registry"]
    assert {"salut", "bonjour"} <= {c["label"] for c in reg}
    # No unit closed yet.
    course_row = await db.get_course(uid, cid)
    assert [u for u in course_row["units"] if not u.get("in_progress")] == []

    # Lesson 2 → opens a NEW chapter, closing "Greetings" into a unit.
    lid2 = await main._author_next_lesson(course, access, "gemini-2.5-flash-lite", uid)
    assert lid2 != lid1
    chapter2 = await db.get_active_plan(cid)
    assert chapter2["title"] == "Numbers"
    course_row = await db.get_course(uid, cid)
    closed = [u for u in course_row["units"] if not u.get("in_progress")]
    assert len(closed) == 1 and closed[0]["title"] == "Greetings"


# ── SRS deck → lesson generation ─────────────────────────────────────────────

async def _seed_card(uid, target, gloss, lang="yue", *, interval=0.0, reps=0,
                     step=None, ease=2.5, seen=True):
    cid = await db.create_card(uid, gloss, target, target_lang=lang)
    if seen:
        await db.update_face_review(uid, cid, "target", {
            "interval_days": interval, "ease_factor": ease, "repetitions": reps,
            "next_review": "2030-01-01", "learning_step": step,
        })
    return cid


@pytest.mark.asyncio
async def test_get_known_words_filters_and_orders(fresh_db):
    uid = fresh_db
    await _seed_card(uid, "你好", "hello", interval=10, reps=4)          # strong
    await _seed_card(uid, "多謝", "thanks", interval=3, reps=1)          # known (interval)
    await _seed_card(uid, "再見", "goodbye", interval=0.01, reps=1, step=1)  # in learning
    await _seed_card(uid, "唔該", "excuse me", seen=False)               # never seen
    sus = await _seed_card(uid, "犀利", "amazing", interval=20, reps=5)  # suspended
    await db.set_card_suspended(uid, sus, True)
    await _seed_card(uid, "bonjour", "hello", lang="fr", interval=10, reps=4)  # other lang

    words = await db.get_known_words(uid, "yue")
    texts = [w["target_text"] for w in words]
    assert texts == ["你好", "多謝"]          # strongest first; others excluded
    assert words[0]["gloss"] == "hello"


@pytest.mark.asyncio
async def test_get_weak_cards_finds_low_ease_and_relapsed(fresh_db):
    uid = fresh_db
    await _seed_card(uid, "你好", "hello", interval=10, reps=4, ease=2.6)       # strong
    await _seed_card(uid, "難", "difficult", interval=1, reps=3, ease=1.5)      # low ease
    await _seed_card(uid, "跌", "to fall", reps=2, step=0, ease=2.5)            # relapsed
    await _seed_card(uid, "新", "new", reps=0, step=0, ease=2.5)                # brand new, not weak
    weak = await db.get_weak_cards(uid, "yue")
    texts = {w["target_text"] for w in weak}
    assert texts == {"難", "跌"}
    assert weak[0]["target_text"] == "難"     # lowest ease first


def test_prompts_include_deck_sections():
    known = [{"target_text": "你好", "gloss": "hello"}]
    weak = [{"target_text": "難", "gloss": "difficult"}]
    p = learning._build_lesson_prompt("yue", _CONCEPTS, [], None, None,
                                      known_words=known, weak_words=weak)
    assert "KNOWN FLASHCARD WORDS" in p and "你好 = hello" in p
    assert "STRUGGLING FLASHCARD WORDS" in p and "難 = difficult" in p
    # Absent lists → no sections.
    p2 = learning._build_lesson_prompt("yue", _CONCEPTS, [], None, None)
    assert "FLASHCARD" not in p2

    pp = learning._build_plan_prompt("yue", "A1", [], [], known_words=known)
    assert "ALREADY KNOWS" in pp and "你好 = hello" in pp


def test_filter_new_concepts_drops_known_deck_words():
    import main
    plan = [
        {"kind": "vocab", "key": "hello", "label": "你好", "gloss": "hello"},
        {"kind": "vocab", "key": "thanks", "label": "多謝", "gloss": "thanks"},
        {"kind": "grammar", "key": "copula", "label": "你好", "gloss": "..."},  # grammar exempt
    ]
    out = main._filter_new_concepts(plan, [], known_texts={"你好"})
    assert [c["key"] for c in out] == ["thanks", "copula"]


# ── Mastery ledger ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mastery_record_and_retrieve(fresh_db):
    uid = fresh_db
    results = [
        {"concept_key": "greeting_hello", "correct": 3, "total": 4},
        {"concept_key": "article_el_la",  "correct": 1, "total": 5},
    ]
    await db.record_concept_results(uid, "es", results)
    summary = await db.get_mastery_summary(uid, "es")
    by_key = {r["concept_key"]: r for r in summary}
    assert by_key["greeting_hello"]["correct"] == 3
    assert by_key["greeting_hello"]["total"] == 4
    assert by_key["article_el_la"]["correct"] == 1


@pytest.mark.asyncio
async def test_mastery_increments_on_second_attempt(fresh_db):
    uid = fresh_db
    await db.record_concept_results(uid, "fr", [{"concept_key": "present_er", "correct": 2, "total": 3}])
    await db.record_concept_results(uid, "fr", [{"concept_key": "present_er", "correct": 3, "total": 3}])
    summary = await db.get_mastery_summary(uid, "fr")
    assert summary[0]["correct"] == 5
    assert summary[0]["total"] == 6


@pytest.mark.asyncio
async def test_mastery_ignores_bad_rows(fresh_db):
    uid = fresh_db
    await db.record_concept_results(uid, "yue", [
        {"concept_key": "",          "correct": 1, "total": 1},   # blank key
        {"concept_key": "good_key",  "correct": 0, "total": 0},   # zero total
        {"concept_key": "valid_key", "correct": 1, "total": 2},
    ])
    summary = await db.get_mastery_summary(uid, "yue")
    assert len(summary) == 1
    assert summary[0]["concept_key"] == "valid_key"


# ── Chapter budget + running summary (planner rework A) ──────────────────────

def test_clamp_chapter_budget():
    assert learning.clamp_chapter_budget(None) == learning.DEFAULT_CHAPTER_BUDGET
    assert learning.clamp_chapter_budget("nope") == learning.DEFAULT_CHAPTER_BUDGET
    assert learning.clamp_chapter_budget(99) == learning.CHAPTER_BUDGET_MAX
    assert learning.clamp_chapter_budget(0) == learning.CHAPTER_BUDGET_MIN
    assert learning.clamp_chapter_budget(3) == 3


def test_roll_chapter_summary_joins_and_caps():
    assert learning.roll_chapter_summary("", "first") == "first"
    assert learning.roll_chapter_summary("first", "second") == "first; second"
    # Oldest content drops first when over the cap.
    rolled = learning.roll_chapter_summary("a" * 700, "TAIL", cap=100)
    assert len(rolled) == 100 and rolled.endswith("TAIL")


def test_chapter_block_progress_and_forced_close():
    ch = {"title": "Food", "objective": "Order a meal", "summary": "so far", "budget": 3}
    block = learning._chapter_block(ch, lessons_done=1, budget_reached=False)
    assert "1 of ~3" in block and "Food" in block
    forced = learning._chapter_block(ch, lessons_done=3, budget_reached=True)
    assert "REACHED ITS PLANNED LENGTH" in forced
    assert 'MUST open a new chapter' in forced


def test_plan_prompt_units_and_feedback_sections():
    p = learning._build_plan_prompt(
        "fr", "A1", [], [],
        unit_summaries=[{"title": "Greetings", "summary": "hi/bye"}],
        avoid_feedback="You proposed X — already taught.",
    )
    assert "Completed units" in p and "Greetings" in p
    assert "DO NOT REPEAT" in p and "already taught" in p
    # planned_lessons is part of the requested schema.
    assert "planned_lessons" in p
    # Absent → no sections.
    p2 = learning._build_plan_prompt("fr", "A1", [], [])
    assert "Completed units" not in p2 and "DO NOT REPEAT" not in p2


@pytest.mark.asyncio
async def test_plan_next_lesson_normalizes_budget_and_forces_new(monkeypatch):
    async def fake_call(prompt, **kw):
        return json.dumps({
            "chapter_action": "continue",
            "chapter": {"title": "T", "objective": "O", "summary": "S",
                        "planned_lessons": 99},
            "skill": {"kind": "vocab", "key": "k", "label": "l", "gloss": "g"},
            "target_items": [],
        })
    monkeypatch.setattr(learning.llm, "call", fake_call)
    spec = await learning.plan_next_lesson(
        "fr", "A1", api_key="x", current_chapter={"title": "T"})
    assert spec["chapter"]["budget"] == learning.CHAPTER_BUDGET_MAX
    assert spec["chapter_action"] == "continue"
    # budget_reached deterministically overrides the model's "continue".
    spec = await learning.plan_next_lesson(
        "fr", "A1", api_key="x", current_chapter={"title": "T"}, budget_reached=True)
    assert spec["chapter_action"] == "new"


def test_lesson_prompt_source_block():
    p = learning._build_lesson_prompt("fr", _CONCEPTS, [], source="THE BOOK'S RULE")
    assert "SOURCE MATERIAL" in p and "THE BOOK'S RULE" in p
    assert "SOURCE MATERIAL" not in learning._build_lesson_prompt("fr", _CONCEPTS, [])


# ── Orchestration: budget forcing, summary rollup, replan, queue ─────────────

def _mk_access():
    return types.SimpleNamespace(api_key="x", anthropic_key=None,
                                 model_reader="gemini-2.5-flash-lite")


def _fake_author_factory(captured=None):
    async def fake_author(target_lang, concepts, *a, **k):
        if captured is not None:
            captured.append(k)
        authored = {"teach": [], "drills": [
            {"kind": "recognition", "concept": concepts[0]["key"],
             "target": concepts[0]["label"] or "x", "gloss": concepts[0]["gloss"] or "g",
             "distractors": ["zz1", "zz2"]},
        ]}
        content = learning.assemble_lesson(target_lang, concepts, authored)
        return {"title": "T", "objective": "O", "summary": "S-sum", "content": content,
                "_raw_prompt": "AP", "_raw_response": "AR"}
    return fake_author


def _no_cefr(monkeypatch):
    import main
    async def no_cefr(*a, **k):
        return ""
    monkeypatch.setattr(main, "_known_cefr_stats", no_cefr)


@pytest.mark.asyncio
async def test_author_next_lesson_budget_forces_new_chapter(fresh_db, monkeypatch):
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    course = {"id": cid, "target_lang": "fr", "level": "A1"}
    _no_cefr(monkeypatch)

    # A chapter at its budget: 2 open lessons, budget 2.
    await db.set_active_plan(cid, {"title": "Ch1", "objective": "o", "summary": "s", "budget": 2})
    for i in (1, 2):
        await db.create_lesson(cid, i, f"L{i}", "", [
            {"kind": "vocab", "key": f"old{i}", "label": f"vieux{i}", "gloss": f"old{i}"},
        ], {"segments": []}, f"s{i}")

    async def fake_plan(*a, **k):
        # The model stubbornly says "continue" — the backstop must override it.
        assert k.get("budget_reached") is True
        return {"chapter_action": "continue", "chapter": {},
                "skill": {"kind": "vocab", "key": "num", "label": "un", "gloss": "one"},
                "scope": "broad", "focus": "new",
                "target_items": [{"label": "un", "gloss": "one"}],
                "_raw_prompt": "P", "_raw_response": "R"}
    monkeypatch.setattr(main.learning, "plan_next_lesson", fake_plan)
    monkeypatch.setattr(main.learning, "author_lesson", _fake_author_factory())

    await main._author_next_lesson(course, _mk_access(), "gemini-2.5-flash-lite", uid)

    course_row = await db.get_course(uid, cid)
    closed = [u for u in course_row["units"] if not u.get("in_progress")]
    assert len(closed) == 1 and closed[0]["title"] == "Ch1"
    chapter = await db.get_active_plan(cid)
    # Forced-new with no chapter payload falls back to the skill label + default budget.
    assert chapter["title"] == "un"
    assert chapter["budget"] == learning.DEFAULT_CHAPTER_BUDGET


@pytest.mark.asyncio
async def test_author_next_lesson_rolls_chapter_summary(fresh_db, monkeypatch):
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    course = {"id": cid, "target_lang": "fr", "level": "A1"}
    _no_cefr(monkeypatch)

    async def fake_plan(*a, **k):
        return {"chapter_action": "new",
                "chapter": {"title": "Ch1", "objective": "o", "summary": "opened",
                            "budget": 3},
                "skill": {"kind": "vocab", "key": "num", "label": "un", "gloss": "one"},
                "scope": "broad", "focus": "new",
                "target_items": [{"label": "un", "gloss": "one"}],
                "_raw_prompt": "P", "_raw_response": "R"}
    monkeypatch.setattr(main.learning, "plan_next_lesson", fake_plan)
    monkeypatch.setattr(main.learning, "author_lesson", _fake_author_factory())

    await main._author_next_lesson(course, _mk_access(), "gemini-2.5-flash-lite", uid)
    chapter = await db.get_active_plan(cid)
    # The authored lesson's summary is rolled into the chapter's running summary.
    assert "opened" in chapter["summary"] and "S-sum" in chapter["summary"]
    assert chapter["budget"] == 3


@pytest.mark.asyncio
async def test_author_next_lesson_replans_on_duplicate(fresh_db, monkeypatch):
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    course = {"id": cid, "target_lang": "fr", "level": "A1"}
    _no_cefr(monkeypatch)
    # "salut" is already taught.
    await db.create_lesson(cid, 1, "L1", "", [
        {"kind": "vocab", "key": "greet", "label": "salut", "gloss": "hi"},
    ], {"segments": []}, "s1")

    calls = []
    async def fake_plan(*a, **k):
        calls.append(k)
        if len(calls) == 1:
            assert not k.get("avoid_feedback")
            # Re-proposes only already-taught material.
            return {"chapter_action": "continue", "chapter": {},
                    "skill": {"kind": "vocab", "key": "greet2", "label": "salut", "gloss": "hi"},
                    "scope": "broad", "focus": "new",
                    "target_items": [{"label": "salut", "gloss": "hi"}],
                    "_raw_prompt": "P1", "_raw_response": "R1"}
        # Second call must carry explicit duplicate feedback.
        assert "salut" in k.get("avoid_feedback", "")
        return {"chapter_action": "continue", "chapter": {},
                "skill": {"kind": "vocab", "key": "greet3", "label": "bonjour", "gloss": "hello"},
                "scope": "broad", "focus": "new",
                "target_items": [{"label": "bonjour", "gloss": "hello"}],
                "_raw_prompt": "P2", "_raw_response": "R2"}
    monkeypatch.setattr(main.learning, "plan_next_lesson", fake_plan)
    monkeypatch.setattr(main.learning, "author_lesson", _fake_author_factory())

    lid = await main._author_next_lesson(course, _mk_access(), "gemini-2.5-flash-lite", uid)
    assert lid and len(calls) == 2
    reg = (await db.get_next_lesson_context(cid))["concept_registry"]
    assert "bonjour" in {c["label"] for c in reg}


@pytest.mark.asyncio
async def test_author_next_lesson_502_when_replan_still_duplicate(fresh_db, monkeypatch):
    import main
    from fastapi import HTTPException
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    course = {"id": cid, "target_lang": "fr", "level": "A1"}
    _no_cefr(monkeypatch)
    await db.create_lesson(cid, 1, "L1", "", [
        {"kind": "vocab", "key": "greet", "label": "salut", "gloss": "hi"},
    ], {"segments": []}, "s1")

    async def fake_plan(*a, **k):
        return {"chapter_action": "continue", "chapter": {},
                "skill": {"kind": "vocab", "key": "greet_again", "label": "salut", "gloss": "hi"},
                "scope": "broad", "focus": "new",
                "target_items": [{"label": "salut", "gloss": "hi"}],
                "_raw_prompt": "P", "_raw_response": "R"}
    monkeypatch.setattr(main.learning, "plan_next_lesson", fake_plan)
    monkeypatch.setattr(main.learning, "author_lesson", _fake_author_factory())

    with pytest.raises(HTTPException) as e:
        await main._author_next_lesson(course, _mk_access(), "gemini-2.5-flash-lite", uid)
    assert e.value.status_code == 502
    # No duplicate lesson shipped.
    assert (await db.get_next_lesson_context(cid))["lesson_num"] == 2


@pytest.mark.asyncio
async def test_semantic_dedup_grammar_drops_paraphrase(fresh_db, monkeypatch):
    import main
    registry = [{"kind": "grammar", "key": "present_er",
                 "label": "-er present tense", "gloss": "conjugating regular -er verbs"}]
    dup = {"kind": "grammar", "key": "er_verbs_present",
           "label": "regular -er verbs", "gloss": "present tense of -er verbs"}
    distinct = {"kind": "grammar", "key": "negation",
                "label": "negation ne...pas", "gloss": "negating verbs"}
    vocab = {"kind": "vocab", "key": "pomme", "label": "pomme", "gloss": "apple"}

    old_text = main._concept_embed_text(registry[0])
    dup_text = main._concept_embed_text(dup)
    vecs = {old_text: [1.0, 0.0], dup_text: [0.99, 0.14],
            main._concept_embed_text(distinct): [0.0, 1.0]}

    async def fake_embed(texts, api_key, **kw):
        return [vecs[t] for t in texts]
    monkeypatch.setattr(main.embeddings, "embed", fake_embed)

    out = await main._semantic_dedup_grammar([dup, distinct, vocab], registry, "fr", "key")
    assert [c["key"] for c in out] == ["negation", "pomme"]

    # Second run hits the embedding_cache — no embed call needed.
    async def boom(*a, **k):
        raise AssertionError("embed should be cached")
    monkeypatch.setattr(main.embeddings, "embed", boom)
    out2 = await main._semantic_dedup_grammar([dup, distinct, vocab], registry, "fr", "key")
    assert [c["key"] for c in out2] == ["negation", "pomme"]

    # Best-effort: an embedding failure keeps the string-dedup result.
    async def fail(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(main.embeddings, "embed", fail)
    out3 = await main._semantic_dedup_grammar([{**dup, "label": "brand new label x"}],
                                              registry, "fr", "key")
    assert len(out3) == 1


@pytest.mark.asyncio
async def test_maybe_close_chapter_on_completion(fresh_db):
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    await db.set_active_plan(cid, {"title": "Ch1", "objective": "o", "summary": "done stuff", "budget": 2})
    l1 = await db.create_lesson(cid, 1, "L1", "", [], {"segments": []}, "")
    l2 = await db.create_lesson(cid, 2, "L2", "", [], {"segments": []}, "")

    await db.complete_lesson(uid, l1, 90)
    await main._maybe_close_chapter(cid)
    assert (await db.get_active_plan(cid))["title"] == "Ch1"   # not all done yet

    await db.complete_lesson(uid, l2, 100)
    await main._maybe_close_chapter(cid)
    assert await db.get_active_plan(cid) is None
    course_row = await db.get_course(uid, cid)
    closed = [u for u in course_row["units"] if not u.get("in_progress")]
    assert len(closed) == 1 and closed[0]["title"] == "Ch1"
    assert closed[0]["summary"] == "done stuff"


@pytest.mark.asyncio
async def test_maybe_close_chapter_waits_for_budget_and_queue(fresh_db):
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    # Budget 3, only 1 lesson generated + completed → chapter stays open.
    await db.set_active_plan(cid, {"title": "Ch1", "objective": "", "summary": "", "budget": 3})
    l1 = await db.create_lesson(cid, 1, "L1", "", [], {"segments": []}, "")
    await db.complete_lesson(uid, l1, 100)
    await main._maybe_close_chapter(cid)
    assert (await db.get_active_plan(cid))["title"] == "Ch1"

    # Budget met + all open lessons complete → chapter closes into a unit.
    # (Textbook lessons no longer flow through active_plan/the queue no longer
    # gates chapter closure — they live in their own units.)
    await db.set_active_plan(cid, {"title": "Ch1", "objective": "", "summary": "", "budget": 1})
    await main._maybe_close_chapter(cid)
    assert await db.get_active_plan(cid) is None


# ── Lesson queue (textbook import) ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lesson_queue_roundtrip(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    items = [
        {"unit_title": "Ch 1", "unit_size": 2,
         "spec": {"title": "A", "skill": {"kind": "vocab", "key": "a"}}, "source": "srcA"},
        {"unit_title": "Ch 1", "unit_size": 2,
         "spec": {"title": "B", "skill": {"kind": "grammar", "key": "b"}}, "source": "srcB"},
    ]
    assert await db.add_lesson_queue(cid, items) == 2
    assert await db.count_lesson_queue(cid) == 2
    first = await db.peek_lesson_queue(cid)
    assert first["unit_title"] == "Ch 1" and first["spec"]["title"] == "A"
    assert first["source"] == "srcA" and first["unit_size"] == 2
    await db.pop_lesson_queue(first["id"])
    second = await db.peek_lesson_queue(cid)
    assert second["spec"]["title"] == "B"
    await db.clear_lesson_queue(cid)
    assert await db.count_lesson_queue(cid) == 0
    assert await db.peek_lesson_queue(cid) is None


@pytest.mark.asyncio
async def test_author_textbook_lesson_consumes_unit_queue(fresh_db, monkeypatch):
    """Textbook lessons are authored via the unit-scoped path: planner skipped,
    lesson attached DIRECTLY to its textbook unit, active_plan untouched."""
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    course = {"id": cid, "target_lang": "fr", "level": "A1"}
    _no_cefr(monkeypatch)
    unit_id = await db.create_textbook_unit_row(cid, "Book Ch 1")
    await db.add_lesson_queue(cid, [{
        "unit_title": "Book Ch 1", "unit_size": 2,
        "spec": {"title": "Greetings from the book",
                 "skill": {"kind": "vocab", "key": "greet", "label": "salut", "gloss": "hi"},
                 "target_items": [{"label": "salut", "gloss": "hi"}]},
        "source": "THE BOOK RULE: salut is informal.",
    }, {
        "unit_title": "Book Ch 1", "unit_size": 2,
        "spec": {"title": "More greetings",
                 "skill": {"kind": "vocab", "key": "bye", "label": "au revoir", "gloss": "bye"},
                 "target_items": [{"label": "au revoir", "gloss": "bye"}]},
        "source": "THE BOOK RULE: au revoir is formal.",
    }], unit_id=unit_id)

    async def fake_plan(*a, **k):
        raise AssertionError("planner must be skipped for textbook lessons")
    monkeypatch.setattr(main.learning, "plan_next_lesson", fake_plan)
    captured = []
    monkeypatch.setattr(main.learning, "author_lesson", _fake_author_factory(captured))

    lid = await main._author_textbook_lesson(course, unit_id, _mk_access(), "gemini-2.5-flash-lite", uid)
    assert lid
    # Author was grounded in the first queued item's source notes.
    assert captured[0].get("source") == "THE BOOK RULE: salut is informal."
    # No AI chapter opened — textbook lessons never touch active_plan.
    assert await db.get_active_plan(cid) is None
    # Lesson attached directly to the textbook unit; one still queued.
    full = await db.get_course(uid, cid)
    tb = [u for u in full["units"] if u.get("theme") == "textbook"]
    assert len(tb) == 1 and tb[0]["id"] == unit_id
    assert len(tb[0]["lessons"]) == 1 and tb[0]["queued_remaining"] == 1
    assert await db.count_lesson_queue_for_unit(unit_id) == 1
    # Concept registered so the AI planner sees it as taught.
    reg = (await db.get_next_lesson_context(cid))["concept_registry"]
    assert "salut" in {c["label"] for c in reg}


@pytest.mark.asyncio
async def test_author_retries_a_soft_failure_before_giving_up(fresh_db, monkeypatch):
    """A flaky author call (bad JSON, or drills that all fail validation and
    leave the lesson empty) is retried once. Making the learner tap "Generate"
    again — and wait through the whole author call a second time — is exactly
    what this does, only worse."""
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    course = {"id": cid, "target_lang": "fr", "level": "A1"}
    _no_cefr(monkeypatch)
    unit_id = await db.create_textbook_unit_row(cid, "Book Ch 1")
    await db.add_lesson_queue(cid, [{
        "unit_title": "Book Ch 1", "unit_size": 1,
        "spec": {"title": "Greetings",
                 "skill": {"kind": "vocab", "key": "greet", "label": "salut", "gloss": "hi"},
                 "target_items": [{"label": "salut", "gloss": "hi"}]},
        "source": "THE BOOK RULE: salut is informal.",
    }], unit_id=unit_id)

    good = _fake_author_factory()
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model returned junk")
        return await good(*a, **k)

    monkeypatch.setattr(main.learning, "author_lesson", flaky)
    lid = await main._author_textbook_lesson(
        course, unit_id, _mk_access(), "gemini-2.5-flash-lite", uid)
    assert lid and calls["n"] == 2
    assert await db.count_lesson_queue_for_unit(unit_id) == 0   # queue consumed once

    # A lesson with no usable exercises is also retried, then reported.
    unit2 = await db.create_textbook_unit_row(cid, "Book Ch 2")
    await db.add_lesson_queue(cid, [{
        "unit_title": "Book Ch 2", "unit_size": 1,
        "spec": {"title": "Empty",
                 "skill": {"kind": "vocab", "key": "bye", "label": "au revoir", "gloss": "bye"},
                 "target_items": [{"label": "au revoir", "gloss": "bye"}]},
        "source": "",
    }], unit_id=unit2)
    calls["n"] = 0

    async def empty(*a, **k):
        calls["n"] += 1
        return {"title": "T", "objective": "O", "summary": "S", "_raw_prompt": "P",
                "_raw_response": "R", "content": {"segments": [{"exercises": []}]}}

    monkeypatch.setattr(main.learning, "author_lesson", empty)
    with pytest.raises(main.HTTPException) as err:
        await main._author_textbook_lesson(
            course, unit2, _mk_access(), "gemini-2.5-flash-lite", uid)
    assert err.value.status_code == 502
    assert calls["n"] == main._AUTHOR_ATTEMPTS
    # The failed attempt didn't eat the queued lesson — Generate can try again.
    assert await db.count_lesson_queue_for_unit(unit2) == 1
