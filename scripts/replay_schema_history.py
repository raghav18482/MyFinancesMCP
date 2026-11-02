"""Build a stand-in for the production schema by replaying the schema history.

Production was never created from one model: each deploy ran ``create_all``
(new tables only) and then that release's ``ALTER TABLE … ADD COLUMN``
statements. This script repeats that on an empty database, one commit at a
time, oldest first, for every commit that touched ``db/models.py`` or
``db/engine.py`` up to ``--until`` (default ``HEAD``).

It is a **proxy**. Two things it cannot know:

- which commits were actually deployed (it assumes every one was);
- the SQLModel version each deploy ran (``requirements.txt`` left it unpinned
  until October 2026, and older versions mapped ``datetime`` to
  ``timestamp without time zone``). It uses the installed version.

The real ``pg_dump`` of production replaces it (``docs/db/baseline-drift.md``).

Usage::

    python scripts/replay_schema_history.py --url postgresql+psycopg://… [--until REV]

The target database's ``public`` schema is dropped first, so the URL must name
a throwaway database: anything but localhost is refused.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ALTER = re.compile(r'"(ALTER TABLE [^"]+)"')

_CHILD = r'''
import importlib.util, sys
from sqlalchemy import create_engine, text
spec = importlib.util.spec_from_file_location("replayed_models", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["replayed_models"] = mod  # postponed annotations resolve here
spec.loader.exec_module(mod)
from sqlmodel import SQLModel
engine = create_engine(sys.argv[2])
with engine.begin() as c:
    SQLModel.metadata.create_all(c)
    for stmt in sys.argv[3:]:
        c.execute(text(stmt))
'''


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout


def schema_commits(until: str) -> list[str]:
    out = _git("log", "--reverse", "--format=%H", until, "--", "db/models.py", "db/engine.py")
    return [c for c in out.split() if c]


def alter_statements(engine_src: str) -> list[str]:
    return _ALTER.findall(engine_src)


def _show(commit: str, path: str) -> str | None:
    try:
        return _git("show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None


def replay(url: str, until: str = "HEAD", python: str = sys.executable) -> list[str]:
    host = urlsplit(url.replace("+psycopg", "")).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit("refusing: --url must point at a local throwaway database")
    from sqlalchemy import create_engine, text

    with create_engine(url).begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    applied = []
    with tempfile.TemporaryDirectory() as tmp:
        for commit in schema_commits(until):
            models = _show(commit, "db/models.py")
            if models is None:
                continue
            path = os.path.join(tmp, f"models_{commit[:12]}.py")
            with open(path, "w") as f:
                f.write(models)
            alters = alter_statements(_show(commit, "db/engine.py") or "")
            subprocess.run([python, "-I", "-c", _CHILD, path, url, *alters], check=True)
            applied.append(commit[:12])
    return applied


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--until", default="HEAD")
    args = ap.parse_args()
    for c in replay(args.url, args.until):
        print("replayed", c)


if __name__ == "__main__":
    main()
