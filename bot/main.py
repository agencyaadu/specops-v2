from __future__ import annotations
import asyncio
import logging
import os
import sys
from pathlib import Path

# Load .env.local in dev — Railway injects env vars directly.
try:
    from dotenv import load_dotenv
    for env_file in (".env.local", ".env"):
        p = Path(__file__).resolve().parent.parent / env_file
        if p.exists():
            load_dotenv(p)
            break
except ImportError:
    pass

import discord
from discord import app_commands

# Make `bot/` importable when running `python bot/main.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# If GOOGLE_SERVICE_ACCOUNT_JSON is set (e.g. on Railway), materialise it
# into a file and point GOOGLE_SERVICE_ACCOUNT_FILE at it.
_sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if _sa_json:
    _sa_path = "/tmp/gcp-sa.json"
    Path(_sa_path).write_text(_sa_json)
    os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = _sa_path

from db import init_pool, init_schema  # noqa: E402
from commands import onboard, attendance, validate  # noqa: E402
import sheets_mirror  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


class SpecOpsBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await init_pool()
        await init_schema()
        log.info("DB ready")

        onboard.register(self.tree, self)
        attendance.register(self.tree, self)
        validate.register(self.tree)

        # Global sync; takes ~minutes to propagate. For instant testing,
        # set GUILD_ID to copy_global_to + sync per-guild.
        guild_id = os.environ.get("GUILD_ID")
        if guild_id:
            g = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
            log.info("slash commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("slash commands synced globally")

        # Fire and forget — runs forever in the background.
        asyncio.create_task(sheets_mirror.run_loop())

    async def on_ready(self):
        log.info("logged in as %s (id=%s) — guilds=%d", self.user, self.user.id, len(self.guilds))


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN not set")
    SpecOpsBot().run(token, log_handler=None)  # internal class name kept for code stability


if __name__ == "__main__":
    main()
