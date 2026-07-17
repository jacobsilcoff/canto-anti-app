# Canto Flashcards for Even G2

Review due flashcards on Even Realities G2 glasses. Grades sync to the main
app, including XP, streaks, and SRS scheduling.

## Build a Developer Hub beta

```bash
npm install
npm run beta
```

The command type-checks, tests, builds, and creates `canto-flashcards.ehpk` in
this directory. Upload that file as the beta build in Even Developer Hub.

## First-run setup

1. In the main app, open **Settings → Even G2 glasses**.
2. Generate and copy a plugin token.
3. Open the plugin in Even Hub and paste the token on the phone screen.

The glasses use a tap to reveal, then a tap/swipe-up for “got it” and a
swipe-down for “again.” Short prompts are rasterized as a large, centered
4-bit image because the native G2 text container has no font-size control.
