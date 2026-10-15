"""Account deletion, in the app (``DELETE /api/account``) and on the web
(``/delete-account``), and the public legal pages the stores link to.

Run directly (``python tests/test_account_deletion.py``) or under pytest.
"""
from __future__ import annotations

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_spa_api import GOOD_PASSWORD, NUMBER, WRONG_PASSWORD, harness  # noqa: E402


@contextlib.contextmanager
def deletion_harness():
    """The API harness, with the deletion service on the same temp database."""
    from services import account_deletion

    with harness() as h:
        original = account_deletion.get_session
        account_deletion.get_session = h.service.get_session
        try:
            yield h
        finally:
            account_deletion.get_session = original


def _signed_in(h) -> int:
    view = h.enrol_row()
    res = h.http.post("/api/auth/login", json={
        "country_code": "91", "whatsapp_number": "9876543210", "app_password": GOOD_PASSWORD,
    })
    assert res.status_code == 200, res.text
    return view["id"]


def _seed(h, user_id: int) -> None:
    """Something in every table that hangs off a user."""
    from db.models import ChatThread, DecisionRecord, Log, RiskProfile, Schedule, utcnow

    with h.service.get_session() as db:
        schedule = Schedule(user_id=user_id, interval_minutes=1440, next_run=utcnow())
        db.add(schedule)
        db.flush()
        db.add(Log(user_id=user_id, schedule_id=schedule.id, status="success"))
        db.add(RiskProfile(
            user_id=user_id, age=30, goal="income", horizon_years=5, risk_tolerance="low",
            tax_bracket="20%", max_single_order_value=10000, max_position_pct=10,
            allowed_products="DELIVERY", max_daily_trades=3,
        ))
        db.add(ChatThread(user_id=user_id, adk_session_id="adk-1"))
        db.add(DecisionRecord(
            user_id=user_id, proposal_id="p1", symbol="TCS-EQ", side="BUY",
            quantity=1, outcome="executed",
        ))
        db.commit()


def _rows(h, user_id: int) -> dict[str, int]:
    from sqlmodel import select

    from db.models import ChatThread, DecisionRecord, Log, RiskProfile, Schedule, User

    with h.service.get_session() as db:
        count = lambda model: len(db.exec(select(model).where(model.user_id == user_id)).all())  # noqa: E731
        return {
            "user": 1 if db.get(User, user_id) else 0,
            "threads": count(ChatThread),
            "schedules": count(Schedule),
            "logs": count(Log),
            "risk": count(RiskProfile),
            "decisions_linked": count(DecisionRecord),
            "decisions_total": len(db.exec(select(DecisionRecord)).all()),
        }


def test_wrong_password_deletes_nothing_and_keeps_the_session():
    with deletion_harness() as h:
        uid = _signed_in(h)
        _seed(h, uid)
        res = h.http.request("DELETE", "/api/account", json={"app_password": WRONG_PASSWORD})
        assert res.status_code == 400, "a 401 would sign the app out"
        assert res.json()["field"] == "app_password"
        assert _rows(h, uid)["user"] == 1
        assert h.bootstrap()["authenticated"] is True
    print("ok  a wrong password is a 400 on the field; nothing is deleted")


def test_right_password_deletes_everything_but_unlinked_audit_records():
    with deletion_harness() as h:
        uid = _signed_in(h)
        _seed(h, uid)
        res = h.http.request("DELETE", "/api/account", json={"app_password": GOOD_PASSWORD})
        assert res.status_code == 200, res.text
        assert res.json() == {"ok": True, "deleted": True}
        rows = _rows(h, uid)
        assert rows == {
            "user": 0, "threads": 0, "schedules": 0, "logs": 0, "risk": 0,
            "decisions_linked": 0, "decisions_total": 1,
        }, rows
        assert h.bootstrap() == {"authenticated": False}, "deleting also signs out"
        login = h.http.post("/api/auth/login", json={
            "country_code": "91", "whatsapp_number": "9876543210", "app_password": GOOD_PASSWORD,
        })
        assert login.status_code == 401, "the account is gone"
    print("ok  deletion removes the user and everything hanging off it, then signs out")


def test_deletion_needs_a_session():
    with deletion_harness() as h:
        res = h.http.request("DELETE", "/api/account", json={"app_password": GOOD_PASSWORD})
        assert res.status_code == 401
    print("ok  no session, no deletion")


def test_password_guessing_is_throttled():
    with deletion_harness() as h:
        _signed_in(h)
        codes = [
            h.http.request("DELETE", "/api/account", json={"app_password": WRONG_PASSWORD}).status_code
            for _ in range(12)
        ]
        assert codes[0] == 400 and 429 in codes, codes
    print("ok  repeated wrong passwords hit the login throttle")


def test_web_form_deletes_with_number_and_password():
    with deletion_harness() as h:
        uid = h.enrol_row()["id"]
        form = {"country_code": "91", "whatsapp_number": "98765 43210", "confirm": "yes"}

        wrong = h.http.post("/delete-account", data={**form, "app_password": WRONG_PASSWORD})
        assert wrong.status_code == 400
        assert "don&#39;t match" in wrong.text or "don't match" in wrong.text
        assert _rows(h, uid)["user"] == 1

        unconfirmed = h.http.post(
            "/delete-account", data={**form, "confirm": "", "app_password": GOOD_PASSWORD}
        )
        assert unconfirmed.status_code == 400
        assert _rows(h, uid)["user"] == 1

        ok = h.http.post("/delete-account", data={**form, "app_password": GOOD_PASSWORD})
        assert ok.status_code == 200
        assert "has been deleted" in ok.text
        assert _rows(h, uid)["user"] == 0
    print("ok  the web form deletes only with the right number, password and confirmation")


def test_legal_pages_are_public():
    with deletion_harness() as h:
        for path, heading in [
            ("/privacy", "Privacy policy"),
            ("/terms", "Terms of use"),
            ("/risk-disclosure", "Risk disclosure"),
            ("/delete-account", "Delete your Agentfolio account"),
        ]:
            res = h.http.get(path)
            assert res.status_code == 200, path
            assert heading in res.text, path
            assert "Draft" in res.text, "pages carry the draft notice until legal review"
    print("ok  privacy, terms, risk and delete-account pages load without signing in")


if __name__ == "__main__":
    assert NUMBER  # imported for the harness's enrolled row
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"\n{passed} tests passed")
