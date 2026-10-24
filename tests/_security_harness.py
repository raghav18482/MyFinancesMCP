"""Shared fixtures for the read-only, auth and isolation tests.

The real app — web routers, session manager, MCP server — over a temporary
SQLite database and a fake broker. ``session_manager.AngelOneClient`` is
replaced, so the real :class:`session_manager.SessionManager` (keys, locks,
TTL) runs unchanged but no broker is ever contacted.

``FakeBroker`` deliberately has no order methods: if anything ever tried to
place, modify or cancel an order, it would fail loudly here.
"""
from __future__ import annotations

import collections
import contextlib
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key-not-real")

PASSWORD = "correct-horse-battery"

# Two accounts. Every secret is distinctive so a leak test can grep for it.
USERS = {
    "A": {
        "client_id": "AA1111",
        "api_key": "APIKEY-ALPHA-9f3c",
        "pin": "PIN-ALPHA-4821",
        "totp": "JBSWY3DPEHPK3PXP",
        "dial": "91",
        "national": "98765 43210",
        "e164": "+919876543210",
        "symbol": "INFY-EQ",
    },
    "B": {
        "client_id": "BB2222",
        "api_key": "APIKEY-BRAVO-7d1e",
        "pin": "PIN-BRAVO-9034",
        "totp": "KRSXG5DSNFXGOIDB",
        "dial": "91",
        "national": "91234 56789",
        "e164": "+919123456789",
        "symbol": "TCS-EQ",
    },
}

MUTATION_NAMES = ("place_order", "modify_order", "cancel_order", "placeOrder",
                  "modifyOrder", "cancelOrder", "smart_api", "_smart_api")


class FakeBroker:
    """Stands in for ``AngelOneClient``. Read methods only; records every call."""

    logins: collections.Counter = collections.Counter()
    login_delay = 0.0
    _lock = threading.Lock()

    def __init__(self, api_key, client_id, password, totp_secret):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_secret = totp_secret
        self.calls: list[str] = []
        self._symbol = next((u["symbol"] for u in USERS.values() if u["client_id"] == client_id),
                            "NONE-EQ")

    @property
    def client_code(self):
        return self.client_id

    def ensure_session(self):
        if not getattr(self, "_logged_in", False):
            if FakeBroker.login_delay:
                import time
                time.sleep(FakeBroker.login_delay)
            with FakeBroker._lock:
                FakeBroker.logins[self.client_id] += 1
            self._logged_in = True

    def _rec(self, name):
        self.calls.append(name)

    def get_profile(self):
        self._rec("get_profile")
        return {"status": True, "data": {"name": f"Owner of {self.client_id}",
                                         "clientcode": self.client_id, "exchanges": ["NSE"],
                                         "products": ["DELIVERY"]}}

    def get_holdings(self):
        self._rec("get_holdings")
        return {"status": True, "data": [{
            "tradingsymbol": self._symbol, "exchange": "NSE", "symboltoken": "1594",
            "quantity": 10, "averageprice": 100.0, "ltp": 110.0, "profitandloss": 100.0,
            "pnlpercentage": 10.0, "close": 108.0,
        }]}

    def get_all_holdings(self):
        self._rec("get_all_holdings")
        return {"status": True, "data": {"totalholding": {"totalholdingvalue": 1100,
                                                            "totalinvvalue": 1000,
                                                            "totalprofitandloss": 100,
                                                            "totalpnlpercentage": 10}}}

    def get_positions(self):
        self._rec("get_positions")
        return {"status": True, "data": [{
            "tradingsymbol": self._symbol, "producttype": "INTRADAY", "netqty": "1",
            "buyavgprice": "100", "sellavgprice": "0", "ltp": "101", "pnl": "1",
        }]}

    def get_order_book(self):
        self._rec("get_order_book")
        return {"status": True, "data": []}

    def get_trade_book(self):
        self._rec("get_trade_book")
        return {"status": True, "data": []}

    def get_funds(self):
        self._rec("get_funds")
        return {"status": True, "data": {"availablecash": "5000", "net": "5000"}}

    def get_ltp(self, exchange, tradingsymbol, symboltoken):
        self._rec("get_ltp")
        return {"status": True, "data": {"ltp": 110.0, "tradingsymbol": tradingsymbol}}

    def search_scrip(self, exchange, search_text):
        self._rec("search_scrip")
        return {"status": True, "data": [{"tradingsymbol": f"{search_text}-EQ",
                                          "symboltoken": "1594", "exchange": exchange}]}

    def get_candle_data(self, params):
        self._rec("get_candle_data")
        return {"status": True, "data": []}

    def get_market_data(self, mode, exchange_tokens):
        self._rec("get_market_data")
        return {"status": True, "data": {"fetched": []}}


