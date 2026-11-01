# R0 test baseline

Recorded 2026-11-01, before any Phase 0 change. Branch `claude/user-angleone-credential-split-478e62` (based on `main` at `0a6dfe2`). Run locally with `DATABASE_URL` and `ENCRYPTION_KEY` unset.

## Backend (`MyFinancesMCP`)

Runner: `.venv/bin/python tests/<file>.py`, one process per file (Python 3.12). Every file exited 0.

| File | Tests | Seconds |
|---|---|---|
| test_account_deletion | 6 | 4 |
| test_app_login | 23 | 1 |
| test_app_wiring | 7 | 2 |
| test_compliance | 13 | 3 |
| test_decision_record | 5 | 0 |
| test_feature_contract | 13 | 1 |
| test_fundamental_prompt | 12 | 0 |
| test_fundamental_scoring | 30 | 0 |
| test_fundamental_service | 8 | 2 |
| test_isolation_and_sessions | 13 | 4 |
| test_labeling | 10 | 0 |
| test_market_stream | 6 | 6 |
| test_marketstore | 9 | 0 |
| test_mcp_auth | 12 | 4 |
| test_mcp_oauth | 18 | 4 |
| test_readonly_boundary | 14 | 3 |
| test_realtime_feed | 7 | 1 |
| test_spa_api | 29 | 3 |
| test_validation | 12 | 1 |
| test_whatsapp_send | 4 | 0 |
| **Total** | **251** | ~40 |

Logged tracebacks and warnings come only from deliberate negative-path cases (malformed password hashes, a rejected TOTP, a broken trade book, replayed OAuth codes, throttled tokens) and from dev-mode config warnings (`ALLOWED_HOSTS` unset, short `SESSION_SECRET`). No test failed.

Side effect: `test_marketstore` (via `services/marketstore/universe.py`) writes `data/universe.parquet`, which is git-ignored.

## UI (`AgentfolioUI`)

Runner: `npm test` (`vitest run`, Node 24.20.0). 20 files, **215 tests passed**.

## PostgreSQL harness

`scripts/test_pg.sh` starts a disposable `postgres:16` container on `127.0.0.1` at a random port, exports `TEST_POSTGRES_URL`, runs `tests/*_pg.py` and removes the container on exit. No `*_pg.py` tests exist yet, so for now it only runs its connection smoke check.

Verified 2026-11-01 with Docker 29.7.2 and `postgres:16` (server 16.15, image `5db6dac2ef5d`, 663 MB):

- With no arguments: the connection smoke check runs, then exit 0.
- With one passing and one failing throwaway script: the passing one ran a two-connection `ORDER BY … LIMIT 1 FOR UPDATE SKIP LOCKED` check (the second connection skipped the locked row). The harness reported `1 passed, 1 failed` and exited 1.
- No containers were left behind after either run.
- With the Docker daemon stopped, it prints `docker daemon is not running` and exits 2.
