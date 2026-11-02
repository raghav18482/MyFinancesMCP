"""A normalized description of a PostgreSQL schema, and its hash.

:func:`catalog` reads the system catalogs of the current schema: every table's
columns (type, nullability, default, identity), constraints, indexes and
triggers, plus sequences, functions, views, enums, extensions and the list of
schemas. Two databases with equal catalogs have the same schema.

Column *order* is kept but attribute numbers are not, so a production table
with dropped columns compares equal to a fresh restore of its dump. CHECK
constraints are compared without casts and parentheses (:func:`normalize_check`).

Used by ``scripts/schema_diff.py`` (compare two databases) and by the baseline
stamp (the live database must match the rehearsed one exactly).
"""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import text

_TABLES = """
SELECT c.relname
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')
"""

_COLUMNS = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated, a.attnum
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')
   AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY c.relname, a.attnum
"""

_CONSTRAINTS = """
SELECT c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid)
  FROM pg_constraint k
  JOIN pg_class c ON c.oid = k.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema()
"""

_INDEXES = """
SELECT t.relname, i.relname, pg_get_indexdef(i.oid)
  FROM pg_index x
  JOIN pg_class i ON i.oid = x.indexrelid
  JOIN pg_class t ON t.oid = x.indrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname = current_schema()
"""

_TRIGGERS = """
SELECT c.relname, g.tgname, pg_get_triggerdef(g.oid)
  FROM pg_trigger g
  JOIN pg_class c ON c.oid = g.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema() AND NOT g.tgisinternal
"""

_SEQUENCES = """
SELECT s.relname, format_type(q.seqtypid, NULL), q.seqincrement, q.seqmin, q.seqmax,
       q.seqcycle, oc.relname, oa.attname
  FROM pg_sequence q
  JOIN pg_class s ON s.oid = q.seqrelid
  JOIN pg_namespace n ON n.oid = s.relnamespace
  LEFT JOIN pg_depend d ON d.objid = s.oid AND d.classid = 'pg_class'::regclass
                       AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
  LEFT JOIN pg_class oc ON oc.oid = d.refobjid
  LEFT JOIN pg_attribute oa ON oa.attrelid = d.refobjid AND oa.attnum = d.refobjsubid
 WHERE n.nspname = current_schema()
"""

_FUNCTIONS = """
SELECT p.proname, pg_get_function_identity_arguments(p.oid), md5(pg_get_functiondef(p.oid))
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = current_schema() AND p.prokind IN ('f', 'p')
"""

_VIEWS = """
SELECT c.relname, c.relkind, md5(pg_get_viewdef(c.oid))
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema() AND c.relkind IN ('v', 'm')
"""

_ENUMS = """
SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
  FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
  JOIN pg_namespace n ON n.oid = t.typnamespace
 WHERE n.nspname = current_schema()
 GROUP BY t.typname
"""

_EXTENSIONS = "SELECT extname FROM pg_extension"

_SCHEMAS = """
SELECT nspname FROM pg_namespace
 WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'
