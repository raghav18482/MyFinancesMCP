"""Alembic on a real PostgreSQL: the guard, db_identity, the stamp, the startup check.

Needs TEST_POSTGRES_URL; run through ``scripts/test_pg.sh``. Skips otherwise.
Each test works in its own fresh database on that server.
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from contextlib import contextmanager
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

URL = os.environ.get("TEST_POSTGRES_URL")


@contextmanager
def fresh_db():
    """A new empty database; yields its engine (NullPool) and URL."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    name = f"t_{uuid.uuid4().hex[:10]}"
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = URL.rsplit("/", 1)[0] + "/" + name
    engine = create_engine(url, poolclass=NullPool)
    try:
        yield engine, url
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _cfg(conn, label=None):
    import db_stamp_baseline as st

    return st.alembic_config(conn, label=label)


def upgrade(engine, rev, *, label=None, gate=False):
    from alembic import command

    with engine.connect() as c:
        cfg = _cfg(c, label)
        if gate:
            cfg.attributes["baseline_gate_passed"] = True
        command.upgrade(cfg, rev)
        c.commit()


def downgrade(engine, rev):
    from alembic import command

    with engine.connect() as c:
        command.downgrade(_cfg(c), rev)
        c.commit()


def refused(fn, needle=""):
    from migrations.guard import MigrationRefused

    try:
        fn()
    except MigrationRefused as e:
        assert needle in str(e), (needle, str(e))
        return
    raise AssertionError("expected MigrationRefused")


def scalar(engine, sql, **params):
    from sqlalchemy import text

    with engine.connect() as c:
        return c.execute(text(sql), params).scalar()


def legacy_production_db(engine):
    """A database shaped like production before R7: the baseline's tables, no Alembic."""
    from sqlalchemy import text

    upgrade(engine, "0001_baseline")
    with engine.begin() as c:
        c.execute(text("DROP TABLE alembic_version"))
        c.execute(text(
            "INSERT INTO users (whatsapp_number, angel_api_key, angel_client_id, angel_password_encrypted, "
            "angel_totp_secret_encrypted, is_active, created_at, updated_at) "
            "VALUES ('+910000000000', 'k', 'AB1234', 'p', 't', true, now(), now())"
        ))
        # Stand-ins for ADK's tables, with rows the migrations must not touch.
        c.execute(text("CREATE TABLE sessions (id text PRIMARY KEY, state jsonb)"))
        c.execute(text("CREATE TABLE events (id text PRIMARY KEY, session_id text, body text)"))
        c.execute(text("INSERT INTO sessions VALUES ('s1', '{\"k\": 1}')"))
        c.execute(text("INSERT INTO events VALUES ('e1', 's1', 'hello')"))


def report_for(engine, **overrides):
    from migrations.compare import BASELINE_TABLES, model_drift
    from migrations.fingerprint import fingerprint

    with engine.connect() as c:
        report = {
            "status": "PASS", "source": "dump", "dump_sha256": "d" * 64,
            "fingerprint": fingerprint(c),
            "baseline_tables_fingerprint": fingerprint(c, BASELINE_TABLES),
            "allowed_model_drift": model_drift(c),
        }
    report.update(overrides)
    return report


def stamped_production_db(engine):
    import db_stamp_baseline as st

    legacy_production_db(engine)
    st.stamp(engine, target="production", report=report_for(engine), dump_sha="d" * 64)


# ── the guard's state matrix ─────────────────────────────────────────────


def test_empty_database_builds_outside_production_only():
    with fresh_db() as (engine, _):
        upgrade(engine, "0001a_db_identity", label="test")
        assert scalar(engine, "SELECT label FROM db_identity") == "test"
    with fresh_db() as (engine, _), mock.patch.dict(os.environ, {"APP_ENV": "production"}):
        refused(lambda: upgrade(engine, "0001a_db_identity", label="test"), "empty database in production")
        assert scalar(engine, "SELECT to_regclass('users') IS NULL")


def test_only_the_stamp_may_label_production():
    with fresh_db() as (engine, _):
        refused(lambda: upgrade(engine, "0001a_db_identity", label="production"), "label a database production")


