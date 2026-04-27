"""
ETL: load /Users/croww/Downloads/op_assignments_rows.csv into op_assignments.

Maps:
- old op_id (e.g. 'antariksh-infra-build_shift-a')  -> new short id (MU-AI-U1-SA)
  via operations table joined on factory + shift (case insensitive)
- email -> people.pan (must already be onboarded; otherwise SKIP & report)
- role  -> uppercase

Run:
    .venv/bin/python scripts/import_assignments_csv.py /Users/croww/Downloads/op_assignments_rows.csv
"""
from __future__ import annotations
import asyncio
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

import asyncpg


def split_old_op_id(s: str) -> tuple[str, str]:
    """ 'antariksh-infra-build_shift-a' -> ('ANTARIKSH INFRA BUILD', 'SHIFT-A') """
    if "_" not in s:
        return s.upper().replace("-", " "), ""
    factory_part, shift_part = s.rsplit("_", 1)
    factory_name = factory_part.replace("-", " ").upper().strip()
    shift_slug = shift_part.upper().strip()
    return factory_name, shift_slug


async def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python scripts/import_assignments_csv.py <csv>")
    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        sys.exit(f"file not found: {csv_path}")

    con = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)

    # Build lookups
    ppl_rows = await con.fetch("SELECT pan, discord_id, lower(email) AS email FROM people WHERE email IS NOT NULL")
    email_to_person = {r["email"]: (r["pan"], r["discord_id"]) for r in ppl_rows}
    print(f"onboarded people with email: {len(email_to_person)}")

    op_rows = await con.fetch(
        "SELECT operation_id, factory_id, shift FROM operations"
    )
    fac_rows = await con.fetch("SELECT factory_id, name FROM factories")
    name_to_factory = {r["name"].upper(): r["factory_id"] for r in fac_rows}

    # Normalise factory name to alphanum-only so 'COMSYN T.T' and
    # 'COMSYN T T' and 'COMSYN T-T' all collide on 'COMSYNTT'.
    import re as _re
    def _alnum(s: str) -> str:
        return _re.sub(r"[^A-Z0-9]+", "", s.upper())

    op_by_key: dict[tuple[str, str], str] = {}
    op_by_factory: dict[str, list[str]] = defaultdict(list)
    for r in op_rows:
        fact_name = next((f["name"] for f in fac_rows if f["factory_id"] == r["factory_id"]), None)
        if fact_name:
            op_by_key[(_alnum(fact_name), r["shift"])] = r["operation_id"]
            op_by_factory[_alnum(fact_name)].append(r["operation_id"])

    # Aliases for shift normalization (CSV old format -> DB canonical)
    shift_aliases = {
        "SHIFT-A": "SHIFT-A",
        "SHIFT-B": "SHIFT-B",
        "SHIFT-C": "SHIFT-C",
        "A": "SHIFT-A", "B": "SHIFT-B", "C": "SHIFT-C",
        "NIGHT": "NIGHT",
        "MORNING": "MORNING",
        "MORNING-SHIFT": "MORNING SHIFT",  # may be stored either way
        "9-30-AM-TO-5-30-PM": "9:30 AM TO 5:30 PM",
        "10AM-TO-6PM": "10AM TO 6PM",
    }

    rows = list(csv.DictReader(csv_path.open(newline="", encoding="utf-8")))
    print(f"loaded {len(rows)} csv rows")

    inserted, skipped_no_op, skipped_no_pan, skipped_dup = 0, 0, 0, 0
    skipped_emails: list[str] = []
    skipped_ops: list[str] = []

    async with con.transaction():
        for r in rows:
            old_id = r["op_id"].strip()
            email  = (r["email"] or "").strip().lower()
            role   = (r["role"]  or "").strip().upper()

            if not old_id or not email or not role:
                continue

            # Resolve op via alphanum-only factory match.
            factory_name, shift_slug = split_old_op_id(old_id)
            fact_key = _alnum(factory_name)
            shift_candidates = [shift_slug, shift_aliases.get(shift_slug, shift_slug), shift_slug.replace("-", " ")]

            new_op_id = None
            for s in shift_candidates:
                if (fact_key, s) in op_by_key:
                    new_op_id = op_by_key[(fact_key, s)]
                    break

            # Fallback: if factory has exactly one op total, use it.
            if not new_op_id and fact_key in op_by_factory and len(op_by_factory[fact_key]) == 1:
                new_op_id = op_by_factory[fact_key][0]

            if not new_op_id:
                skipped_no_op += 1
                skipped_ops.append(f"{old_id} (factory={factory_name!r} shift={shift_slug!r})")
                continue

            # Resolve person
            pp = email_to_person.get(email)
            if not pp:
                skipped_no_pan += 1
                skipped_emails.append(email)
                continue

            pan, discord_id = pp
            try:
                await con.execute(
                    """
                    INSERT INTO op_assignments
                        (operation_id, person_pan, person_discord_id, role, assigned_by_discord_id)
                    VALUES ($1, $2, $3, $4, NULL)
                    ON CONFLICT (operation_id, person_pan, role) DO UPDATE
                        SET state = 'ACTIVE', updated_at = now()
                    """,
                    new_op_id, pan, discord_id, role,
                )
                inserted += 1
            except Exception as e:
                print(f"  ! insert failed for {new_op_id} / {pan} / {role}: {e}")
                skipped_dup += 1

    print()
    print(f"inserted/updated: {inserted}")
    print(f"skipped (op not found):     {skipped_no_op}")
    print(f"skipped (person not onboarded): {skipped_no_pan}")
    print(f"skipped (insert errors):    {skipped_dup}")

    if skipped_ops:
        print("\nunmatched op_ids:")
        for o in skipped_ops[:20]:
            print(f"  {o}")
    if skipped_emails:
        print("\nemails not on roster (will assign once they /onboard):")
        for e in sorted(set(skipped_emails)):
            print(f"  {e}")

    await con.close()


if __name__ == "__main__":
    asyncio.run(main())
