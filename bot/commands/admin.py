from __future__ import annotations
import re
import discord
from discord import app_commands

from db import pool
from short_id import build_op_id, city_code, split_factory_unit

# All role names UPPERCASE — must match Discord role names exactly.
GENERAL_PLUS = {"FREDDY", "GENERAL"}     # can do anything

VALID_RANKS = ("OPERATOR", "CAPTAIN", "CHIEF")


def _has_role(member: discord.Member | discord.User, names: set[str]) -> bool:
    if isinstance(member, discord.Member):
        return any(r.name in names for r in member.roles)
    return False


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")


async def _person_for_user(user: discord.abc.User) -> dict | None:
    async with pool().acquire() as con:
        return await con.fetchrow(
            "SELECT pan, name FROM people WHERE discord_id = $1", str(user.id),
        )


async def _is_assigned(operation_id: str, discord_id: str, role: str) -> bool:
    async with pool().acquire() as con:
        return bool(await con.fetchval(
            """
            SELECT 1 FROM op_assignments
             WHERE operation_id = $1 AND person_discord_id = $2
               AND role = $3 AND state = 'ACTIVE'
            """,
            operation_id, discord_id, role,
        ))


async def _operation_autocomplete(interaction: discord.Interaction, current: str
                                  ) -> list[app_commands.Choice[str]]:
    async with pool().acquire() as con:
        rows = await con.fetch(
            """
            SELECT operation_id FROM operations
             WHERE state = 'ACTIVE' AND operation_id ILIKE $1
             ORDER BY operation_id LIMIT 25
            """,
            f"%{current}%",
        )
    return [app_commands.Choice(name=r["operation_id"], value=r["operation_id"]) for r in rows]


async def _assign(interaction: discord.Interaction, operation: str,
                  user: discord.Member, role: str):
    """Common implementation for /assign-chief|captain|operator."""
    person = await _person_for_user(user)
    if not person:
        await interaction.followup.send(
            f"❌ {user.mention} isn't on the roster yet — they need `/onboard` first.",
            ephemeral=True,
        )
        return

    async with pool().acquire() as con:
        op = await con.fetchrow("SELECT operation_id FROM operations WHERE operation_id = $1",
                                operation.strip())
    if not op:
        await interaction.followup.send(
            f"❌ Op `{operation}` not found.", ephemeral=True,
        )
        return

    async with pool().acquire() as con:
        await con.execute(
            """
            INSERT INTO op_assignments
                (operation_id, person_pan, person_discord_id, role, assigned_by_discord_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (operation_id, person_pan, role) DO UPDATE
                SET state = 'ACTIVE',
                    assigned_by_discord_id = EXCLUDED.assigned_by_discord_id,
                    updated_at = now()
            """,
            op["operation_id"], person["pan"], str(user.id), role, str(interaction.user.id),
        )

    await interaction.followup.send(
        f"✅ Assigned {user.mention} as **{role}** on `{op['operation_id']}` "
        f"({person['name']} · `{person['pan']}`).",
        ephemeral=True,
    )


