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

## Optional multi-user alerts

The bot still sends alerts to `TELEGRAM_CHAT_ID` when no subscriber service is
configured. For approved subscribers, configure two additional GitHub Actions
secrets:

- `TELEGRAM_SUBSCRIBERS_URL`: an HTTPS endpoint on the Cloudflare Worker,
  such as `https://<worker-host>/subscriber-ids`
- `TELEGRAM_SUBSCRIBERS_TOKEN`: a long random bearer token, also configured as
  a secret in the Cloudflare Worker

The endpoint must reject requests without the matching bearer token and return
JSON in the form `{"chat_ids": ["123456789", "987654321"]}`. It must return
**only administrator-approved private chat IDs**, stored in Cloudflare KV or
another private datastore. Do **not** commit chat IDs, membership requests, or
tokens to this repository: it is public.

The monitor fetches approved IDs once per run, removes duplicates, includes the
original owner's `TELEGRAM_CHAT_ID`, and sends each alert to every subscriber.
If the subscriber endpoint is unavailable, it continues sending to the owner
only. A failed recipient cannot block alerts to the others.

The Worker must separately handle `/start`, pending requests, admin-only
Approve/Deny actions, `/users`, and `/remove`, and gate interactive commands
to approved users. This repository does **not** contain the deployed Worker
source, so those features require a separate Worker update and KV binding.

The first run creates the baseline state without sending a false H-board-change alert.
