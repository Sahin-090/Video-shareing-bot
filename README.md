# Telegram Video Delivery Bot

Python (aiogram 3 + asyncpg) bot for Hosting. Videos live in a private Telegram channel; the bot stores only Telegram `file_id`s and metadata in PostgreSQL and delivers videos straight into the user's chat. No Redis, no downloads, no files kept on Hostinger.

## How it works
- User sends a number → picks 🆕 New / 📦 Old → bot sends that many **unique** videos → `✅ N unique videos delivered.` + 🔄 Request Again.
- **New** = latest channel post → oldest. **Old** = oldest channel post → latest by default (ordered by post date) (`OLD_VIDEO_ORDER`: `asc`, `desc` or `random`). If a category runs short it is topped up from the rest of the library, and if the library has fewer than requested the user gets `⚠️ You requested 20 videos, but only 13 unique videos are currently available.` followed by all 13. "No videos" is only ever shown when the library is really empty.
- Session state lives in the `user_states` table, so restarts are safe. Double taps and parallel requests are blocked with an atomic state claim.
- Flood-waits are honoured automatically (delivery and scanning); videos that can no longer be sent are deactivated and reported to admins.
- The channel link/username/ID is never sent to users, and delivered videos carry no "forwarded from" header or channel caption.

## How videos get into the library
Telegram bots **cannot read channel history**, so the bot uses two clients:

1. **History scanner (Telethon / MTProto)** – logs in as a normal account that is a member of the channel, walks the *whole* history oldest → newest and registers every video (message id, `file_unique_id`, date, caption, duration, size, MIME type, width, height). Nothing is downloaded; only metadata goes into PostgreSQL.
2. **Bot API** – delivers videos to users and sees new channel posts live.

**Bot API `file_id` bridge.** A Telethon media id is not a Bot API `file_id`, so it is never stored as one. After a scan, the *bot* forwards each scanned post to the admin who ran `/scan` (a supported Bot API workflow), reads the real `file_id` from the forwarded message, stores it and deletes the forwarded copy (≈1 video/second, nothing downloaded). Delivery then uses `send_video(file_id)`. If Telegram prevents this for a post (bot not a channel admin, protected content, deleted post) no id is faked: the admin gets a report and that video keeps being delivered with `copy_message` from the channel, which requires the bot to be a channel administrator.

**Two separate clients:** the Bot API client (`BOT_TOKEN`) handles users, buttons, delivery and admin commands; the Telethon **user** client (`API_ID` + `API_HASH` + `SESSION_STRING`) is used *only* for reading channel history. `SESSION_STRING` must come from a normal user account that is a member of the channel. A session created by logging in with the bot token is rejected at startup, because bots may not call `GetHistoryRequest` (`BotMethodInvalidError`).

### One-time setup
1. Get `API_ID` and `API_HASH` at <https://my.telegram.org> → *API development tools*.
2. On your own computer run `pip install telethon && python generate_session.py` and log in with an account that is a member of the channel. Copy the printed `SESSION_STRING`. Treat it like a password.
3. In Hosting → *Variables* add `API_ID`, `API_HASH`, `SESSION_STRING`, `SOURCE_Url_ID` (e.g. `-1001234567890`).
4. Make the bot an administrator of the channel.
5. As an admin, send `/scan` to the bot.

### Keeping it current
- New posts are detected **automatically**: instantly through the Bot API, and every `AUTO_SCAN_MINUTES` by an incremental scan that continues from the last checkpoint (so posts made while the bot was down are still picked up). No need to run `/scan` again.
- Re-running `/scan` is always safe: it re-reads the full history and only adds what is missing.

### Duplicates (fixed)
Every channel **post** is one row, identified by `UNIQUE(channel_id, telegram_message_id)`. The same video posted twice (or in two channels) is stored as two rows, so scan totals match the channels (e.g. 5,001 found = 5,001 stored). Re-scanning is still safe: the same post is never stored twice.
- Videos added by hand are still unique by `file_unique_id` (`uq_videos_manual_uid`).
- **Admin Panel → Bot Settings → ♻️ Send duplicates** (default OFF):
  - OFF: a video that exists in several posts is sent **once per user** (New uses its latest post, Old its oldest post).
  - ON: every post is sent, copies included; only the same post is never repeated for one user.
