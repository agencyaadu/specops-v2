from __future__ import annotations
import re
import discord
from discord import app_commands

from db import pool

# Roles allowed to run admin commands.
ADMIN_ROLES = {"FREDDY", "General"}


def _is_admin(member: discord.Member | discord.User) -> bool:
    if isinstance(member, discord.Member):
        return any(r.name in ADMIN_ROLES for r in member.roles)
    return False


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def register(tree: app_commands.CommandTree, client: discord.Client):

    @tree.command(name="factory-add", description="Register a factory site.")
    @app_commands.describe(
        factory_id="Slug (e.g. mumbai). Lowercase, hyphens only.",
        name="Display name (e.g. Mumbai Plant)",
    )
    async def factory_add(interaction: discord.Interaction, factory_id: str, name: str):
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                "Only FREDDY/General can run this.", ephemeral=True,
            )
            return

        slug = _slugify(factory_id)
        if not slug:
            await interaction.response.send_message("Invalid factory_id.", ephemeral=True)
            return
        name_clean = name.strip().upper()
        if not name_clean:
            await interaction.response.send_message("Name required.", ephemeral=True)
            return

        async with pool().acquire() as con:
            existing = await con.fetchval("SELECT name FROM factories WHERE factory_id = $1", slug)
            if existing:
                await interaction.response.send_message(
                    f"⚠️ Factory `{slug}` already exists ({existing}).", ephemeral=True,
                )
                return
            await con.execute(
                """
                INSERT INTO factories (factory_id, name, created_by)
                VALUES ($1, $2, $3)
                """,
                slug, name_clean, str(interaction.user.id),
            )

        await interaction.response.send_message(
            f"✅ Factory registered. `{slug}` · {name_clean}", ephemeral=True,
        )

    @tree.command(name="op-add", description="Register an operation (factory + shift + password).")
    @app_commands.describe(
        factory_id="Existing factory slug",
        shift="Shift code (e.g. AM, PM, A, B, C)",
        password="Op password — operators must enter this on /clock-in",
    )
    async def op_add(interaction: discord.Interaction, factory_id: str, shift: str, password: str):
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                "Only FREDDY/General can run this.", ephemeral=True,
            )
            return

        factory_slug = _slugify(factory_id)
        shift_slug = _slugify(shift)
        if not factory_slug or not shift_slug:
            await interaction.response.send_message("Invalid factory_id or shift.", ephemeral=True)
            return
        password = password.strip()
        if not password:
            await interaction.response.send_message("Password required.", ephemeral=True)
            return

        op_id = f"{factory_slug}-{shift_slug}"

        async with pool().acquire() as con:
            factory = await con.fetchrow("SELECT name FROM factories WHERE factory_id = $1", factory_slug)
            if not factory:
                await interaction.response.send_message(
                    f"⚠️ No factory `{factory_slug}`. Register it first with `/factory-add`.",
                    ephemeral=True,
                )
                return

            existing = await con.fetchval("SELECT operation_id FROM operations WHERE operation_id = $1", op_id)
            if existing:
                await con.execute(
                    """
                    UPDATE operations
                       SET op_password = $2, state = 'ACTIVE', updated_at = now()
                     WHERE operation_id = $1
                    """,
                    op_id, password,
                )
                await interaction.response.send_message(
                    f"♻️ Op `{op_id}` already existed — password rotated.",
                    ephemeral=True,
                )
                return

            await con.execute(
                """
                INSERT INTO operations (operation_id, factory_id, shift, op_password, created_by)
                VALUES ($1, $2, $3, $4, $5)
                """,
                op_id, factory_slug, shift_slug.upper(), password, str(interaction.user.id),
            )

        await interaction.response.send_message(
            f"✅ Op registered. `{op_id}` · password set.\nOperators can now `/clock-in` to it.",
            ephemeral=True,
        )

    @tree.command(name="op-list", description="List active operations.")
    async def op_list(interaction: discord.Interaction):
        async with pool().acquire() as con:
            rows = await con.fetch(
                """
                SELECT o.operation_id, f.name AS factory_name, o.shift, o.state
                  FROM operations o
                  JOIN factories f ON f.factory_id = o.factory_id
                 WHERE o.state = 'ACTIVE'
                 ORDER BY o.factory_id, o.shift
                """
            )
        if not rows:
            await interaction.response.send_message("No active ops yet.", ephemeral=True)
            return

        lines = [f"`{r['operation_id']:<24}` {r['factory_name']} · {r['shift']}" for r in rows]
        await interaction.response.send_message(
            "**Active operations:**\n" + "\n".join(lines), ephemeral=True,
        )
