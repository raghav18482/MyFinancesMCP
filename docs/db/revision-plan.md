# Database revisions

From R7, Alembic owns the production schema. The app runs no DDL in
production. Its startup only checks that the database is at a revision this
release lists in `db/schema_version.py`, and refuses to start otherwise.

## Revisions

| Revision | Release | What it does | Expand-only |
|---|---|---|---|
| `0001_baseline` | R7 | The app's tables as production has them before Alembic. Production is *stamped* at it, never upgraded through it. | yes |
| `0001a_db_identity` | R7 | `db_identity`: one guarded row labelling the database `production`, `rehearsal`, `development` or `test`. | yes |
| `0002_delivery_ledger` | R10 | `briefing_deliveries`: one row per briefing send, its states and lease (`services/schedular/ledger.py`) | yes |
| `0003_broker_connections` | R11 | `broker_connections` (not used by the app yet), and `users.firebase_uid`, `auth_phone_e164`, `email`, `last_login_at` | yes |
| `0004_users_angel_nullable` | next phase (reserved) | Angel columns on `users` become nullable | no |

A revision can appear in an earlier release's `COMPATIBLE_REVISIONS` only if
it is expand-only and is reserved here before that release ships. Such
revisions are also listed in that release's `RESERVED_REVISIONS`. R7 to R9
list `0002_delivery_ledger` this way, so a rollback from R10 to any of them
still starts.

## Rules (enforced by `migrations/env.py` and `scripts/check_migrations.py`)

- The database decides, not the URL. The rules for each database state are in
  `migrations/guard.py`.
- **Production:**
  - It needs `ALEMBIC_PRODUCTION_RELEASE` equal to the release's `RELEASE_ID`,
    and the exact `TARGET_REVISION`; `head` is refused.
  - Downgrade and stamp are never run.
  - It connects only through `MIGRATION_DATABASE_URL`, which must be Neon's
    *direct* endpoint, not the `-pooler` host.
- **A database with tables but no revision** (production before R7) is
  refused, except by `scripts/db_stamp_baseline.py` once its checks pass.
- **What a revision may contain:**
  - only the `op` calls the scanner allows;
  - SQL only as inline literals reviewed in `migrations/raw_sql_review.json`;
  - nothing that names an ADK table (`sessions`, `events`, `app_states`,
    `user_states`, `adk_internal_metadata`).

  Run `python scripts/check_migrations.py` before review.
- **A downgrade that would lose rows refuses** (`IrreversibleWithData`).
- **Local development:**
  - an empty database: `MIGRATION_DATABASE_URL=… alembic -x label=development upgrade head`;
  - an old, unmanaged local database: `init_db` keeps it working as before.

The Python environment that runs these commands needs `alembic` installed
(`pip install -r requirements.txt`). On macOS, the rehearsal also needs
`greenlet`, which SQLAlchemy installs only on Linux.

## R7 rollout: stamping production

Every step needs your approval. Nothing here writes to production until
step 4.

1. **Dump production's schema (read-only)** over the direct endpoint, into the
   encrypted volume. The second command adds ADK's one-row version table;
   without it ADK refuses the restored copy.
   ```bash
   docker run --rm postgres:16 pg_dump --schema-only --no-owner --no-privileges "$DIRECT_URL" > prod_schema.sql
   docker run --rm postgres:16 pg_dump --data-only --inserts --table=adk_internal_metadata "$DIRECT_URL" >> prod_schema.sql
   ```
   Use a `postgres` image whose major version is at least the server's.
2. **Rehearse locally** against a throwaway container (as `scripts/test_pg.sh`
   starts one):
   ```bash
   python scripts/db_rehearse.py --admin-url postgresql+psycopg://postgres:…@127.0.0.1:PORT/postgres --dump prod_schema.sql
   ```
   It must print `PASS`. On `FAIL`, every A-vs-C difference is listed in
   `docs/db/baseline-report.json`. Correct `0001_baseline` to match
   production (not the other way round), record the decision in
   `baseline-drift.md`, and rehearse again.
3. **Rehearse on a Neon branch of production** (R2 of the plan):
   ```bash
   python scripts/db_stamp_baseline.py --target rehearsal --url "$BRANCH_DIRECT_URL"
   ```
4. **Stamp production**, in one transaction that rolls back on any failed
   check:
   ```bash
   MIGRATION_DATABASE_URL="$DIRECT_URL" python scripts/db_stamp_baseline.py --target production --confirm-production --dump prod_schema.sql
   ```
   This must happen after production runs R6 and before R7 is deployed.
   Between the dump and the stamp, no deploy may change the schema; if one
   does, the fingerprint check refuses and you repeat from step 1.
5. **Deploy R7.** Its startup check requires `0001a_db_identity`.
6. **Verify (read-only):**
   - `SELECT version_num FROM alembic_version` gives `0001a_db_identity`;
   - `SELECT label FROM db_identity` gives `production`.

**Rollback:** redeploy R6. It ignores `alembic_version` and `db_identity`.
Production is never downgraded.

## Later releases

- **Before deploying**, the pre-deploy step runs:
  ```bash
  ALEMBIC_PRODUCTION_RELEASE=<RELEASE_ID> alembic upgrade <TARGET_REVISION>
  ```
- **Take a backup or Neon branch first** (R10, R11).

## R10 rollout: the delivery ledger

1. **Take a backup or Neon branch.**
2. **Pre-deploy:**
   ```bash
   ALEMBIC_PRODUCTION_RELEASE=R10 MIGRATION_DATABASE_URL="$DIRECT_URL" alembic upgrade 0002_delivery_ledger
   ```
   This creates the table, its indexes and trigger. No existing row changes.
3. **Deploy R10.** Its startup requires `0002_delivery_ledger`.
4. **Verify (read-only):**
   ```sql
   SELECT status, count(*) FROM briefing_deliveries GROUP BY status;
   ```
   Within 3 days, no `pending` or `sending` row should be older than its
   lease.

Schedules the R5 scheduler left `running` during the overlap are reclaimed as
`unknown`, never resent (`repository.reclaim_expired`).

**Rollback:** redeploy R9. R7–R9 list `0002_delivery_ledger` as reserved and
compatible, so they start on it and leave the table alone. R10 likewise lists
`0003_broker_connections` for R11. Production is never downgraded.

## R11 rollout: broker tables

1. **Gate (read-only):**
   ```bash
   MIGRATION_DATABASE_URL="$DIRECT_URL" python scripts/check_broker_ids.py
   ```
   It must print `PASS`: every client code fits `^[A-Z0-9]{4,16}$` after
   trimming and upper-casing, and none collide. It prints counts and shapes
   only. On `FAIL`, revise the pattern in `0003` before going on.
2. **Take a backup or Neon branch.**
3. **Pre-deploy:**
   ```bash
   ALEMBIC_PRODUCTION_RELEASE=R11 MIGRATION_DATABASE_URL="$DIRECT_URL" alembic upgrade 0003_broker_connections
   ```
   The new `users` columns are nullable with no default (metadata only). The
   checks and unique indexes on `users` lock it briefly; `lock_timeout` is 5 s,
   so a busy table makes the upgrade fail rather than queue.
4. **Deploy R11.** Its startup requires `0003_broker_connections`.

**Rollback:** redeploy R10, which lists `0003` as compatible. Nothing writes
to `broker_connections` until the next phase, so the table stays empty.

## Optional hardening

The `db_identity` triggers stop accidents, not a deliberate actor with SQL
access. To stop that too:
- give `db_identity` to its own owner role;
- grant the app and migrator roles `SELECT` only.

This needs Neon role management (U9).
