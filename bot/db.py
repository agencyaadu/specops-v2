from __future__ import annotations
import os
import asyncpg

_pool: "asyncpg.Pool | None" = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=5,
            statement_cache_size=0,
        )
    return _pool


def pool() -> asyncpg.Pool:
    assert _pool is not None, "db pool not initialized"
    return _pool


async def init_schema() -> None:
    p = await init_pool()
    async with p.acquire() as con:
        await con.execute(SCHEMA_SQL)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS people (
    pan               text PRIMARY KEY,
    discord_id        text NOT NULL UNIQUE,
    discord_username  text,
    google_id         text UNIQUE,
    email             text,

    name              text NOT NULL,
    wa_number         text NOT NULL,

    dob               date,
    location          text,
    languages         text,
    hardest_problem   text,
    headshot_url      text,
    intro_video_url   text,

    bank_name         text,
    account_number    text,
    ifsc              text,
    upi_id            text,

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Bring older rows in line with the current shape (must happen
-- BEFORE any CREATE INDEX that references the new columns).
ALTER TABLE people ADD COLUMN IF NOT EXISTS google_id text;
ALTER TABLE people ADD COLUMN IF NOT EXISTS email     text;
ALTER TABLE people DROP COLUMN IF EXISTS people_id;
DROP SEQUENCE IF EXISTS people_id_seq;

CREATE INDEX IF NOT EXISTS people_discord_idx ON people (discord_id);
CREATE INDEX IF NOT EXISTS people_email_idx   ON people (email);
CREATE UNIQUE INDEX IF NOT EXISTS people_google_id_idx ON people (google_id);

CREATE TABLE IF NOT EXISTS factories (
    id            bigserial PRIMARY KEY,
    factory_id    text NOT NULL UNIQUE,
    name          text NOT NULL,
    city          text,
    state         text NOT NULL DEFAULT 'ACTIVE',
    created_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE factories ADD COLUMN IF NOT EXISTS city text;

CREATE TABLE IF NOT EXISTS operations (
    id              bigserial PRIMARY KEY,
    operation_id    text NOT NULL UNIQUE,
    factory_id      text NOT NULL REFERENCES factories (factory_id) ON DELETE RESTRICT,
    shift           text NOT NULL,
    op_password     text,
    state           text NOT NULL DEFAULT 'ACTIVE',
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Extended fields for the imported CSV. Nullable additions.
ALTER TABLE operations ADD COLUMN IF NOT EXISTS unit                  text NOT NULL DEFAULT 'MAIN';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS city                  text;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS map_link              text;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS poc1                  jsonb;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS poc2                  jsonb;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS sales_team_name       text;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS shift_start           time;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS shift_end             time;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS reporting_time        time;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS deployment_start      time;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS collection_start      time;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS report_submission_time time;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS final_closing_time    time;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS whatsapp_group_url    text;
-- Drop the NOT NULL on op_password so imports without one can land.
ALTER TABLE operations ALTER COLUMN op_password DROP NOT NULL;

CREATE INDEX IF NOT EXISTS operations_factory_idx ON operations (factory_id);
CREATE INDEX IF NOT EXISTS operations_state_idx   ON operations (state);
CREATE INDEX IF NOT EXISTS operations_city_idx    ON operations (city);

CREATE TABLE IF NOT EXISTS op_assignments (
    id                       bigserial PRIMARY KEY,
    operation_id             text NOT NULL REFERENCES operations (operation_id) ON DELETE RESTRICT,
    person_pan               text NOT NULL REFERENCES people (pan) ON DELETE RESTRICT,
    person_discord_id        text NOT NULL,
    role                     text NOT NULL,
    state                    text NOT NULL DEFAULT 'ACTIVE',
    assigned_by_discord_id   text,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (operation_id, person_pan, role)
);

CREATE INDEX IF NOT EXISTS op_assign_op_idx     ON op_assignments (operation_id);
CREATE INDEX IF NOT EXISTS op_assign_pan_idx    ON op_assignments (person_pan);
CREATE INDEX IF NOT EXISTS op_assign_role_idx   ON op_assignments (role);
CREATE INDEX IF NOT EXISTS op_assign_disc_idx   ON op_assignments (person_discord_id);

CREATE TABLE IF NOT EXISTS attendance (
    at_id                          bigserial PRIMARY KEY,
    pp_pan                         text NOT NULL REFERENCES people (pan) ON DELETE RESTRICT,
    pp_discord_id                  text NOT NULL,

    op_id                          text NOT NULL,
    role                           text NOT NULL,

    clock_in_time                  timestamptz NOT NULL DEFAULT now(),
    clock_out_time                 timestamptz,

    photo_url                      text,

    selected_validator_discord_id  text,
    validator_discord_id           text,
    validation                     text NOT NULL DEFAULT 'PENDING',
    validated_at                   timestamptz,
    rejection_reason               text,

    guild_id                       text,
    validation_message_id          text,
    thread_id                      text,

    created_at                     timestamptz NOT NULL DEFAULT now()
);

-- columns added after initial release; safe to no-op if already there
ALTER TABLE attendance ADD COLUMN IF NOT EXISTS selected_validator_discord_id text;
ALTER TABLE attendance ADD COLUMN IF NOT EXISTS thread_id text;

CREATE INDEX IF NOT EXISTS attendance_op_idx            ON attendance (op_id);
CREATE INDEX IF NOT EXISTS attendance_pp_idx            ON attendance (pp_pan);
CREATE INDEX IF NOT EXISTS attendance_pp_discord_idx    ON attendance (pp_discord_id);
CREATE INDEX IF NOT EXISTS attendance_status_idx        ON attendance (validation);
"""
