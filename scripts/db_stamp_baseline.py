"""Hand an existing database to Alembic: stamp the baseline, label it, prove it.

::

    MIGRATION_DATABASE_URL=… python scripts/db_stamp_baseline.py \\
        --target production --confirm-production --dump prod_schema.sql

    python scripts/db_stamp_baseline.py --target rehearsal --url postgresql+psycopg://…

One connection, one transaction. Any failure rolls everything back and leaves
the database exactly as it was:

1. ``pg_advisory_xact_lock``: a second run waits, then finds the stamp done
   and refuses.
2. Pre-checks: no ``alembic_version`` or ``db_identity`` yet; the rehearsal
   report (``docs/db/baseline-report.json``) says PASS; for production, it was
   made from the real dump (not the history proxy) and the dump file's SHA-256
   matches it; the live schema's fingerprint equals the report's.
3. ``alembic stamp 0001_baseline``: inserts the version row, no other DDL.
4. ``alembic upgrade 0001a_db_identity -x label=<target>``.
5. Equivalence re-check inside the transaction: the live app tables still
   match the baseline's recorded fingerprint, and the differences between the
   database and ``db/models.py`` are exactly the ones the report accepted.
6. Post-checks: one ``db_identity`` row with the target label; revision 0001a.
7. COMMIT.

A stamp is not proof the schemas match: steps 2 and 5, and the rehearsal that
wrote the report, are.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_REPORT = REPO / "docs" / "db" / "baseline-report.json"
BASELINE = "0001_baseline"
IDENTITY = "0001a_db_identity"
#: pg_advisory_xact_lock key for the stamp ("STAMP" in ASCII, as a bigint).
STAMP_LOCK = 0x5354414D50
TARGETS = ("production", "rehearsal")


class StampRefused(RuntimeError):
    pass


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def alembic_config(conn, label: str | None = None):
    from alembic.config import Config

    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.attributes["connection"] = conn
    cfg.cmd_opts = Namespace(x=[f"label={label}"] if label else [])
    return cfg


def _precheck(conn, report: dict, target: str, dump_sha: str | None) -> None:
    from sqlalchemy import text

    from migrations.fingerprint import fingerprint

    for table in ("alembic_version", "db_identity"):
        if conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}).scalar():
            raise StampRefused(f"{table} already exists: this database is already under Alembic")
    if report.get("status") != "PASS":
        raise StampRefused("the rehearsal report does not say PASS")
    if target == "production":
        if report.get("source") != "dump":
            raise StampRefused("production needs a report made from the production dump, not the proxy")
        if not dump_sha or dump_sha != report.get("dump_sha256"):
            raise StampRefused("the dump file does not match the one the report was made from")
    live = fingerprint(conn)
    if live != report.get("fingerprint"):
        raise StampRefused("the live schema differs from the rehearsed one; rehearse again")


def _equivalence_check(conn, report: dict) -> None:
    from migrations.fingerprint import fingerprint
    from migrations.compare import BASELINE_TABLES, model_drift

    if fingerprint(conn, BASELINE_TABLES) != report.get("baseline_tables_fingerprint"):
        raise StampRefused("the app tables no longer match the baseline")
    drift = model_drift(conn)
    if sorted(drift) != sorted(report.get("allowed_model_drift", [])):
        raise StampRefused(f"model drift is not the documented set: {drift}")


def _postcheck(conn, target: str) -> None:
    from sqlalchemy import text

    labels = [r[0] for r in conn.execute(text("SELECT label FROM db_identity"))]
    if labels != [target]:
        raise StampRefused(f"db_identity holds {labels}, expected [{target!r}]")
    rev = [r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))]
    if rev != [IDENTITY]:
        raise StampRefused(f"alembic_version is {rev}, expected [{IDENTITY!r}]")


def stamp(engine, *, target: str, report: dict, dump_sha: str | None = None, fail_at: str | None = None) -> None:
    """Run steps 1–7 on ``engine``. ``fail_at`` injects a failure (tests only)."""
    from alembic import command
    from sqlalchemy import text

    if target not in TARGETS:
        raise StampRefused(f"target must be one of {TARGETS}")
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": STAMP_LOCK})
            _precheck(conn, report, target, dump_sha)

            cfg = alembic_config(conn)
            cfg.attributes["baseline_gate_passed"] = True
            command.stamp(cfg, BASELINE)
            if fail_at == "after_stamp":
                raise RuntimeError("injected failure after stamp")

            cfg = alembic_config(conn, label=target)
            cfg.attributes["baseline_gate_passed"] = True
            command.upgrade(cfg, IDENTITY)
            if fail_at == "after_identity":
                raise RuntimeError("injected failure after db_identity")

            if fail_at == "equivalence":
                raise StampRefused("injected equivalence failure")
            _equivalence_check(conn, report)
            _postcheck(conn, target)
            trans.commit()
        except BaseException:
            trans.rollback()
            raise


def main() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    ap = argparse.ArgumentParser(description="Stamp the Alembic baseline on an existing database.")
    ap.add_argument("--target", required=True, choices=TARGETS)
    ap.add_argument("--confirm-production", action="store_true")
    ap.add_argument("--dump", help="the schema dump the report was made from (required for production)")
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--url", help="rehearsal only; production always uses MIGRATION_DATABASE_URL")
    args = ap.parse_args()

    if args.target == "production":
        if not args.confirm_production:
            raise SystemExit("production needs --confirm-production")
        if args.url:
            raise SystemExit("production reads MIGRATION_DATABASE_URL, never --url")
        if not args.dump:
            raise SystemExit("production needs --dump")
    url = args.url or os.environ.get("MIGRATION_DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("set MIGRATION_DATABASE_URL (Neon's direct endpoint)")
    report = json.loads(Path(args.report).read_text())
    dump_sha = file_sha256(args.dump) if args.dump else None

    stamp(create_engine(url, poolclass=NullPool), target=args.target, report=report, dump_sha=dump_sha)
    print(f"stamped {BASELINE}, labelled {args.target}, now at {IDENTITY}")


if __name__ == "__main__":
    main()
