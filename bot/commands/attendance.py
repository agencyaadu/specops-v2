from __future__ import annotations
import os
import re
from datetime import datetime, timezone
import discord
from discord import app_commands

from db import pool
from storage import upload_attachment
from commands.validate import build_validation_view, build_pending_embed

VALIDATION_CHANNEL = os.environ.get("ATTENDANCE_VALIDATION_CHANNEL", "attendance-validation")

VALID_ROLES = {"OPERATOR", "CAPTAIN", "CHIEF"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff")

# Selected validator must hold one of these Discord roles. (Case-sensitive.)
VALIDATOR_ROLE_FOR = {
    "OPERATOR": {"CAPTAIN", "CHIEF", "GENERAL", "FREDDY"},
    "CAPTAIN":  {"CHIEF", "GENERAL", "FREDDY"},
    "CHIEF":    {"GENERAL", "FREDDY"},
}


def _humanize(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


def _is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/"):
        return True
    fn = (att.filename or "").lower()
    return any(fn.endswith(ext) for ext in IMAGE_EXTS)


def _member_role_names(m: discord.Member) -> set[str]:
    return {r.name for r in m.roles}


# ─── Validation thread plumbing ──────────────────────────────────────────────

async def _create_validation_thread(
    guild: discord.Guild, at_id: int, op_id: str,
    operator: discord.abc.User, validator: discord.Member,
    embed: discord.Embed, view: discord.ui.View,
) -> tuple[discord.Thread | None, str | None]:
    """Create a private thread in #attendance-validation with operator + validator,
    post the embed + buttons. Returns (thread, error)."""
    channel = discord.utils.get(guild.text_channels, name=VALIDATION_CHANNEL)
    if channel is None:
        return None, f"no `#{VALIDATION_CHANNEL}` channel"

    thread_name = f"at_id-{at_id} · {op_id}"[:100]
    try:
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,  # 24h
        )
    except discord.Forbidden:
        return None, (
            f"FREDDY can't create threads in `#{VALIDATION_CHANNEL}` — give it "
            "**Create Private Threads** + **Send Messages in Threads** + **Manage Threads**"
        )
    except Exception as e:
        return None, f"thread create failed: `{e}`"

    # Add the validator (and operator if Member) explicitly.
    try:
        await thread.add_user(validator)
        if isinstance(operator, discord.Member):
            await thread.add_user(operator)
    except discord.Forbidden:
        pass  # at minimum we can post; viewers limited to those mentioned

    try:
        await thread.send(embed=embed, view=view)
    except Exception as e:
        return thread, f"posted thread but couldn't send embed: `{e}`"

    return thread, None


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

    async def operation_autocomplete(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        async with pool().acquire() as con:
            rows = await con.fetch(
                """
                SELECT operation_id
                  FROM operations
                 WHERE state = 'ACTIVE' AND operation_id ILIKE $1
                 ORDER BY operation_id LIMIT 25
                """,
                f"%{current}%",
            )
        return [app_commands.Choice(name=r["operation_id"], value=r["operation_id"]) for r in rows]

    async def validator_autocomplete(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete validator from op_assignments at the right rank tier.
        Falls back to FREDDY/GENERAL Discord role members for CHIEF clock-ins."""
        # Pull the op + role the user has typed/chosen so far.
        op_id = (interaction.namespace.operation or "").strip() if hasattr(interaction.namespace, "operation") else ""
        role_choice = getattr(interaction.namespace, "role", None)
        role_value = role_choice.value if role_choice else None

        VALIDATOR_RANKS_FOR = {
            "OPERATOR": ["CAPTAIN", "CHIEF"],
            "CAPTAIN":  ["CHIEF"],
            "CHIEF":    [],  # see fallback below
        }

        choices: list[app_commands.Choice[str]] = []

        if op_id and role_value and role_value in VALIDATOR_RANKS_FOR:
            ranks = VALIDATOR_RANKS_FOR[role_value]
            if ranks:
                async with pool().acquire() as con:
                    rows = await con.fetch(
                        """
                        SELECT a.person_discord_id, a.role, p.name
                          FROM op_assignments a
                          JOIN people p ON p.pan = a.person_pan
                         WHERE a.operation_id = $1
                           AND a.state = 'ACTIVE'
                           AND a.role = ANY($2::text[])
                         ORDER BY array_position($2::text[], a.role), p.name
                         LIMIT 25
                        """,
                        op_id, ranks,
                    )
                for r in rows:
                    label = f"{r['name']} · {r['role']}"
                    if current.lower() in label.lower():
                        choices.append(app_commands.Choice(name=label[:100], value=r["person_discord_id"]))

        # CHIEF role -> validator is a GENERAL/FREDDY (server role, not assignment).
        if role_value == "CHIEF" and interaction.guild is not None:
            allowed_roles = {"FREDDY", "GENERAL"}
            for member in interaction.guild.members:
                if any(r.name in allowed_roles for r in member.roles):
                    label = f"{member.display_name} · GENERAL"
                    if current.lower() in label.lower():
                        choices.append(app_commands.Choice(name=label[:100], value=str(member.id)))
                if len(choices) >= 25:
                    break

        return choices[:25]

    @tree.command(name="clock-in", description="Start your tour: pick op + role + photo + validator.")
    @app_commands.describe(
        operation="Op you're posted to (autocomplete from active ops)",
        role="Your role on this op",
        photo="Sitrep photo proof",
        validator="Who'll validate (autocomplete shows the assigned validators for this op)",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="OPERATOR", value="OPERATOR"),
        app_commands.Choice(name="CAPTAIN",  value="CAPTAIN"),
        app_commands.Choice(name="CHIEF",    value="CHIEF"),
    ])
    @app_commands.autocomplete(operation=operation_autocomplete, validator=validator_autocomplete)
    async def clock_in(
        interaction: discord.Interaction,
        operation: str,
        role: app_commands.Choice[str],
        photo: discord.Attachment,
        validator: str,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with pool().acquire() as con:
            person = await con.fetchrow(
                "SELECT pan, name FROM people WHERE discord_id = $1",
                str(interaction.user.id),
            )
        if not person:
            await interaction.followup.send("Not on the roster yet. Run `/onboard` first.", ephemeral=True)
            return

        async with pool().acquire() as con:
            op = await con.fetchrow(
                "SELECT operation_id, state FROM operations WHERE operation_id = $1",
                operation.strip(),
            )
        if not op:
            await interaction.followup.send(
                f"❌ Op `{operation}` not found. Use the autocomplete to pick from active ops.",
                ephemeral=True,
            )
            return
        if op["state"] != "ACTIVE":
            await interaction.followup.send(f"❌ Op `{operation}` is inactive.", ephemeral=True)
            return

        # `validator` arrives as a discord_id string from autocomplete.
        try:
            validator_id = int(validator.strip())
        except (ValueError, AttributeError):
            await interaction.followup.send(
                "❌ Pick a validator from the autocomplete list (don't type a name freehand).",
                ephemeral=True,
            )
            return

        validator_member = interaction.guild.get_member(validator_id) if interaction.guild else None
        if validator_member is None:
            await interaction.followup.send(
                "❌ Couldn't resolve that validator in this server. Pick again from the autocomplete.",
                ephemeral=True,
            )
            return
        if validator_member.id == interaction.user.id:
            await interaction.followup.send("❌ Can't pick yourself as validator.", ephemeral=True)
            return

        # Verify validator is actually assigned at the right rank for this op
        # (or has FREDDY/GENERAL Discord role for CHIEF clock-ins).
        VALIDATOR_RANKS = {"OPERATOR": ("CAPTAIN", "CHIEF"), "CAPTAIN": ("CHIEF",)}
        if role.value in VALIDATOR_RANKS:
            async with pool().acquire() as con:
                ok = await con.fetchval(
                    """
                    SELECT 1 FROM op_assignments
                     WHERE operation_id = $1 AND person_discord_id = $2
                       AND role = ANY($3::text[]) AND state = 'ACTIVE'
                    """,
                    operation.strip(), str(validator_member.id),
                    list(VALIDATOR_RANKS[role.value]),
                )
            if not ok:
                await interaction.followup.send(
                    f"❌ {validator_member.mention} isn't assigned as a "
                    f"{'/'.join(VALIDATOR_RANKS[role.value])} on this op. "
                    "Ask command to assign them, or pick someone who is.",
                    ephemeral=True,
                )
                return
        else:  # CHIEF — validator must hold FREDDY or GENERAL Discord role
            allowed = {"FREDDY", "GENERAL"}
            if not (_member_role_names(validator_member) & allowed):
                await interaction.followup.send(
                    f"❌ {validator_member.mention} can't validate a CHIEF. "
                    f"Need one of: {', '.join(sorted(allowed))}.",
                    ephemeral=True,
                )
                return

        validator = validator_member  # use the Member object for the rest of the handler

        if not _is_image_attachment(photo):
            await interaction.followup.send("❌ That doesn't look like an image attachment.", ephemeral=True)
            return

        # One clock-in per (operator, op, IST date).
        async with pool().acquire() as con:
            dup = await con.fetchrow(
                """
                SELECT at_id, validation
                  FROM attendance
                 WHERE pp_discord_id = $1
                   AND op_id = $2
                   AND (clock_in_time AT TIME ZONE 'Asia/Kolkata')::date
                       = (now() AT TIME ZONE 'Asia/Kolkata')::date
                 ORDER BY at_id DESC LIMIT 1
                """,
                str(interaction.user.id), op["operation_id"],
            )
        if dup:
            await interaction.followup.send(
                f"❌ You already clocked in to `{op['operation_id']}` today "
                f"(`at_id={dup['at_id']}`, status `{dup['validation']}`). "
                "One per op per day.",
                ephemeral=True,
            )
            return

        try:
            photo_url = await upload_attachment(photo.url, photo.content_type or "image/jpeg")
        except Exception as e:
            await interaction.followup.send(f"❌ Photo upload failed: `{e}`.", ephemeral=True)
            return

        async with pool().acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO attendance
                  (pp_pan, pp_discord_id, op_id, role, photo_url,
                   selected_validator_discord_id, guild_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING at_id, clock_in_time
                """,
                person["pan"], str(interaction.user.id), op["operation_id"], role.value, photo_url,
                str(validator.id), str(interaction.guild_id),
            )

        embed = build_pending_embed(
            at_id=row["at_id"], pan=person["pan"], name=person["name"],
            discord_user=interaction.user, op_id=op["operation_id"], role=role.value,
            clock_in_time=row["clock_in_time"], photo_url=photo_url,
            validator=validator,
        )
        view = build_validation_view(row["at_id"])

        thread, err = await _create_validation_thread(
            interaction.guild, row["at_id"], op["operation_id"],
            interaction.user, validator, embed, view,
        )
        if thread is not None:
            async with pool().acquire() as con:
                await con.execute(
                    "UPDATE attendance SET thread_id = $2 WHERE at_id = $1",
                    row["at_id"], str(thread.id),
                )

        confirm = (
            f"✅ On the clock. `at_id={row['at_id']}` · `{op['operation_id']}` · {role.value}.\n"
            f"Validator: {validator.mention}\n"
        )
        if err:
            confirm += f"⚠️ {err}."
        elif thread is not None:
            confirm += f"Validation thread: {thread.mention}"
        await interaction.followup.send(confirm, ephemeral=True)

    @tree.command(name="clock-out", description="Close your current tour.")
    async def clock_out(interaction: discord.Interaction):
        async with pool().acquire() as con:
            person = await con.fetchrow(
                "SELECT pan FROM people WHERE discord_id = $1", str(interaction.user.id),
            )
        if not person:
            await interaction.response.send_message(
                "Not on the roster yet. Run `/onboard` first.", ephemeral=True,
            )
            return

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
