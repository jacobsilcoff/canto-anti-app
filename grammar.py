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


def conjugate_present(verb: str, lang: str = "fr") -> dict[str, str]:
    """Return {person: form} for the present tense, or {} if not conjugable."""
    if lang != "fr" or not verb:
        return {}
    verb = verb.strip().lower()
    forms = _FR_PRESENT.get(verb) or _fr_regular_present(verb)
    if not forms:
        return {}
    return dict(zip(PERSONS, forms))


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
            "type": "choice", "concept_key": concept_key,
            "instruction": f"Conjugate “{verb}” for “{person}”",
            "prompt": "", "prompt_lang": "english",
            "options": opts, "answer": opts.index(correct),
            "tip": "present tense",
        })
    return exercises
