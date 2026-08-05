"""Report REDUNDANCY between generated lessons in a course (read-only diagnostic).

"Two lessons cover essentially the same content" is invisible to the generator's
own guards, because those guards compare *concept keys and labels* — not what a
lesson actually ended up teaching and drilling. This script compares the stored
lessons directly, so you can see the overlap the pipeline shipped.

For every pair of lessons in a course it measures four independent signals:

  words   Jaccard over the native words the lesson TAUGHT (`concepts_json`: a
          vocab concept contributes itself, a grammar concept contributes its
          `items` — the same derivation `db.get_course_vocab` uses).
  drills  Jaccard over the correct answers of the lesson's graded exercises
          (the strongest "same content" signal — two lessons can differ on paper
          and still drill the same sentences).
  keys    shared concept keys (should be ~always empty; non-empty means the
          registry dedup was bypassed).
  title   token overlap of title + objective + summary.

Each flagged pair is annotated with WHY the pipeline let it through, so a run
tells you which guard to fix rather than just that something is wrong.

Usage (on the server, from the app directory):

    docker compose exec -T app python scripts/lesson_overlap.py --list
    docker compose exec -T app python scripts/lesson_overlap.py --user 1
    docker compose exec -T app python scripts/lesson_overlap.py --course 3 --verbose
    docker compose exec -T app python scripts/lesson_overlap.py --course 3 --json > overlap.json

Read-only: it opens the DB in immutable mode and never writes.
"""
import argparse
import json
import os
import sqlite3
import sys
from itertools import combinations

DB_PATH = os.getenv("DB_PATH", "data/cards.db")

# A pair is reported when ANY signal clears its threshold.
WORD_THRESHOLD = 0.30     # ≥30% of the taught words are shared
DRILL_THRESHOLD = 0.25    # ≥25% of drilled answers are shared
TITLE_THRESHOLD = 0.50    # near-identical titles/objectives

_TITLE_STOP = {
    "a", "an", "and", "at", "be", "can", "for", "from", "how", "in", "into",
    "is", "it", "of", "on", "or", "the", "to", "use", "using", "with", "you",
    "your", "lesson", "practice", "basic", "basics", "intro", "introduction",
}


def connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        sys.exit(f"No database at {path!r} — pass --db or set DB_PATH.")
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ── Extraction ───────────────────────────────────────────────────────────────

def taught_words(concepts_json: str) -> tuple[set[str], set[str]]:
    """(registered vocab labels, grammar-item labels) — the words a lesson taught,
    split by whether the registry can see them.

    A vocab concept becomes its own `course_concepts` row, so its label is
    visible to the planner and to `_filter_new_concepts`. A grammar concept's
    words live in its `items` array and are NEVER registered, so this is the only
    place they can be read back.
    """
    try:
        concepts = json.loads(concepts_json or "[]")
    except (ValueError, TypeError):
        return set(), set()
    if not isinstance(concepts, list):
        return set(), set()
    vocab: set[str] = set()
    items: set[str] = set()
    for c in concepts:
        if not isinstance(c, dict):
            continue
        if (c.get("kind") or "vocab").strip() == "grammar":
            for w in c.get("items") or []:
                if isinstance(w, dict) and (w.get("label") or "").strip():
                    items.add(w["label"].strip())
        elif (c.get("label") or "").strip():
            vocab.add(c["label"].strip())
    return vocab, items


def concept_keys(concepts_json: str) -> set[str]:
    try:
        concepts = json.loads(concepts_json or "[]")
    except (ValueError, TypeError):
        return set()
    if not isinstance(concepts, list):
        return set()
    return {(c.get("key") or "").strip() for c in concepts
            if isinstance(c, dict) and (c.get("key") or "").strip()}


def drill_answers(content_json: str) -> set[str]:
    """The correct answer of every graded exercise, normalized.

    Covers the assembled exercise shapes in learning._assemble_drill:
    choice/listening (options[answer]), word_bank (joined answer tokens),
    cloze (options[answer] or answer text) and match (each pair's target).
    """
    try:
        content = json.loads(content_json or "{}")
    except (ValueError, TypeError):
        return set()
    if not isinstance(content, dict):
        return set()
    out: set[str] = set()
    for seg in content.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        for ex in seg.get("exercises") or []:
            if not isinstance(ex, dict):
                continue
            opts = ex.get("options")
            idx = ex.get("answer")
            if isinstance(opts, list) and isinstance(idx, int) and 0 <= idx < len(opts):
                out.add(_norm(str(opts[idx])))
                continue
            if isinstance(idx, str) and idx.strip():
                out.add(_norm(idx))
            toks = ex.get("answer_tokens")
            if isinstance(toks, list) and toks:
                out.add(_norm("".join(str(t) for t in toks)))
            for pair in ex.get("pairs") or []:
                if isinstance(pair, dict) and (pair.get("target") or "").strip():
                    out.add(_norm(pair["target"]))
    out.discard("")
    return out


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold().strip(".!?。！？,，;；:： ")


