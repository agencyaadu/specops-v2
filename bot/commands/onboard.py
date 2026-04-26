from __future__ import annotations
import re
import asyncio
import discord
from discord import app_commands

from db import pool

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
WA_RE = re.compile(r"^\+?\d{7,15}$")

PROMPT_TIMEOUT = 180  # seconds per step


async def _ask(client: discord.Client, dm: discord.DMChannel, user: discord.User,
               prompt: str, validator, error_msg: str) -> str | None:
    """Send a prompt in DM and wait for the user's reply. Validate; re-prompt up to 3 times."""
    await dm.send(prompt)

    for attempt in range(3):
        def check(msg: discord.Message) -> bool:
            return msg.author.id == user.id and isinstance(msg.channel, discord.DMChannel)

        try:
            msg = await client.wait_for("message", check=check, timeout=PROMPT_TIMEOUT)
        except asyncio.TimeoutError:
            await dm.send("⏳ Timed out. Run `/onboard` again when you're ready.")
            return None

        value = msg.content.strip()
        cleaned = validator(value)
        if cleaned is not None:
            return cleaned

        await dm.send(f"❌ {error_msg}")

    await dm.send("Too many tries. Run `/onboard` again to restart.")
    return None


def _validate_name(s: str) -> str | None:
    s = s.strip()
    if not s or len(s) > 200:
        return None
    return s.upper()


def _validate_pan(s: str) -> str | None:
    s = s.strip().upper().replace(" ", "")
    if not PAN_RE.match(s):
        return None
    return s


def _validate_wa(s: str) -> str | None:
    s = re.sub(r"[^\d+]", "", s.strip())
    if not WA_RE.match(s):
        return None
    return s


def register(tree: app_commands.CommandTree, client: discord.Client):
    @tree.command(name="onboard", description="Create your SPEC-OPS profile (step-by-step in DMs).")
    async def onboard(interaction: discord.Interaction):
        user = interaction.user

        # Open a DM channel.
        try:
            dm = user.dm_channel or await user.create_dm()
            await dm.send(f"👋 Hi {user.mention}! Let's set up your SPEC-OPS profile. I'll ask 3 quick questions.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't DM you. Open your privacy settings → enable DMs from server members, then run `/onboard` again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "📬 Check your DMs — I'll walk you through it.",
            ephemeral=True,
        )

        # Step 1: name
        name = await _ask(
            client, dm, user,
            "**1/3 — Full name?**\n(e.g. Jane Doe — will be stored in UPPERCASE)",
            _validate_name,
            "Name should be 1–200 characters.",
        )
        if name is None:
            return

        # Step 2: PAN
        pan = await _ask(
            client, dm, user,
            f"Got it, **{name}**.\n\n**2/3 — PAN number?**\n(format: 5 letters + 4 digits + 1 letter, e.g. ABCDE1234F)",
            _validate_pan,
            "PAN format invalid. Expected like `ABCDE1234F`.",
        )
        if pan is None:
            return

        # Step 3: WhatsApp
        wa = await _ask(
            client, dm, user,
            "**3/3 — WhatsApp number?**\n(with country code, e.g. +91 98765 43210)",
            _validate_wa,
            "WhatsApp number invalid. Expected 7–15 digits, optional leading `+`.",
        )
        if wa is None:
            return

        discord_id = str(user.id)
        discord_username = user.name

        # Conflict checks + INSERT/UPDATE.
        async with pool().acquire() as con:
            existing_pan = await con.fetchval("SELECT pan FROM people WHERE discord_id = $1", discord_id)
            if existing_pan and existing_pan != pan:
                await dm.send(
                    f"⚠️ This Discord account is already linked to PAN `{existing_pan}`. "
                    "Ask an admin to unlink first if this is a new PAN."
                )
                return

            pan_owner = await con.fetchval("SELECT discord_id FROM people WHERE pan = $1", pan)
            if pan_owner and pan_owner != discord_id:
                await dm.send("⚠️ That PAN is already linked to a different Discord account.")
                return

            if existing_pan == pan:
                await con.execute(
                    """
                    UPDATE people
                       SET name = $2, wa_number = $3, discord_username = $4, updated_at = now()
                     WHERE pan = $1
                    """,
                    pan, name, wa, discord_username,
                )
                await dm.send(f"✅ Profile **updated**.\n\n**Name:** {name}\n**PAN:** `{pan}`\n**WhatsApp:** {wa}")
            else:
                await con.execute(
                    """
                    INSERT INTO people (pan, discord_id, discord_username, name, wa_number)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    pan, discord_id, discord_username, name, wa,
                )
                await dm.send(
                    f"✅ Profile **created**.\n\n"
                    f"**Name:** {name}\n**PAN:** `{pan}`\n**WhatsApp:** {wa}\n\n"
                    f"You can now use `/clock-in` in any server channel."
                )
