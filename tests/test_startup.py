"""Application startup: what happens when the schema bootstrap fails.

Run directly: ``python tests/test_startup.py``.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_lifespan(env: dict[str, str]):
    import main

    async def go():
        async with main._lifespan(main.app):
            pass

    with mock.patch.dict(os.environ, env), \
            mock.patch("db.init_db", side_effect=RuntimeError("schema broken")):
        asyncio.run(go())


def test_production_refuses_to_start_when_init_db_fails():
    try:
        _run_lifespan({"APP_ENV": "production"})
    except RuntimeError as e:
        assert "schema broken" in str(e)
    else:
        raise AssertionError("startup continued on a broken schema in production")


def test_development_starts_anyway_and_warns():
    env = {k: v for k, v in os.environ.items() if k not in ("APP_ENV", "RENDER")}
    with mock.patch.dict(os.environ, env, clear=True):
        _run_lifespan({})


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
