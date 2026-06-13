"""Tests for the Foundations module (Hangul jamo engine + curated build)."""
import foundations as F


def test_compose_and_decompose_roundtrip():
    assert F.compose_syllable("ㄱ", "ㅏ") == "가"
    assert F.compose_syllable("ㅎ", "ㅏ", "ㄴ") == "한"
    assert F.decompose_hangul("가") == {"ㄱ", "ㅏ"}
    assert F.decompose_hangul("안녕") == {"ㅇ", "ㅏ", "ㄴ", "ㅕ"}


def test_decompose_splits_compounds():
    # 왜 = ㅇ + ㅙ(ㅗㅐ); 닭 = ㄷ + ㅏ + ㄺ(ㄹㄱ)
    assert F.decompose_hangul("왜") == {"ㅇ", "ㅗ", "ㅐ"}
    assert F.decompose_hangul("닭") == {"ㄷ", "ㅏ", "ㄹ", "ㄱ"}


def test_korean_track_exists():
    assert F.has_foundations("ko")
    assert not F.has_foundations("fr")


def test_build_units_structure():
    units = F.build_units("ko")
    assert len(units) >= 3
    # every lesson carries pre-built content with segments
    for u in units:
        assert u["theme"] == "foundations"
        for l in u["lessons"]:
            assert "segments" in l["content"]


def test_word_lessons_only_use_taught_letters():
    """Every word in a 'words' lesson must decompose to letters taught so far."""
    track = F.FOUNDATIONS["ko"]
    taught: set[str] = set()
    units = F.build_units("ko")
    # walk track + built units in lockstep to know taught-set per lesson
    ti = 0
    track_lessons = [l for u in track["units"] for l in u["lessons"]]
    built_lessons = [l for u in units for l in u["lessons"]]
    for tl, bl in zip(track_lessons, built_lessons):
        if tl["type"] == "words":
            items = [it["target"] for s in bl["content"]["segments"] for it in s["teach"]["items"]]
            for w in items:
                assert F.decompose_hangul(w) <= taught, f"{w} uses an untaught letter"
        if tl["type"] == "graphemes":
            taught |= {g["symbol"] for g in tl["graphemes"]}


def test_block_build_targets_are_valid():
    units = F.build_units("ko")
    seen = 0
    for u in units:
        for l in u["lessons"]:
            for s in l["content"]["segments"]:
                for e in s["exercises"]:
                    if e["type"] == "block_build":
                        seen += 1
                        assert len(e["target"]) == 1  # one syllable block
                        assert e["consonants"] and e["vowels"]
                        # target must be spellable from the offered jamo
                        used = F.decompose_hangul(e["target"])
                        offered = set(e["consonants"]) | set(e["vowels"])
                        assert used <= offered, f"{e['target']} needs jamo outside the keyboard"
    assert seen > 0


# ── Abugida engine (Hindi / Telugu) ──────────────────────────────────────────

def test_indic_decompose_is_char_iteration():
    # कि = क + ि (two code points); the abugida is already decomposed.
    assert F.decompose_indic("कि") == {"क", "ि"}
    assert F.decompose_indic("नाम") == {"न", "ा", "म"}
    assert F.decompose_indic("కాకి") == {"క", "ా", "ి"}


def test_abugida_tracks_exist():
    assert F.has_foundations("hi")
    assert F.has_foundations("te")
    assert F.FOUNDATIONS["hi"]["script_type"] == "abugida"
    assert F.FOUNDATIONS["te"]["script_type"] == "abugida"


@__import__("pytest").mark.parametrize("lang", ["hi", "te"])
def test_abugida_words_only_use_taught_letters(lang):
    """Every word in a 'words' lesson must decompose to letters taught so far."""
    track = F.FOUNDATIONS[lang]
    units = F.build_units(lang)
    taught: set[str] = set()
    tlessons = [l for u in track["units"] for l in u["lessons"]]
    blessons = [l for u in units for l in u["lessons"]]
    for tl, bl in zip(tlessons, blessons):
        if tl["type"] == "words":
            items = [it["target"] for s in bl["content"]["segments"] for it in s["teach"]["items"]]
            assert items, f"{lang}: a words lesson produced no readable words"
            for w in items:
                assert F.decompose_indic(w) <= taught, f"{lang}: {w} uses an untaught letter"
        taught |= {g["symbol"] for g in F._lesson_taught_graphemes(tl)}


@__import__("pytest").mark.parametrize("lang", ["hi", "te"])
def test_abugida_blends_use_concat_and_valid_keyboard(lang):
    for u in F.build_units(lang):
        for l in u["lessons"]:
            for s in l["content"]["segments"]:
                for e in s["exercises"]:
                    if e["type"] == "block_build":
                        assert e["compose"] == "concat"
                        used = F.decompose_indic(e["target"])
                        offered = set(e["consonants"]) | set(e["vowels"])
                        assert used <= offered, f"{lang}: {e['target']} outside keyboard"


