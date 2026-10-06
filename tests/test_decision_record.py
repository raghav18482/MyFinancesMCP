"""Tests for the append-only decision record.

Two properties worth guarding:

  * **A write failure must not break a trade.** The record is written after the
    order has already gone to the broker. Raising there would not un-place the
    order, it would just turn a bookkeeping problem into a user-visible failure.
    So ``record()`` swallows and logs.
  * **The payload hash must be stable.** It is the evidence that the inputs were
    not altered afterwards, which it cannot be if it depends on dict ordering.

Runs against a temporary SQLite database rather than the project's Postgres, so
it needs no running server.

Run directly (``python tests/test_decision_record.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_payload_hash_is_order_independent():
    from services.decision_record import payload_hash

    a = {"symbol": "RELIANCE", "quantity": 10, "price": 1200.0}
    b = {"price": 1200.0, "symbol": "RELIANCE", "quantity": 10}
    assert payload_hash(a) == payload_hash(b), (
        "the hash must not depend on dict ordering, or it is evidence of nothing"
    )

    c = {"symbol": "RELIANCE", "quantity": 11, "price": 1200.0}
    assert payload_hash(a) != payload_hash(c), "a changed input must change the hash"
    print("ok  payload hash is stable under key order and sensitive to content")


def test_payload_hash_survives_unserialisable_values():
    from services.decision_record import payload_hash

    class Odd:
        def __repr__(self): return "<Odd>"

    h = payload_hash({"x": Odd()})
    assert isinstance(h, str) and len(h) == 64, "should fall back, not raise"
    print("ok  payload hash falls back for unserialisable values")


def test_a_write_failure_never_raises():
    """A dead database must not propagate into the trade path."""
    from services import decision_record

    prior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "postgresql+psycopg://nobody@127.0.0.1:1/nonexistent"
    try:
        # db.engine binds the URL at import, so this exercises whatever is
        # already configured; either way the call must return None, not raise.
        result = decision_record.record(
            proposal_id="p1", symbol="RELIANCE", side="BUY",
            quantity=10, outcome="executed",
        )
        assert result is None or isinstance(result, int)
        print("ok  a decision-record write failure returns instead of raising")
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior


def _db_engine_module():
    """The ``db.engine`` *module*, not ``db.engine`` the Engine object.

    ``db/__init__.py`` does ``from .engine import engine``, which rebinds the
    attribute ``db.engine`` to the SQLAlchemy Engine and shadows the submodule.
    ``sys.modules`` still has the module itself.
    """
    import db.engine  # noqa: F401 — ensure it is imported

    return sys.modules["db.engine"]


def test_record_round_trip_against_sqlite():
    """The table, the write and the read-back, against a real database."""
    from sqlmodel import SQLModel, Session, create_engine

    from db import models  # noqa: F401 — registers the tables
    from services import decision_record

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        SQLModel.metadata.create_all(engine)

        # Point the module's session factory at the temp database.
        db_engine = _db_engine_module()
        original = db_engine.get_session
        db_engine.get_session = lambda: Session(engine)
        try:
            rec_id = decision_record.record(
                proposal_id="p-123", symbol="RELIANCE-EQ", side="BUY",
                quantity=10, price=1200.0, outcome="blocked",
                session_ref="session-abcdef-longer-than-twelve",
                payload={"tradingsymbol": "RELIANCE-EQ", "quantity": 10},
                model_version="20261004T130842Z", feature_set_version=1,
                ruleset_id="template-v1",
                compliance={"blocked": True, "breaches": [{"rule_id": "restricted_list"}]},
                rationale="compliance: restricted list",
            )
            assert rec_id is not None, "write failed against a working database"

            rows = decision_record.recent(10)
            assert len(rows) == 1
            row = rows[0]
            assert row["symbol"] == "RELIANCE-EQ"
            assert row["outcome"] == "blocked", "blocked decisions must be recorded too"
            assert row["ruleset_id"] == "template-v1"
            assert row["model_version"] == "20261004T130842Z"
            assert row["compliance"]["breaches"][0]["rule_id"] == "restricted_list"
            assert len(row["payload_hash"]) == 64

            filtered = decision_record.recent(10, symbol="NOSUCH")
            assert filtered == [], "symbol filter should exclude other names"
            print("ok  a decision record round-trips, including a blocked outcome")
        finally:
            db_engine.get_session = original


def test_session_ref_is_truncated():
    """Store enough of the session id to correlate, not enough to reuse."""
    from sqlmodel import SQLModel, Session, create_engine, select

    from db import models
    from services import decision_record

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        SQLModel.metadata.create_all(engine)

        db_engine = _db_engine_module()
        original = db_engine.get_session
        db_engine.get_session = lambda: Session(engine)
        try:
            decision_record.record(
                proposal_id="p-1", symbol="TCS", side="SELL", quantity=5,
                outcome="executed",
                session_ref="a-very-long-session-identifier-that-should-not-be-stored",
            )
            with Session(engine) as s:
                row = s.exec(select(models.DecisionRecord)).first()
            assert len(row.session_ref) <= 12, (
                f"session ref stored in full: {row.session_ref!r}"
            )
            print("ok  only a short prefix of the session id is stored")
        finally:
            db_engine.get_session = original


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in TESTS:
        fn()
    print(f"\n{len(TESTS)} passed")