"""


_CASTS = re.compile(r"::(?:character varying|text|integer|bigint|numeric|boolean)(?:\[\])?")


def normalize_check(definition: str) -> str:
    """A CHECK constraint's text without casts, parentheses or spaces.

    PostgreSQL does not round-trip these exactly: ``channel IN ('a', 'b')``
    created by ``create_all`` and the same constraint restored from a
    ``pg_dump`` deparse with casts in different places. Without this a
    restored dump would never match the live database it came from.
    """
    return re.sub(r"[()\s]", "", _CASTS.sub("", definition))


def _rows(conn, sql: str) -> list[list]:
    return [list(r) for r in conn.execute(text(sql))]


def catalog(conn, tables: tuple[str, ...] | None = None) -> dict:
    """The schema as plain, sorted data. ``tables`` limits the per-table parts."""
    keep = (lambda name: True) if tables is None else (lambda name: name in tables)

    result: dict = {"tables": {}}
    for (name,) in _rows(conn, _TABLES):
        if keep(name):
            result["tables"][name] = {"columns": [], "constraints": [], "indexes": [], "triggers": []}

    for rel, col, typ, notnull, default, identity, generated, _num in _rows(conn, _COLUMNS):
        if rel in result["tables"]:
            result["tables"][rel]["columns"].append([col, typ, notnull, default, identity, generated])
    for rel, name, kind, definition in _rows(conn, _CONSTRAINTS):
        if rel in result["tables"]:
            if kind == "c":
                definition = normalize_check(definition)
            result["tables"][rel]["constraints"].append([name, kind, definition])
    for rel, name, definition in _rows(conn, _INDEXES):
        if rel in result["tables"]:
            result["tables"][rel]["indexes"].append([name, definition])
    for rel, name, definition in _rows(conn, _TRIGGERS):
        if rel in result["tables"]:
            result["tables"][rel]["triggers"].append([name, definition])
    for t in result["tables"].values():
        for part in ("constraints", "indexes", "triggers"):
            t[part].sort()

    if tables is None:
        result["sequences"] = sorted(_rows(conn, _SEQUENCES), key=repr)
        result["functions"] = sorted(_rows(conn, _FUNCTIONS))
        result["views"] = sorted(_rows(conn, _VIEWS))
        result["enums"] = sorted(_rows(conn, _ENUMS))
        result["extensions"] = sorted(r[0] for r in _rows(conn, _EXTENSIONS))
        result["schemas"] = sorted(r[0] for r in _rows(conn, _SCHEMAS))
    else:
        result["sequences"] = sorted(
            (r for r in _rows(conn, _SEQUENCES) if r[6] is None or keep(r[6])), key=repr
        )
    return json.loads(json.dumps(result, default=str))


def fingerprint(conn, tables: tuple[str, ...] | None = None) -> str:
    """SHA-256 of :func:`catalog`, stable across runs."""
    blob = json.dumps(catalog(conn, tables), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def diff(a: dict, b: dict) -> list[str]:
    """Human-readable differences between two catalogs, ``a`` first."""
    out: list[str] = []
    ta, tb = a.get("tables", {}), b.get("tables", {})
    for name in sorted(set(ta) | set(tb)):
        if name not in tb:
            out.append(f"table {name}: only in A")
            continue
        if name not in ta:
            out.append(f"table {name}: only in B")
            continue
        ca = {c[0]: c for c in ta[name]["columns"]}
        cb = {c[0]: c for c in tb[name]["columns"]}
        for col in sorted(set(ca) | set(cb)):
            if ca.get(col) != cb.get(col):
                out.append(f"column {name}.{col}: A={ca.get(col)} B={cb.get(col)}")
        if [c[0] for c in ta[name]["columns"] if c[0] in cb] != [c[0] for c in tb[name]["columns"] if c[0] in ca]:
            out.append(f"table {name}: column order differs")
        for part in ("constraints", "indexes", "triggers"):
            pa = {tuple(x) for x in map(tuple, ta[name][part])}
            pb = {tuple(x) for x in map(tuple, tb[name][part])}
            for x in sorted(pa - pb):
                out.append(f"{part[:-1]} {name}: only in A: {list(x)}")
            for x in sorted(pb - pa):
                out.append(f"{part[:-1]} {name}: only in B: {list(x)}")
    for key in ("sequences", "functions", "views", "enums", "extensions", "schemas"):
        if a.get(key) != b.get(key):
            sa_, sb_ = {json.dumps(x) for x in a.get(key) or []}, {json.dumps(x) for x in b.get(key) or []}
            for x in sorted(sa_ - sb_):
                out.append(f"{key}: only in A: {x}")
            for x in sorted(sb_ - sa_):
                out.append(f"{key}: only in B: {x}")
    return out


def table_snapshot(conn, tables: tuple[str, ...]) -> dict:
    """Per table: its catalog entry, row count and an MD5 of its rows in order.

    Used to prove a migration run left ADK's tables exactly as they were.
    Missing tables are recorded as ``None``.
    """
    cat = catalog(conn, tables)["tables"]
    out = {}
    for name in tables:
        if name not in cat:
            out[name] = None
            continue
        count, digest = conn.execute(text(
            f'SELECT count(*), md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) '
            f'FROM "{name}" t'
        )).one()
        out[name] = {"definition": cat[name], "rows": count, "md5": digest}
    return out
