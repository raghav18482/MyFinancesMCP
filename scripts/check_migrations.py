"""Static check of every revision in migrations/versions/. Fails closed.

A revision may only:

- import ``from alembic import op``, ``import sqlalchemy as sa``,
  ``from sqlalchemy.dialects import postgresql``, ``from migrations import
  helpers`` and ``uuid``;
- call the ``op`` functions in :data:`ALLOWED_OP` (directly: ``op.execute`` is
  never stored or passed around), ``sa.*`` and ``postgresql.*`` constructors,
  the :data:`ALLOWED_HELPERS` with a literal app-table name, ``uuid.uuid4``,
  ``str`` and ``ValueError``;
- pass SQL only as an inline string literal (to ``op.execute``, ``sa.text``,
  ``sa.CheckConstraint`` or ``op.create_check_constraint``), optionally
  ``sa.text(<literal>).bindparams(name=value)``. Every such literal's SHA-256
  must be listed in ``migrations/raw_sql_review.json``.

No other functions, classes or lambdas; no rebinding of ``op``, ``sa``,
``postgresql`` or ``helpers``; no ``getattr``, ``eval``, ``exec``,
``compile``, ``__import__``, ``importlib``, ``globals`` and the like; no
f-strings, ``+`` or ``.format`` building SQL. Any string (docstrings aside)
naming one of ADK's tables as a whole word fails, reviewed or not.

This is defence in depth, not proof: the proof that migrations leave ADK's
tables alone is the snapshot comparison in the rehearsal.

Usage::

    python scripts/check_migrations.py            # exit 1 with one line per problem
    python scripts/check_migrations.py --unreviewed   # print hashes still to review
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VERSIONS = REPO / "migrations" / "versions"
REVIEW_FILE = REPO / "migrations" / "raw_sql_review.json"

sys.path.insert(0, str(REPO))

ALLOWED_IMPORTS = {
    ("alembic", "op", None),
    ("sqlalchemy", None, "sa"),
    ("sqlalchemy.dialects", "postgresql", None),
    ("migrations", "helpers", None),
    ("uuid", None, None),
}
ALLOWED_OP = {
    "create_table", "add_column", "create_index", "create_unique_constraint",
    "create_foreign_key", "create_check_constraint", "alter_column", "execute", "f",
}
ALTER_COLUMN_KWARGS = {"nullable", "server_default", "existing_type", "existing_nullable",
                       "existing_server_default"}
ALLOWED_HELPERS = {"drop_table_if_empty", "drop_column_if_all_null", "x_argument"}
HELPER_CONSTANTS = {"LABELS", "APP_TABLES"}
TABLE_HELPERS = {"drop_table_if_empty", "drop_column_if_all_null"}
PROTECTED = {"op", "sa", "postgresql", "helpers", "uuid"}
BANNED_NAMES = {"getattr", "setattr", "delattr", "eval", "exec", "compile", "__import__",
                "importlib", "globals", "locals", "vars", "open", "__builtins__"}
PLAIN_CALLS = {"str", "ValueError"}
MODULE_NAMES = {"revision", "down_revision", "branch_labels", "depends_on", "EXPAND_ONLY"}
_ADK = re.compile(r"\b(adk_internal_metadata|sessions|events|app_states|user_states)\b", re.I)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _app_tables() -> tuple[str, ...]:
    from migrations.helpers import APP_TABLES

    return APP_TABLES


class Checker(ast.NodeVisitor):
    def __init__(self, path: Path, reviewed: dict):
        self.path = path
        self.rel = path.relative_to(REPO).as_posix()
        self.reviewed = reviewed
        self.problems: list[str] = []
        self.sql_literals: list[str] = []
        self._allowed_attr_nodes: set[int] = set()
        self._docstrings: set[int] = set()

    def fail(self, node, msg: str) -> None:
        self.problems.append(f"{self.rel}:{getattr(node, 'lineno', 0)}: {msg}")

    # ── structure ───────────────────────────────────────────────────────
    def check_module(self, tree: ast.Module) -> None:
        for body_owner in [tree] + [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            body = body_owner.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                self._docstrings.add(id(body[0].value))
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self.check_import(node)
            elif isinstance(node, ast.FunctionDef):
                if node.name not in ("upgrade", "downgrade"):
                    self.fail(node, f"function {node.name} is not allowed (only upgrade/downgrade)")
                if node.decorator_list:
                    self.fail(node, "decorators are not allowed")
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if len(names) != len(node.targets) or not set(names) <= MODULE_NAMES:
                    self.fail(node, "only revision metadata may be assigned at module level")
                if not isinstance(node.value, ast.Constant):
                    self.fail(node, "revision metadata must be a literal")
            elif isinstance(node, ast.Expr) and id(node.value) in self._docstrings:
                pass
            else:
                self.fail(node, f"{type(node).__name__} is not allowed at module level")
        self.visit(tree)

    def check_import(self, node) -> None:
        if isinstance(node, ast.Import):
            for a in node.names:
                key = (a.name, None, a.asname)
                if key not in ALLOWED_IMPORTS:
                    self.fail(node, f"import {a.name}{' as ' + a.asname if a.asname else ''} is not allowed")
        else:
            for a in node.names:
                key = (node.module, a.name, a.asname)
                if key not in ALLOWED_IMPORTS:
                    self.fail(node, f"from {node.module} import {a.name}"
                                    f"{' as ' + a.asname if a.asname else ''} is not allowed")

    def visit_FunctionDef(self, node):
        for inner in ast.walk(node):
            if inner is not node and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                       ast.Lambda, ast.ClassDef)):
                self.fail(inner, "nested functions, lambdas and classes are not allowed")
            if inner is not node and isinstance(inner, (ast.Import, ast.ImportFrom)):
                self.fail(inner, "imports inside functions are not allowed")
        for a in node.args.args + node.args.kwonlyargs:
            if a.arg in PROTECTED:
                self.fail(node, f"argument {a.arg} shadows a protected name")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.fail(node, "classes are not allowed")

    def visit_Lambda(self, node):
        self.fail(node, "lambdas are not allowed")
        self.generic_visit(node)

    def visit_Global(self, node):
        self.fail(node, "global is not allowed")

    visit_Nonlocal = visit_Global

    # ── rebinding ──────────────────────────────────────────────────────
    def _targets(self, node):
        for t in ast.walk(node):
            if isinstance(t, ast.Name) and isinstance(t.ctx, (ast.Store, ast.Del)) and t.id in PROTECTED:
                self.fail(t, f"{t.id} must not be rebound")

    def visit_Assign(self, node):
        for t in node.targets:
            self._targets(t)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._targets(node.target)
        self.generic_visit(node)

    visit_AnnAssign = visit_AugAssign

    def visit_For(self, node):
        self._targets(node.target)
        self.generic_visit(node)

    def visit_With(self, node):
        for item in node.items:
            if item.optional_vars is not None:
                self._targets(item.optional_vars)
        self.generic_visit(node)

    def visit_NamedExpr(self, node):
        self._targets(node.target)
        self.generic_visit(node)

    def visit_Delete(self, node):
        for t in node.targets:
            self._targets(t)
        self.generic_visit(node)

    # ── names and strings ─────────────────────────────────────────────
    def visit_Name(self, node):
        if node.id in BANNED_NAMES:
            self.fail(node, f"{node.id} is not allowed")

    def visit_JoinedStr(self, node):
        self.fail(node, "f-strings are not allowed")

    def visit_Constant(self, node):
        if isinstance(node.value, str) and id(node) not in self._docstrings and _ADK.search(node.value):
            self.fail(node, "string names an ADK table")

    def visit_BinOp(self, node):
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and isinstance(side.value, str):
                self.fail(node, "strings must not be built with operators")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if id(node) in self._allowed_attr_nodes:
            self.generic_visit(node)
            return
        base = node.value
        if isinstance(base, ast.Name) and base.id == "op":
            self.fail(node, f"op.{node.attr} may only be called directly")
        elif isinstance(base, ast.Name) and base.id == "helpers":
            if node.attr not in HELPER_CONSTANTS:
                self.fail(node, f"helpers.{node.attr} may only be called directly")
        elif isinstance(base, ast.Name) and base.id == "uuid":
            self.fail(node, f"uuid.{node.attr} is not allowed")
        if node.attr in ("format", "__dict__", "__class__", "__globals__", "__builtins__"):
            self.fail(node, f".{node.attr} is not allowed")
        self.generic_visit(node)

    # ── calls ─────────────────────────────────────────────────────────
    def _sql_literal(self, node, where: str) -> None:
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            self.fail(node, f"{where} needs an inline string literal")
            return
        self.sql_literals.append(node.value)
        if _sha(node.value) not in self.reviewed:
            self.fail(node, f"unreviewed SQL literal {_sha(node.value)[:16]}… in {where}")

    def _is_sa_text(self, node) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "sa"
                and node.func.attr == "text")

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in PLAIN_CALLS:
                self.fail(node, f"call to {func.id} is not allowed")
        elif isinstance(func, ast.Attribute):
            base = func.value
            self._allowed_attr_nodes.add(id(func))
            if isinstance(base, ast.Name) and base.id == "op":
                self.check_op_call(node, func.attr)
            elif isinstance(base, ast.Name) and base.id == "helpers":
                self.check_helper_call(node, func.attr)
            elif isinstance(base, ast.Name) and base.id == "uuid":
                if func.attr != "uuid4":
                    self.fail(node, f"uuid.{func.attr} is not allowed")
            elif isinstance(base, ast.Name) and base.id in ("sa", "postgresql"):
                if func.attr == "text":
                    if len(node.args) != 1 or node.keywords:
                        self.fail(node, "sa.text takes exactly one literal")
                    else:
                        self._sql_literal(node.args[0], "sa.text")
                elif func.attr == "CheckConstraint":
                    if node.args:
                        self._sql_literal(node.args[0], "sa.CheckConstraint")
                    else:
                        self.fail(node, "sa.CheckConstraint needs its condition first")
            elif func.attr == "bindparams" and self._is_sa_text(base):
                if node.args:
                    self.fail(node, "bindparams takes keyword arguments only")
            else:
                self.fail(node, f"call to .{func.attr} is not allowed")
        else:
            self.fail(node, "calls must name what they call")
        self.generic_visit(node)

    def check_op_call(self, node, attr: str) -> None:
        if attr not in ALLOWED_OP:
            self.fail(node, f"op.{attr} is not allowed")
            return
        if attr == "execute":
            if len(node.args) != 1 or node.keywords:
                self.fail(node, "op.execute takes exactly one argument")
                return
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                self._sql_literal(arg, "op.execute")
            elif self._is_sa_text(arg):
                pass  # the literal is checked when sa.text is visited
            elif (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                  and arg.func.attr == "bindparams" and self._is_sa_text(arg.func.value)):
                pass
            else:
                self.fail(node, "op.execute needs a literal or sa.text(<literal>)[.bindparams(...)]")
        elif attr == "alter_column":
            extra = {k.arg for k in node.keywords} - ALTER_COLUMN_KWARGS
            if extra:
                self.fail(node, f"op.alter_column may not change {sorted(extra)}")
        elif attr == "create_check_constraint":
            if len(node.args) >= 3:
                self._sql_literal(node.args[2], "op.create_check_constraint")
            else:
                self.fail(node, "op.create_check_constraint needs (name, table, condition)")

    def check_helper_call(self, node, attr: str) -> None:
        if attr not in ALLOWED_HELPERS:
            self.fail(node, f"helpers.{attr} is not allowed")
            return
        if attr in TABLE_HELPERS:
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Constant) and first.value in _app_tables()):
                self.fail(node, f"helpers.{attr} needs a literal app-table name")
            for a in node.args[1:]:
                if not isinstance(a, ast.Constant):
                    self.fail(node, f"helpers.{attr} takes literal arguments")


def check_file(path: Path, reviewed: dict) -> Checker:
    checker = Checker(path, reviewed)
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        checker.fail(e, f"does not parse: {e.msg}")
        return checker
    checker.check_module(tree)
    return checker


def load_reviewed(path: Path = REVIEW_FILE) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def check_all(versions: Path = VERSIONS, reviewed: dict | None = None) -> tuple[list[str], list[str]]:
    reviewed = load_reviewed() if reviewed is None else reviewed
    problems, literals = [], []
    files = sorted(p for p in versions.glob("*.py") if p.name != "__init__.py")
    if not files:
        problems.append(f"{versions}: no revisions found")
    for path in files:
        c = check_file(path, reviewed)
        problems += c.problems
        literals += c.sql_literals
    return problems, literals


def main() -> None:
    ap = argparse.ArgumentParser(description="Static check of migration revisions.")
    ap.add_argument("--unreviewed", action="store_true",
                    help="print the SHA-256 and text of SQL literals not yet reviewed")
    args = ap.parse_args()
    problems, literals = check_all()
    if args.unreviewed:
        reviewed = load_reviewed()
        for lit in literals:
            if _sha(lit) not in reviewed:
                print(json.dumps({"sha256": _sha(lit), "sql": lit}))
        return
    for p in problems:
        print(p)
    if problems:
        sys.exit(1)
    print(f"ok: {len(list(VERSIONS.glob('*.py')))} revisions, {len(literals)} reviewed SQL literals")


if __name__ == "__main__":
    os.chdir(REPO)
    main()
