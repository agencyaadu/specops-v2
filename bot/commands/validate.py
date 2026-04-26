from __future__ import annotations
from datetime import datetime, timezone
import discord
from discord import app_commands

from db import pool

# Discord roles allowed to validate.
# OPERATOR -> CAPTAIN; CAPTAIN -> CHIEF; CHIEF -> GENERAL.
VALIDATOR_FOR = {
    "OPERATOR": {"Captain", "Chief", "General"},
    "CAPTAIN":  {"Chief", "General"},
    "CHIEF":    {"General"},
}


def _user_role_names(member: discord.Member) -> set[str]:
    return {r.name for r in member.roles}


def build_pending_embed(*, at_id: int, pan: str, name: str, discord_user: discord.User,
                        op_id: str, role: str, clock_in_time: datetime, photo_url: str | None) -> discord.Embed:
    e = discord.Embed(
        title=f"Attendance · at_id {at_id}",
        description=f"**{name}** · `{pan}`",
        color=discord.Color.orange(),
        timestamp=clock_in_time,
    )
    e.add_field(name="Discord", value=discord_user.mention, inline=True)
    e.add_field(name="Op", value=f"`{op_id}`", inline=True)
    e.add_field(name="Role", value=role, inline=True)
    e.add_field(name="Status", value="🟠 PENDING", inline=False)
    if photo_url:
        e.set_image(url=photo_url)
    e.set_footer(text="Clock-in")
    return e


def build_resolved_embed(base: discord.Embed, decision: str, validator: discord.Member,
                         reason: str | None = None) -> discord.Embed:
    color = discord.Color.green() if decision == "CONFIRMED" else discord.Color.red()
    icon = "✅" if decision == "CONFIRMED" else "❌"
    new = discord.Embed(title=base.title, description=base.description, color=color, timestamp=base.timestamp)
    for f in base.fields:
        if f.name == "Status":
            value = f"{icon} {decision} by {validator.mention}"
            if decision == "REJECTED" and reason:
                value += f"\nReason: {reason}"
            new.add_field(name="Status", value=value, inline=False)
        else:
            new.add_field(name=f.name, value=f.value, inline=f.inline)
    if base.image and base.image.url:
        new.set_image(url=base.image.url)
    new.set_footer(text=base.footer.text or "Clock-in")
    return new


class RejectModal(discord.ui.Modal, title="Reject attendance"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=500, required=True)

    def __init__(self, at_id: int, parent_view: "ValidationView"):
        super().__init__()
        self.at_id = at_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await self.parent_view._resolve(interaction, "REJECTED", reason=str(self.reason))


class ValidationView(discord.ui.View):
    def __init__(self, at_id: int):
        super().__init__(timeout=None)
        self.at_id = at_id

        approve = discord.ui.Button(style=discord.ButtonStyle.success, label="Approve",
                                    custom_id=f"att:approve:{at_id}")
        reject  = discord.ui.Button(style=discord.ButtonStyle.danger,  label="Reject",
                                    custom_id=f"att:reject:{at_id}")
        approve.callback = self._on_approve
        reject.callback  = self._on_reject
        self.add_item(approve)
        self.add_item(reject)

    async def _check_permission(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        async with pool().acquire() as con:
            row = await con.fetchrow(
                "SELECT role, validation, pp_discord_id FROM attendance WHERE at_id = $1", self.at_id,
            )
        if not row:
            return False, "Attendance row not found."
        if row["validation"] != "PENDING":
            return False, f"Already {row['validation'].lower()}."
        if str(interaction.user.id) == row["pp_discord_id"]:
            return False, "You can't validate your own attendance."

        if not isinstance(interaction.user, discord.Member):
            return False, "This control only works inside a server."

        allowed = VALIDATOR_FOR.get(row["role"], set())
        if not (_user_role_names(interaction.user) & allowed):
            return False, f"Need one of these roles to validate a {row['role']}: {', '.join(sorted(allowed))}."
        return True, None

    async def _on_approve(self, interaction: discord.Interaction):
        ok, msg = await self._check_permission(interaction)
        if not ok:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
            return
        await self._resolve(interaction, "CONFIRMED")

    async def _on_reject(self, interaction: discord.Interaction):
        ok, msg = await self._check_permission(interaction)
        if not ok:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
            return
        await interaction.response.send_modal(RejectModal(self.at_id, self))

    async def _resolve(self, interaction: discord.Interaction, decision: str, reason: str | None = None):
        async with pool().acquire() as con:
            await con.execute(
                """
                UPDATE attendance
                   SET validation = $2,
                       validator_discord_id = $3,
                       validated_at = $4,
                       rejection_reason = CASE WHEN $2 = 'REJECTED' THEN $5 ELSE NULL END
                 WHERE at_id = $1
                """,
                self.at_id, decision, str(interaction.user.id),
                datetime.now(timezone.utc), reason,
            )

        # Edit the embed in place.
        try:
            base = interaction.message.embeds[0]
            new_embed = build_resolved_embed(base, decision, interaction.user, reason)
            for child in self.children:
                child.disabled = True
            if interaction.response.is_done():
                await interaction.message.edit(embed=new_embed, view=self)
                await interaction.followup.send(f"Recorded as **{decision}**.", ephemeral=True)
            else:
                await interaction.response.edit_message(embed=new_embed, view=self)
        except Exception:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Recorded as **{decision}**.", ephemeral=True)


def build_validation_view(at_id: int) -> discord.ui.View:
    return ValidationView(at_id)


def register(tree: app_commands.CommandTree):
    # No slash commands here — validation happens via buttons on attendance embeds.
    return
