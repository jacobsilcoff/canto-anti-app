"""Tests for the grammar engine (French present-tense conjugation + form drills)."""
import grammar


def test_regular_er():
    assert list(grammar.conjugate_present("parler").values()) == \
        ["parle", "parles", "parle", "parlons", "parlez", "parlent"]


def test_regular_ir_and_re():
    assert grammar.conjugate_present("finir")["nous"] == "finissons"
    assert grammar.conjugate_present("vendre")["il"] == "vend"


def test_spelling_changes():
    assert grammar.conjugate_present("manger")["nous"] == "mangeons"
    assert grammar.conjugate_present("commencer")["nous"] == "commençons"


def test_irregulars():
    assert grammar.conjugate_present("être")["nous"] == "sommes"
    assert grammar.conjugate_present("avoir")["ils"] == "ont"
    assert grammar.conjugate_present("aller")["je"] == "vais"
    assert grammar.conjugate_present("faire")["vous"] == "faites"


def test_pronoun_elision():
    assert grammar.with_pronoun("je", "ai") == "j'ai"
    assert grammar.with_pronoun("je", "parle") == "je parle"
    assert grammar.with_pronoun("nous", "avons") == "nous avons"


def test_non_french_has_no_conjugation():
    assert not grammar.has_conjugation("yue")
    assert grammar.conjugate_present("foo", lang="yue") == {}


def test_build_form_drills_are_correct():
    exs = grammar.build_conjugation_exercises("parler", "key_parler", n=3)
    assert len(exs) == 3
    forms = set(grammar.conjugate_present("parler").values())
    for e in exs:
        assert e["type"] == "choice"
        # the marked answer is the real conjugated form for the asked person
        person = e["instruction"].split("for ")[1].strip("“”")
        assert e["options"][e["answer"]] == grammar.conjugate_present("parler")[person]
        # all options are real cells of the paradigm (plausible distractors)
        assert set(e["options"]) <= forms
