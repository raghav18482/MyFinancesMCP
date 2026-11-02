"""Read-only gate for revision 0003: do production's Angel client codes fit?

``broker_connections.broker_account_id`` must be ``upper(btrim(code))`` and
match ``^[A-Z0-9]{4,16}$``, and be unique per broker. Before 0003 runs, every
existing ``users.angel_client_id`` must pass, or the pattern is revised first
(plan G-R11, queries Q6–Q8).

::

    MIGRATION_DATABASE_URL=… python scripts/check_broker_ids.py

Runs in a READ ONLY transaction and prints counts and shapes only (``A`` for a
letter, ``9`` for a digit), never a client code or user id. Exit 0 when every
code passes and none collide after normalising; 1 otherwise.
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

PATTERN = "^[A-Z0-9]{4,16}$"

_NORMALIZED = "upper(btrim(coalesce(angel_client_id, '')))"

_SUMMARY = text(
    f"""
    SELECT count(*) AS total,
           count(*) FILTER (WHERE {_NORMALIZED} ~ :pattern) AS passing,
           min(length(btrim(angel_client_id))) AS min_len,
           max(length(btrim(angel_client_id))) AS max_len
      FROM users
    """
)

_SHAPES = text(
    f"""
    SELECT regexp_replace(regexp_replace({_NORMALIZED}, '[A-Z]', 'A', 'g'), '[0-9]', '9', 'g') AS shape,
           count(*) AS n,
           bool_and({_NORMALIZED} ~ :pattern) AS ok
      FROM users GROUP BY 1 ORDER BY 2 DESC
    """
)

_COLLISIONS = text(
    f"""
    SELECT count(*) FROM (
        SELECT {_NORMALIZED} FROM users GROUP BY 1 HAVING count(*) > 1
    ) d
    """
)

_MISSING_CREDENTIALS = text(
    """
    SELECT count(*) FROM users
     WHERE btrim(coalesce(angel_api_key, '')) = ''
        OR coalesce(angel_password_encrypted, '') = ''
        OR coalesce(angel_totp_secret_encrypted, '') = ''
    """
)


def check(conn) -> dict:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    summary = conn.execute(_SUMMARY, {"pattern": PATTERN}).one()
    shapes = [(r.shape, int(r.n), bool(r.ok)) for r in conn.execute(_SHAPES, {"pattern": PATTERN})]
    return {
        "total": int(summary.total),
        "passing": int(summary.passing),
        "min_len": summary.min_len,
        "max_len": summary.max_len,
        "shapes": shapes,
        "collisions": int(conn.execute(_COLLISIONS).scalar()),
        "missing_credentials": int(conn.execute(_MISSING_CREDENTIALS).scalar()),
    }


def passes(result: dict) -> bool:
    return result["passing"] == result["total"] and result["collisions"] == 0


def main() -> None:
    url = (os.environ.get("MIGRATION_DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("set MIGRATION_DATABASE_URL (read-only use)")
    connect_args = {"sslmode": "require"} if "sslmode=require" in url.lower() else {}
    with create_engine(url, poolclass=NullPool, connect_args=connect_args).connect() as conn:
        with conn.begin():
            result = check(conn)
    print(f"users: {result['total']}, client codes passing {PATTERN}: {result['passing']}")
    print(f"trimmed length: {result['min_len']}..{result['max_len']}")
    for shape, n, ok in result["shapes"]:
        print(f"  {'ok  ' if ok else 'FAIL'} {shape or '(empty)'}: {n}")
    print(f"codes shared by several users after normalising: {result['collisions']}")
    print(f"users missing a credential (the backfill skips them): {result['missing_credentials']}")
    if passes(result):
        print("PASS: revision 0003 can run")
        return
    print("FAIL: revise the pattern (or fix the data) before 0003")
    sys.exit(1)


if __name__ == "__main__":
    main()
