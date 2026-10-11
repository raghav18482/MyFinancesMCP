"""Crash-safe, serialised writes for the parquet partitions under ``data/``.

Every store here read-modify-writes one ``part.parquet`` per partition. Writing
straight over that file truncates it first, so a process killed mid-write (an
interrupted backfill, a server restart) leaves a file nobody can read — and
with year partitions that is a whole year of history.

Both helpers keep their side files next to the partition but never under the
name ``part.parquet``, so readers that open exactly that name never see them.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from typing import Iterator

import pandas as pd

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: thread lock only
    fcntl = None


_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


@contextlib.contextmanager
def partition_lock(path: str) -> Iterator[None]:
    """Exclusive access to one partition, across threads and processes.

    The thread lock covers concurrent requests inside one server. The flock on
    a sidecar file covers a second process (the stdio MCP server alongside the
    web app, or a backfill) writing the same data directory. The sidecar is
    never replaced, so every writer locks the same inode.
    """
    key = os.path.abspath(path)
    with _thread_locks_guard:
        lock = _thread_locks.setdefault(key, threading.Lock())
    with lock:
        if fcntl is None:
            yield
            return
        with open(key + ".lock", "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def atomic_write_parquet(df: pd.DataFrame, path: str) -> None:
    """Write ``df`` to ``path`` so readers see the old file or the new one, never half.

    The temp file sits in the same directory so ``os.replace`` is a rename on
    one filesystem, which is atomic. Its name is ``.part-*.tmp``, so a leftover
    from a crash is never mistaken for a partition.
    """
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".part-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            df.to_parquet(fh, index=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; keep the partition's usual mode
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
