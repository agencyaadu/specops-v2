"""
Deterministic short-code generation for operation_id.

Format: CC-FF-Un-XX  (city, factory, unit, shift) — all uppercase.

If a generated id collides with an existing one, factory_code() can be
asked to pick from a ranked list of alternates.
"""
from __future__ import annotations
import re

CITY_OVERRIDES = {
    "NASHIK": "NS",
    "MANDI-GOBINDGARH": "MG",
}

SHIFT_OVERRIDES = {
    "shift a": "SA", "shift b": "SB", "shift c": "SC",
    "a": "SA", "b": "SB", "c": "SC",
    "night": "NI",
    "morning": "MO", "morning shift": "MO",
    "evening": "EV",
}

# Noise words to drop when forming factory initials.
NOISE = {"PVT", "LTD", "PRIVATE", "LIMITED", "INC",
         "DIVISION", "INDUSTRIES", "CREATION", "CREATIONS"}


def city_code(city: str | None) -> str:
    if not city:
        return "??"
    if city.upper() in CITY_OVERRIDES:
        return CITY_OVERRIDES[city.upper()]
    parts = [p for p in re.split(r"[^A-Z0-9]+", city.upper()) if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0])[:2]
    return parts[0][:2] if parts else "??"


# "X UNIT N", "X UNIT N&M", or trailing standalone digit
UNIT_RE = re.compile(r"\bUNIT[\s\-]*([0-9]+(?:[\s\-&][0-9]+)?)\b", re.IGNORECASE)
TRAILING_NUM_RE = re.compile(r"^(.*?)\s+([0-9]+)\s*$")


def split_factory_unit(factory_name: str) -> tuple[str, str]:
    """Return (factory_base, unit_code). Default unit = U1."""
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


def factory_code_alternates(factory_base: str) -> list[str]:
    """Yield candidate 2-char codes in preferred order."""
    name = factory_base.upper()
    parts = [p for p in re.split(r"[^A-Z0-9]+", name) if p]
    informative = [p for p in parts if p not in NOISE] or parts
    cands: list[str] = []

    if not informative:
        return ["??"]

    if len(informative) == 1:
        # Single-word: first 2, then first+last, then first+second-letter pairs
        w = informative[0]
        cands.append(w[:2])
        if len(w) > 2:
            cands.append(w[0] + w[-1])
            cands.append(w[0] + w[2])
        return _dedupe(cands)

    # Multi-word: word-pair initials in preference order
    pairs = [(0, 1)]
    for j in range(2, len(informative)):
        pairs.append((0, j))
    for i in range(1, len(informative)):
        for j in range(i + 1, len(informative)):
            pairs.append((i, j))
    for i, j in pairs:
        cands.append(informative[i][0] + informative[j][0])
    # Final fallbacks
    for w in informative:
        cands.append(w[:2])
    return _dedupe(cands)


def _dedupe(seq: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def shift_code(shift: str) -> str:
    s = shift.strip().lower()
    if s in SHIFT_OVERRIDES:
        return SHIFT_OVERRIDES[s]
    if re.search(r"\bto\b|\bam\b|\bpm\b", s):
        return "DA"
    parts = [p for p in re.split(r"[^a-z0-9]+", s) if p]
    if not parts:
        return "??"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def build_op_id(city: str, factory_base: str, unit: str, shift: str,
                taken_ids: set[str] | None = None) -> str:
    """Build CC-FF-Un-XX. If a collision with `taken_ids` would occur,
    walks alternate factory codes until a free id is found."""
    taken = taken_ids or set()
    cc = city_code(city)
    sc = shift_code(shift)
    for fc in factory_code_alternates(factory_base):
        candidate = f"{cc}-{fc}-{unit}-{sc}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(f"could not find a unique id for {city}/{factory_base}/{unit}/{shift}")
