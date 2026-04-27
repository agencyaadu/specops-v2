"""
One-shot ETL: load /Users/croww/Downloads/operations_rows.csv into v2 schema.

- factory_id    = slugify(factory_name)              UPPERCASE-HYPHENATED
- city          = upper(location.strip())
- shift_slug    = slugify(shift)                     UPPERCASE
- unit          = 'MAIN'                             (CSV has no unit col)
- operation_id  = f"{city}-{factory_id}-{unit}-{shift}"  (ALL CAPS)

Cleanup:
- Trim, normalise spacing & punctuation
- Slugify with - separator, no leading/trailing hyphens
- Empty-string fields stored as NULL
- Unique factories deduped by factory_id
- Times parsed as 'HH:MM:SS' (or None)
- POCs collapsed into jsonb {name, phone, role}
- whatsapp_group_url empty in source -> NULL

Run:
    set -a && source .env.local && set +a && \
    .venv/bin/python scripts/import_ops_csv.py /Users/croww/Downloads/operations_rows.csv
"""
from __future__ import annotations
import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime, time
from pathlib import Path

import asyncpg


def _slug(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.strip()).strip("-")
    return s.upper()


def _city(s: str | None) -> str:
    return _slug(s) or ""


def _val(s: str | None) -> str | None:
    s = (s or "").strip()
    return s or None


def _time(s: str | None) -> time | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    print(f"  ! could not parse time: {s!r}")
    return None


def _bool(s: str | None) -> bool:
    return (s or "").strip().lower() in ("true", "t", "1", "yes", "y")


def _poc(name: str, phone: str, role: str) -> dict | None:
    name, phone, role = (name or "").strip(), (phone or "").strip(), (role or "").strip()
    if not (name or phone or role):
        return None
    return {"name": name or None, "phone": phone or None, "role": role or None}


async def import_csv(con: asyncpg.Connection, csv_path: Path) -> dict:
    rows = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    print(f"loaded {len(rows)} rows from {csv_path.name}")

    factories: dict[str, dict] = {}
    operations: list[dict] = []

    for r in rows:
        factory_name = (r["factory_name"] or "").strip()
        location     = (r["location"]     or "").strip()
        shift_raw    = (r["shift"]        or "").strip()

        if not factory_name or not shift_raw:
            print(f"  ! skipping row with missing factory_name/shift: {r}")
            continue

        factory_slug = _slug(factory_name)
        city         = _city(location)
        shift_slug   = _slug(shift_raw)
        unit         = "MAIN"
        operation_id = f"{city}-{factory_slug}-{unit}-{shift_slug}".strip("-")

        if factory_slug not in factories:
            factories[factory_slug] = {
                "factory_id": factory_slug,
                "name":       factory_name.upper(),
                "city":       city or None,
            }

        operations.append({
            "operation_id": operation_id,
            "factory_id":   factory_slug,
            "shift":        shift_slug,
            "unit":         unit,
            "city":         city or None,
            "map_link":     _val(r.get("map_link")),
            "poc1":         _poc(r.get("poc1_name", ""), r.get("poc1_phone", ""), r.get("poc1_role", "")),
            "poc2":         _poc(r.get("poc2_name", ""), r.get("poc2_phone", ""), r.get("poc2_role", "")),
            "sales_team_name":          _val(r.get("sales_team_name")),
            "shift_start":              _time(r.get("shift_start")),
            "shift_end":                _time(r.get("shift_end")),
            "reporting_time":           _time(r.get("reporting_time")),
            "deployment_start":         _time(r.get("deployment_start")),
            "collection_start":         _time(r.get("collection_start")),
            "report_submission_time":   _time(r.get("report_submission_time")),
            "final_closing_time":       _time(r.get("final_closing_time")),
            "whatsapp_group_url":       _val(r.get("whatsapp_group_url")),
            "state":                    "ACTIVE" if _bool(r.get("is_active")) else "INACTIVE",
        })

    # Insert factories first.
    inserted_factories = 0
    skipped_factories = 0
    async with con.transaction():
        for f in factories.values():
            r = await con.execute(
                """
                INSERT INTO factories (factory_id, name, city)
                VALUES ($1, $2, $3)
                ON CONFLICT (factory_id) DO NOTHING
                """,
                f["factory_id"], f["name"], f["city"],
            )
            if r.endswith(" 1"):
                inserted_factories += 1
            else:
                skipped_factories += 1

    # Insert operations.
    inserted_ops = 0
    skipped_ops = 0
    async with con.transaction():
        for o in operations:
            r = await con.execute(
                """
                INSERT INTO operations
                  (operation_id, factory_id, shift, unit, city,
                   map_link, poc1, poc2, sales_team_name,
                   shift_start, shift_end, reporting_time,
                   deployment_start, collection_start,
                   report_submission_time, final_closing_time,
                   whatsapp_group_url, state)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
                ON CONFLICT (operation_id) DO NOTHING
                """,
                o["operation_id"], o["factory_id"], o["shift"], o["unit"], o["city"],
                o["map_link"],
                json.dumps(o["poc1"]) if o["poc1"] else None,
                json.dumps(o["poc2"]) if o["poc2"] else None,
                o["sales_team_name"],
                o["shift_start"], o["shift_end"], o["reporting_time"],
                o["deployment_start"], o["collection_start"],
                o["report_submission_time"], o["final_closing_time"],
                o["whatsapp_group_url"], o["state"],
            )
            if r.endswith(" 1"):
                inserted_ops += 1
            else:
                skipped_ops += 1

    return {
        "factories_inserted": inserted_factories,
        "factories_skipped": skipped_factories,
        "operations_inserted": inserted_ops,
        "operations_skipped": skipped_ops,
        "operation_ids": [o["operation_id"] for o in operations],
    }


async def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python scripts/import_ops_csv.py <path-to-csv>")
    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        sys.exit(f"file not found: {csv_path}")

    con = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    try:
        result = await import_csv(con, csv_path)
        print()
        print(f"factories: +{result['factories_inserted']} new, {result['factories_skipped']} already-existed")
        print(f"operations: +{result['operations_inserted']} new, {result['operations_skipped']} already-existed")
        print()
        print("operation_ids created:")
        for op_id in result["operation_ids"]:
            print(f"  {op_id}")
    finally:
        await con.close()


if __name__ == "__main__":
    asyncio.run(main())