def _modules_with_get_session():
    import adminApi.router
    import adminApi.service
    import services.account_deletion
    import services.mcp_tokens
    import services.oauth_provider
    import web.dependencies
    import web.routers.agent
    import web.routers.briefing

    return [adminApi.router, adminApi.service, services.account_deletion, services.mcp_tokens,
            services.oauth_provider, web.dependencies, web.routers.agent, web.routers.briefing]


class Harness:
    def __init__(self, engine, app, service):
        self.engine = engine
        self.app = app
        self.service = service
        self.ids: dict[str, int] = {}

    def client(self):
        """A fresh browser (own cookie jar) against the same app."""
        from fastapi.testclient import TestClient

        return TestClient(self.app, base_url="https://testserver")

    def login(self, who: str, http=None):
        http = http or self.client()
        u = USERS[who]
        res = http.post("/api/auth/login", json={"country_code": u["dial"],
                                                 "whatsapp_number": u["national"],
                                                 "app_password": PASSWORD})
        assert res.status_code == 200, res.text
        return http

    def db(self):
        from sqlmodel import Session

        return Session(self.engine)


@contextlib.contextmanager
def harness(*, app_factory=None):
    """Temp DB, fake broker, both users enrolled. Yields a :class:`Harness`."""
    from sqlmodel import Session, SQLModel, create_engine
    from sqlalchemy.pool import NullPool

    import db as db_pkg
    import session_manager
    from adminApi import service
    from db import models  # noqa: F401
    from services import login_throttle, mcp_tokens, oauth_provider

    tmp = tempfile.TemporaryDirectory()
    engine = create_engine(
        f"sqlite:///{os.path.join(tmp.name, 't.db')}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,  # a connection per session, so threaded tests are real
    )
    SQLModel.metadata.create_all(engine)

    def factory():
        return Session(engine)

    undo = []

    def patch(obj, name, value):
        original = getattr(obj, name)
        undo.append(lambda: setattr(obj, name, original))
        setattr(obj, name, value)

    db_engine_mod = sys.modules["db.engine"]
    patch(db_pkg, "get_session", factory)
    patch(db_engine_mod, "get_session", factory)
    for mod in _modules_with_get_session():
        patch(mod, "get_session", factory)
    patch(session_manager, "AngelOneClient", FakeBroker)

    sessions = session_manager.sessions
    sessions._sessions.clear()
    sessions._created_at.clear()
    sessions._key_locks.clear()
    FakeBroker.logins.clear()
    FakeBroker.login_delay = 0.0
    login_throttle._by_identifier.clear()
    login_throttle._by_ip.clear()
    mcp_tokens.reset_rate_limits()
    oauth_provider.reset_rate_limits()
    import web.dependencies as deps
    deps._recent_rebuilds.clear()

    try:
        for key, u in USERS.items():
            service.create_user(
                whatsapp_number=u["e164"], angel_api_key=u["api_key"],
                angel_client_id=u["client_id"], angel_password=u["pin"],
                angel_totp_secret=u["totp"], app_password=PASSWORD,
            )
        if app_factory is None:
            from web.app import create_app as app_factory  # noqa: N806
        h = Harness(engine, app_factory(), service)
        for key, u in USERS.items():
            h.ids[key] = service.find_by_client_id(u["client_id"])["id"]
        yield h
    finally:
        sessions._sessions.clear()
        sessions._created_at.clear()
        sessions._key_locks.clear()
        for fn in reversed(undo):
            fn()
        tmp.cleanup()


def run_all(namespace: dict) -> None:
    passed = 0
    for name, fn in sorted(namespace.items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"\n{passed} tests passed")
