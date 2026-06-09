"""Tests for _next_batch — how unit-plan concepts are grouped into micro-lessons.

Grammar is taught alone (denser); straightforward vocab packs together (up to 3)
so simple words can be introduced implicitly in glossed drills.
"""
import os

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")
os.environ.setdefault("API_KEY_ENC_KEY", "")

# A real Fernet key is needed for main's crypto import.
from cryptography.fernet import Fernet
os.environ["API_KEY_ENC_KEY"] = Fernet.generate_key().decode()

import main


def _v(k):
    return {"kind": "vocab", "key": k}


def _g(k):
    return {"kind": "grammar", "key": k}


def test_empty():
    assert main._next_batch([]) == []


def test_grammar_taught_alone():
    batch = main._next_batch([_g("present"), _v("a"), _v("b")])
    assert [c["key"] for c in batch] == ["present"]


def test_vocab_packs_up_to_three():
    batch = main._next_batch([_v("a"), _v("b"), _v("c"), _v("d")])
    assert [c["key"] for c in batch] == ["a", "b", "c"]


def test_vocab_run_stops_at_grammar():
    batch = main._next_batch([_v("a"), _v("b"), _g("g"), _v("c")])
    assert [c["key"] for c in batch] == ["a", "b"]


def test_single_vocab():
    batch = main._next_batch([_v("a"), _g("g")])
    assert [c["key"] for c in batch] == ["a"]