def test_telugu_vowel_audio_has_visarga():
    """The Telugu voice can't articulate a bare independent vowel — appending a
    visarga makes the spoken clip audible. Display + romanisation stay the bare
    letter; only the spoken `audio` is the visarga'd form."""
    for v in F._TE_VOWELS:
        assert v["audio"] == v["symbol"] + "ః", f"{v['symbol']} audio should carry visarga"
        assert v["audio"] != v["symbol"]
        assert v["roman"] and "ః" not in v["roman"]   # romanisation untouched
    # Hindi vowels (which the voice handles fine) must NOT be altered.
    for v in F._HI_VOWELS:
        assert v["audio"] == v["symbol"]


@__import__("pytest").mark.parametrize("lang", ["ko", "hi", "te"])
def test_foundations_exercises_hide_romanization(lang):
    """Foundations is the reading track, so every exercise that shows target text
    must set hide_roman (romanisation is the answer → never shown inline)."""
    for u in F.build_units(lang):
        for l in u["lessons"]:
            for s in l["content"]["segments"]:
                for e in s["exercises"]:
                    if e["type"] in ("choice", "listening", "match", "block_build"):
                        assert e.get("hide_roman") is True, f"{lang}: {e['type']} must hide_roman"


# ── Tonal tracks (yue / cmn) ──────────────────────────────────────────────────

@__import__("pytest").mark.parametrize("lang", ["yue", "cmn"])
def test_tonal_tracks_exist(lang):
    assert F.has_foundations(lang)
    assert F.FOUNDATIONS[lang]["script_type"] == "tonal"


@__import__("pytest").mark.parametrize("lang", ["yue", "cmn"])
def test_tonal_units_have_content(lang):
    units = F.build_units(lang)
    assert len(units) >= 3
    for u in units:
        assert u["theme"] == "foundations"
        for l in u["lessons"]:
            assert "segments" in l["content"]
            for s in l["content"]["segments"]:
                assert "teach" in s and "exercises" in s


@__import__("pytest").mark.parametrize("lang", ["yue", "cmn"])
def test_tonal_exercises_hide_roman(lang):
    """All tonal foundation exercises must set hide_roman (romanisation is tested)."""
    for u in F.build_units(lang):
        for l in u["lessons"]:
            for s in l["content"]["segments"]:
                for e in s["exercises"]:
                    if e["type"] in ("choice", "listening"):
                        assert e.get("hide_roman") is True, \
                            f"{lang}: {e['type']} missing hide_roman"


@__import__("pytest").mark.parametrize("lang", ["yue", "cmn"])
def test_tonal_tone_exercises_have_valid_answers(lang):
    """Every choice exercise must have answer in [0, len(options))."""
    for u in F.build_units(lang):
        for l in u["lessons"]:
            for s in l["content"]["segments"]:
                for e in s["exercises"]:
                    if e["type"] == "choice":
                        assert 0 <= e["answer"] < len(e["options"]), \
                            f"{lang}: answer {e['answer']} out of range for {e['options']}"


def test_yue_six_tones_covered():
    """The Cantonese track must have tone pairs for all six tones."""
    units = F.build_units("yue")
    all_tone_nums = set()
    for u in units:
        for l in u["lessons"]:
            for s in l["content"]["segments"]:
                for item in s["teach"].get("items", []):
                    roman = item.get("target_roman", "")
                    if roman and roman[-1].isdigit():
                        all_tone_nums.add(int(roman[-1]))
    assert all_tone_nums >= {1, 2, 3, 4, 5, 6}, \
        f"Missing tones: {({1,2,3,4,5,6} - all_tone_nums)}"


def test_cmn_four_tones_covered():
    """The Mandarin track must have tone pairs for all four tones."""
    units = F.build_units("cmn")
    tone_marks = {"ā": 1, "á": 2, "ǎ": 3, "à": 4,
                  "ē": 1, "é": 2, "ě": 3, "è": 4,
                  "ī": 1, "í": 2, "ǐ": 3, "ì": 4,
                  "ō": 1, "ó": 2, "ǒ": 3, "ò": 4,
                  "ū": 1, "ú": 2, "ǔ": 3, "ù": 4}
    found = set()
    for u in units:
        for l in u["lessons"]:
            for s in l["content"]["segments"]:
                for item in s["teach"].get("items", []):
                    for ch in item.get("target_roman", ""):
                        if ch in tone_marks:
                            found.add(tone_marks[ch])
    assert found >= {1, 2, 3, 4}, f"Missing tones: {({1,2,3,4} - found)}"
