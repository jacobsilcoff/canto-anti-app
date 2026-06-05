"""Grammar engine — reliable verb conjugation + form drills (IDEAS item 43).

Forms are computed from regular rules + a curated table of common/irregular verbs
(generated and verified against verbecc/Verbiste at dev time — see
scripts/dump_fr_conjugations is not committed; verbecc is a dev-only oracle).
No heavy runtime deps. French present tense for the MVP; the structure
generalises — add a table + rules (and optionally an external engine) per language.
"""
import random

PERSONS = ["je", "tu", "il", "nous", "vous", "ils"]

# Curated present-tense forms (bare, no pronoun), verified against Verbiste.
_FR_PRESENT: dict[str, list[str]] = {
    "être": ["suis", "es", "est", "sommes", "êtes", "sont"],
    "avoir": ["ai", "as", "a", "avons", "avez", "ont"],
    "aller": ["vais", "vas", "va", "allons", "allez", "vont"],
    "faire": ["fais", "fais", "fait", "faisons", "faites", "font"],
    "vouloir": ["veux", "veux", "veut", "voulons", "voulez", "veulent"],
    "pouvoir": ["peux", "peux", "peut", "pouvons", "pouvez", "peuvent"],
    "devoir": ["dois", "dois", "doit", "devons", "devez", "doivent"],
    "prendre": ["prends", "prends", "prend", "prenons", "prenez", "prennent"],
    "venir": ["viens", "viens", "vient", "venons", "venez", "viennent"],
    "dire": ["dis", "dis", "dit", "disons", "dites", "disent"],
    "voir": ["vois", "vois", "voit", "voyons", "voyez", "voient"],
    "savoir": ["sais", "sais", "sait", "savons", "savez", "savent"],
    "partir": ["pars", "pars", "part", "partons", "partez", "partent"],
    "dormir": ["dors", "dors", "dort", "dormons", "dormez", "dorment"],
    "mettre": ["mets", "mets", "met", "mettons", "mettez", "mettent"],
    "boire": ["bois", "bois", "boit", "buvons", "buvez", "boivent"],
    "manger": ["mange", "manges", "mange", "mangeons", "mangez", "mangent"],
    "commencer": ["commence", "commences", "commence", "commençons", "commencez", "commencent"],
    "acheter": ["achète", "achètes", "achète", "achetons", "achetez", "achètent"],
    "appeler": ["appelle", "appelles", "appelle", "appelons", "appelez", "appellent"],
    "préférer": ["préfère", "préfères", "préfère", "préférons", "préférez", "préfèrent"],
    "sortir": ["sors", "sors", "sort", "sortons", "sortez", "sortent"],
    "lire": ["lis", "lis", "lit", "lisons", "lisez", "lisent"],
    "écrire": ["écris", "écris", "écrit", "écrivons", "écrivez", "écrivent"],
    "connaître": ["connais", "connais", "connaît", "connaissons", "connaissez", "connaissent"],
    "attendre": ["attends", "attends", "attend", "attendons", "attendez", "attendent"],
}


def _fr_regular_present(verb: str) -> list[str] | None:
    """Regular French present tense for -er / -ir (finir-type) / -re verbs."""
    if len(verb) < 3:
        return None
    stem, ending = verb[:-2], verb[-2:]
    if ending == "er":
        forms = [stem + s for s in ["e", "es", "e", "ons", "ez", "ent"]]
        if verb.endswith("ger"):       # manger → nous mangeons
            forms[3] = stem + "eons"
        elif verb.endswith("cer"):     # commencer → nous commençons
            forms[3] = stem[:-1] + "çons"
        return forms
    if ending == "ir":
        return [stem + s for s in ["is", "is", "it", "issons", "issez", "issent"]]
    if ending == "re":
        return [stem + s for s in ["s", "s", "", "ons", "ez", "ent"]]
    return None


def has_conjugation(lang: str) -> bool:
    return lang == "fr"


def is_reflexive(verb: str) -> bool:
    """Pronominal/reflexive infinitive (se laver, s'appeler, s'asseoir)."""
    v = (verb or "").strip().lower()
    return v.startswith("se ") or v.startswith("s'") or v.startswith("s’")


def conjugate_present(verb: str, lang: str = "fr") -> dict[str, str]:
    """Return {person: form} for the present tense, or {} if not conjugable.

    Reflexive/pronominal verbs (s'appeler, se lever) are NOT handled — they need
    a reflexive-pronoun paradigm (me/te/se…) we don't model, and the bare -er
    rule would silently produce wrong forms (s'appelent). We return {} so callers
    fall back rather than drill incorrect French.
    """
    if lang != "fr" or not verb:
        return {}
    verb = verb.strip().lower()
    if is_reflexive(verb):
        return {}
    forms = _FR_PRESENT.get(verb) or _fr_regular_present(verb)
    if not forms:
        return {}
    return dict(zip(PERSONS, forms))


def conjugation_table(verb: str, lang: str = "fr") -> dict | None:
    """A reliable, ENGINE-computed conjugation table for the teach screen, or
    None if not conjugable. Canonical layout: person down (1/2/3), singular vs
    plural across, each cell = pronoun + form ("je mange" / "nous mangeons")."""
    forms = conjugate_present(verb, lang)
    if not forms:
        return None
    rows = [
        [with_pronoun("je", forms["je"]), with_pronoun("nous", forms["nous"])],
        [with_pronoun("tu", forms["tu"]), with_pronoun("vous", forms["vous"])],
        [f'il / elle {forms["il"]}', f'ils / elles {forms["ils"]}'],
    ]
    return {"title": f"{verb} — present tense", "columns": ["singular", "plural"], "rows": rows}


def with_pronoun(person: str, form: str) -> str:
    """Attach the subject pronoun, with je→j' elision before a vowel sound."""
    if person == "je" and form[:1].lower() in "aeiouhâàéèêîïôûœ":
        return "j'" + form
    return person + " " + form


def build_conjugation_exercises(verb: str, concept_key: str, n: int = 3) -> list[dict]:
    """Deterministic 'form drill' exercises (reusing the `choice` type): given a
    verb + a person, pick the correct conjugated form. Distractors are other
    cells of the same paradigm — automatically plausible, exactly one correct."""
    forms = conjugate_present(verb)
    if not forms:
        return []
    all_forms = list(dict.fromkeys(forms.values()))
    exercises = []
    for person in random.sample(PERSONS, min(n, len(PERSONS))):
        correct = forms[person]
        distractors = [f for f in all_forms if f != correct]
        random.shuffle(distractors)
        opts = [correct] + distractors[:3]
        random.shuffle(opts)
        exercises.append({
            "type": "choice", "concept_key": concept_key, "grammar": True,
            "instruction": f"Conjugate “{verb}” for “{person}” (present)",
            "prompt": "", "prompt_lang": "english",
            "options": opts, "answer": opts.index(correct),
            "tip": f"{verb} — present tense",
        })
    return exercises
