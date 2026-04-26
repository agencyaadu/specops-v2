from __future__ import annotations
import re
import discord
from discord import app_commands

from db import pool

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


class OnboardModal(discord.ui.Modal, title="Onboard — SpecOps"):
    pan = discord.ui.TextInput(label="PAN Number", placeholder="ABCDE1234F", min_length=10, max_length=10)
    name = discord.ui.TextInput(label="Full Name", placeholder="JANE DOE", max_length=200)
    wa_number = discord.ui.TextInput(label="WhatsApp Number", placeholder="+91 98xxx xxxxx", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        pan = self.pan.value.strip().upper()
        if not PAN_RE.match(pan):
            await interaction.response.send_message(
                "❌ PAN format invalid. Expected 5 letters + 4 digits + 1 letter (e.g. ABCDE1234F).",
                ephemeral=True,
            )
            return

        name = self.name.value.strip().upper()
        wa = re.sub(r"[^\d+]", "", self.wa_number.value.strip())
        discord_id = str(interaction.user.id)
        discord_username = interaction.user.name

        async with pool().acquire() as con:
            existing_pan = await con.fetchval("SELECT pan FROM people WHERE discord_id = $1", discord_id)
            if existing_pan and existing_pan != pan:
                await interaction.response.send_message(
                    f"⚠️ This Discord account is already linked to PAN {existing_pan}. Ask an admin to unlink first.",
                    ephemeral=True,
                )
                return

            pan_owner = await con.fetchval("SELECT discord_id FROM people WHERE pan = $1", pan)
            if pan_owner and pan_owner != discord_id:
                await interaction.response.send_message(
                    "⚠️ That PAN is already linked to a different Discord account.",
                    ephemeral=True,
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
                msg = f"✅ Profile updated. PAN `{pan}`."
            else:
                await con.execute(
                    """
                    INSERT INTO people (pan, discord_id, discord_username, name, wa_number)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    pan, discord_id, discord_username, name, wa,
                )
                msg = f"✅ Profile created. PAN `{pan}`. You can now `/clock-in`."

        await interaction.response.send_message(msg, ephemeral=True)


def register(tree: app_commands.CommandTree):
    @tree.command(name="onboard", description="Create your SpecOps profile (PAN + name + WhatsApp).")
    async def onboard(interaction: discord.Interaction):
        await interaction.response.send_modal(OnboardModal())
