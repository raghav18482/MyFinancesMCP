"""Revision 0003 on a real PostgreSQL: broker_connections, the new users columns,
and the read-only client-code gate.

Needs TEST_POSTGRES_URL; run through ``scripts/test_pg.sh``. Skips otherwise.
"""
from __future__ import annotations

import os
import sys
import uuid

from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

URL = os.environ.get("TEST_POSTGRES_URL")

_F = Fernet(Fernet.generate_key())
CT = _F.encrypt(b"secret").decode()  # a real Fernet token, about 100 characters
assert len(CT) >= 100


def _reset(rev="0003_broker_connections"):
    from alembic import command
    from sqlalchemy import text

    import db_stamp_baseline as st
    from test_delivery_ledger_pg import _engine

    engine = _engine()
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    with engine.connect() as c:
        command.upgrade(st.alembic_config(c, label="test"), rev)
        c.commit()
    with engine.begin() as c:
        for uid in (1, 2):
            c.execute(text(
                "INSERT INTO users (id, whatsapp_number, angel_api_key, angel_client_id, "
                "angel_password_encrypted, angel_totp_secret_encrypted, is_active, created_at, updated_at) "
                "VALUES (:id, :wa, 'k', :cid, 'p', 't', true, now(), now())"
            ), {"id": uid, "wa": f"+91000000000{uid}", "cid": f"AB123{uid}"})
    return engine


def _x(engine, sql, **params):
    from sqlalchemy import text

    with engine.begin() as c:
        return c.execute(text(sql), params)


def _insert(engine, **over):
    row = {"user_id": 1, "broker": "angelone", "account": "AB1231", "api": CT, "pw": CT, "totp": CT,
           "kv": 1, "status": "active", "by": "app", "run": None}
    row.update(over)
    return _x(engine,
              "INSERT INTO broker_connections (user_id, broker, broker_account_id, api_key_encrypted, "
              "password_encrypted, totp_secret_encrypted, key_version, status, created_by, backfill_run_id) "
              "VALUES (:user_id, :broker, :account, :api, :pw, :totp, :kv, :status, :by, CAST(:run AS uuid)) "
              "RETURNING id", **row).scalar_one()


def _refused(fn, needle):
    from sqlalchemy.exc import IntegrityError

    try:
        fn()
    except IntegrityError as e:
        assert needle in str(e), (needle, str(e).splitlines()[0])
        return
    raise AssertionError(f"accepted; expected {needle}")


def test_a_well_formed_connection_is_accepted_with_defaults():
    engine = _reset()
    cid = _insert(engine)
    row = _x(engine, "SELECT status, created_by, row_version FROM broker_connections WHERE id = :id", id=cid).one()
    assert tuple(row) == ("active", "app", 1)


def test_broker_account_id_must_be_trimmed_upper_case_and_simple():
    engine = _reset()
    for bad in ("ab1234", " AB1234", "AB1234 ", "", "AB-1234", "AB1", "A" * 17):
        _refused(lambda: _insert(engine, account=bad), "ck_bc_account_id")
    _refused(lambda: _insert(engine, account=None), "null value")
    _insert(engine, account="AB1234")


def test_one_connection_per_user_and_per_account():
    engine = _reset()
    _insert(engine, user_id=1, account="AB1234")
    _refused(lambda: _insert(engine, user_id=2, account="AB1234"), "uq_bc_account")
    _refused(lambda: _insert(engine, user_id=1, account="CD5678"), "uq_bc_user_broker")


def test_credentials_must_look_like_fernet_tokens():
    engine = _reset()
    short = "gAAAAA" + "x" * 20
    for col, ck in (("api", "ck_bc_api_key"), ("pw", "ck_bc_password"), ("totp", "ck_bc_totp")):
        for bad in ("plaintext-pin-8364", "", short, "gAAAAA" + "!" * 120):
            _refused(lambda: _insert(engine, **{col: bad}), ck)
        _refused(lambda: _insert(engine, **{col: None}), "null value")


def test_enumerations_key_version_and_backfill_provenance():
    engine = _reset()
    run = str(uuid.uuid4())
    for over, ck in (({"kv": 0}, "ck_bc_key_version"), ({"broker": "zerodha"}, "ck_bc_broker"),
                     ({"status": "paused"}, "ck_bc_status"), ({"by": "admin"}, "ck_bc_created_by"),
                     ({"by": "backfill"}, "ck_bc_backfill_run"), ({"run": run}, "ck_bc_backfill_run")):
        _refused(lambda: _insert(engine, **over), ck)
    _insert(engine, by="backfill", run=run)


