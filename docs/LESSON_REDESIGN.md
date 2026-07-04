# Lesson Redesign — audit + proposal (bite-sized steps, gamification, pacing, customization)

> **Status: PROPOSAL — nothing here is implemented.** This doc + the mockups in
> `mockups/lesson-redesign/` are the deliverable of a design/ideation pass. It is
> written to be self-contained: a fresh Claude session should be able to implement
> any phase below without other context. Review the mockups at
> `https://dev.canto-ank.silcoff-labs.ca/mockups/lesson-redesign/` (dev-only, no login).

---

## 1 · Why this exists

Owner request (2026-07): audit the lesson section and propose improvements for

1. **Gamification** — what would make this competitive with Duolingo?
2. **Lesson size/organization** — lessons feel long; drill runs drag; make things
   more "bite-sized" (e.g. long drill sets could be their own step).
3. **Teaching content & pacing** — better interactive learning, better pacing.
4. **Customization** — let learners shape lessons where possible.

Explicit ask for creativity and willingness to make big changes; mockups first,
implementation in a later session.

---

## 2 · Audit — how a lesson works today

### 2.1 Pipeline (files & functions)

| Piece | Where | What it does today |
|---|---|---|
| Planner | `learning.plan_next_lesson` (`learning.py:266`) | 1 cheap LLM call picks the next skill from live state; returns `lesson_spec` |
| Author | `learning.author_lesson` (`learning.py:815`) | 1 LLM call writes teach blocks + 7–12 drills for the skill |
| Assembly | `learning.assemble_lesson` (`learning.py:732`) | Deterministic: validates drills, builds answer keys, **always emits exactly ONE segment** `{teach:{intro,blocks}, exercises:[…]}` + appends one `construction_drill` per grammar concept |
| Orchestration | `main._author_next_lesson` (`main.py:2989`), `main.next_lesson` (`main.py:3121`) | plan → chapter bookkeeping → dedup → author → `db.create_lesson`; batch loop 1–6; metering |
| Review interleave | `main._pick_review_concepts` (`main.py:2944`) | up to 2 old concepts join the drill set (weak-mastery first) |
| Player | `static/learn.html` (~2 900 lines) | `openLesson → startSegment → renderTeach → startExercises → renderExercise loop → afterSegment → mistake review → finishLesson → results` |
| Completion | `main.complete_lesson` (`main.py:3296`) | XP (first completion only, clamp `_MAX_LESSON_XP=300`), crowns 0–3, mastery ledger, auto-add vocab to deck |

Key present-day facts an implementer needs:

- **The player already supports multi-segment lessons** (`player.segments`,
  `startSegment(i)`, `afterSegment()` — `learn.html:2165–2198`), including a
  per-segment teach screen ("part i/n" title). Only the *generator* is stuck at
  one segment. This is the single cheapest structural lever we have.
- Drill count is hardcoded in the author prompt: `n_drills = "8–12" if review
  else "7–10"` (`learning.py:463`).
- The teach section renders as **one scroll page** of all blocks
  (`renderTeach`, `learn.html:2486`) — a grammar lesson's 4–8 blocks are a wall
  of text before the first interaction.
- `construction_drill` (LLM-graded 4-turn translate loop, `tutor.run_lesson_drill`)
  is appended **at the end of the already-long drill run**, one per grammar
  concept (`assemble_lesson`, `learning.py:796–806`).
- Existing gamification: combo meter + escalating XP (`comboXp = 10 + 2·min(combo,5)`,
  `learn.html:2010`), perfect-lesson bonus (+25), crowns (replays), daily XP ring
  (goal fixed at `_DAILY_XP_GOAL = 50`, `main.py:2172`), streak (`study_activity`),
  confetti/sfx/haptics. Mini-games (`speed_round`, `audio_blitz`, `memory_match`)
  exist but are **foundations-track only** (`foundations.py`, self-managed
  exercises in the `SELF_MANAGED` set).
- Existing social plumbing (unused by lessons): `friendships` table +
  `db.get_friends`, `points_ledger` (per-user, per-lang, timestamped) — everything
  a friends leaderboard needs already exists.
- Existing customization: course CEFR level at creation, `learner_profile`
  free-text setting, `lesson_buffer` (auto-generate-ahead count), admin-only
  `lesson_model`. **No learner-facing control over lesson length, focus, or
  daily goal.**

### 2.2 Pain points (ranked by user impact)

1. **The lesson is a monolith.** Teach-wall → 8–14 graded drills → a 4-turn AI
   drill → mistake review, all as one sitting (~15–20 interactions). Duolingo
   sessions are 4–8 interactions; ours feel like a class period, not a snack.
2. **All teaching is front-loaded and passive.** Read everything, then do
   everything. Retention research and every competitor do: teach a little →
   use it immediately → teach the next bit.