def title_tokens(*parts: str) -> set[str]:
    words = " ".join(parts or ()).casefold().replace("-", " ").split()
    return {w.strip(".,!?()") for w in words
            if w not in _TITLE_STOP and len(w) > 2} - {""}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Analysis ─────────────────────────────────────────────────────────────────

def load_lessons(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT l.id, l.lesson_num, l.title, l.objective, l.summary,
                  l.concepts_json, l.content, l.completed_at,
                  COALESCE(u.theme, '') AS theme, u.title AS unit_title
           FROM course_lessons l
           LEFT JOIN course_units u ON u.id = l.unit_id
           WHERE l.course_id=? AND COALESCE(u.theme,'') != 'foundations'
           ORDER BY l.lesson_num, l.id""",
        (course_id,),
    ).fetchall()
    lessons = []
    for r in rows:
        vocab, items = taught_words(r["concepts_json"])
        lessons.append({
            "id": r["id"], "lesson_num": r["lesson_num"],
            "title": r["title"] or "", "objective": r["objective"] or "",
            "summary": r["summary"] or "",
            # 'textbook' vs '' (AI-planned) — the source that authored it.
            "source": "textbook" if r["theme"] == "textbook" else "ai",
            "unit_title": r["unit_title"] or "(in-progress chapter)",
            "done": r["completed_at"] is not None,
            "words": vocab | items,
            "vocab_labels": vocab, "item_labels": items,
            "keys": concept_keys(r["concepts_json"]),
            "drills": drill_answers(r["content"]),
            "titles": title_tokens(r["title"] or "", r["objective"] or "",
                                   r["summary"] or ""),
        })
    return lessons


def explain(a: dict, b: dict, shared_keys: set[str]) -> list[str]:
    """Why the generator's guards let this pair through.

    Mirrors the real guards: `_filter_new_concepts` (concept keys + vocab
    labels), `_semantic_dedup_grammar` (grammar label+gloss embeddings) and the
    textbook path's deliberate `deduped or raw_concepts` override.
    """
    why = []
    first, second = (a, b) if a["lesson_num"] <= b["lesson_num"] else (b, a)
    shared_words = a["words"] & b["words"]

    if shared_keys:
        why.append(f"shared concept KEYS ({', '.join(sorted(shared_keys))}) — "
                   "only the textbook path ships these, by design "
                   "(`deduped or raw_concepts`)")
    # Words carried in a grammar concept's `items` are not dedup inputs at all:
    # the filter compares vocab-concept labels, and items never become rows.
    via_items = sorted(shared_words & (a["item_labels"] | b["item_labels"]))
    if via_items:
        why.append(f"{' '.join(via_items[:6])} ride in a grammar concept's "
                   "`items` — items are never registered in course_concepts and "
                   "are never compared, so re-teaching them is unguarded")
    both_vocab = sorted(shared_words & a["vocab_labels"] & b["vocab_labels"])
    if both_vocab and second["source"] == "ai" and not shared_keys:
        why.append(f"{' '.join(both_vocab[:6])} were registered vocab concepts "
                   "on both sides — _filter_new_concepts should have caught this; "
                   "suspect a citation-form mismatch (spacing, classifier, "
                   "punctuation)")
    if second["source"] == "textbook" and (both_vocab or shared_keys):
        why.append("the later lesson came from a textbook — that path teaches "
                   "known material anyway, so the overlap is intentional")
    if not shared_keys and not shared_words:
        why.append("no key or label collision at all — only the CONTENT "
                   "overlaps, and nothing in the pipeline compares lesson "
                   "content or titles")
    return why


def analyse(lessons: list[dict]) -> list[dict]:
    flagged = []
    for a, b in combinations(lessons, 2):
        w = jaccard(a["words"], b["words"])
        d = jaccard(a["drills"], b["drills"])
        t = jaccard(a["titles"], b["titles"])
        shared_keys = a["keys"] & b["keys"]
        if not (w >= WORD_THRESHOLD or d >= DRILL_THRESHOLD
                or t >= TITLE_THRESHOLD or shared_keys):
            continue
        flagged.append({
            "a": {"id": a["id"], "num": a["lesson_num"], "title": a["title"],
                  "source": a["source"], "unit": a["unit_title"]},
            "b": {"id": b["id"], "num": b["lesson_num"], "title": b["title"],
                  "source": b["source"], "unit": b["unit_title"]},
            "word_overlap": round(w, 3),
            "drill_overlap": round(d, 3),
            "title_overlap": round(t, 3),
            "score": round(max(w, d, t) + (0.5 if shared_keys else 0), 3),
            "shared_keys": sorted(shared_keys),
            "shared_words": sorted(set(a["words"]) & set(b["words"]))[:20],
            "shared_drills": sorted(a["drills"] & b["drills"])[:12],
            "why": explain(a, b, shared_keys),
        })
    flagged.sort(key=lambda p: -p["score"])
    return flagged


def report(conn: sqlite3.Connection, course: sqlite3.Row, verbose: bool) -> dict:
    lessons = load_lessons(conn, course["id"])
    flagged = analyse(lessons)
    n_ai = sum(1 for l in lessons if l["source"] == "ai")
    n_tb = len(lessons) - n_ai

    # Words the planner can see (registered as vocab concepts) vs words it
    # cannot (taught only inside a grammar concept's `items`). The second set is
    # exactly the blind spot that lets a later lesson re-teach them.
    registered = conn.execute(
        "SELECT COUNT(*) FROM course_concepts WHERE course_id=?", (course["id"],)
    ).fetchone()[0]
    # A word counts as invisible when the lesson that FIRST taught it did so
    # only through a grammar concept's `items` — from then on, every later plan
    # is free to teach it again.
    seen: set[str] = set()
    all_words: set[str] = set()
    invisible_set: set[str] = set()
    for l in lessons:
        all_words |= l["words"]
        invisible_set |= (l["item_labels"] - l["vocab_labels"]) - seen
        seen |= l["words"]
    invisible = sorted(invisible_set)

    print(f"\n=== course {course['id']} · {course['target_lang']} "
          f"(level {course['level']}, user {course['user_id']}) ===")
    print(f"{len(lessons)} lessons ({n_ai} AI-planned, {n_tb} from textbooks) · "
          f"{registered} registered concepts · {len(all_words)} distinct words taught")
    if invisible:
        print(f"  ⚠ {len(invisible)} of them are invisible to the planner "
              f"(taught only inside a grammar concept's `items`): "
              f"{' '.join(invisible[:12])}"
              f"{' …' if len(invisible) > 12 else ''}")

    if not flagged:
        print("  ✓ no lesson pair crosses the overlap thresholds.")
    for p in flagged:
        print(f"\n  • L{p['a']['num']} “{p['a']['title']}” [{p['a']['source']}]"
              f"\n    L{p['b']['num']} “{p['b']['title']}” [{p['b']['source']}]"
              f"\n    words {p['word_overlap']:.0%} · drills {p['drill_overlap']:.0%}"
              f" · title {p['title_overlap']:.0%}")
        if p["shared_keys"]:
            print(f"    shared keys : {', '.join(p['shared_keys'])}")
        if p["shared_words"]:
            print(f"    shared words: {' '.join(p['shared_words'])}")
        if verbose and p["shared_drills"]:
            print(f"    same answers: {' | '.join(p['shared_drills'])}")
        for w in p["why"]:
            print(f"    ↳ {w}")

    return {
        "course_id": course["id"], "target_lang": course["target_lang"],
        "user_id": course["user_id"],
        "lessons": len(lessons), "ai_lessons": n_ai, "textbook_lessons": n_tb,
        "registered_concepts": registered, "distinct_words_taught": len(all_words),
        "words_invisible_to_planner": invisible,
        "flagged_pairs": flagged,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB_PATH, help=f"SQLite path (default {DB_PATH})")
    ap.add_argument("--course", type=int, action="append",
                    help="course id (repeatable); default = every course")
    ap.add_argument("--user", type=int, help="restrict to one user id")
    ap.add_argument("--list", action="store_true",
                    help="list courses with lesson counts and exit")
    ap.add_argument("--verbose", action="store_true",
                    help="also print the identical drill answers")
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON on stdout")
    args = ap.parse_args()

    conn = connect(args.db)
    sql = ("SELECT c.id, c.user_id, c.target_lang, c.level, u.username, "
           "  (SELECT COUNT(*) FROM course_lessons l WHERE l.course_id=c.id) AS n "
           "FROM courses c JOIN users u ON u.id=c.user_id")
    params: tuple = ()
    if args.user:
        sql += " WHERE c.user_id=?"
        params = (args.user,)
    sql += " ORDER BY c.user_id, c.id"
    courses = [r for r in conn.execute(sql, params).fetchall()
               if not args.course or r["id"] in args.course]

    if args.list:
        for c in courses:
            print(f"course {c['id']:>4}  user {c['user_id']} ({c['username']})  "
                  f"{c['target_lang']}  {c['n']} lessons")
        return

    reports = [report(conn, c, args.verbose) for c in courses if c["n"]]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        total = sum(len(r["flagged_pairs"]) for r in reports)
        print(f"\n{total} overlapping lesson pair(s) across "
              f"{len(reports)} course(s).")


if __name__ == "__main__":
    main()
