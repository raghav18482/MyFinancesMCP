"""Compare the app tables of two databases, and a database with the models.

::

    python scripts/schema_diff.py A_URL B_URL          # schema A vs schema B
    python scripts/schema_diff.py --models A_URL       # schema A vs db/models.py

Prints one line per difference and exits 1 if there are any. Only the app
tables (``migrations.helpers.APP_TABLES``, without ``db_identity``) are
compared; ADK's and Alembic's tables are ignored. Read-only.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migrations.compare import model_drift, schema_diff  # noqa: E402


def main() -> None:
    from sqlalchemy import create_engine

    ap = argparse.ArgumentParser(description="Compare app-table schemas.")
    ap.add_argument("--models", action="store_true", help="compare one database with the models")
    ap.add_argument("urls", nargs="+")
    args = ap.parse_args()
    if args.models:
        with create_engine(args.urls[0]).connect() as a:
            out = model_drift(a)
    else:
        with create_engine(args.urls[0]).connect() as a, create_engine(args.urls[1]).connect() as b:
            out = schema_diff(a, b)
    for line in out:
        print(line)
    sys.exit(1 if out else 0)


if __name__ == "__main__":
    main()
