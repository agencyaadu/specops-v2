# SpecOps v2

Append-only rebuild of [SpecOps](https://github.com/agencyaadu/specops).

- **v1** (production): https://spec-ops.best — stays untouched.
- **v2** (this repo): clean rebuild on a 5-table append-only schema.

## Quick links

- **Schema**: [`docs/schema.dbml`](docs/schema.dbml) — paste into
  [dbdiagram.io](https://dbdiagram.io/d) to render.
- **Context**: [`CLAUDE.md`](CLAUDE.md) — what v2 is, why, how.
- **v1 summary**: [`docs/v1_project_summary.pdf`](docs/v1_project_summary.pdf).

## Build plan (batched)

1. **Batch 1** — Onboarding + Attendance (current).
2. Batch 2 — TBD.
3. Batch 3 — TBD.

Each batch: wireframe → db schema slice → ETL pipeline → handlers → UI.
