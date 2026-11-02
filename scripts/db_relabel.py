"""Change a database's ``db_identity`` label, deliberately.

::

    python scripts/db_relabel.py --url … --from rehearsal --to development --confirm <db_identity.id>

The guard trigger accepts the change only with ``app.db_identity_relabel`` set
to ``<id>:<from>:<to>`` in the same transaction, which this script does after
checking ``--confirm`` against the row. Nothing relabels a database to
``production``; only the baseline stamp writes that label.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from migrations.helpers import LABELS  # noqa: E402


class RelabelRefused(RuntimeError):
    pass


def relabel(engine, *, old: str, new: str, confirm: str) -> None:
    from sqlalchemy import text

    if new == "production":
        raise RelabelRefused("no database is relabelled production")
    if old not in LABELS or new not in LABELS:
        raise RelabelRefused(f"labels are {', '.join(LABELS)}")
    with engine.begin() as conn:
        row = conn.execute(text("SELECT id::text, label FROM db_identity FOR UPDATE")).one()
        if row.id != confirm:
            raise RelabelRefused("--confirm does not match this database's id")
        if row.label != old:
            raise RelabelRefused(f"this database is labelled {row.label!r}, not {old!r}")
        conn.execute(text("SELECT set_config('app.db_identity_relabel', :v, true)"),
                     {"v": f"{row.id}:{old}:{new}"})
        conn.execute(text("UPDATE db_identity SET label = :new"), {"new": new})


def main() -> None:
    from sqlalchemy import create_engine

    ap = argparse.ArgumentParser(description="Relabel db_identity.")
    ap.add_argument("--url", required=True)
    ap.add_argument("--from", dest="old", required=True)
    ap.add_argument("--to", dest="new", required=True)
    ap.add_argument("--confirm", required=True, help="the database's db_identity.id")
    args = ap.parse_args()
    relabel(create_engine(args.url), old=args.old, new=args.new, confirm=args.confirm)
    print(f"relabelled {args.old} -> {args.new}")


if __name__ == "__main__":
    main()
