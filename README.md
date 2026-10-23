# ZEE AI BOT — starter project

Telegram bot starter for LORD ZEEMAN DOMAIN with:
- Required-channel membership check for `/start` and public features
- `/help`, `/about`, `/cg`
- Gemini-powered text chat
- `/rps` to play Rock-Paper-Scissors against the bot
- `/rps @username` to challenge a known bot user
- `/wallet` and `/zc` with SQLite-backed ZC balance and transaction history
- Owner-only `/addadmin` and `/removeadmin`; `/listadmin` for admins
- Initial owner ID `7946587480` and senior admin ID `7328699671`

## Important notes
1. Keep `BOT_TOKEN` and `GEMINI_API_KEY` secret. Never commit real keys.
2. Add the bot as an **administrator** to `@ZEEMANDOMAIN` so Telegram permits membership checks.
3. Users must open the bot and press `/start` before username-based challenges can find them.
4. In this starter, PvP moves are held in process memory after a challenge is accepted. Use a single worker; for multi-worker deployments, persist game moves in SQLite/PostgreSQL before scaling.
5. SQLite is appropriate for an initial single-worker deployment. For higher traffic, use managed PostgreSQL and persistent database configuration.
6. Telegram bots cannot reliably discover a user's numeric ID from an arbitrary username. For `/addadmin` and `/removeadmin`, numeric IDs are reliable; replying to the target user's message is also supported. Username lookup works only for users already known to this bot.
7. `/tiktok`, `/quiz`, `/tempadmin`, `/bc`, `/kidnapme`, `/steal`, and broad group-management commands are not implemented yet; they are planned next features, not working commands in this starter.
8. The bot's AI chat sends user text to Google's Gemini API. Tell users appropriately and do not send private/sensitive information.

## Local run
Install dependencies and set environment variables, then run:
`python bot.py`

## Hosting
Deploy this repository as a persistent Python worker/background service, not a static website. Add `BOT_TOKEN`, `GEMINI_API_KEY`, `REQUIRED_CHANNEL`, and `CHANNEL_URL` in the host's environment/secret settings. Ensure the database file is on persistent storage or migrate to managed PostgreSQL before relying on wallet balances long-term.
