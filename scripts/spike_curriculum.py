"""Spike: generate + pretty-print a CEFR curriculum for eyeballing (IDEAS item 43).

Throwaway tool to judge curriculum-generation quality before building the player.
    python scripts/spike_curriculum.py fr yue        # default level A1
    python scripts/spike_curriculum.py es A2
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()
import learning


def _print_course(lang: str, course: dict):
    units = course.get("units", [])
    n_lessons = sum(len(u.get("lessons", [])) for u in units)
    n_concepts = sum(
        len(l.get("new_concepts", []))
        for u in units for l in u.get("lessons", [])
    )
    print("=" * 78)
    print(f"{course.get('language', lang)}  ·  {course.get('level','?')}  ·  "
          f"{len(units)} units / {n_lessons} lessons / {n_concepts} concepts")
    print("=" * 78)
    for ui, u in enumerate(units, 1):
        print(f"\nUNIT {ui}: {u.get('title','')}  —  {u.get('objective','')}")
        for li, l in enumerate(u.get("lessons", []), 1):
            print(f"  {ui}.{li}  {l.get('title','')}")
            print(f"        obj: {l.get('objective','')}")
            concepts = l.get("new_concepts", [])
            for c in concepts:
                kind = c.get("kind", "?")[:1].upper()
                label = c.get("label", "")
                gloss = c.get("gloss", "")
                key = c.get("key", "")
                print(f"        [{kind}] {label}  ({gloss})  ·{key}")
    print()


async def main():
    langs = [a for a in sys.argv[1:] if not a.upper() in learning.LEVELS] or ["fr", "yue"]
    level = next((a.upper() for a in sys.argv[1:] if a.upper() in learning.LEVELS), "A1")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set (.env)")
    results = await asyncio.gather(*[
        learning.generate_curriculum(lang, level, api_key=api_key) for lang in langs
    ])
    for lang, course in zip(langs, results):
        _print_course(lang, course)


if __name__ == "__main__":
    asyncio.run(main())
