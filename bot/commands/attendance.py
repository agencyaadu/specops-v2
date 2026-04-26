from __future__ import annotations
import os
from datetime import datetime, timezone
import discord
from discord import app_commands

from db import pool
from storage import upload_attachment
from commands.validate import build_validation_view, build_pending_embed

VALIDATION_CHANNEL = os.environ.get("ATTENDANCE_VALIDATION_CHANNEL", "attendance-validation")


def _humanize(delta_seconds: float) -> str:
    h, rem = divmod(int(delta_seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


async def _post_to_validation_channel(guild: discord.Guild, embed: discord.Embed, view: discord.ui.View):
    channel = discord.utils.get(guild.text_channels, name=VALIDATION_CHANNEL)
    if channel is None:
        return None
    try:
        msg = await channel.send(embed=embed, view=view)
        return msg
    except discord.Forbidden:
        return None


def register(tree: app_commands.CommandTree):
    @tree.command(name="clock-in", description="Clock in for a shift (attach a photo).")
    @app_commands.describe(
        op_id="Op identifier (e.g. factoryslug-am)",
        role="Your role on this op",
        photo="Selfie or work-area photo proof",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="OPERATOR", value="OPERATOR"),
        app_commands.Choice(name="CAPTAIN", value="CAPTAIN"),
        app_commands.Choice(name="CHIEF", value="CHIEF"),
    ])
    async def clock_in(
        interaction: discord.Interaction,
        op_id: str,
        role: app_commands.Choice[str],
        photo: discord.Attachment,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        discord_id = str(interaction.user.id)
        async with pool().acquire() as con:
            person = await con.fetchrow("SELECT pan, name FROM people WHERE discord_id = $1", discord_id)
        if not person:
            await interaction.followup.send("❌ Run `/onboard` first to create your profile.", ephemeral=True)
            return

        if not (photo.content_type or "").startswith("image/"):
            await interaction.followup.send("❌ Photo must be an image.", ephemeral=True)
            return

        try:
            photo_url = await upload_attachment(photo.url, photo.content_type)
        except Exception as e:
            await interaction.followup.send(f"❌ Photo upload failed: {e}", ephemeral=True)
            return

        async with pool().acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO attendance (pp_pan, pp_discord_id, op_id, role, photo_url, guild_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING at_id, clock_in_time
                """,
                person["pan"], discord_id, op_id.strip(), role.value, photo_url,
                str(interaction.guild_id) if interaction.guild_id else None,
            )

        if interaction.guild is not None:
            embed = build_pending_embed(
                at_id=row["at_id"], pan=person["pan"], name=person["name"],
                discord_user=interaction.user, op_id=op_id.strip(), role=role.value,
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

        await interaction.followup.send(
            f"✅ Clocked in. `at_id={row['at_id']}` · op `{op_id}` · role `{role.value}`. Posted for validation.",
            ephemeral=True,
        )

    @tree.command(name="clock-out", description="Clock out of your current open shift.")
    async def clock_out(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        discord_id = str(interaction.user.id)

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
            await interaction.followup.send("❌ No open clock-in found. Did you `/clock-in` first?", ephemeral=True)
            return

        elapsed = (row["clock_out_time"] - row["clock_in_time"]).total_seconds()
        await interaction.followup.send(
            f"✅ Clocked out. `at_id={row['at_id']}` · worked {_humanize(elapsed)}.",
            ephemeral=True,
        )
