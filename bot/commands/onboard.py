from __future__ import annotations
import os
import re
import discord
from discord import app_commands

from db import pool
from web import google_signin_url, is_configured as oauth_is_configured

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
WA_RE = re.compile(r"^\+?\d{7,15}$")

ONBOARDING_CHANNEL = os.environ.get("ONBOARDING_CHANNEL", "onboarding")


class OnboardModal(discord.ui.Modal, title="SPEC-OPS · Brief"):
    full_name = discord.ui.TextInput(
        label="Full Name",
        placeholder="JANE DOE",
        max_length=200,
        required=True,
    )
    pan = discord.ui.TextInput(
        label="PAN Number",
        placeholder="ABCDE1234F",
        min_length=10,
        max_length=10,
        required=True,
    )
    wa_number = discord.ui.TextInput(
        label="WhatsApp Number (with country code)",
        placeholder="+91 98765 43210",
        max_length=20,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        name_raw = self.full_name.value.strip()
        pan_raw = self.pan.value.strip().upper().replace(" ", "")
        wa_raw = re.sub(r"[^\d+]", "", self.wa_number.value.strip())

        if not name_raw or len(name_raw) > 200:
            await interaction.response.send_message(
                "❌ Name should be 1–200 characters.",
                ephemeral=True,
            )
            return
        if not PAN_RE.match(pan_raw):
            await interaction.response.send_message(
                "❌ PAN format off. Expected 5 letters + 4 digits + 1 letter (e.g. `ABCDE1234F`).",
                ephemeral=True,
            )
            return
        if not WA_RE.match(wa_raw):
            await interaction.response.send_message(
                "❌ WhatsApp number invalid. 7–15 digits, optional leading `+`.",
                ephemeral=True,
            )
            return

        name = name_raw.upper()
        pan = pan_raw
        wa = wa_raw

        discord_id = str(interaction.user.id)
        discord_username = interaction.user.name

        async with pool().acquire() as con:
            existing_pan = await con.fetchval(
                "SELECT pan FROM people WHERE discord_id = $1", discord_id,
            )
            if existing_pan and existing_pan != pan:
                await interaction.response.send_message(
                    f"⚠️ This Discord is already on the roster as `{existing_pan}`. "
                    "Talk to command if this is a new PAN.",
                    ephemeral=True,
                )
                return

            pan_owner = await con.fetchval(
                "SELECT discord_id FROM people WHERE pan = $1", pan,
            )
            if pan_owner and pan_owner != discord_id:
                await interaction.response.send_message(
                    "⚠️ That PAN is already on the roster under another account. Talk to command.",
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
                msg = f"✅ Roster updated. `{pan}` · {name} · {wa}"
            else:
                await con.execute(
                    """
                    INSERT INTO people (pan, discord_id, discord_username, name, wa_number)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    pan, discord_id, discord_username, name, wa,
                )
                msg = (
                    f"✅ You're on the SPEC-OPS roster.\n"
                    f"`{pan}` · {name} · {wa}\n\n"
                    f"Now tap **Sign in with Google** so you can log in to the web app later."
                )
            # Show the Google-link button right after submit if not already linked.
            already_linked = await con.fetchval("SELECT google_id FROM people WHERE pan = $1", pan)
        view = None if already_linked else GoogleLinkView(pan, discord_id)
        await interaction.response.send_message(msg, view=view, ephemeral=True)


class OnboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)  # 10 min

    @discord.ui.button(label="Open brief", style=discord.ButtonStyle.secondary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OnboardModal())


class GoogleLinkView(discord.ui.View):
    """Posted in the ephemeral confirmation after a successful onboard.
    Single Link-style button — opens browser straight to Google sign-in for
    this specific (PAN, discord_id) pair."""
    def __init__(self, pan: str, discord_id: str):
        super().__init__(timeout=900)
        if oauth_is_configured():
            url = google_signin_url(pan, discord_id)
            self.add_item(discord.ui.Button(
                label="Sign in with Google",
                style=discord.ButtonStyle.link,
                url=url,
            ))
        else:
            # OAuth env not set yet — disable the button with a hint.
            disabled = discord.ui.Button(
                label="Sign in with Google (not configured yet)",
                style=discord.ButtonStyle.secondary,
                disabled=True,
            )
            self.add_item(disabled)


def register(tree: app_commands.CommandTree, client: discord.Client):
    @tree.command(name="onboard", description="Get on the SPEC-OPS roster.")
    async def onboard(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Run this in a server, not a DM.", ephemeral=True,
            )
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
            f"Welcome aboard, {interaction.user.mention}. SPEC-OPS — sharpest ops in India.\n"
            f"Tap **Open brief** to file your details (name, PAN, WhatsApp). Takes 30 seconds.",
            view=OnboardView(),
        )
