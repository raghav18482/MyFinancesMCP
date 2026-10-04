"""Structural tests for the web package.

These guard the refactor: every endpoint stays registered, no two endpoints
collide, and the layering between routers and services is not quietly broken
by a future import.

Run directly (``python tests/test_app_wiring.py``) or under pytest.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app import create_app
from web.routers import all_routers

ROUTERS_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "routers"

# Every endpoint the dashboard serves, excluding the admin router and the
# FastAPI/Starlette built-ins. If you add a route, add it here too.
EXPECTED = {
    ("GET", "/"),
    ("GET", "/setup"),
    ("GET", "/connect"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/logout"),
    ("POST", "/api/feedback"),
    ("GET", "/dashboard"),
    ("GET", "/positions"),
    ("GET", "/orders"),
    ("GET", "/analytics"),
    ("GET", "/research"),
    ("GET", "/learn"),
    ("GET", "/sectors"),
    ("GET", "/news"),
    ("GET", "/agent"),
    ("GET", "/trading"),
    ("GET", "/api/news/portfolio"),
    ("GET", "/api/news/search"),
    ("POST", "/api/news/sentiment"),
    ("GET", "/api/portfolio/analytics"),
    ("GET", "/api/portfolio/candles"),
    ("GET", "/api/portfolio/predict"),
    ("GET", "/api/portfolio/beta"),
    ("GET", "/api/research/fundamental"),
    ("POST", "/api/research/fundamental/summary"),
    ("GET", "/api/research/technical"),
    ("GET", "/api/sectors/overview"),
    ("GET", "/api/sectors/breadth"),
    ("POST", "/api/ai/insights"),
    ("POST", "/api/ai/ask"),
    ("GET", "/api/agent/threads"),
    ("POST", "/api/agent/threads"),
    ("GET", "/api/agent/threads/{thread_id}/messages"),
    ("PATCH", "/api/agent/threads/{thread_id}"),
    ("DELETE", "/api/agent/threads/{thread_id}"),
    ("POST", "/api/agent/chat"),
    ("POST", "/api/agent/new-chat"),
    ("GET", "/api/premium/status"),
    ("POST", "/api/premium/register"),
    ("GET", "/api/briefing/schedule"),
    ("POST", "/api/briefing/schedule"),
    ("DELETE", "/api/briefing/schedule"),
    ("POST", "/api/briefing/send-now"),
    ("GET", "/api/briefing/logs"),
    ("GET", "/api/trading/profile"),
    ("POST", "/api/trading/profile"),
    ("GET", "/api/trading/proposals"),
    ("POST", "/api/trading/proposals/{proposal_id}/approve"),
    ("POST", "/api/trading/proposals/{proposal_id}/reject"),
    ("GET", "/api/market/stream"),
}


def _registered() -> set[tuple[str, str]]:
    """Every (method, path) across all domain routers."""
    pairs = set()
    for router in all_routers:
        for route in router.routes:
            for method in getattr(route, "methods", []) or []:
                if method in ("HEAD", "OPTIONS"):
                    continue
                pairs.add((method, route.path))
    return pairs


def test_every_expected_endpoint_is_registered():
    missing = EXPECTED - _registered()
    assert not missing, f"endpoints disappeared: {sorted(missing)}"


def test_no_unexpected_endpoints():
    extra = _registered() - EXPECTED
    assert not extra, f"undeclared endpoints, add them to EXPECTED: {sorted(extra)}"


def test_no_duplicate_routes():
    seen, dupes = set(), []
    for router in all_routers:
        for route in router.routes:
            for method in getattr(route, "methods", []) or []:
                pair = (method, route.path)
                if pair in seen:
                    dupes.append(pair)
                seen.add(pair)
    assert not dupes, f"two handlers claim the same route: {dupes}"


def test_app_builds_and_mounts_static():
    app = create_app()
    mounts = {r.path for r in app.routes if type(r).__name__ == "Mount"}
    assert "/static" in mounts
    assert "/static/data" in mounts


def test_middleware_order_is_preserved():
    # add_middleware prepends, so TrustedHost must end up outermost.
    app = create_app()
    names = [m.cls.__name__ for m in app.user_middleware]
    assert names == ["TrustedHostMiddleware", "SessionMiddleware"], names


def test_routers_do_not_import_each_other():
    """A router reaching into another router means the shared code belongs in
    web/dependencies.py or services/, not in a sibling endpoint module."""
    offenders = []
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            if mod and mod.startswith("web.routers"):
                offenders.append(f"{path.name} imports {mod}")
    assert not offenders, offenders


def test_services_do_not_import_the_web_layer():
    """services/ sits below web/; an import the other way is a layering break."""
    root = ROUTERS_DIR.parent.parent / "services"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            if mod and (mod == "web" or mod.startswith("web.") or mod == "web_app"):
                offenders.append(f"services/{path.relative_to(root)} imports {mod}")
    assert not offenders, offenders


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
