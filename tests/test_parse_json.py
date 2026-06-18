import translation


def test_parses_object():
    r = translation._parse_json('{"a": 1, "b": [1, 2, 3]}')
    assert isinstance(r, dict)
    assert r["a"] == 1 and r["b"] == [1, 2, 3]


def test_parses_array_of_objects():
    """Regression: array responses (e.g. suggest_vocab_for_label) were mangled
    into invalid JSON because the parser only extracted between { and }."""
    r = translation._parse_json('[{"target": "手", "source": "hand"}, {"target": "頭", "source": "head"}]')
    assert isinstance(r, list)
    assert len(r) == 2
    assert r[0]["target"] == "手"


def test_parses_array_of_strings():
    r = translation._parse_json('["noun", "food"]')
    assert r == ["noun", "food"]


def test_strips_code_fence_around_array():
    r = translation._parse_json('```json\n[{"a": 1}]\n```')
    assert r == [{"a": 1}]


def test_strips_code_fence_around_object():
    r = translation._parse_json('```json\n{"x": [1, 2]}\n```')
    assert r == {"x": [1, 2]}


def test_trims_preamble_and_suffix_object():
    r = translation._parse_json('Here is the lesson:\n{"title": "t"}\nDone.')
    assert r == {"title": "t"}


def test_trims_prose_around_array():
    r = translation._parse_json('[{"a": 1}, {"b": 2}] that is all')
    assert r == [{"a": 1}, {"b": 2}]


def test_object_with_bracket_in_preamble_falls_back():
    """A stray [ before the JSON object must not derail object parsing."""
    r = translation._parse_json('Options [A, B]:\n{"choice": "A"}')
    assert r == {"choice": "A"}
