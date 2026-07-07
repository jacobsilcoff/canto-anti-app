# Flashcards on Even glasses (MentraOS plugin)

Review your **due flashcards from the main app** on Even Realities glasses, hands-free.
Every grade is sent to your account, so you **earn XP and keep your streak / daily
quests in sync** exactly like reviewing on the web.

Because the glasses have no speaker, audio is never used:

- **Non-Latin languages** (Cantonese, Mandarin, Japanese, Korean, Hindi, Russian, Thai, …)
  show **romanization** — as the prompt on the pronunciation face, and in the reveal
  on the other faces.
- **Latin-alphabet languages** (French, Spanish, German, …) have no romanization, so
  the pronunciation face is an audio-only "listen and identify" challenge that can't
  work on the glasses — those cards are **skipped** automatically.

## How it works

This is a [MentraOS](https://mentra.glass) app: a small cloud service (this folder)
that MentraOS connects to your glasses. It calls the main app's REST API with a
long-lived **API token** scoped to your account.

```
glasses ⇄ MentraOS phone app ⇄ this plugin ⇄ your flashcard site's API
```

## Setup

### 1. Get an API token from your account

In the flashcard site, go to **Settings → Even glasses → Generate token** and copy it.
(You can revoke/regenerate any time; the old token stops working immediately.)

### 2. Register the app in the MentraOS console

1. Create an app at [console.mentra.glass](https://console.mentra.glass) and copy its
   **package name** and **API key**.
2. Under **Configuration Management → Import app_config.json**, import
   [`app_config.json`](./app_config.json) to create the settings (Site URL, API token,
   controls).

### 3. Run the plugin

```bash
cp .env.example .env      # fill in PACKAGE_NAME, MENTRAOS_API_KEY, PORT
bun install
bun run dev
```

Expose it publicly (MentraOS connects to it) — e.g. with ngrok — and set that URL as
the app's server URL in the console. See the
[MentraOS quickstart](https://docs.mentra.glass) for the console/ngrok details.

### 4. Enter your details on the phone

In the MentraOS app, open this app's **settings** and set:

- **Site URL** — e.g. `https://canto-ank.silcoff-labs.ca`
- **API token** — the token from step 1

(For a single-user self-hosted deployment you can instead hard-code `CANTO_BASE_URL`
and `CANTO_API_TOKEN` in `.env`; the per-user settings take precedence when set.)

## Using it

Start the app from the glasses. Then, per card:

| State | Short press (tap) | Long press (hold) |
|-------|-------------------|-------------------|
| Prompt shown | Reveal the answer | Reveal the answer |
| Answer shown | **Got it** → SRS "good" (full XP) | **Missed it** → SRS "again" |

Turn on **Swap tap / hold** in settings to reverse the grade gestures.

When the queue is empty you get a summary (cards done, XP earned, streak). Press again
to re-check for anything newly due.

## Development

```bash
bun test              # unit tests for the face-skip + romanization rules
bun run build         # typecheck (tsc --noEmit)
```

- `src/canto.ts` — REST client (bearer auth).
- `src/cards.ts` — pure display logic: which faces are playable, and the
  prompt/answer per face. Unit-tested in `src/cards.test.ts`.
- `src/index.ts` — the MentraOS `AppServer`: reads settings, drives the Pass/Fail
  review loop, POSTs grades.
