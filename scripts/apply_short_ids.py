"""
Rename existing operation_ids to the short CC-FF-Un-XX form.
Uses bot/short_id.build_op_id with collision protection.

Run once: .venv/bin/python scripts/apply_short_ids.py
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
from short_id import build_op_id, split_factory_unit  # noqa: E402


async def main():
    con = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)

    rows = await con.fetch(
        """
        SELECT o.operation_id AS old_id, o.factory_id, o.shift, o.city,
               f.name AS factory_name
          FROM operations o
          JOIN factories  f ON f.factory_id = o.factory_id
         ORDER BY o.city, f.name, o.shift
        """
    )

    taken: set[str] = set()
    plan: list[tuple[str, str, str]] = []  # (old, new, unit)

    for r in rows:
        fac_base, unit = split_factory_unit(r["factory_name"])
        new_id = build_op_id(r["city"], fac_base, unit, r["shift"], taken)
        taken.add(new_id)
        plan.append((r["old_id"], new_id, unit))

    # Print plan
    print(f"{'OLD':<55} {'NEW':<18} {'UNIT'}")
    print("-" * 80)
    for old, new, unit in plan:
        print(f"{old:<55} {new:<18} {unit}")
    if len(taken) != len(plan):
        print(f"\nCOLLISION DETECTED: {len(plan)} ops mapped to {len(taken)} unique ids")
        await con.close()
        sys.exit(1)
    print(f"\nall {len(plan)} unique. applying...")

    # Apply in a transaction. Update attendance.op_id refs too.
    async with con.transaction():
        for old, new, unit in plan:
            await con.execute(
                "UPDATE attendance SET op_id = $2 WHERE op_id = $1",
                old, new,
            )
            await con.execute(
                "UPDATE operations SET operation_id = $2, unit = $3 WHERE operation_id = $1",
                old, new, unit,
            )

    print("done.")
    await con.close()


if __name__ == "__main__":
    asyncio.run(main())