def test_the_trigger_versions_raw_updates():
    engine = _reset()
    cid = _insert(engine)
    before = _x(engine, "SELECT row_version, updated_at FROM broker_connections WHERE id = :id", id=cid).one()
    _x(engine, "UPDATE broker_connections SET status = 'needs_reauth' WHERE id = :id", id=cid)
    after = _x(engine, "SELECT row_version, updated_at FROM broker_connections WHERE id = :id", id=cid).one()
    assert after.row_version == before.row_version + 1 and after.updated_at > before.updated_at


def test_deleting_a_user_deletes_their_connection():
    engine = _reset()
    _insert(engine)
    _x(engine, "DELETE FROM users WHERE id = 1")
    assert _x(engine, "SELECT count(*) FROM broker_connections").scalar_one() == 0


def test_new_users_columns_are_checked_and_unique_when_set():
    engine = _reset()
    _refused(lambda: _x(engine, "UPDATE users SET firebase_uid = '' WHERE id = 1"), "ck_users_firebase_uid")
    _refused(lambda: _x(engine, "UPDATE users SET firebase_uid = :u WHERE id = 1", u="x" * 129),
             "ck_users_firebase_uid")
    for bad in ("+0123456789", "919876543210", "+91 98765 43210", "+1234567", "+1234567890123456"):
        _refused(lambda: _x(engine, "UPDATE users SET auth_phone_e164 = :p WHERE id = 1", p=bad),
                 "ck_users_auth_phone")
    _x(engine, "UPDATE users SET firebase_uid = 'uid-1', auth_phone_e164 = '+910000000001' WHERE id = 1")
    _refused(lambda: _x(engine, "UPDATE users SET firebase_uid = 'uid-1' WHERE id = 2"), "uq_users_firebase_uid")
    _refused(lambda: _x(engine, "UPDATE users SET auth_phone_e164 = '+910000000001' WHERE id = 2"),
             "uq_users_auth_phone")
    _x(engine, "INSERT INTO users (id, angel_api_key, angel_client_id, angel_password_encrypted, "
               "angel_totp_secret_encrypted, is_active, created_at, updated_at) "
               "VALUES (3, 'k', 'AB1233', 'p', 't', true, now(), now())")  # NULLs do not collide


def test_the_models_match_the_migration():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    import db.models  # noqa: F401

    engine = _reset()
    tables = {"broker_connections", "users"}

    def include_object(obj, name, type_, reflected, compare_to):
        table = obj if type_ == "table" else getattr(obj, "table", None)
        return getattr(table, "name", name) in tables

    with engine.connect() as c:
        mc = MigrationContext.configure(c, opts={"compare_type": True, "include_object": include_object})
        diffs = compare_metadata(mc, SQLModel.metadata)
    assert diffs == [], diffs


def test_downgrade_refuses_to_drop_data():
    from alembic import command

    import db_stamp_baseline as st
    from migrations.helpers import IrreversibleWithData

    engine = _reset()
    with engine.connect() as c:
        command.downgrade(st.alembic_config(c), "0002_delivery_ledger")
        c.commit()
    assert _x(engine, "SELECT to_regclass('broker_connections') IS NULL").scalar_one()
    engine = _reset()
    _x(engine, "UPDATE users SET email = 'a@example.com' WHERE id = 1")
    try:
        with engine.connect() as c:
            command.downgrade(st.alembic_config(c), "0002_delivery_ledger")
    except IrreversibleWithData:
        pass
    else:
        raise AssertionError("dropped users.email holding values")
    assert _x(engine, "SELECT version_num FROM alembic_version").scalar_one() == "0003_broker_connections"


def test_client_code_gate_reports_counts_only():
    import check_broker_ids as gate

    engine = _reset("0002_delivery_ledger")
    with engine.connect() as c, c.begin():
        ok = gate.check(c)
    assert gate.passes(ok) and ok["total"] == ok["passing"] == 2, ok
    assert "AB1231" not in repr(ok), "no client code in the output"

    _x(engine, "INSERT INTO users (id, angel_api_key, angel_client_id, angel_password_encrypted, "
               "angel_totp_secret_encrypted, is_active, created_at, updated_at) VALUES "
               "(3, 'k', ' ab1231', 'p', 't', true, now(), now()), "
               "(4, '', 'XY-99', 'p', 't', true, now(), now())")
    with engine.connect() as c, c.begin():
        bad = gate.check(c)
    assert not gate.passes(bad)
    assert bad["passing"] == 3 and bad["collisions"] == 1 and bad["missing_credentials"] == 1, bad
    assert ("AA-99", 1, False) in bad["shapes"], bad["shapes"]

    from sqlalchemy.exc import DBAPIError

    with engine.connect() as c, c.begin():
        gate.check(c)
        try:
            c.exec_driver_sql("UPDATE users SET is_active = is_active")
        except DBAPIError as e:
            assert "read-only" in str(e)
        else:
            raise AssertionError("the gate's transaction allowed a write")


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
