/**
 * Even glasses flashcard plugin (MentraOS app).
 *
 * Drives a hands-free Pass/Fail review of your canto-anti-app due cards on the
 * glasses HUD. Each grade is POSTed to the site, which awards XP and keeps your
 * streak / daily quests in sync — the same as reviewing on the web.
 *
 * Controls (Even Realities G1 TouchBar):
 *   • before reveal:  any press          → show the answer
 *   • after reveal:   short press        → "got it"   (→ SRS "good")
 *                     long  press        → "missed it" (→ SRS "again")
 *   (swap short/long with the "Swap tap / hold" setting)
 */
import { AppServer, type AppSession } from "@mentra/sdk";
import { CantoClient, CantoError, type Quality } from "./canto.js";
import { buildView, playableCards, type CardView } from "./cards.js";
import type { DueCard } from "./canto.js";

const PACKAGE_NAME = process.env.PACKAGE_NAME;
const MENTRAOS_API_KEY = process.env.MENTRAOS_API_KEY;
const PORT = Number(process.env.PORT || 3000);

if (!PACKAGE_NAME) throw new Error("PACKAGE_NAME is required");
if (!MENTRAOS_API_KEY) throw new Error("MENTRAOS_API_KEY is required");

type Phase = "prompt" | "reveal";

/** Per-connection review state. */
class ReviewLoop {
  private queue: DueCard[] = [];
  private idx = 0;
  private phase: Phase = "prompt";
  private view: CardView | null = null;
  private xp = 0;
  private graded = 0;
  private busy = false;

  constructor(
    private readonly session: AppSession,
    private readonly canto: CantoClient,
    private readonly swapControls: boolean,
    private readonly showNotes: boolean,
  ) {}

  /** Fetch the due session and show the first card (or a done screen). */
  async begin(): Promise<void> {
    this.session.layouts.showTextWall("Loading your flashcards…");
    let romanizable: Set<string>;
    let cards: DueCard[];
    try {
      [romanizable, { cards }] = await Promise.all([
        this.canto.romanizableLangs(),
        this.canto.dueSession(),
      ]);
    } catch (e) {
      return this.showError(e);
    }
    this.queue = playableCards(cards, romanizable);
    this.idx = 0;
    this.xp = 0;
    this.graded = 0;
    if (this.queue.length === 0) {
      this.session.layouts.showReferenceCard("All caught up 🎉", "No cards due right now.");
      return;
    }
    this.render();
  }

  /** A TouchBar press. `long` is true for a long/hold press. */
  async onPress(long: boolean): Promise<void> {
    if (this.busy) return;
    // Finished screen → any press restarts (re-fetches due cards).
    if (this.idx >= this.queue.length) return this.begin();

    if (this.phase === "prompt") {
      this.phase = "reveal";
      return this.render();
    }
    // reveal phase → grade. short = pass, long = fail (swapped by setting).
    const pass = this.swapControls ? long : !long;
    await this.grade(pass ? "good" : "again");
  }

  private async grade(quality: Quality): Promise<void> {
    const card = this.queue[this.idx];
    this.busy = true;
    try {
      const res = await this.canto.review(card.card_id, card.face, quality);
      this.xp += res.xp || 0;
    } catch (e) {
      this.busy = false;
      return this.showError(e);
    }
    this.busy = false;
    this.graded += 1;
    this.idx += 1;
    this.phase = "prompt";
    if (this.idx >= this.queue.length) return this.finish();
    this.render();
  }

  private render(): void {
    const card = this.queue[this.idx];
    if (this.phase === "prompt") this.view = buildView(card);
    const view = this.view!;
    const progress = `${this.idx + 1}/${this.queue.length}`;
    if (this.phase === "prompt") {
      this.session.layouts.showReferenceCard(
        `${view.hint}  ·  ${progress}`,
        `${view.prompt}\n\n( press to reveal )`,
      );
    } else {
      const notes =
        this.showNotes && card.notes && card.notes.trim()
          ? `\n\n${card.notes.trim()}`
          : "";
      const legend = this.swapControls ? "hold ✓  ·  tap ✗" : "tap ✓  ·  hold ✗";
      this.session.layouts.showReferenceCard(
        `${view.hint}  ·  ${progress}`,
        `${view.prompt}\n———\n${view.answer}${notes}\n\n${legend}`,
      );
    }
  }

  private async finish(): Promise<void> {
    let tail = "";
    try {
      const s = await this.canto.streak();
      tail = `\n🔥 ${s.streak}   ⭐ ${s.points_today}/${s.daily_goal}`;
    } catch {
      /* summary is best-effort */
    }
    this.session.layouts.showReferenceCard(
      "Session done ✓",
      `${this.graded} cards   +${this.xp} XP${tail}\n\n( press to check for more )`,
    );
  }

  private showError(e: unknown): void {
    const msg = e instanceof CantoError ? e.message : "Something went wrong.";
    this.session.layouts.showReferenceCard("Flashcards", msg);
  }
}

class FlashcardsApp extends AppServer {
  constructor() {
    super({ packageName: PACKAGE_NAME!, apiKey: MENTRAOS_API_KEY!, port: PORT });
  }

  protected async onSession(session: AppSession): Promise<void> {
    const baseUrl =
      session.settings.get<string>("canto_base_url", "") || process.env.CANTO_BASE_URL || "";
    const token =
      session.settings.get<string>("canto_api_token", "") || process.env.CANTO_API_TOKEN || "";
    const swapControls = session.settings.get<boolean>("swap_controls", false);
    const showNotes = session.settings.get<boolean>("show_notes", true);

    if (!baseUrl || !token) {
      session.layouts.showReferenceCard(
        "Set up needed",
        "Open this app's settings in MentraOS and enter your site URL + API token (generate it in the site's Settings → Even glasses).",
      );
      return;
    }

    const loop = new ReviewLoop(
      session,
      new CantoClient(baseUrl, token),
      swapControls,
      showNotes,
    );

    // TouchBar presses drive the whole loop. The SDK returns an unsubscribe fn.
    session.events.onButtonPress((data: { pressType?: string }) => {
      const long = (data?.pressType || "").toLowerCase() === "long";
      void loop.onPress(long);
    });

    await loop.begin();
  }
}

new FlashcardsApp().start().catch((e) => {
  console.error("Failed to start:", e);
  process.exit(1);
});
