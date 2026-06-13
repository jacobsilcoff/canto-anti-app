"""Tests for _concepts_from_spec — how a planner lesson_spec becomes the concept
list the author teaches AND the registry records.

A GRAMMAR skill is ONE concept carrying its forms as `items` (coverage within the
lesson, not separate registry rows). A VOCAB lesson registers EACH target_item as
its own vocab concept. This is the successor to the old micro-lesson batching.
"""
import os

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")
os.environ.setdefault("API_KEY_ENC_KEY", "")

# A real Fernet key is needed for main's crypto import.
from cryptography.fernet import Fernet
os.environ["API_KEY_ENC_KEY"] = Fernet.generate_key().decode()

import main


def test_empty_spec():
    assert main._concepts_from_spec({}) == []


def test_grammar_is_one_concept_with_items():
    spec = {
        "skill": {"kind": "grammar", "key": "present_er", "label": "-er present", "gloss": "rule"},
        "target_items": [{"label": "parler", "gloss": "to speak"},
                         {"label": "manger", "gloss": "to eat"}],
    }
    concepts = main._concepts_from_spec(spec)
    assert [c["key"] for c in concepts] == ["present_er"]
    assert concepts[0]["kind"] == "grammar"
    assert [i["label"] for i in concepts[0]["items"]] == ["parler", "manger"]


def test_grammar_key_synthesized_from_label():
    spec = {"skill": {"kind": "grammar", "label": "Comparatives with plus"}, "target_items": []}
    concepts = main._concepts_from_spec(spec)
    assert concepts[0]["key"]            # a slug was derived
    assert concepts[0]["kind"] == "grammar"


def test_vocab_registers_each_item():
    spec = {
        "skill": {"kind": "vocab", "key": "food", "label": "food", "gloss": "food words"},
        "target_items": [{"label": "pomme", "gloss": "apple"},
                         {"label": "pain", "gloss": "bread"},
                         {"label": "lait", "gloss": "milk"}],
    }
    concepts = main._concepts_from_spec(spec)
    assert [c["label"] for c in concepts] == ["pomme", "pain", "lait"]
    assert all(c["kind"] == "vocab" and c["key"] for c in concepts)


def test_vocab_item_blank_labels_dropped():
    spec = {
        "skill": {"kind": "vocab", "label": "x"},
        "target_items": [{"label": "  ", "gloss": "blank"}, {"label": "chat", "gloss": "cat"}],
    }
    concepts = main._concepts_from_spec(spec)
    assert [c["label"] for c in concepts] == ["chat"]


def test_vocab_keys_deduped_within_lesson():
    # Two items whose glosses slug to the same key still get distinct keys.
    spec = {
        "skill": {"kind": "vocab", "label": "x"},
        "target_items": [{"label": "banque", "gloss": "bank"},
                         {"label": "rive", "gloss": "bank"}],
    }
    concepts = main._concepts_from_spec(spec)
    keys = [c["key"] for c in concepts]
    assert len(set(keys)) == 2


def test_vocab_falls_back_to_skill_when_no_items():
    spec = {"skill": {"kind": "vocab", "key": "merci", "label": "merci", "gloss": "thanks"},
            "target_items": []}
    concepts = main._concepts_from_spec(spec)
    assert [c["label"] for c in concepts] == ["merci"]
