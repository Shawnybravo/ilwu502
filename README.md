# ILWU 502 Telegram monitor

Checks every 5 minutes and:

1. Sends a Telegram alert whenever the H BOARD work pin changes.
2. Totals the 4:30 board as:
   - every number in each ship's `gangs` field
   - plus every numeric `qty` in `fsd_jobs`
   - plus every numeric `qty` in `dp_jobs`
3. Sends a Telegram alert whenever a new or changed 4:30 board is over 200 jobs.

## GitHub secrets required

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

The first run creates the baseline state without sending a false H-board-change alert.
