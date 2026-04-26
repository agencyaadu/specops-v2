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
PHOTO_TIMEOUT = 180

VALID_ROLES = {"OPERATOR", "CAPTAIN", "CHIEF"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff")


def _is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/"):
        return True
    fn = (att.filename or "").lower()
    return any(fn.endswith(ext) for ext in IMAGE_EXTS)


def _humanize(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


def _validate_op_id(s: str) -> str | None:
    s = s.strip()
    if not s or len(s) > 80:
        return None
    return re.sub(r"\s+", "-", s).lower()


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


# ─── Clock-in: single-shot slash command (op_id + role + photo inline) ───────


# ─── Clock-out: button → confirm ─────────────────────────────────────────────

class ClockOutView(discord.ui.View):
    def __init__(self, at_id: int, op_id: str, role: str, clock_in_time: datetime):
        super().__init__(timeout=600)
        self.at_id = at_id
        self.op_id = op_id
        self.role = role
        self.clock_in_time = clock_in_time

    @discord.ui.button(label="Stand down", style=discord.ButtonStyle.secondary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with pool().acquire() as con:
            row = await con.fetchrow(
                """
                UPDATE attendance
                   SET clock_out_time = $2
                 WHERE at_id = $1 AND clock_out_time IS NULL
                RETURNING at_id, clock_in_time, clock_out_time
                """,
                self.at_id, datetime.now(timezone.utc),
            )
        if not row:
            await interaction.response.edit_message(
                content="❌ This tour was already closed.", view=None,
            )
            return

        elapsed = (row["clock_out_time"] - row["clock_in_time"]).total_seconds()
        await interaction.response.edit_message(
            content=(
                f"✅ Stood down. `at_id={row['at_id']}` · `{self.op_id}` · {self.role}\n"
                f"{_humanize(elapsed)} on the books. Good work."
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled. Still on the clock.", view=None)


# ─── Slash command registration ──────────────────────────────────────────────

def register(tree: app_commands.CommandTree, client: discord.Client):

    @tree.command(name="clock-in", description="Start your tour: op ID, role, photo.")
    @app_commands.describe(
        op_id="Where you're posted (e.g. mumbai-am)",
        role="Your role on this op",
        photo="Sitrep photo proof",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="OPERATOR", value="OPERATOR"),
        app_commands.Choice(name="CAPTAIN",  value="CAPTAIN"),
        app_commands.Choice(name="CHIEF",    value="CHIEF"),
    ])
    async def clock_in(
        interaction: discord.Interaction,
        op_id: str,
        role: app_commands.Choice[str],
        photo: discord.Attachment,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        # Defer: photo upload + DB write can take a few seconds.
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with pool().acquire() as con:
            person = await con.fetchrow(
                "SELECT pan, name FROM people WHERE discord_id = $1", str(interaction.user.id),
            )
        if not person:
            await interaction.followup.send("Not on the roster yet. Run `/onboard` first.", ephemeral=True)
            return

        op_id_clean = _validate_op_id(op_id)
        if op_id_clean is None:
            await interaction.followup.send("❌ Op ID should be 1–80 chars.", ephemeral=True)
            return

        if not _is_image_attachment(photo):
            await interaction.followup.send("❌ That doesn't look like an image attachment.", ephemeral=True)
            return

        try:
            photo_url = await upload_attachment(photo.url, photo.content_type or "image/jpeg")
        except Exception as e:
            await interaction.followup.send(f"❌ Photo upload failed: `{e}`.", ephemeral=True)
            return

        async with pool().acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO attendance (pp_pan, pp_discord_id, op_id, role, photo_url, guild_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING at_id, clock_in_time
                """,
                person["pan"], str(interaction.user.id), op_id_clean, role.value, photo_url,
                str(interaction.guild_id),
            )

        embed = build_pending_embed(
            at_id=row["at_id"], pan=person["pan"], name=person["name"],
            discord_user=interaction.user, op_id=op_id_clean, role=role.value,
            clock_in_time=row["clock_in_time"], photo_url=photo_url,
        )
        view = build_validation_view(row["at_id"])
        vm = await _post_to_validation_channel(interaction.guild, embed, view)
        if vm is not None:
            async with pool().acquire() as con:
                await con.execute(
                    "UPDATE attendance SET validation_message_id = $2 WHERE at_id = $1",
                    row["at_id"], str(vm.id),
                )

        await interaction.followup.send(
            f"✅ On the clock. `at_id={row['at_id']}` · `{op_id_clean}` · {role.value}.\n"
            f"Awaiting confirmation by command. `/clock-out` when your tour ends.",
            ephemeral=True,
        )

    @tree.command(name="clock-out", description="Close your current tour.")
    async def clock_out(interaction: discord.Interaction):
        async with pool().acquire() as con:
            row = await con.fetchrow(
                """
                SELECT at_id, op_id, role, clock_in_time
                  FROM attendance
                 WHERE pp_discord_id = $1 AND clock_out_time IS NULL
                 ORDER BY clock_in_time DESC LIMIT 1
                """,
                str(interaction.user.id),
            )

        if not row:
            await interaction.response.send_message(
                "No open tour found. Run `/clock-in` to start one.", ephemeral=True,
            )
            return

        elapsed = (datetime.now(timezone.utc) - row["clock_in_time"]).total_seconds()
        await interaction.response.send_message(
            content=(
                f"Open tour: `at_id={row['at_id']}` · `{row['op_id']}` · {row['role']}\n"
                f"Running {_humanize(elapsed)}. Stand down?"
            ),
            view=ClockOutView(row["at_id"], row["op_id"], row["role"], row["clock_in_time"]),
            ephemeral=True,
        )