3. **The construction drill double-lengthens grammar lessons** — it's the best
   exercise we have and it's buried at the point of maximum fatigue.
4. **No mid-term goals between "this lesson" and "the streak".** No quests, no
   checkpoints, no social comparison. XP accrues but *pushes* nothing.
5. **Nothing to do with a finished path** except replay for crowns or generate
   more. No practice hub for AI-lesson material (mini-games are foundations-only).
6. **Zero learner agency** — can't say "short lessons please", "more
   conversation practice", "I already know this — let me skip".

---

## 3 · Proposals

Each has: what/why, implementation sketch, and a T-shirt estimate.
**Bold** = recommended for Phase 1.

### Theme A — bite-sized structure (the big one)

#### **A1 · Multi-segment "steps" — author lessons as 2–4 micro-steps** (M)

One authored lesson becomes 2–4 **steps**, each = 0–2 teach blocks + 3–5 drills,
teach interleaved with practice. The player already supports this; the change is
almost entirely in generation:

- `learning._build_lesson_prompt`: ask for `"steps": [{teach_blocks:[…],
  drills:[…]}, …]` instead of flat `teach` + `drills`. Rules: step 1 =
  warm-up/hook (1 recognition drill on known material or the lesson's easiest new
  item), each subsequent step introduces ≤2 blocks then immediately drills them;
  final step mixes everything. Keep total drills the same (or governed by A3).
- `learning.assemble_lesson`: map each authored step → one segment (reuse all
  existing per-drill validation verbatim). Fallback: if the model returns the old
  flat shape, wrap it in one segment (back-compat is free — old stored lessons
  already play).
- `examples/lesson_example.json`: rewrite the golden example in step shape —
  this is the main quality lever, per the existing convention.
- Player: `startSegment` already handles it. Add a **segmented progress bar**
  (one pill per step, filling left→right) in place of the single bar; label the
  teach screen "Step 2 of 4 · <step topic>".
- `construction_drill` becomes its **own final step** (segment with no teach,
  one exercise) instead of trailing the drill run — see A2.

Files: `learning.py`, `examples/lesson_example.json`, `static/learn.html`
(progress bar + minor `startSegment` label work). No schema change —
`content.segments` is already the stored shape.

#### **A2 · Construction drill: separate step, 3 turns, skippable** (S)

- In `assemble_lesson`, emit the construction drill as its own segment (see A1).
- Cut default turns 4 → 3 (`tutor`/`learn.html` drill loop constant).
- Add a "Skip AI practice" ghost button under the drill (counts as formative
  pass, same as the current LLM-failure path).