def register(tree: app_commands.CommandTree, client: discord.Client):

    # ─── Op creation ────────────────────────────────────────────────────────

    @tree.command(name="op-create", description="Create an operation (FREDDY/GENERAL only).")
    @app_commands.describe(
        factory_id="Factory slug (e.g. mumbai). If not yet registered, use /factory-add first.",
        shift="Shift label (Shift A / Shift B / Night / Morning / 10am to 6pm ...)",
        unit="Unit code (default U1). Use U2/U3 if a factory has multiple units.",
    )
    async def op_create(interaction: discord.Interaction, factory_id: str, shift: str, unit: str = "U1"):
        if not _has_role(interaction.user, GENERAL_PLUS):
            await interaction.response.send_message(
                "Only FREDDY/GENERAL can create ops.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        factory_slug = _slugify(factory_id)
        if not factory_slug or not shift.strip():
            await interaction.followup.send("Invalid factory_id or shift.", ephemeral=True)
            return

        unit_code = unit.strip().upper() or "U1"

        async with pool().acquire() as con:
            factory = await con.fetchrow(
                "SELECT name, city FROM factories WHERE factory_id = $1", factory_slug,
            )
            if not factory:
                await interaction.followup.send(
                    f"⚠️ No factory `{factory_slug}`. Register it first with `/factory-add`.",
                    ephemeral=True,
                )
                return
            if not factory["city"]:
                await interaction.followup.send(
                    f"⚠️ Factory `{factory_slug}` has no city set. "
                    f"Re-register with `/factory-add`.",
                    ephemeral=True,
                )
                return

            taken = {r["operation_id"] for r in await con.fetch("SELECT operation_id FROM operations")}
            fac_base, _ = split_factory_unit(factory["name"])
            op_id = build_op_id(factory["city"], fac_base, unit_code, shift, taken)

            await con.execute(
                """
                INSERT INTO operations (operation_id, factory_id, shift, unit, city, created_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                op_id, factory_slug, shift.strip().upper(), unit_code, factory["city"],
                str(interaction.user.id),
            )

        await interaction.followup.send(
            f"✅ Op `{op_id}` registered. Now staff it:\n"
            f"  `/assign-chief operation:{op_id} user:@chief`\n"
            f"  `/assign-captain operation:{op_id} user:@captain`\n"
            f"  `/assign-operator operation:{op_id} user:@operator`",
            ephemeral=True,
        )

    # ─── Factory create ─────────────────────────────────────────────────────

    @tree.command(name="factory-add", description="Register a factory (FREDDY/GENERAL only).")
    @app_commands.describe(
        factory_id="Slug (lowercase, hyphens — e.g. mumbai-x).",
        name="Display name (e.g. Mumbai Plant)",
        city="City name — used as 2-char prefix in op IDs (e.g. Mumbai → MU)",
    )
    async def factory_add(interaction: discord.Interaction, factory_id: str, name: str, city: str):
        if not _has_role(interaction.user, GENERAL_PLUS):
            await interaction.response.send_message(
                "Only FREDDY/GENERAL can register factories.", ephemeral=True,
            )
            return

        slug = _slugify(factory_id)
        name_clean = name.strip().upper()
        city_clean = re.sub(r"[^A-Za-z0-9 ]+", "", city.strip()).upper()
        if not slug or not name_clean or not city_clean:
            await interaction.response.send_message("Invalid input.", ephemeral=True)
            return

        async with pool().acquire() as con:
            existing = await con.fetchval("SELECT name FROM factories WHERE factory_id = $1", slug)
            if existing:
                await interaction.response.send_message(
                    f"⚠️ Factory `{slug}` already exists ({existing}).", ephemeral=True,
                )
                return
            await con.execute(
                "INSERT INTO factories (factory_id, name, city, created_by) VALUES ($1, $2, $3, $4)",
                slug, name_clean, city_clean, str(interaction.user.id),
            )

        await interaction.response.send_message(
            f"✅ Factory registered. `{slug}` · {name_clean} · city `{city_clean}` (code `{city_code(city_clean)}`)",
            ephemeral=True,
        )

    # ─── Op listing ─────────────────────────────────────────────────────────

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

    @tree.command(name="op-roster", description="Show personnel assigned to an operation.")
    @app_commands.describe(operation="Op to inspect")
    @app_commands.autocomplete(operation=_operation_autocomplete)
    async def op_roster(interaction: discord.Interaction, operation: str):
        async with pool().acquire() as con:
            rows = await con.fetch(
                """
                SELECT a.role, a.person_discord_id, p.name, p.pan
                  FROM op_assignments a
                  JOIN people p ON p.pan = a.person_pan
                 WHERE a.operation_id = $1 AND a.state = 'ACTIVE'
                 ORDER BY CASE a.role
                            WHEN 'CHIEF' THEN 1
                            WHEN 'CAPTAIN' THEN 2
                            WHEN 'OPERATOR' THEN 3
                          END, p.name
                """,
                operation.strip(),
            )
        if not rows:
            await interaction.response.send_message(
                f"No personnel assigned to `{operation}`.", ephemeral=True,
            )
            return

        out = [f"**Roster for `{operation}`:**"]
        for role in ("CHIEF", "CAPTAIN", "OPERATOR"):
            people = [r for r in rows if r["role"] == role]
            if not people:
                continue
            out.append(f"\n**{role}**")
            for r in people:
                out.append(f"  · <@{r['person_discord_id']}> · {r['name']} · `{r['pan']}`")
        await interaction.response.send_message("\n".join(out), ephemeral=True)

    # ─── Assign commands ────────────────────────────────────────────────────

    @tree.command(name="assign-chief", description="Assign a CHIEF to an operation (FREDDY/GENERAL only).")
    @app_commands.describe(operation="Op to assign on", user="Who")
    @app_commands.autocomplete(operation=_operation_autocomplete)
    async def assign_chief(interaction: discord.Interaction, operation: str, user: discord.Member):
        if not _has_role(interaction.user, GENERAL_PLUS):
            await interaction.response.send_message(
                "Only FREDDY/GENERAL can assign chiefs.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _assign(interaction, operation, user, "CHIEF")

    @tree.command(name="assign-captain", description="Assign a CAPTAIN to an op (assigned CHIEF or above).")
    @app_commands.describe(operation="Op to assign on", user="Who")
    @app_commands.autocomplete(operation=_operation_autocomplete)
    async def assign_captain(interaction: discord.Interaction, operation: str, user: discord.Member):
        if not _has_role(interaction.user, GENERAL_PLUS):
            if not await _is_assigned(operation.strip(), str(interaction.user.id), "CHIEF"):
                await interaction.response.send_message(
                    f"You must be the assigned CHIEF on `{operation}` (or FREDDY/GENERAL) to assign captains here.",
                    ephemeral=True,
                )
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _assign(interaction, operation, user, "CAPTAIN")

    @tree.command(name="assign-operator", description="Assign an OPERATOR to an op (assigned CAPTAIN or above).")
    @app_commands.describe(operation="Op to assign on", user="Who")
    @app_commands.autocomplete(operation=_operation_autocomplete)
    async def assign_operator(interaction: discord.Interaction, operation: str, user: discord.Member):
        if not _has_role(interaction.user, GENERAL_PLUS):
            chief = await _is_assigned(operation.strip(), str(interaction.user.id), "CHIEF")
            captain = await _is_assigned(operation.strip(), str(interaction.user.id), "CAPTAIN")
            if not (chief or captain):
                await interaction.response.send_message(
                    f"You must be the assigned CAPTAIN or CHIEF on `{operation}` (or FREDDY/GENERAL) "
                    "to assign operators here.",
                    ephemeral=True,
                )
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _assign(interaction, operation, user, "OPERATOR")

    # ─── Unassign ───────────────────────────────────────────────────────────

    @tree.command(name="unassign", description="Remove someone from an op assignment (rank-gated like assign).")
    @app_commands.describe(operation="Op", user="Who", role="Which assignment role to remove")
    @app_commands.autocomplete(operation=_operation_autocomplete)
    @app_commands.choices(role=[
        app_commands.Choice(name="OPERATOR", value="OPERATOR"),
        app_commands.Choice(name="CAPTAIN",  value="CAPTAIN"),
        app_commands.Choice(name="CHIEF",    value="CHIEF"),
    ])
    async def unassign(interaction: discord.Interaction, operation: str,
                       user: discord.Member, role: app_commands.Choice[str]):
        # Same rank gating as the corresponding assign-X.
        if not _has_role(interaction.user, GENERAL_PLUS):
            if role.value == "CHIEF":
                await interaction.response.send_message(
                    "Only FREDDY/GENERAL can remove CHIEFs.", ephemeral=True,
                )
                return
            if role.value == "CAPTAIN":
                if not await _is_assigned(operation.strip(), str(interaction.user.id), "CHIEF"):
                    await interaction.response.send_message(
                        "Need to be assigned CHIEF (or FREDDY/GENERAL).", ephemeral=True,
                    )
                    return
            if role.value == "OPERATOR":
                chief = await _is_assigned(operation.strip(), str(interaction.user.id), "CHIEF")
                captain = await _is_assigned(operation.strip(), str(interaction.user.id), "CAPTAIN")
                if not (chief or captain):
                    await interaction.response.send_message(
                        "Need to be assigned CAPTAIN or CHIEF (or FREDDY/GENERAL).", ephemeral=True,
                    )
                    return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with pool().acquire() as con:
            r = await con.execute(
                """
                UPDATE op_assignments
                   SET state = 'INACTIVE', updated_at = now()
                 WHERE operation_id = $1 AND person_discord_id = $2 AND role = $3
                   AND state = 'ACTIVE'
                """,
                operation.strip(), str(user.id), role.value,
            )
        await interaction.followup.send(
            f"✅ Removed {user.mention} from **{role.value}** on `{operation}`.",
            ephemeral=True,
        )