def test_existing_tables_without_a_revision_are_refused():
    from alembic import command

    with fresh_db() as (engine, _):
        legacy_production_db(engine)
        refused(lambda: upgrade(engine, "0001a_db_identity", label="test"), "no Alembic revision")

        def stamp_directly():
            with engine.connect() as c:
                command.stamp(_cfg(c), "0001_baseline")
        refused(stamp_directly, "no Alembic revision")
        assert scalar(engine, "SELECT to_regclass('alembic_version') IS NULL")


def test_production_needs_the_release_token_and_the_exact_target():
    from db import schema_version

    with fresh_db() as (engine, _):
        stamped_production_db(engine)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALEMBIC_PRODUCTION_RELEASE", None)
            refused(lambda: upgrade(engine, schema_version.TARGET_REVISION), "ALEMBIC_PRODUCTION_RELEASE")
        with mock.patch.dict(os.environ, {"ALEMBIC_PRODUCTION_RELEASE": "R0"}):
            refused(lambda: upgrade(engine, schema_version.TARGET_REVISION), "ALEMBIC_PRODUCTION_RELEASE")
        with mock.patch.dict(os.environ, {"ALEMBIC_PRODUCTION_RELEASE": schema_version.RELEASE_ID}):
            refused(lambda: upgrade(engine, "head"), "upgrade to exactly")
            refused(lambda: downgrade(engine, "0001_baseline"), "never run against the production")
            upgrade(engine, schema_version.TARGET_REVISION)  # allowed
        assert scalar(engine, "SELECT version_num FROM alembic_version") == schema_version.TARGET_REVISION


def test_production_environment_needs_the_direct_endpoint():
    from alembic import command
    from alembic.config import Config

    import db_stamp_baseline as st

    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    with mock.patch.dict(os.environ, {"APP_ENV": "production"}):
        os.environ.pop("MIGRATION_DATABASE_URL", None)
        refused(lambda: command.upgrade(cfg, st.IDENTITY), "MIGRATION_DATABASE_URL must be set")
        os.environ["MIGRATION_DATABASE_URL"] = "postgresql+psycopg://u:p@ep-x-pooler.neon.tech/db"
        refused(lambda: command.upgrade(cfg, st.IDENTITY), "not the pooler")


# ── db_identity ───────────────────────────────────────────────────────────


def _raises(engine, sql, needle, **params):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    try:
        with engine.begin() as c:
            c.execute(text(sql), params)
    except DBAPIError as e:
        assert needle in str(e), (needle, str(e))
        return
    raise AssertionError(f"expected {sql!r} to fail")


def test_db_identity_cannot_be_changed_by_accident():
    with fresh_db() as (engine, _):
        upgrade(engine, "0001a_db_identity", label="development")
        _raises(engine, "INSERT INTO db_identity (singleton, id, label) VALUES (false, gen_random_uuid(), 'test')",
                "written only by its migration")
        _raises(engine, "DELETE FROM db_identity", "cannot be deleted")
        _raises(engine, "TRUNCATE db_identity", "cannot be deleted")
        _raises(engine, "UPDATE db_identity SET id = gen_random_uuid()", "only db_identity.label")
        _raises(engine, "UPDATE db_identity SET created_at = now() - interval '1 day'", "only db_identity.label")
        _raises(engine, "UPDATE db_identity SET label = 'test'", "relabel through")
        _raises(engine, "UPDATE db_identity SET label = 'production'", "only by the baseline stamp")
        assert scalar(engine, "SELECT count(*) FROM db_identity") == 1


def test_relabel_script_needs_the_right_id():
    import db_relabel

    with fresh_db() as (engine, _):
        upgrade(engine, "0001a_db_identity", label="rehearsal")
        real_id = scalar(engine, "SELECT id::text FROM db_identity")
        for kw, needle in (({"old": "rehearsal", "new": "test", "confirm": str(uuid.uuid4())}, "does not match"),
                           ({"old": "development", "new": "test", "confirm": real_id}, "not 'development'"),
                           ({"old": "rehearsal", "new": "production", "confirm": real_id}, "relabelled production")):
            try:
                db_relabel.relabel(engine, **kw)
            except db_relabel.RelabelRefused as e:
                assert needle in str(e), (needle, str(e))
            else:
                raise AssertionError(kw)
        db_relabel.relabel(engine, old="rehearsal", new="development", confirm=real_id)
        assert scalar(engine, "SELECT label FROM db_identity") == "development"


