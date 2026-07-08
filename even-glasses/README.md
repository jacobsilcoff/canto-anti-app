# Flashcards on Even Realities G2 glasses

Review your **due flashcards from the main app** on Even Realities G2 glasses, hands-free.
Every grade is sent to your account, so you **earn XP and keep your streak / daily
quests in sync** exactly like reviewing on the web.

Because the glasses have no speaker, audio is never used:

- **Non-Latin languages** (Cantonese, Mandarin, Japanese, Korean, Hindi, Russian, Thai, ...)
  show **romanization** — as the prompt on the pronunciation face, and in the reveal
  on the other faces.
- **Latin-alphabet languages** (French, Spanish, German, ...) have no romanization, so
  the pronunciation face is an audio-only "listen and identify" challenge that can't
  work on the glasses — those cards are **skipped** automatically.

## How it works

This is an [Even Hub](https://hub.evenrealities.com) plugin: a web app that runs in the
iPhone Even App's WebView and communicates with your glasses over BLE. It calls the
main app's REST API with a long-lived **API token** scoped to your account.

```
glasses <-BLE-> iPhone Even App (WebView) <-HTTPS-> your flashcard site's API
```

## Setup

### 1. Get an API token from your account

In the flashcard site, go to **Settings > Even glasses > Generate token** and copy it.
(You can revoke/regenerate any time; the old token stops working immediately.)

### 2. Sideload the plugin (development)

```bash
npm install
npm run dev         # Start Vite dev server on 0.0.0.0:5173
npm run qr          # Generate QR code for sideloading
```

Scan the QR code from the Even App to load the plugin on your glasses. The first time
you'll see a setup screen on your phone — enter your site URL and API token.

### 3. Package for distribution

```bash
npm run pack        # Build + package as flashcards.ehpk
```

Upload the `.ehpk` to the Even Hub developer portal.

## Using it

Start the plugin from the glasses. Then, per card:

| State | Tap | Double-tap | Swipe up | Swipe down |
|-------|-----|------------|----------|------------|
| Prompt shown | Reveal answer | Reveal answer | — | — |
| Answer shown | **Got it** (SRS "good") | **Missed it** (SRS "again") | **Got it** | **Missed it** |

When the queue is empty you get a summary (cards done, XP earned, streak). Tap again
to re-check for anything newly due.

## Development

```bash
npm run dev           # Vite dev server with hot reload
npm run typecheck     # TypeScript type check (tsc --noEmit)
npm test              # Unit tests (vitest)
```

- `src/api.ts` — REST client (bearer auth).
- `src/cards.ts` — pure display logic: which faces are playable, and the
  prompt/answer per face. Unit-tested in `src/cards.test.ts`.
- `src/config.ts` — localStorage config persistence.
- `src/main.ts` — the Even Hub plugin: reads config, drives the review loop via
  the SDK bridge, handles touch/swipe events, and POSTs grades.
