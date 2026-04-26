# SpecOps v2

Fresh rebuild of SpecOps on an append-only schema. v1 stays untouched at
`/Users/croww/specops/` and in production; this repo is the clean slate.

## What SpecOps is

A factory operations management app. Workers (operators) are deployed to
factories in shifts. Captains and chiefs supervise. The dashboard tracks
daily reports + attendance. Admin surface for assigning people and
managing operations.

## Roles (rank ladder, top to bottom)

- **FREDDY** — top, full access
- **GENERAL** — full management surface
- **CHIEF** — scoped to assigned operations
- **CAPTAIN** — files reports + attendance for assigned operations
- **OPERATOR** — works on the floor
- **VIEWER** — read-only

Approval chain runs one rank up: OPERATOR → CAPTAIN → CHIEF → GENERAL.

## Architecture in one picture

```
4 forms ── T (validate + uppercase + slugify) ──▶ INSERT
                                                    │
                                                    ▼
                                       5 append-only tables in v2:
                                       operators, factories,
                                       operations, attendance, events
                                                    │
                                                    ▼
                                       Views collapse to
                                       "latest row per identifier"
                                                    │
                                                    ▼
                                       Dashboard / Sheets / Analytics
                                       read from views
```

## What v2 means in plain words

- **4 forms**: onboarding, factory visit report, assignment, daily
  (attendance + events).
- Each form = one INSERT into one of 5 tables.
- **No UPDATE, no DELETE** — corrections are new rows with the same
  stable key. Enforced by Postgres BEFORE UPDATE/DELETE triggers.
- **Latest row wins**, read via views.
- All writes go through a validator chain: validate → uppercase →
  slugify → insert.

## The 5 tables (see `docs/schema.dbml` for full DDL)

1. **operators** — every human in the system. Key: `pan_hash`. Replaces
   v1 `submissions` + `bot_roles`.
2. **factories** — physical sites + visit-report data. Key: `factory_id`
   (slug of name). `shift_count` constrains shifts on `operations`.
3. **operations** — assignment of an operator to a factory + shift.
   Key: `operation_id` = `factory_id + shift_slug + pan_hash[:12]`.
4. **attendance** — per-person attendance per shift per date.
   Key: `(factory_id, shift, report_date, person_pan_hash)`.
   Approval chain via `status` + `validator_role` + `validated_by_email`.
5. **events** — daily report numbers + ad-hoc ops events. Polymorphic
   via `kind` + `payload` jsonb.

## The 10 read views

- `v_operators_current` / `v_operators_active`
- `v_factories_current` / `v_factories_active`
- `v_operations_current` / `v_operations_active`
- `v_attendance_current`
- `v_daily_report_current`
- `v_roster` — active operators on active operations, joined to profiles
- `v_dashboard_today` — per-(factory, shift) snapshot for IST today

## Stack (carrying over from v1)

- **Frontend**: vanilla HTML + JS, Vercel
- **Backend**: FastAPI + asyncpg, Railway (v1 used Render; v2 on Railway)
- **Database**: Postgres on Supabase
- **Storage**: Supabase Storage (photos, docs)
- **Auth**: Google OAuth + JWT (operators), separate password flow (admin)
- **Sync**: Google Sheets (operations + onboarding submissions)

## Pages (target surface, mirrors v1)

- `/` — landing
- `/onboard` — public onboarding form
- `/ops` — post-login router
- `/ops/general` — operations management (Ops Admin)
- `/ops/captain` — captain home
- `/ops/dashboard` — today's snapshot
- `/ops/analytics` — time-range trends
- `/ops/validate` — attendance validation queue
- `/report/:op_id` — daily report form
- `/attendance/:op_id` — attendance form
- `/admin` — password-flow data export

## Carry-forward gotchas from v1

- **DB password URL-encoding**: Supabase password contains a literal
  `@` — must be `%40`-encoded in `DATABASE_URL`.
- **load_dotenv ordering**: must run before router imports, or env vars
  read at import time will be empty.
- **asyncpg + Supabase pooler**: set `statement_cache_size=0` when
  behind the transaction pooler (port 6543).
- **`new URL()` in browser**: pass `location.origin` as the base when
  the path is relative — bare relative strings throw "Invalid URL".
- **OAuth fallbacks**: use `??` not `||` for `BACKEND_URL` defaults —
  empty string is falsy under `||` and falls through to localhost.
- **Trailing slashes**: keep frontend POST path, Vercel rewrite, and
  FastAPI route in agreement.
- **Encryption key rotation**: don't change `ENCRYPTION_KEY` against a
  populated DB — old rows become unreadable.

## Why v2 (the rebuild rationale)

v1 grew organically: mutable tables, mixed write paths, ad-hoc role
checks. v2 collapses the model:

- **One INSERT per form** — easy to reason about, easy to audit.
- **Append-only** — full history for free, no destructive ops, trivial
  rollback (insert a corrected row).
- **Views do the dedup** — read code never thinks about "which row is
  current".
- **Explicit approval chain** — attendance status transitions are
  first-class, not bolted on.

## Repo layout (target)

```
specops-v2/
  CLAUDE.md            ← this file
  README.md
  docs/
    schema.dbml        ← canonical schema (paste into dbdiagram.io)
    v1_project_summary.pdf
  backend/             ← FastAPI app (TBD)
  frontend/            ← static pages (TBD)
```

## Working on this repo

- v1 is the reference for behavior — read `/Users/croww/specops/` when
  in doubt about what a form/page does.
- Schema is the spec — if code disagrees with `docs/schema.dbml`, the
  schema wins, fix the code.
- Everything UPPERCASE for human-entered names (factory_name, full_name).
- Everything slugified for IDs (`factory_id`, `operation_id`).