# ── the stamp ─────────────────────────────────────────────────────────────


def _untouched(engine):
    assert scalar(engine, "SELECT to_regclass('alembic_version') IS NULL")
    assert scalar(engine, "SELECT to_regclass('db_identity') IS NULL")
    assert scalar(engine, "SELECT count(*) FROM users") == 1


def test_stamp_succeeds_and_leaves_data_and_adk_alone():
    from migrations.fingerprint import table_snapshot

    with fresh_db() as (engine, _):
        legacy_production_db(engine)
        with engine.connect() as c:
            before = table_snapshot(c, ("sessions", "events"))
        import db_stamp_baseline as st

        st.stamp(engine, target="production", report=report_for(engine), dump_sha="d" * 64)
        assert scalar(engine, "SELECT version_num FROM alembic_version") == "0001a_db_identity"
        assert scalar(engine, "SELECT label FROM db_identity") == "production"
        assert scalar(engine, "SELECT count(*) FROM users") == 1
        with engine.connect() as c:
            assert table_snapshot(c, ("sessions", "events")) == before


def test_stamp_is_all_or_nothing():
    import db_stamp_baseline as st

    for fail_at in ("after_stamp", "after_identity", "equivalence"):
        with fresh_db() as (engine, _):
            legacy_production_db(engine)
            try:
                st.stamp(engine, target="production", report=report_for(engine), dump_sha="d" * 64,
                         fail_at=fail_at)
            except (RuntimeError, st.StampRefused):
                pass
            else:
                raise AssertionError(fail_at)
            _untouched(engine)


def test_stamp_refuses_a_report_that_does_not_fit():
    from sqlalchemy import text

    import db_stamp_baseline as st

    def expect_refusal(report, sha, needle):
        try:
            st.stamp(engine, target="production", report=report, dump_sha=sha)
        except st.StampRefused as e:
            assert needle in str(e), (needle, str(e))
        else:
            raise AssertionError(needle)
        assert scalar(engine, "SELECT to_regclass('alembic_version') IS NULL")

    with fresh_db() as (engine, _):
        legacy_production_db(engine)
        expect_refusal(report_for(engine, status="FAIL"), "d" * 64, "does not say PASS")
        expect_refusal(report_for(engine, source="proxy"), "d" * 64, "not the proxy")
        expect_refusal(report_for(engine), "e" * 64, "dump file does not match")
        expect_refusal(report_for(engine, allowed_model_drift=[]), "d" * 64, "model drift")
        stale = report_for(engine)
        with engine.begin() as c:
            c.execute(text("ALTER TABLE users ADD COLUMN surprise text"))
        expect_refusal(stale, "d" * 64, "live schema differs")


def test_two_concurrent_stamps_one_commits():
    import db_stamp_baseline as st

    with fresh_db() as (engine, _):
        legacy_production_db(engine)
        report = report_for(engine)
        results, barrier = [], threading.Barrier(2)

        def run():
            barrier.wait()
            try:
                st.stamp(engine, target="production", report=report, dump_sha="d" * 64)
                results.append("ok")
            except st.StampRefused as e:
                results.append(f"refused: {e}")

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results)[0] == "ok" and results.count("ok") == 1, results
        assert scalar(engine, "SELECT count(*) FROM db_identity") == 1


# ── startup and the release manifest ─────────────────────────────────────


def test_production_startup_needs_a_listed_revision():
    from sqlalchemy import text

    from db import init_db, schema_version

    with fresh_db() as (engine, _):
        legacy_production_db(engine)
        try:
            init_db(bind=engine, production=True)
        except schema_version.IncompatibleSchema as e:
            assert "no Alembic revision" in str(e)
        else:
            raise AssertionError("started without a revision")
        assert scalar(engine, "SELECT to_regclass('app_instances') IS NOT NULL")

    with fresh_db() as (engine, _):
        upgrade(engine, schema_version.TARGET_REVISION, label="test")
        init_db(bind=engine, production=True)  # listed: starts
        for rev in ("0001_baseline", "9999_from_the_future"):
            with engine.begin() as c:
                c.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": rev})
            try:
                init_db(bind=engine, production=True)
            except schema_version.IncompatibleSchema:
                pass
            else:
                raise AssertionError(f"started at {rev}")