- After upgrading, run **one Full Scan** to register the copies that the old unique index had dropped.

### Numbering
`video_number` is one global sequence (Video #1, #2, …), independent of Telegram message ids. Scanned videos are numbered oldest → newest without gaps, inside a lock so concurrent writers cannot collide. A video keeps its number for life. (Older versions numbered New and Old separately; the first start of this version renumbers existing videos once into the global sequence, ordered by date.)

### Restart safety
Scan progress (`scan_state` table) is checkpointed only *after* each batch is written. If Hosting restarts mid-scan, the next scan continues from the checkpoint; a scan that was "running" at restart is shown as `interrupted`. A single bad message or database row is logged and skipped, never fatal.

## Deploy on Hosting
1. Create the bot with @BotFather and copy the token.
2. Push this folder to a GitHub repo → Hosting → *New Project → Deploy from GitHub*.
3. In the project, *New → Database → PostgreSQL*.
4. In the bot service → *Variables*: `BOT_TOKEN`, `ADMIN_IDS`, and `DATABASE_URL` (reference the Postgres service's `DATABASE_URL`), plus the scanner variables above. Add the optional variables from `.env.example` as needed.
5. The `Procfile` runs `python bot.py` as a worker. The schema and migrations are applied automatically on every start (`database/schema.sql`, idempotent).
6. Send `/scan` to the bot as an admin.

Run only **one** instance of the service (Telegram long-polling doesn't support several). No Redis, no volumes, no files kept on Hostinger.

## Admin commands (IDs in `ADMIN_IDS`)
| Command | Purpose |
|---|---|
| `/scan` | Full scan of the whole channel history, with progress and a final report |
| `/scanstatus` | Scanner state, counts and checkpoint |
| `/scanstop` | Stop a running scan (progress is kept) |
| `/stats` | Library and usage statistics |
| `/videos` | Video statistics (`/videos new\|old\|pending\|all [page]` lists videos with IDs) |
| `/addvideo`, `/batch new\|old\|off` | Add videos by sending them to the bot |
| `/editvideo <id> number <n>` / `category <new\|old>` / `caption <text>` | Edit a video (a hand-set category is no longer re-aged) |
| `/deletevideo <id>` | Delete a video |
| `/scanlegacy <new\|old> [from_id] [to_id]` | The old forward-based import, kept as a fallback that needs no API keys |
| `/help` | Command list |

Non-admins get no response to these commands.

## Broadcast (`/broadcast` or Admin Panel → 📢 Broadcast)

Send a modern announcement card to every active user:

1. Press **✍️ New Broadcast** and send your message (a bold first line works well as a title; Telegram formatting, links and emoji are kept).
2. Check the **preview** - it is exactly what users receive - optionally **🧪 Send test to me**.
3. Press **🚀 Send to everyone**. A live progress bar appears, and **⏹️ Stop Broadcast** ends it early.

Users blocked by an admin or who stopped the bot are skipped. Sending is rate-limited (about 20 messages per second) and respects Telegram flood limits. Users who block the bot are marked so the next broadcast skips them. Only one broadcast can run at a time. The finished broadcast is recorded in 📋 Logs.

## Local run
```
pip install -r requirements.txt
cp .env.example .env   # fill in, then export the variables
python bot.py
```

## Upgrade: unique delivery, menus, auto-delete

Added on top of the existing bot (scanner, `/scan`, categories, duplicate protection and admin commands are unchanged):

- **Never the same video twice per user.** `user_video_history` (`UNIQUE(user_id, video_id)`) records every delivery. New, Old, More New, More Old, ✨ Fresh Batch and 🔄 New Request all exclude it. It lives in the database, so restarts, redeploys and `/scan` never clear it. Admins can wipe one user with `/resethistory <user_id>`.
- **Strict collections.** New only returns New and Old only returns Old. If fewer unseen videos remain than requested, the user is told and gets what is left, then is offered the other collection / a new request. When a collection is used up, a contextual "end of collection" message is shown.
- **2-minute auto-delete (restart-safe).** Each video message the bot sends is stored in `delivered_messages` with `delete_at`. A background worker (`services/cleanup_service.py`) deletes due messages from the *user's chat* only; the source channel, `videos` and the history are never touched. After a Hostinger restart overdue rows are deleted on the first tick. Users who delete a message themselves, or block the bot, are handled silently.
- **Request lock + rate limit** (database-based, no Redis): double taps never start parallel deliveries (`user_states`), and `RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW` seconds caps request spam (`user_rate_limits`).
- **UI:** premium `/start`, main menu (`/menu`), `📊 My Requests`, `ℹ️ Help`, progress state, post-delivery buttons (🔥 More New, ♻️ More Old, ✨ Fresh Batch, 🔄 New Request, 🏠 Main Menu). All texts/keyboards are in `ui.py`.
- **Admin:** `/stats` now shows deliveries, duplicate deliveries prevented, auto-delete setting and last scan.

New environment variables (all optional): `AUTO_DELETE_SECONDS` (default 120), `RATE_LIMIT_REQUESTS` (5), `RATE_LIMIT_WINDOW` (60).

**Auto-delete env:** set `AUTO_DELETE_SECONDS` in Hostinger → *Variables* (seconds, minimum 5, default 120; e.g. `300` = 5 minutes). A value saved from the admin panel (⏱️ Auto-delete) overrides the env value; reset it in the panel to use the env again. See `.env.example` for all variables.
The schema changes are additive and applied automatically on start. History starts at the upgrade: deliveries made before it were never recorded, so users may receive those videos once more.

## Upgrade: Admin Panel, developer page, configurable public UI

Added on top of everything above; the scanner, `/scan`, categories, duplicate protection,
unique per-user history, 2-minute auto-delete and all admin commands are unchanged.

### Public UI
- `/start` is the new welcome screen with buttons `🎬 Request Videos · 🆕 New · 📦 Old · 🔥 More Videos · 👨‍💻 Developer · ℹ️ Help`. Every screen has `⬅️ Back` / `🏠 Main Menu`.
- `👨‍💻 Developer` shows a profile page with link buttons (Website / Instagram / Telegram / GitHub). A button only appears when its link is set. Defaults come from `DEVELOPER_*`; the admin can override them in the panel.
- Delivery: `⏳ Preparing N unique videos...` → clean caption (`🎬 Video #125 / 🆕 New Collection / ⏱️ Auto-delete: 2 minutes`) → `✅ N unique videos delivered.` + `🔥 More New · ♻️ More Old · ✨ Fresh Batch · 🔄 New Request · 👨‍💻 Developer · 🏠 Main Menu`.
- Video presentation: videos are never re-encoded, cropped, padded, thumbnailed or wrapped. The stored `width/height/duration` and `supports_streaming=True` are passed to Telegram (or the post is copied as is), so portrait stays portrait, landscape stays landscape and Telegram's own viewer decides how it is shown. The source-channel caption is **not** shown unless an admin turns on *Keep source caption* (channel captions can contain channel names or links).

### Admin Panel (`/admin`, only IDs in `ADMIN_IDS`)
Dashboard → 📊 Statistics · 🎬 Video Manager · 👥 User Manager · 📡 Channel Scanner · 🤖 Bot Control · 🛠️ Maintenance · 🎨 Public Settings · 👨‍💻 Developer Settings · ⚙️ Bot Settings · 📋 Logs. Every page has `⬅️ Back` and `🏠 Dashboard`.
- **Security:** a normal user sending `/admin` or pressing any `adm:` button gets `⛔ You don't have permission to access the Admin Panel.` (same reply for everyone, nothing about who is an admin). Router filters *and* a second check inside every callback verify `user_id in ADMIN_IDS` on the server. Admin buttons are never shown to normal users.
- **Bot OFF / Maintenance / Block:** enforced by one middleware in front of every message and button press. Admins always pass, so they can turn the bot back on, end maintenance, scan, read logs and manage videos while it is closed. State is stored in PostgreSQL (`bot_settings`, `users.is_banned`) and survives Hostinger restarts.
- **Dangerous actions** (delete video, block user, clear history, bot OFF, enable maintenance) ask `⚠️ Are you sure? ✅ Confirm / ❌ Cancel`.
- **Editable texts** (welcome, help, auto-delete notice, maintenance title/message/emoji, developer bio) are validated, limited to `<b> <i> <u> <s> <code>` (everything else is escaped), shown as a **preview** and only saved on `✅ Save`. `{auto_delete}` and `{max}` placeholders work. Send `default` to restore the built-in text.
- **Public buttons:** each of Request / New / Old / More / Help / Developer can be switched off; the panel refuses to switch off the last way to request videos, and the server rejects taps on old messages that still carry a disabled button.
- **Custom button:** name, emoji, position (top/bottom), enabled. Target is either an `http(s)` link (validated, no credentials / no `javascript:`) or one of a fixed list of built-in actions — never free-form callback data.
- **Bot Settings:** max videos per request (1–200), auto-delete seconds (5–86400), New / Old ordering, delivery batch size, rate limit, welcome on/off, developer button, keep source caption, maintenance. Applied immediately, no restart.
- **Scanner:** Full / Quick scan, live progress (`🔎 Scanning channel...` with scanned / found / added / duplicates / errors), Stop, and a guard against two scans at once.
- **Logs:** recent safe events (requests, deliveries, scans, added videos, duplicates, auto-deletes, errors) in the `event_log` table, filterable and paged; configured secrets (`BOT_TOKEN`, `API_HASH`, `SESSION_STRING`, `DATABASE_URL` and its password) are scrubbed before anything is stored. Entries are pruned after 14 days.
- **Errors:** users only ever see `⚠️ Something went wrong. Please try again.`; admins get the exception *type* only; tracebacks stay in the server log.
- **State:** admin typing/preview state is kept in `user_states` (`admin_input` / `admin_preview`), separate from the users' delivery lock. No Redis.

New tables (additive, applied automatically): `bot_settings`, `event_log`; new columns `users.is_banned`, `users.banned_at`. Existing `/stats`, `/scan`, `/videos`, … commands keep working next to the panel.

New optional variables: `DEVELOPER_NAME/WEBSITE/INSTAGRAM/TELEGRAM/GITHUB/BIO`, `NEW_VIDEO_ORDER`, `DELIVERY_BATCH_SIZE`.

### Fix: videos repeatedly sent and deleted after `/scan`
That was the file_id bridge (`AUTO_BRIDGE=true`): after a scan it forwarded every video without a Bot API `file_id` to the admin chat and deleted it, and videos it could not resolve were retried after every scan. The bridge no longer runs automatically (`AUTO_BRIDGE` is ignored); `/scan` and the panel's scans only read metadata. Delivery to users is unchanged (`copy_message`). `/bridge` remains as an explicit manual command.

## Multiple source channels

Set `SOURCE_Url_IDS` to a comma separated list, e.g. `-1001111111111,-1002222222222` (the old `SOURCE_Crl_ID` still works, also with a list).

- **Requirements:** the bot must be an admin of every channel (live detection + `copy_message` delivery), and the `SESSION_STRING` account must be a member of every channel (history scan).
- **Scanning:** `/scan`, Quick Scan and the automatic catch-up go through all channels. Each channel has its own checkpoint (`scan_checkpoints`), so a new channel is imported from its first post while the others only continue where they stopped. A channel that cannot be read does not stop the others; the report says how many failed.
- **Live:** new videos posted in any configured channel are registered immediately.
- **Library:** all channels feed one library with one global video numbering; the same video posted in two channels is stored as two rows (see Duplicates).
- **Existing deployments:** the old single checkpoint is migrated automatically on the first start. `/scanlegacy` (forward based fallback) works on the first channel only.
