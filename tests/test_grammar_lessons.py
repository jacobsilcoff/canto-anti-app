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


def test_conjugation_cloze_options_come_from_the_engine():
    import asyncio
    # Model gives the right answer; the engine supplies the option list (all real
    # cells of the paradigm) and confirms the answer.
    art = asyncio.run(_run(FR_GEN, {"minimal_pairs": [True], "cloze": [True], "reorder": [True]}))
    cloze = next(e for e in art["exercises"]
                 if e["type"] == "choice" and e["instruction"] == "Fill in the blank")
    assert cloze["options"][cloze["answer"]] == grammar.conjugate_present("manger")["je"]  # "mange"
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


def test_reflexive_verb_is_not_conjugated_by_engine():
    # Regression: "s'appeler" must NOT go through the bare -er rule (which would
    # produce the wrong "s'appelent" instead of "s'appellent").
    assert grammar.conjugate_present("s'appeler") == {}
    assert grammar.conjugate_present("se lever") == {}
    assert grammar.is_reflexive("s'appeler") and not grammar.is_reflexive("appeler")


def test_corrupt_reflexive_cloze_is_dropped():
    import asyncio
    # The exact shape that produced "Ils [s'appelent] appellent Dubois": the verb
    # is already in the sentence and tagged as the reflexive infinitive.
    gen = {
        "explain": "Reflexive verbs.", "minimal_pairs": [],
        "cloze": [{"sentence": "Ils ___ appellent Dubois.", "gloss": "They are called Dubois.",
                   "answer": "s'appellent", "verb": "s'appeler", "person": "ils"}],
        "reorder": [],
    }
    art = asyncio.run(_run(gen, {"cloze": [True]},
                           concept={"key": "verb_sappeler", "label": "s'appeler", "gloss": "to be called"}))
    # Engine bails (reflexive) AND the free cloze sanity check rejects the
    # duplicated verb → no exercise survives.
    assert art["exercises"] == []


def test_wrong_paradigm_cell_is_auto_corrected():
    import asyncio
    # Model conjugates the wrong person ("avons" for je); engine fixes it to "ai".
    gen = {
        "explain": "avoir present.", "minimal_pairs": [],
        "cloze": [{"sentence": "J' ___ un chat.", "gloss": "I have a cat.",
                   "answer": "avons", "verb": "avoir", "person": "je"}],
        "reorder": [],
    }
    art = asyncio.run(_run(gen, {"cloze": [True]},
                           concept={"key": "verb_avoir", "label": "avoir", "gloss": "to have"}))
    cloze = next(e for e in art["exercises"] if e["type"] == "choice")
    assert cloze["options"][cloze["answer"]] == "ai"


def test_conjugation_table_is_engine_computed():
    # Reliable paradigm straight from the engine — never the LLM.
    t = grammar.conjugation_table("manger")
    assert t["rows"][0] == ["je", "mange"]
    assert t["rows"][3] == ["nous", "mangeons"]
    assert t["rows"][5] == ["ils / elles", "mangent"]
    assert grammar.conjugation_table("s'appeler") is None   # reflexive → no table


def test_verb_concept_gets_computed_table_in_artifact():
    import asyncio
    art = asyncio.run(_run(FR_GEN, {"minimal_pairs": [True], "cloze": [True], "reorder": [True]}))
    assert art["tables"], "verb concept should carry a conjugation table"
    assert ["nous", "mangeons"] in art["tables"][0]["rows"]


def test_generator_table_kept_only_when_critic_approves():
    import asyncio
    gen = {
        "explain": "Definite articles.", "minimal_pairs": [], "cloze": [], "reorder": [],
        "tables": [{"title": "Definite articles", "columns": ["", "masc", "fem"],
                    "rows": [["sing", "le", "la"], ["plur", "les", "les"]]}],
    }
    concept = {"key": "gender_articles", "label": "definite articles", "gloss": "le/la/les"}
    kept = asyncio.run(_run(gen, {"tables": [True]}, concept=concept))
    assert any(t["title"] == "Definite articles" for t in kept["tables"])
    dropped = asyncio.run(_run(gen, {"tables": [False]}, concept=concept))
    assert dropped["tables"] == []   # not a verb, critic rejected → no tables


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
