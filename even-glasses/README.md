# Canto Flashcards for Even G2

Review due flashcards on Even Realities G2 glasses. Grades sync to the main
app, including XP, streaks, and SRS scheduling.

## Build a Developer Hub beta

```bash
npm run beta
```

The command synchronizes dependencies, type-checks, and tests. If those checks
pass, it increments the patch version in `app.json`, `package.json`, and
`package-lock.json`, then builds and creates `canto-flashcards.ehpk` in this
directory. Upload that file as the beta build for the matching app in Even
Developer Hub.

## First-run setup

1. In the main app, open **Settings → Even G2 glasses**.
2. Generate and copy a plugin token.
3. Open the plugin in Even Hub and paste the token on the phone screen.

On the card front, press once to reveal, swipe up to undo the latest answer (or
return to the prior unreviewed card), swipe down to skip, or press twice to
exit. On the revealed card, press once for “good” or twice for “again.” Undo
restores the card's previous SRS schedule and reverses its XP and review-quest
credit.

Short prompts, including Cantonese, are rasterized as a large, centered PNG
because the native G2 text container has no font-size control. The phone screen
includes a live diagnostics panel showing canvas, image-transfer, input, and
API events; use **Copy log** when diagnosing a hardware-only rendering problem.