def test_init_db_runs_no_ddl_on_a_managed_database():
    from sqlalchemy import text

    from db import init_db

    with fresh_db() as (engine, _):
        upgrade(engine, "0001a_db_identity", label="development")
        with engine.begin() as c:
            c.execute(text("DROP TABLE app_instances"))
        init_db(bind=engine)  # development, but Alembic manages it
        assert scalar(engine, "SELECT to_regclass('app_instances') IS NULL")


def test_release_manifest_is_consistent():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from db import schema_version

    script = ScriptDirectory.from_config(Config(os.path.join(ROOT, "alembic.ini")))
    revs = {r.revision: r for r in script.walk_revisions()}
    assert schema_version.REQUIRED_REVISION in schema_version.COMPATIBLE_REVISIONS
    assert schema_version.TARGET_REVISION in schema_version.COMPATIBLE_REVISIONS
    order = [r.revision for r in reversed(list(script.walk_revisions()))]
    required_at = order.index(schema_version.REQUIRED_REVISION)
    for rev in schema_version.COMPATIBLE_REVISIONS:
        if rev in schema_version.RESERVED_REVISIONS:
            assert rev not in revs, f"{rev} exists now; it is no longer only reserved"
            continue
        assert rev in revs, rev
        if order.index(rev) > required_at:
            assert getattr(revs[rev].module, "EXPAND_ONLY", False) is True, rev
    plan = open(os.path.join(ROOT, "docs", "db", "revision-plan.md")).read()
    for rev in schema_version.RESERVED_REVISIONS:
        assert f"`{rev}`" in plan and "reserved" in plan, f"{rev} is not reserved in the revision plan"
    assert script.get_heads() == [schema_version.TARGET_REVISION], "a new head needs a new manifest"


# ── baseline equivalence and ADK ─────────────────────────────────────────


def test_baseline_equals_the_replayed_schema_history():
    from migrations.compare import schema_diff
    from replay_schema_history import replay

    with fresh_db() as (proxy, proxy_url), fresh_db() as (base, _):
        replay(proxy_url)
        upgrade(base, "0001_baseline")
        with proxy.connect() as a, base.connect() as c:
            assert schema_diff(a, c) == []


def test_adk_tables_survive_the_full_cycle():
    from sqlalchemy import text

    from migrations.fingerprint import table_snapshot

    with fresh_db() as (engine, _):
        with engine.begin() as c:  # stand-ins for ADK's tables, with rows
            c.execute(text("CREATE TABLE sessions (id text PRIMARY KEY, state jsonb)"))
            c.execute(text("CREATE TABLE events (id text PRIMARY KEY, session_id text, body text)"))
            c.execute(text("INSERT INTO sessions VALUES ('s1', '{\"k\": 1}')"))
            c.execute(text("INSERT INTO events VALUES ('e1', 's1', 'hello')"))
        with engine.connect() as c:
            before = table_snapshot(c, ("sessions", "events"))
        upgrade(engine, "0001a_db_identity", label="test")
        downgrade(engine, "base")
        upgrade(engine, "0001a_db_identity", label="test")
        with engine.connect() as c:
            assert table_snapshot(c, ("sessions", "events")) == before


def test_downgrade_refuses_to_drop_data():
    from migrations.helpers import IrreversibleWithData

    with fresh_db() as (engine, _):
        upgrade(engine, "0001a_db_identity", label="test")
        from sqlalchemy import text

        with engine.begin() as c:
            c.execute(text("INSERT INTO app_instances (instance_id, started_at, last_seen_at) "
                           "VALUES ('x', now(), now())"))
        try:
            downgrade(engine, "base")
        except IrreversibleWithData:
            pass
        else:
            raise AssertionError("dropped a table holding rows")
        assert scalar(engine, "SELECT version_num FROM alembic_version") == "0001a_db_identity"


if __name__ == "__main__":
    if not URL:
        print("TEST_POSTGRES_URL not set; skipped (run scripts/test_pg.sh)")
        sys.exit(0)
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed, 0 skipped")