- On the path, the step is visible in the lesson intro sheet ("Learn · Practice
  · ✨ AI Speak") so it feels like a feature, not a tail.

#### A3 · Lesson length setting: Quick / Standard / Thorough (S)

User setting `lesson_length` (default `standard`):
`quick` → 4–6 drills, ≤2 steps, teach ultra-compressed; `standard` → today's
volume in 3 steps; `thorough` → 10–14 drills, 4 steps, extra examples block.
Implementation: plumb through `main._author_next_lesson` → `author_lesson(brief…)`
→ `n_drills` + a sentence in the prompt. Surface in Settings **and** as a chip
row on the lesson intro sheet (B4 mockup). Applies at *generation* time; note in
UI that it affects new lessons.

#### A4 · "Test out" of a lesson (S/M)

"I know this" on any available lesson → a 4-question quiz drawn from the
lesson's own hardest stored drills (last per type — deterministic, no LLM).
Pass ≥3/4 → mark complete (score = quiz %, first-completion XP × 0.5, crown 1).
Fail → drop into the full lesson at step 1. Implementation: client-side filter
of `content.segments[*].exercises` + existing `complete_lesson` route; one flag
in the POST body (`tested_out: true`) if we want to tag it in results.

### Theme B — gamification (Duolingo-competitive)

#### **B1 · Daily quests + chest** (M)

3 rotating daily quests, e.g. *Earn 40 XP* · *Get a 6-combo* · *Complete 1
listening-heavy step* / *Add 3 words to your deck* / *Do a checkpoint*. All are
computable from events the client already tracks (XP, combo, exercise types,
card adds). Completing all 3 opens a **chest** (bonus XP 20–40 + occasionally a
streak freeze, see B5).

- Schema: `daily_quests (user_id, quest_date, quest_key, target, progress,
  claimed)` — 3 rows/user/day, seeded lazily on first `GET /api/quests` of the
  day from a deterministic rotation (hash of user_id+date → pick 3 from ~10
  quest templates so friends see different mixes).
- Progress updates: piggyback on existing writes — `complete_lesson`,
  `add_points`, card-create — plus one small `POST /api/quests/progress` for
  client-only signals (combo). Keep it forgiving: server trusts client for
  combo-type quests the same way it trusts client XP (already clamped).
- Surfaces: quest card on the Learn page above the path + on Home; results
  screen shows quest ticks advancing (mockup `results.html`).

#### **B2 · Friends weekly XP league** (M)

Weekly leaderboard among the user's accepted friends (+ self), ranked by XP
earned this ISO week from `points_ledger`. **No new tables** — it's one query:
sum points grouped by user over `created_at >= week start` filtered to
`get_friends`. Endpoint `GET /api/league` → `[{user, xp, rank}]`. Surfaces: a
compact strip on the Learn page ("#2 of 5 this week · 40 XP behind Maya") and a
full list sheet. Weekly reset is implicit (the query window). Later, optional:
end-of-week toast for the winner. Zero-friends state: hide entirely (don't guilt
solo users), or show "Invite a friend" chip.

#### **B3 · Unit checkpoint quiz** (M)

When a chapter closes (`db.close_unit` — planner opened a new one), the unit
banner on the path gains a **Checkpoint** node: a 8–10-question quiz sampled
deterministically from that unit's lessons' stored drills (1–2 per lesson,
hardest kinds preferred: cloze/reorder/production). Pass ≥80% → gold
checkpoint shield on the banner + bonus XP (e.g. 40) + each concept's mastery
ledger gets a bump. No LLM, no new content generation.

- Schema: `course_units.checkpoint_passed` (or reuse a `unit_meta` JSON) +
  score. One route `GET /api/units/{id}/checkpoint` (assemble) and completion
  via the existing lesson-complete pattern (`POST /api/units/{id}/checkpoint`).
- Player: it's just a drills-only lesson (`skipTeach`), badge in results.

#### B4 · Lightning round — timed remix step for AI lessons (S)

Generalize the foundations `speed_round` to AI-lesson material: 60-second timed
run of quick-fire recognition/production prompts built **from the lesson's own
stored drills** (choice-type only, 3 options, shuffled). Offer it as (a) an
optional extra node under a completed lesson ("⚡ Lightning") and (b) a quest
target. All client-side assembly; formative (XP via combo, no mastery writes).

#### B5 · Streak freeze (S)

Earned, not bought: 1 freeze per 10 completed lessons (cap 2 equipped). Schema:
`user_settings` keys (`streak_freezes`, `streak_freeze_used_date`) + a check in
`db.get_streak`'s gap logic: a 1-day gap with a freeze available consumes it and
preserves the streak (write a `study_activity` marker row or a ledger note).
Surface: flame pill shows a small shield when equipped; toast when consumed.

#### B6 · Practice hub (M, later)

A "Practice" entry under the path collecting: mistakes review (from
`concept_mastery` weak concepts → drills-only lessons of stored drills),
lightning rounds, and the existing foundations games. Pure recombination of
stored content; good Phase 3 filler once A/B land.

### Theme C — teaching content & pacing

#### **C1 · Tap-through teach cards with inline quick-checks** (M)

Replace the teach wall with a **card deck**: one block per screen (prose, table,
examples…), Continue advances; a thin dot-progress on top. After every 1–2
cards, the author inserts a `quick_check` — a one-tap formative question about
the *rule just shown* ("Which one is correct? → 我食咗 / 我咗食") that gives
instant feedback but is ungraded (no mistake queue, no mastery write, no combo
break; a correct answer still bumps combo/XP so it *feels* rewarding).

- Author prompt: allow block type `{type:"quick_check", question, options[2–3],
  answer, why}` inside teach blocks; `_clean_block` gains a case (validate answer
  ∈ options — same "strict in what you grade" rule: we place/shuffle).
- Player: `renderTeach` becomes a per-block pager (state in `player.teachIdx`);
  quick_check renders like a mini choice drill with the `why` line as feedback.
  Fallback for old lessons: page through their blocks the same way.
- This + A1 means "read a wall" never happens: max 2 cards before interaction.

#### C2 · Warm-up recall opener (S)

Move the interleaved review drills (already picked by `_pick_review_concepts`)
to the **front** of the lesson as a labeled "Warm-up" step (retrieval practice
before new input; also a gentler on-ramp than new material cold). Prompt change
in `_review_block` (placement instruction) + `assemble_lesson` ordering. Pairs
naturally with A1's step 1.

#### C3 · Recap card on results (S)

The results screen gets a "What you learned" recap: the lesson's key table or
top 3 example sentences (first `table` block, else first `examples` block —
already in `content`), collapsed under the score. Zero generation cost; big
retention win. Also the natural place for "review this lesson tomorrow" framing.

#### C4 · Listening-first variant drills (S, later)

For each `production` drill the author already writes, the assembler can *also*
emit an audio-prompt variant (same options, prompt = TTS of the gloss'd answer
sentence) and pick ONE at random per play — replays stop being identical.
Client-only; uses existing TTS cache.

### Theme D — customization

#### **D1 · Adjustable daily goal** (S)

Replace `_DAILY_XP_GOAL = 50` with setting `daily_xp_goal` (10 = Casual /
30 = Regular / 50 = Serious / 100 = Intense). Read in the two places main.py
returns `daily_goal`; a picker in Settings + first-run nudge on the Learn page
("Set your pace"). The ring/quests read it automatically.

#### **D2 · Course focus dial** (S)

Setting `course_focus`: `balanced` (default) / `grammar` / `vocab` /
`conversation`. Passed into `plan_next_lesson` as one prompt line steering the
skill mix (conversation → prefer phrase-pattern skills + more construction
drills). Surfaced next to the level chips at course creation + changeable in
the course header ⚙ sheet (applies to future lessons).

#### D3 · Lesson intro sheet (S, UI-only)

Tapping a path node opens a small sheet before the player: title, objective,
step list ("📖 Learn · ✏️ Practice ×2 · ✨ AI Speak"), estimated minutes,
concept chips, then **Start** / **⚡ Practice only** / **Test out** (A4) /
length chips (A3). Consolidates the current scattered `⚡ Practice`/`🎯 AI
Drills` mini-buttons (which are easy to fat-finger on the path today) into one
purposeful surface. Mockup: `lesson-intro.html`.

#### D4 · Per-lesson "more like this" (S, later)

On results: 👍/👎 "How was this lesson?" One tap stores a signal appended to
`learner_profile`-adjacent setting (`lesson_feedback` ring buffer) that the
planner prompt quotes ("learner disliked: long grammar tables; liked: dialogue
drills"). Cheap personalization loop with zero new UI surface elsewhere.

---

## 4 · Recommended phasing

| Phase | Contents | Rationale |
|---|---|---|
| **1 — the shape change** | A1 steps + A2 construction-step + C2 warm-up + C1 teach cards/quick-checks | One coherent generation+player change; transforms perceived length. All four touch the same prompt/assembler/player code — do together, one PR. |
| **2 — the motivation layer** | B1 quests + B3 checkpoints + D1 daily goal + C3 recap | Mid-term goals; all deterministic (no new LLM spend). |
| **3 — social + agency** | B2 league + A3 length + D2 focus + D3 intro sheet + A4 test-out | League is the only multi-user feature (needs friends adoption); the rest is settings plumbing. |
| Later | B4 lightning, B5 freeze, B6 practice hub, C4 variants, D4 feedback | Nice-to-haves once the above proves out. |

Estimated total for Phases 1–3: ~6–9 focused sessions. Phase 1 alone is
shippable and addresses the loudest complaint (lesson length/pacing).

**Compatibility guarantees to preserve** (all phases):
- Old stored lessons (flat or 1-segment) must keep playing — the player's
  segment normalization (`openLesson`, learn.html:2121) already handles this;
  keep the assembler's fallback wrap.
- Answer keys stay correct-by-construction: any new drill/quick-check type gets
  validated + shuffled server-side in `assemble_lesson`/`_clean_block`, never
  trusting a model-supplied index.
- Foundations lessons are pre-built data (`foundations.build_units`) — they
  already emit multi-segment content and must not be re-shaped; gate new
  player chrome on `player.theme !== 'foundations'` where it doesn't apply.
- XP stays first-completion-only, clamped server-side (raise `_MAX_LESSON_XP`
  only if quest/checkpoint bonuses route through separate `reason` values in
  `points_ledger` — recommended: `'quest'`, `'checkpoint'` reasons, each with
  its own clamp).

---

## 5 · Mockups

`mockups/lesson-redesign/` (self-contained HTML, shared `../mock.css` tokens,
same phone-frame gallery pattern as the site redesign):

| File | Shows |
|---|---|
| `index.html` | Gallery + the idea list in one page |
| `learn-path.html` | Path with quest card, league strip, checkpoint node (B1/B2/B3) |
| `lesson-intro.html` | Pre-lesson sheet: steps, length chips, test-out (D3/A3/A4) |
| `lesson-steps.html` | Player mid-lesson: segmented step bar, teach card + quick-check (A1/C1) |
| `lesson-speak.html` | Construction drill as its own "AI Speak" step, 3 turns, skippable (A2) |
| `checkpoint.html` | Checkpoint quiz result with shield + bonus XP (B3) |
| `results.html` | Results with XP breakdown, quest ticks, recap card, league delta (B1/B2/C3) |

Dev preview: `/mockups/lesson-redesign/` on the dev site (the existing
`IS_DEV`-gated `/mockups` static mount serves subdirectories as-is).
