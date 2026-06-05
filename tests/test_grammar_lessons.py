"""Tests for the grammar content pipeline (generator + critic, blocks model).

The LLM is faked via `call_json` — generator vs critic prompts are told apart by
the word "examiner" (only the critic prompt has it). No network/API calls.
"""
import asyncio

import grammar
import grammar_lessons


def _fake(gen: dict, crit: dict):
    async def call_json(prompt: str) -> dict:
        return crit if "examiner" in prompt else gen
    return call_json


FR_GEN = {
    "blocks": [
        {"type": "prose", "text": "-er verbs: drop -er and add the present endings."},
        {"type": "table", "title": "manger — present", "columns": ["singular", "plural"],
         "rows": [["je mange", "nous mangeons"], ["tu manges", "vous mangez"], ["il mange", "ils mangent"]]},
        {"type": "examples", "items": [{"text": "Je mange une pomme.", "gloss": "I eat an apple."}]},
        {"type": "contrast", "a": {"text": "Le chat dort.", "gloss": "The cat sleeps."},
         "b": {"text": "Les chats dorment.", "gloss": "The cats sleep."}, "label": "singular vs plural subject"},
    ],
    "cloze": [{"sentence": "Je ___ une pomme.", "gloss": "I eat an apple.",
               "answer": "mange", "verb": "manger", "person": "je"}],
    "reorder": [{"sentence": "Tu manges du pain.", "tokens": ["Tu", "manges", "du", "pain"]}],
}
FR_CONCEPT = {"key": "verb_present_manger", "label": "manger",
              "gloss": "the verb manger (to eat) — present tense"}
ALL_OK = {"blocks": [True, True, True, True], "cloze": [True], "reorder": [True]}


async def _run(gen, crit, lang="fr", concept=None):
    return await grammar_lessons.generate_grammar_content(
        lang, concept or FR_CONCEPT, call_json=_fake(gen, crit))


def test_happy_path_builds_blocks_and_drills():
    art = asyncio.run(_run(FR_GEN, ALL_OK))
    assert [b["type"] for b in art["blocks"]] == ["prose", "table", "examples", "contrast"]
    # contrast-recognition (choice) + cloze (choice) + reorder (word_bank)
    assert sorted(e["type"] for e in art["exercises"]) == ["choice", "choice", "word_bank"]
    assert all(e.get("grammar") for e in art["exercises"])


def test_block_content_is_normalized_with_romanization_key():
    art = asyncio.run(_run(FR_GEN, ALL_OK))
    ex_block = next(b for b in art["blocks"] if b["type"] == "examples")
    assert "roman" in ex_block["items"][0]            # computed (empty for fr)
    contrast = next(b for b in art["blocks"] if b["type"] == "contrast")
    assert contrast["a"]["text"] and contrast["b"]["text"] and "roman" in contrast["a"]



def test_conjugation_cloze_options_come_from_the_engine():
    art = asyncio.run(_run(FR_GEN, ALL_OK))
    cloze = next(e for e in art["exercises"]
                 if e["type"] == "choice" and e["instruction"] == "Fill in the blank")
    assert cloze["options"][cloze["answer"]] == grammar.conjugate_present("manger")["je"]  # "mange"
    assert set(cloze["options"]) <= set(grammar.conjugate_present("manger").values())


def test_contrast_recognition_answer_is_one_of_the_two_sentences():
    art = asyncio.run(_run(FR_GEN, ALL_OK))
    mp = next(e for e in art["exercises"]
              if e["type"] == "choice" and e["instruction"] == "Which sentence means this?")
    assert set(mp["options"]) == {"Le chat dort.", "Les chats dorment."}
    assert mp["options"][mp["answer"]] in mp["options"]


def test_reflexive_verb_is_not_conjugated_by_engine():
    assert grammar.conjugate_present("s'appeler") == {}
    assert grammar.conjugate_present("se lever") == {}
    assert grammar.is_reflexive("s'appeler") and not grammar.is_reflexive("appeler")


def test_corrupt_reflexive_cloze_is_dropped():
    gen = {
        "blocks": [{"type": "prose", "text": "Reflexive verbs."}],
        "cloze": [{"sentence": "Ils ___ appellent Dubois.", "gloss": "They are called Dubois.",
                   "answer": "s'appellent", "verb": "s'appeler", "person": "ils"}],
        "reorder": [],
    }
    art = asyncio.run(_run(gen, {"blocks": [True], "cloze": [True], "reorder": []},
                           concept={"key": "verb_sappeler", "label": "s'appeler", "gloss": "to be called"}))
    assert art["exercises"] == []   # reflexive → engine bails; duplicate-word guard drops free cloze


def test_wrong_paradigm_cell_is_auto_corrected():
    gen = {
        "blocks": [],
        "cloze": [{"sentence": "J' ___ un chat.", "gloss": "I have a cat.",
                   "answer": "avons", "verb": "avoir", "person": "je"}],
        "reorder": [],
    }
    art = asyncio.run(_run(gen, {"cloze": [True]},
                           concept={"key": "verb_avoir", "label": "avoir", "gloss": "to have"}))
    cloze = next(e for e in art["exercises"] if e["type"] == "choice")
    assert cloze["options"][cloze["answer"]] == "ai"


def test_non_conjugable_language_uses_free_cloze():
    gen = {
        "blocks": [{"type": "prose", "text": "Spanish gender agreement."}],
        "cloze": [{"sentence": "Yo ___ pan.", "gloss": "I eat bread.", "answer": "como"}],
        "reorder": [],
    }
    art = asyncio.run(_run(gen, {"blocks": [True], "cloze": [True], "reorder": []}, lang="es",
                            concept={"key": "verb_comer", "label": "comer", "gloss": "to eat"}))
    cloze = next(e for e in art["exercises"] if e["type"] == "choice")
    assert cloze["options"][cloze["answer"]] == "como"
    assert cloze["prompt"] == "Yo ___ pan."


def test_conjugation_table_engine_helper_still_correct():
    # Engine util retained (a reliable oracle / future use), even though the LLM
    # now authors the displayed tables.
    t = grammar.conjugation_table("manger")
    assert t["columns"] == ["singular", "plural"]
    assert t["rows"][0] == ["je mange", "nous mangeons"]
    assert grammar.conjugation_table("s'appeler") is None
