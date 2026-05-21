# Uni Sports Bot

Small Playwright bot for checking a Uni Trier sports slot and booking it when it becomes available.

The bot uses a persistent browser profile so it can keep a logged-in session. It starts in dry-run mode, so the real booking click is disabled until the selector is verified.

## Setup

```powershell
cd D:\Projects\uni-sports-bot
uv sync
uv run playwright install chromium
```

Create `.env` from `.env.example`, then fill in your own credentials and target slot.

## Run

```powershell
uv run python -m app.main
```

Keep `DRY_RUN=true` until the bot saves a `slot-found` screenshot that clearly points to the correct slot.
