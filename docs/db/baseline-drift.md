# Baseline drift

There are three schemas of the app tables:
- **A**, production's, from its dump;
- **B**, what the current models build (`init_db` on an empty database);
- **C**, what `0001_baseline` builds.

C must equal A exactly. Where B differs, the models are wrong about
production. Each difference is listed here with its decision.

**Status: provisional.** A is currently the *history replay*
(`scripts/replay_schema_history.py`): every schema commit through R6
replayed, as production's deploys would have done. **C equals that replay
exactly** (`tests/test_migrations_pg.py`, and `docs/db/baseline-report.json`
with `source: proxy`). The production dump (plan U1) replaces the replay
before the stamp; until then, every row below is unverified.

## C vs A

| Difference | Decision | Status |
|---|---|---|
| None against the history replay. | — | Re-check against the dump. |

## Models (B) vs production (A), accepted by the stamp as `allowed_model_drift`

| Difference | Why | Decision |
|---|---|---|
| `schedules.status` is nullable with default `'pending'`; the model says `NOT NULL` with no default. | The column was added by `ALTER TABLE … ADD COLUMN … DEFAULT 'pending'` to an existing table. | The baseline keeps production's form; code already treats NULL as claimable (R5). Align the model in a later expand-only revision if wanted. |
| `ix_schedules_status` exists in the model, not in production. | `create_all` never adds indexes to existing tables. | Not created. The scheduler's claim uses `next_run` / `enabled`; add it in a revision only if a query needs it. |
| `users.session_version` has `DEFAULT 0` in production; the model has no server default. | Added by `ALTER … NOT NULL DEFAULT 0`. | Keep. Alembic's comparison ignores server defaults, so this is not in the report's list. |

## Candidates only the real dump can settle

| Candidate | Why it is uncertain | If production differs |
|---|---|---|
| `timestamp with time zone` vs `without` on columns created between April and October 2026. | `sqlmodel` was unpinned until October 2026, and older versions mapped `datetime` to `timestamp without time zone`. | Change the column types in `0001_baseline` to production's; add a row here; decide separately whether to migrate them. |
| Indexes, uniques or foreign keys added to the models after their table first deployed. | `create_all` skips existing tables. The replay assumes every commit was deployed. | Remove them from `0001_baseline`. |
| Index and constraint names. | Depend on the SQLAlchemy version at creation. | Use production's names. |
| Anything outside the app tables (extensions, functions, other schemas). | Not modelled. | The fingerprint check covers them; investigate before stamping. |

## Comparison caveat

CHECK constraints are compared without casts and parentheses
(`migrations/fingerprint.normalize_check`). PostgreSQL does not round-trip
them exactly through `pg_dump`: the feedback-channel `IN (…)` list came back
from a restore with its casts elsewhere. Exact comparison would make a
restored dump never match the live database.
