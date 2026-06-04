"""Tests for the grammar-first content pipeline (generator + critic).

The LLM is faked via `call_json` — generator vs critic prompts are told apart by
the word "examiner" (only the critic prompt has it). No network/API calls.
"""
import grammar
import grammar_lessons


def _fake(gen: dict, crit: dict):
    async def call_json(prompt: str) -> dict:
        return crit if "examiner" in prompt else gen
    return call_json


FR_GEN = {
    "explain": "French present-tense -er verbs drop -er and add endings.",
    "minimal_pairs": [
        {"a_text": "Le chat dort.", "a_gloss": "The cat sleeps.",
         "b_text": "Les chats dorment.", "b_gloss": "The cats sleep.",
         "contrast": "singular vs plural subject"},
    ],
    "cloze": [
        {"sentence": "Je ___ une pomme.", "gloss": "I eat an apple.",
         "answer": "mange", "verb": "manger", "person": "je"},
    ],
    "reorder": [
        {"sentence": "Tu manges du pain.", "tokens": ["Tu", "manges", "du", "pain"]},
    ],
}
FR_CONCEPT = {"key": "verb_present_manger", "label": "manger",
              "gloss": "the verb manger (to eat) — present tense"}


async def _run(gen, crit, lang="fr", concept=None):
    return await grammar_lessons.generate_grammar_content(
        lang, concept or FR_CONCEPT, call_json=_fake(gen, crit))


def test_happy_path_builds_all_pieces():
    import asyncio
    art = asyncio.run(_run(FR_GEN, {"minimal_pairs": [True], "cloze": [True], "reorder": [True]}))
    assert art["explain"]
    assert len(art["minimal_pairs"]) == 1
    types = sorted(e["type"] for e in art["exercises"])
    # minimal-pair recognition (choice) + cloze (choice) + reorder (word_bank)
    assert types == ["choice", "choice", "word_bank"]
    assert all(e.get("grammar") for e in art["exercises"])


def test_conjugation_cloze_answer_is_engine_computed_not_trusted():
    import asyncio
    # Model lies about the answer; the engine must override it.
    gen = {**FR_GEN, "cloze": [{**FR_GEN["cloze"][0], "answer": "WRONG"}]}
    art = asyncio.run(_run(gen, {"minimal_pairs": [True], "cloze": [True], "reorder": [True]}))
    cloze = next(e for e in art["exercises"]
                 if e["type"] == "choice" and e["instruction"] == "Fill in the blank")
    assert cloze["options"][cloze["answer"]] == grammar.conjugate_present("manger")["je"]  # "mange"
    assert "WRONG" not in cloze["options"]
    assert set(cloze["options"]) <= set(grammar.conjugate_present("manger").values())


def test_critic_drops_rejected_items():
    import asyncio
    art = asyncio.run(_run(FR_GEN, {"minimal_pairs": [False], "cloze": [False], "reorder": [True]}))
    assert art["minimal_pairs"] == []
    # Only the reorder survived.
    assert [e["type"] for e in art["exercises"]] == ["word_bank"]


def test_minimal_pair_recognition_answer_is_one_of_the_two_sentences():
    import asyncio
    art = asyncio.run(_run(FR_GEN, {"minimal_pairs": [True], "cloze": [True], "reorder": [True]}))
    mp = next(e for e in art["exercises"]
              if e["type"] == "choice" and e["instruction"] == "Which sentence means this?")
    assert set(mp["options"]) == {"Le chat dort.", "Les chats dorment."}
    assert mp["options"][mp["answer"]] in mp["options"]


def test_non_conjugable_language_uses_free_cloze():
    import asyncio
    gen = {
        "explain": "Spanish gender agreement.",
        "minimal_pairs": [],
        "cloze": [{"sentence": "Yo ___ pan.", "gloss": "I eat bread.", "answer": "como"}],
        "reorder": [],
    }
    art = asyncio.run(_run(gen, {"cloze": [True]}, lang="es",
                            concept={"key": "verb_comer", "label": "comer", "gloss": "to eat"}))
    cloze = next(e for e in art["exercises"] if e["type"] == "choice")
    assert cloze["options"][cloze["answer"]] == "como"
    assert cloze["prompt"] == "Yo ___ pan."
