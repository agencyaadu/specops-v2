from __future__ import annotations
import os
import re
import asyncio
import discord
from discord import app_commands

from db import pool

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
WA_RE = re.compile(r"^\+?\d{7,15}$")

PROMPT_TIMEOUT = 180  # per step
ONBOARDING_CHANNEL = os.environ.get("ONBOARDING_CHANNEL", "onboarding")


async def _ask(client: discord.Client, channel: discord.TextChannel, user: discord.abc.User,
               prompt: str, validator, error_msg: str) -> str | None:
    await channel.send(f"{user.mention} {prompt}")

    for _ in range(3):
        def check(msg: discord.Message) -> bool:
            return msg.author.id == user.id and msg.channel.id == channel.id

        try:
            msg = await client.wait_for("message", check=check, timeout=PROMPT_TIMEOUT)
        except asyncio.TimeoutError:
            await channel.send(f"{user.mention} stood down (timed out). Run `/onboard` to retry.")
            return None

        cleaned = validator(msg.content.strip())
        if cleaned is not None:
            return cleaned

        await channel.send(f"{user.mention} {error_msg}")

    await channel.send(f"{user.mention} too many retries. Restart with `/onboard`.")
    return None


def _validate_name(s: str) -> str | None:
    s = s.strip()
    return s.upper() if s and len(s) <= 200 else None


def _validate_pan(s: str) -> str | None:
    s = s.strip().upper().replace(" ", "")
    return s if PAN_RE.match(s) else None


def _validate_wa(s: str) -> str | None:
    s = re.sub(r"[^\d+]", "", s.strip())
    return s if WA_RE.match(s) else None


def register(tree: app_commands.CommandTree, client: discord.Client):
    @tree.command(name="onboard", description="Get on the SPEC-OPS roster (in #onboarding).")
    async def onboard(interaction: discord.Interaction):
        user = interaction.user

        if interaction.guild is None:
            await interaction.response.send_message("Run this in a server, not a DM.", ephemeral=True)
            return

        target = discord.utils.get(interaction.guild.text_channels, name=ONBOARDING_CHANNEL)
        if target is None:
            await interaction.response.send_message(
                f"No `#{ONBOARDING_CHANNEL}` channel here. Ask command to set it up.",
                ephemeral=True,
            )
            return

        if interaction.channel_id != target.id:
            await interaction.response.send_message(
                f"Wrong channel. Take this to {target.mention}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Welcome aboard, {user.mention}.\n"
            f"SPEC-OPS runs the sharpest ops in India and you're about to join the roster. Three quick checks — "
            f"answer each as a normal message in this channel."
        )

        name = await _ask(
            client, target, user,
            "**1/3** · What's your full name?",
            _validate_name,
            "Name should be 1–200 characters. Try again.",
        )
        if name is None:
            return

        pan = await _ask(
            client, target, user,
            f"Copy that, **{name}**. **2/3** · PAN number?",
            _validate_pan,
            "PAN format off. Expected 5 letters + 4 digits + 1 letter (e.g. `ABCDE1234F`). Try again.",
        )
        if pan is None:
            return

        wa = await _ask(
            client, target, user,
            "**3/3** · WhatsApp number, country code first (e.g. `+91 98765 43210`)?",
            _validate_wa,
            "Number doesn't look right. 7–15 digits, optional leading `+`. Try again.",
        )
        if wa is None:
            return

        discord_id = str(user.id)
        discord_username = user.name

        async with pool().acquire() as con:
            existing_pan = await con.fetchval("SELECT pan FROM people WHERE discord_id = $1", discord_id)
            if existing_pan and existing_pan != pan:
                await target.send(
                    f"{user.mention} this Discord is already on the roster as `{existing_pan}`. "
                    "Talk to command if this is a new PAN."
                )
                return

            pan_owner = await con.fetchval("SELECT discord_id FROM people WHERE pan = $1", pan)
            if pan_owner and pan_owner != discord_id:
                await target.send(
                    f"{user.mention} that PAN is already on the roster under another account. "
                    "Talk to command."
                )
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
                await target.send(
                    f"✅ Roster updated, {user.mention}.\n"
                    f"`{pan}` · {name} · {wa}"
                )
            else:
                await con.execute(
                    """
                    INSERT INTO people (pan, discord_id, discord_username, name, wa_number)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    pan, discord_id, discord_username, name, wa,
                )
                await target.send(
                    f"✅ {user.mention} you're on the SPEC-OPS roster.\n"
                    f"`{pan}` · {name} · {wa}\n\n"
                    f"`/clock-in` when you start your tour. Stay sharp."
                )
