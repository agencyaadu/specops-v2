"""
Preview short op_id codes (CC-FF-Un-XX) for each operation in the DB.
Reads current rows, computes proposed new ID, prints side-by-side.
Doesn't touch the DB. Run import_short_ids.py once you approve.
"""
from __future__ import annotations
import asyncio
import os
import re
from collections import defaultdict

import asyncpg


# Manual overrides for things that don't slot neatly into the rule.
CITY_OVERRIDES = {
    "NASHIK": "NS",
    "MANDI-GOBINDGARH": "MG",
}


def city_code(city: str | None) -> str:
    if not city:
        return "??"
    if city in CITY_OVERRIDES:
        return CITY_OVERRIDES[city]
    parts = [p for p in re.split(r"[^A-Z0-9]+", city.upper()) if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0])[:2]
    return parts[0][:2] if parts else "??"


# Parse "X UNIT N" or "X N" out of factory names.
UNIT_RE = re.compile(r"\bUNIT[\s\-]*([0-9]+(?:[\s\-&][0-9]+)?)\b", re.IGNORECASE)
TRAILING_NUM_RE = re.compile(r"^(.*?)\s+([0-9]+)\s*$")


def split_factory_unit(factory_name: str) -> tuple[str, str]:
    """Returns (factory_base_name, unit_code)."""
    name = factory_name.strip()
    m = UNIT_RE.search(name)
    if m:
        unit = "U" + re.sub(r"[\s\-&]+", "-", m.group(1))
        base = (name[:m.start()] + name[m.end():]).strip(" -")
        return base, unit
    m2 = TRAILING_NUM_RE.match(name)
    if m2:
        return m2.group(1).strip(), "U" + m2.group(2)
    return name, "U1"


def factory_code(factory_base_name: str) -> str:
    """Initials of the words, padded to 2 chars."""
    name = factory_base_name.upper()
    # Drop common noise words from the initialism
    NOISE = {"PVT", "LTD", "PRIVATE", "LIMITED", "INC", "INDIA", "DIVISION", "INDUSTRIES", "CREATION", "CREATIONS"}
    parts = [p for p in re.split(r"[^A-Z0-9]+", name) if p]
    informative = [p for p in parts if p not in NOISE] or parts
    if not informative:
        return "??"
    if len(informative) == 1:
        return informative[0][:2]
    return (informative[0][0] + informative[1][0])[:2]


SHIFT_OVERRIDES = {
    # exact, case-insensitive matches
    "shift a": "SA",
    "shift b": "SB",
    "shift c": "SC",
    "a": "SA",
    "b": "SB",
    "c": "SC",
    "night": "NI",
    "morning": "MO",
    "morning shift": "MO",
    "evening": "EV",
}


def shift_code(shift: str) -> str:
    s = shift.strip().lower()
    if s in SHIFT_OVERRIDES:
        return SHIFT_OVERRIDES[s]
    # Day-range strings like "9:30 am to 5:30 pm" or "10am to 6pm"
    if re.search(r"\bto\b|\bam\b|\bpm\b", s):
        return "DA"
    # Fallback: initials of first two words
    parts = [p for p in re.split(r"[^a-z0-9]+", s) if p]
    if not parts:
        return "??"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


async def main():
    con = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    rows = await con.fetch(
        """
        SELECT o.operation_id, o.factory_id, o.shift, o.city, o.unit,
               f.name AS factory_name
          FROM operations o
          JOIN factories  f ON f.factory_id = o.factory_id
         ORDER BY o.city, f.name, o.shift
        """
    )

    by_short: dict[str, list[str]] = defaultdict(list)
    out_rows = []

    for r in rows:
        cc = city_code(r["city"])
        fac_base, unit_from_name = split_factory_unit(r["factory_name"])
        fc = factory_code(fac_base)
        unit = unit_from_name  # always derived; ignore stored 'MAIN'
        sc = shift_code(r["shift"])
        new_id = f"{cc}-{fc}-{unit}-{sc}"
        by_short[new_id].append(r["operation_id"])
        out_rows.append((r["operation_id"], new_id, fac_base, unit, sc))

    print(f"{'OLD ID':<55} {'NEW ID':<18} {'fac base'} | {'unit'} | {'shift'}")
    print("-" * 110)
    for old, new, fac_base, unit, sc in out_rows:
        marker = "  ! DUP" if len(by_short[new]) > 1 else ""
        print(f"{old:<55} {new:<18} {fac_base[:30]:<30} | {unit:<5} | {sc}{marker}")

    print()
    print("collisions (multiple old ops mapped to the same short id):")
    n_collisions = 0
    for short, olds in by_short.items():
        if len(olds) > 1:
            n_collisions += 1
            print(f"  {short}:")
            for o in olds:
                print(f"    - {o}")
    if n_collisions == 0:
        print("  (none)")

    await con.close()


if __name__ == "__main__":
    asyncio.run(main())
