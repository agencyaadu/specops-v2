# Batch 1 — Plan (Discord-first)

## North star

> Operators talk to a Discord bot. Bot writes to Supabase. Selected tables
> mirror to Google Sheets. No web UI. No public HTTP API.

## Architecture

```
[Discord users]
      │  slash commands, modals, button clicks, photo attachments
      ▼
[Discord bot — Python (discord.py), one process on Railway]
      │  asyncpg                   │  Storage SDK              │  Sheets API
      ▼                            ▼                           ▼
[Supabase Postgres]          [Supabase Storage]         [Google Sheet]
   (people, attendance)        (attendance photos)        (mirror — async)
```

## Components

| # | Component | Where it runs | Owns |
|---|---|---|---|
| 1 | Discord bot process | Railway (worker) | All user interaction, all DB writes |
| 2 | Supabase Postgres | Supabase | Source of truth for `people` + `attendance` |
| 3 | Supabase Storage | Supabase | Attendance photos (one bucket, public URLs) |
| 4 | Sheets mirror task | Inside the bot process (async loop) | Periodically push DB → Google Sheet |

No FastAPI. No frontend. Railway runs one service.

## Data flow per feature

### Onboarding
1. User runs `/onboard` in the server (or DM the bot).
2. Bot opens a **modal** with 3 fields: `PAN`, `Full name`, `WhatsApp number`.
3. On submit:
   - Validate (PAN regex, normalize WA, uppercase name).
   - Look up by Discord ID — if exists, update; else INSERT into `people`.
   - Reply (ephemeral): `✅ Profile created. PAN ABCDE1234F`.
4. Profile is now linked: `discord_id ↔ pan`.

### Clock-in
1. User runs `/clock-in op_id:<text> role:<select>` and **attaches a photo**
   (slash command supports an `attachment` option).
2. Bot:
   - Looks up PAN from `discord_id`. If not found → reply "run `/onboard` first".
   - Uploads attachment to Supabase Storage → public URL.
   - INSERT into `attendance` (clock_in_time = now, photo_url, op_id, role).
   - Posts the new row as an **embed** in `#attendance-validation` channel
     with `[✅ Approve] [❌ Reject]` buttons.
   - Replies to the user (ephemeral): `Clocked in. at_id 42.`

### Clock-out
1. User runs `/clock-out` — no args.
2. Bot finds their latest open row (clock_out_time IS NULL) for their discord_id.
3. UPDATE `clock_out_time = now`. Reply: `Clocked out. Worked 3h 12m.`

### Validation
1. Validator clicks `[✅ Approve]` or `[❌ Reject]` on the embed in
   `#attendance-validation`.
2. Bot checks the clicker has the right Discord role (gating).
3. UPDATE `attendance` (validation, validator_discord_id, validated_at,
   rejection_reason if rejected).
4. Edits the embed in place to show outcome + who validated.

### Sheets mirror
1. Background task in the bot process runs every N minutes.
2. Reads `people` and `attendance` from Postgres.
3. Overwrites the corresponding sheet tabs (full snapshot, not diff —
   simple, idempotent, slow tables).

## Schema deltas (from `docs/batch1_schema.dbml`)

**`people`** — add Discord identity, drop Google:
- ➕ `discord_id` text NOT NULL UNIQUE
- ➕ `discord_username` text
- ➖ `email` (drop — Google not used)
- ➖ `google_id` (drop)

**`attendance`** — drop geotag, swap validator identity:
- ➖ `geotag_lat`, `geotag_lng` (drop — Discord can't capture)
- 🔄 `validator_email` → `validator_discord_id`

Everything else stays.

## Discord role → schema role mapping

Validators need to be gated by Discord role. Open question — see below.

| App role | Discord role (TBD) | Can do |
|---|---|---|
| OPERATOR | @Operator | `/onboard`, `/clock-in`, `/clock-out` |
| CAPTAIN | @Captain | + validate operator attendance |
| CHIEF | @Chief | + validate captain attendance |
| GENERAL | @General | + validate chief attendance |

## Build sequence (in order)

1. **Wipe what we don't need** — delete `frontend/`, `backend/main.py`,
   `backend/auth.py`, `backend/storage.py` becomes a bot-side helper,
   keep `backend/db.py` schema definition.
2. **Restructure** — single `bot/` directory with `main.py`, `db.py`,
   `commands/onboard.py`, `commands/attendance.py`, `commands/validate.py`,
   `sheets_mirror.py`, `storage.py`, `requirements.txt`, `.env.example`.
3. **Schema migration** — update `docs/batch1_schema.dbml` and `db.py` to
   the new shape (Discord identity, no geotag).
4. **Bot scaffold** — bot connects, registers slash commands, replies "pong".
5. **Onboard command** — modal + INSERT.
6. **Clock-in / clock-out commands** — attachment upload + INSERT/UPDATE.
7. **Validation buttons** — interaction handler + role gating.
8. **Sheets mirror** — minimum viable: one tab per table, full snapshot every 10 min.
9. **Railway deploy** — one worker service, env vars, smoke test in Discord.

## Open questions — answer to start build

1. **Discord server / guild** — which one? Need the guild ID (or you
   add the bot and I read it from connection).
2. **Validation channel name** — `#attendance-validation` ok, or something else?
3. **Discord roles for gating** — do existing roles like `@Captain`, `@Chief`,
   `@General` already exist on the server? If not, I'll create role names
   the bot expects and you assign them in Discord.
4. **Multi-server** — single guild, or should the bot work across multiple?
5. **Sheets mirror frequency** — every 10 min ok? Or want push-on-write?
6. **Sheet target** — new Google Sheet for v2, or reuse v1's? Need the
   sheet URL + a service account JSON to write to it.
7. **Photo retention** — keep all photos forever, or expire after N days?

## Stack lock

- **Bot**: `discord.py` 2.x (Python, async, mature)
- **DB driver**: `asyncpg`
- **Storage**: `supabase-py` SDK
- **Sheets**: `gspread` + `google-auth` (service account)
- **Hosting**: Railway, single worker service
- **Python**: 3.12

## What you need to provide

- Supabase project (URL + service role key + connection string)
- Discord bot token (from https://discord.com/developers)
- Discord guild ID
- Google Sheets service account JSON + target sheet URL
- Railway project (I'll write the config; you click deploy)
