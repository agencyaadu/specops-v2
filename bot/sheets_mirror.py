from __future__ import annotations
import asyncio
import logging
import os
import gspread

from db import pool

log = logging.getLogger("sheets_mirror")

_TABLES = ["people", "attendance"]


def _open_sheet():
    gc = gspread.service_account(filename=os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"])
    return gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])


async def _snapshot_table(name: str) -> tuple[list[str], list[list]]:
    async with pool().acquire() as con:
        rows = await con.fetch(f"SELECT * FROM {name} ORDER BY 1")
    if not rows:
        return [], []
    cols = list(rows[0].keys())
    data = [[("" if v is None else str(v)) for v in r.values()] for r in rows]
    return cols, data


async def _push_once():
    sheet = await asyncio.to_thread(_open_sheet)
    for name in _TABLES:
        cols, data = await _snapshot_table(name)
        if not cols:
            cols = ["(empty)"]
            data = []
        ws = await asyncio.to_thread(_get_or_create_tab, sheet, name)
        await asyncio.to_thread(ws.clear)
        await asyncio.to_thread(ws.update, [cols] + data, "A1")
    log.info("sheets mirror push complete")


def _get_or_create_tab(sheet, name: str):
    try:
        return sheet.worksheet(name)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=name, rows=1000, cols=40)


async def run_loop():
    interval = int(os.environ.get("SHEETS_MIRROR_INTERVAL_SECONDS", "300"))
    log.info("sheets mirror loop started — every %ds", interval)
    while True:
        try:
            await _push_once()
        except Exception:
            log.exception("sheets mirror push failed")
        await asyncio.sleep(interval)
