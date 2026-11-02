"""Rehearse the baseline stamp on a local copy, and write the report the real stamp needs.

::

    python scripts/db_rehearse.py --admin-url postgresql+psycopg://postgres:…@127.0.0.1:PORT/postgres \\
        --dump prod_schema.sql            # the real thing
    python scripts/db_rehearse.py --admin-url … --proxy   # history replay, until the dump exists

``--admin-url`` must be a local throwaway server (``scripts/test_pg.sh`` style
container); anything else is refused. It creates three databases there:

A ``rh_a``
    Production's schema: the dump restored, or ``--proxy``'s replay of the
    schema history plus ADK's own tables. Its fingerprint goes in the report;
    the live stamp refuses unless production still has exactly this schema.
C ``rh_c``
    ``0001_baseline`` applied to an empty database. **C must equal A** on the
    app tables, or the report says FAIL.
S ``rh_s``
    A copy of A with app rows and ADK rows (written by ADK's own
    ``DatabaseSessionService``). The stamp runs here as ``rehearsal``; ADK's
    tables are snapshotted (definition, row count, MD5 of rows) before and
    after, and must be identical. Then a downgrade to the baseline and back.

The report (default ``docs/db/baseline-report.json``) records the dump's
SHA-256, A's fingerprint, the baseline's fingerprint, every A-vs-C difference,
the model drift accepted, and PASS or FAIL. Only a PASS report made from the
real dump lets ``scripts/db_stamp_baseline.py --target production`` run.

ADK's session service needs ``greenlet``, which SQLAlchemy installs only on
Linux; on macOS install it into the venv used for the rehearsal.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

DEFAULT_REPORT = REPO / "docs" / "db" / "baseline-report.json"
DATABASES = ("rh_a", "rh_c", "rh_s")


def _local(url: str) -> None:
    host = urlsplit(url.replace("+psycopg", "")).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit("refusing: --admin-url must point at a local throwaway server")


def _db_url(admin_url: str, name: str) -> str:
    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{name}"


def _recreate(admin_url: str, name: str, template: str | None = None) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
    eng.dispose()


def load_dump(url: str, dump: Path) -> None:
    """Run a plain-SQL schema dump without psql: meta-commands are dropped."""
    import psycopg

    sql = "\n".join(line for line in dump.read_text().splitlines() if not line.startswith("\\"))
    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn:
        if "CREATE SCHEMA public;" in sql:
            conn.execute("DROP SCHEMA public CASCADE")
        conn.execute(sql)
        has_meta = conn.execute("SELECT to_regclass('adk_internal_metadata') IS NOT NULL").fetchone()[0]
        if has_meta and not conn.execute("SELECT count(*) FROM adk_internal_metadata").fetchone()[0]:
            raise SystemExit(
                "adk_internal_metadata is empty: ADK refuses such a database. Append its row to the "
                "dump (pg_dump --data-only --inserts --table=adk_internal_metadata), see "
                "docs/db/revision-plan.md"
            )


def adk_seed(url: str, *, rows: bool) -> None:
    """Let ADK create its tables (and, with ``rows``, write a session and an event)."""
    from google.adk.sessions import DatabaseSessionService

    async def go():
        svc = DatabaseSessionService(db_url=url)
        if not rows:
            await svc.prepare_tables()
            return
        from google.adk.events import Event
        from google.genai import types

        session = await svc.create_session(app_name="rehearsal", user_id="rehearsal-user",
                                           state={"app:k": "v", "user:k": "v", "k": "v"})
        await svc.append_event(session, Event(
            author="user", invocation_id="rehearsal-1",
            content=types.Content(role="user", parts=[types.Part(text="rehearsal")]),
        ))

    asyncio.run(go())


def app_seed(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO users (whatsapp_number, angel_api_key, angel_client_id, angel_password_encrypted, "
            "angel_totp_secret_encrypted, is_active, created_at, updated_at, session_version) "
            "VALUES ('+910000000000', 'k', 'RH0001', 'p', 't', true, now(), now(), 0)"
        ))


def rehearse(admin_url: str, *, dump: Path | None, report_path: Path) -> dict:
    from alembic import command
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    import db_stamp_baseline as stamp_mod
    from migrations.compare import BASELINE_TABLES, model_drift, schema_diff
    from migrations.fingerprint import fingerprint, table_snapshot
    from migrations.helpers import ADK_TABLES

    _local(admin_url)
    for name in ("rh_a", "rh_c"):
        _recreate(admin_url, name)
    url_a, url_c, url_s = (_db_url(admin_url, n) for n in DATABASES)

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "dump" if dump else "proxy",
        "dump_sha256": stamp_mod.file_sha256(dump) if dump else None,
    }

    # A: production's schema.
    if dump:
        load_dump(url_a, dump)
    else:
        from replay_schema_history import replay

        report["replayed_commits"] = replay(url_a)
        adk_seed(url_a, rows=False)
    eng_a = create_engine(url_a, poolclass=NullPool)
    with eng_a.connect() as a:
        report["postgres_version"] = a.execute(text("SHOW server_version")).scalar()
        report["fingerprint"] = fingerprint(a)
        report["allowed_model_drift"] = model_drift(a)

    # C: the baseline revision on an empty database.
    eng_c = create_engine(url_c, poolclass=NullPool)
    with eng_c.connect() as c:
        cfg = stamp_mod.alembic_config(c)
        command.upgrade(cfg, stamp_mod.BASELINE)
        c.commit()
    with eng_a.connect() as a, eng_c.connect() as c:
        report["baseline_diff"] = schema_diff(a, c)
        report["baseline_tables_fingerprint"] = fingerprint(c, BASELINE_TABLES)
    report["status"] = "PASS" if not report["baseline_diff"] else "FAIL"

    # S: a copy of A with rows; stamp it, prove ADK untouched, cycle once.
    _recreate(admin_url, "rh_s", template="rh_a")
    eng_s = create_engine(url_s, poolclass=NullPool)
    app_seed(eng_s)
    adk_seed(url_s, rows=True)
    with eng_s.connect() as s:
        before = table_snapshot(s, ADK_TABLES)
    steps = {}
    try:
        stamp_mod.stamp(eng_s, target="rehearsal", report=report, dump_sha=report["dump_sha256"])
        steps["stamp"] = "ok"
        with eng_s.connect() as s:
            cfg = stamp_mod.alembic_config(s)
            command.downgrade(cfg, stamp_mod.BASELINE)
            cfg = stamp_mod.alembic_config(s, label="rehearsal")
            command.upgrade(cfg, stamp_mod.IDENTITY)
            s.commit()
        steps["downgrade_and_upgrade_again"] = "ok"
        with eng_s.connect() as s:
            users = s.execute(text("SELECT count(*) FROM users")).scalar()
        steps["app_rows_kept"] = users == 1
    except Exception as e:  # recorded, and the report fails
        steps["error"] = f"{type(e).__name__}: {e}"
    with eng_s.connect() as s:
        after = table_snapshot(s, ADK_TABLES)
    report["rehearsal_steps"] = steps
    report["adk_tables_present"] = sorted(t for t, v in before.items() if v is not None)
    report["adk_snapshot_identical"] = before == after
    if "error" in steps or not steps.get("app_rows_kept") or not report["adk_snapshot_identical"]:
        report["status"] = "FAIL"

    for e in (eng_a, eng_c, eng_s):
        e.dispose()
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Rehearse the baseline stamp and write its report.")
    ap.add_argument("--admin-url", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dump", type=Path)
    src.add_argument("--proxy", action="store_true")
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = ap.parse_args()
    report = rehearse(args.admin_url, dump=args.dump, report_path=args.report)
    print(f"{report['status']}  source={report['source']}  diff={len(report['baseline_diff'])}  "
          f"adk_identical={report['adk_snapshot_identical']}  steps={report['rehearsal_steps']}")
    print(f"report: {args.report}")
    sys.exit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
