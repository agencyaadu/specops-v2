from __future__ import annotations
import asyncio
import os
import re
from datetime import datetime, timezone
import discord
from discord import app_commands

from db import pool
from storage import upload_attachment
from commands.validate import build_validation_view, build_pending_embed

VALIDATION_CHANNEL = os.environ.get("ATTENDANCE_VALIDATION_CHANNEL", "attendance-validation")
PROMPT_TIMEOUT = 180

VALID_ROLES = {"OPERATOR", "CAPTAIN", "CHIEF"}


def _humanize(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


async def _wait_text(client: discord.Client, channel: discord.abc.Messageable, user: discord.abc.User,
                     validator, error_msg: str) -> str | None:
    """Wait for a text reply. Caller sends the question first."""
    for _ in range(3):
        def check(msg: discord.Message) -> bool:
            return msg.author.id == user.id and msg.channel.id == channel.id
        try:
            msg = await client.wait_for("message", check=check, timeout=PROMPT_TIMEOUT)
        except asyncio.TimeoutError:
            await channel.send(f"{user.mention} stood down (timed out). Run `/clock-in` to retry.")
            return None
        cleaned = validator(msg.content.strip())
        if cleaned is not None:
            return cleaned
        await channel.send(f"{user.mention} {error_msg}")
    await channel.send(f"{user.mention} too many retries. Restart with `/clock-in`.")
    return None


async def _wait_photo(client: discord.Client, channel: discord.abc.Messageable, user: discord.abc.User
                      ) -> tuple[str, str | None] | None:
    """Wait for a message with an image attachment. Caller sends the prompt first."""
    for _ in range(3):
        def check(msg: discord.Message) -> bool:
            return (msg.author.id == user.id
                    and msg.channel.id == channel.id
                    and bool(msg.attachments))
        try:
            msg = await client.wait_for("message", check=check, timeout=PROMPT_TIMEOUT)
        except asyncio.TimeoutError:
            await channel.send(f"{user.mention} stood down (no photo received).")
            return None

        att = msg.attachments[0]
        if not (att.content_type or "").startswith("image/"):
            await channel.send(f"{user.mention} that's not an image. Try again.")
            continue
        return att.url, att.content_type
    await channel.send(f"{user.mention} too many retries. Restart with `/clock-in`.")
    return None


def _validate_op_id(s: str) -> str | None:
    s = s.strip()
    if not s or len(s) > 80:
        return None
    return re.sub(r"\s+", "-", s)


def _validate_role(s: str) -> str | None:
    s = s.strip().upper()
    return s if s in VALID_ROLES else None


async def _post_to_validation_channel(guild: discord.Guild, embed: discord.Embed, view: discord.ui.View):
    channel = discord.utils.get(guild.text_channels, name=VALIDATION_CHANNEL)
    if channel is None:
        return None
    try:
        return await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        return None


def register(tree: app_commands.CommandTree, client: discord.Client):

    @tree.command(name="clock-in", description="Start your tour. Conversational — answer in this channel.")
    async def clock_in(interaction: discord.Interaction):
        user = interaction.user
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("Run this in a server channel.", ephemeral=True)
            return

        discord_id = str(user.id)
        async with pool().acquire() as con:
            person = await con.fetchrow("SELECT pan, name FROM people WHERE discord_id = $1", discord_id)
        if not person:
            await interaction.response.send_message(
                "Not on the roster yet. Run `/onboard` first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Brief, {user.mention}.\n\n"
            f"**1/3 · Where are you posted?**\nDrop the op ID (e.g. `mumbai-am`) below."
        )
        channel = interaction.channel

        op_id = await _wait_text(
            client, channel, user,
            _validate_op_id,
            "op ID should be 1–80 chars. Try again.",
        )
        if op_id is None:
            return

        await channel.send(
            f"{user.mention} Copy. Posted at `{op_id}`.\n\n"
            f"**2/3 · Role on this op?**\nType one: `OPERATOR`, `CAPTAIN`, or `CHIEF`."
        )
        role = await _wait_text(
            client, channel, user,
            _validate_role,
            "use one of: OPERATOR, CAPTAIN, CHIEF.",
        )
        if role is None:
            return

        await channel.send(
            f"{user.mention}\n\n"
            f"**3/3 · Sitrep photo.**\nDrop an image attachment (📎 button → upload)."
        )
        photo = await _wait_photo(client, channel, user)
        if photo is None:
            return

        try:
            photo_url = await upload_attachment(photo[0], photo[1])
        except Exception as e:
            await channel.send(f"{user.mention} photo upload failed: `{e}`. Restart with `/clock-in`.")
            return

        async with pool().acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO attendance (pp_pan, pp_discord_id, op_id, role, photo_url, guild_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING at_id, clock_in_time
                """,
                person["pan"], discord_id, op_id, role, photo_url,
                str(interaction.guild_id),
            )

        embed = build_pending_embed(
            at_id=row["at_id"], pan=person["pan"], name=person["name"],
            discord_user=user, op_id=op_id, role=role,
            clock_in_time=row["clock_in_time"], photo_url=photo_url,
        )
        view = build_validation_view(row["at_id"])
        msg = await _post_to_validation_channel(interaction.guild, embed, view)
        if msg is not None:
            async with pool().acquire() as con:
                await con.execute(
                    "UPDATE attendance SET validation_message_id = $2 WHERE at_id = $1",
                    row["at_id"], str(msg.id),
                )

        await channel.send(
            f"✅ {user.mention} on the clock. `at_id={row['at_id']}` · `{op_id}` · {role}.\n"
            f"Awaiting confirmation by command. `/clock-out` when your tour ends."
        )

    @tree.command(name="clock-out", description="Stand down from your current tour.")
    async def clock_out(interaction: discord.Interaction):
        user = interaction.user
        discord_id = str(user.id)

        async with pool().acquire() as con:
            row = await con.fetchrow(
                """
                UPDATE attendance
                   SET clock_out_time = $2
                 WHERE at_id = (
                       SELECT at_id FROM attendance
                        WHERE pp_discord_id = $1 AND clock_out_time IS NULL
                        ORDER BY clock_in_time DESC LIMIT 1
                 )
                RETURNING at_id, clock_in_time, clock_out_time
                """,
                discord_id, datetime.now(timezone.utc),
            )

        if not row:
            await interaction.response.send_message(
                "No open tour found. Run `/clock-in` to start one.",
                ephemeral=True,
            )
            return

        elapsed = (row["clock_out_time"] - row["clock_in_time"]).total_seconds()
        await interaction.response.send_message(
            f"✅ Stood down. `at_id={row['at_id']}` · {_humanize(elapsed)} on the books. Good work."
        )
