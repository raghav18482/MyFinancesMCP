"""scripts/check_migrations.py: every way around it fails, the real revisions pass.

Run directly: ``python tests/test_check_migrations.py``.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import check_migrations as cm  # noqa: E402

HEADER = '''"""Fixture."""
from alembic import op
import sqlalchemy as sa

from migrations import helpers

revision = "x"
down_revision = None
branch_labels = None
depends_on = None
EXPAND_ONLY = True
'''

GOOD_SQL = "CREATE INDEX ix_fixture ON users (id)"


def _reviewed(*literals: str) -> dict:
    return {hashlib.sha256(s.encode()).hexdigest(): {"reviewer": "test"} for s in literals}


def _check(body: str, *, header: str = HEADER, reviewed: dict | None = None) -> list[str]:
    source = header + "\n\n" + textwrap.dedent(body)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "rev.py"
        path.write_text(source)
        old_repo = cm.REPO
        cm.REPO = root
        try:
            return cm.check_file(path, _reviewed(GOOD_SQL) if reviewed is None else reviewed).problems
        finally:
            cm.REPO = old_repo


def _fails(body: str, needle: str, **kw) -> None:
    problems = _check(body, **kw)
    assert problems, f"expected a failure for:\n{body}"
    assert any(needle in p for p in problems), (needle, problems)


def test_the_real_revisions_pass():
    problems, literals = cm.check_all()
    assert problems == [], problems
    assert literals, "the revisions carry reviewed SQL"


def test_a_reviewed_literal_passes():
    assert _check(f'def upgrade():\n    op.execute("{GOOD_SQL}")\n') == []


def test_an_unreviewed_literal_fails():
    _fails('def upgrade():\n    op.execute("DROP TABLE users")\n', "unreviewed SQL literal")


def test_aliasing_op_execute_fails():
    _fails('def upgrade():\n    e = op.execute\n    e("x")\n', "may only be called directly")


def test_import_aliases_and_unknown_imports_fail():
    _fails("def upgrade():\n    pass\n", "is not allowed",
           header=HEADER.replace("from alembic import op", "from alembic import op as o"))
    _fails("def upgrade():\n    pass\n", "is not allowed", header=HEADER + "import os\n")
    _fails("def upgrade():\n    import os\n", "imports inside functions")


def test_getattr_and_friends_fail():
    _fails('def upgrade():\n    getattr(op, "execute")("x")\n', "getattr is not allowed")
    _fails('def upgrade():\n    eval("1")\n', "eval is not allowed")
    _fails('def upgrade():\n    globals()["op"]\n', "globals is not allowed")


def test_a_local_sql_helper_fails():
    _fails('def _sql():\n    return "x"\n\ndef upgrade():\n    pass\n', "function _sql is not allowed")
    _fails('def upgrade():\n    f = lambda: 1\n', "lambdas are not allowed")


def test_built_sql_fails():
    _fails('def upgrade():\n    t = "users"\n    op.execute(f"DROP TABLE {t}")\n', "f-strings")
    _fails('def upgrade():\n    op.execute("DROP TABLE " + "users")\n', "built with operators")
    _fails('def upgrade():\n    op.execute("DROP TABLE {}".format("users"))\n', "needs a literal")
    _fails('def upgrade():\n    q = "x"\n    op.execute(sa.text(q))\n', "inline string literal")


def test_get_bind_and_unknown_op_calls_fail():
    _fails("def upgrade():\n    op.get_bind()\n", "op.get_bind is not allowed")
    _fails("def upgrade():\n    op.get_context()\n", "op.get_context is not allowed")
    _fails('def upgrade():\n    op.drop_table("users")\n', "op.drop_table is not allowed")
    _fails('def upgrade():\n    op.alter_column("users", "id", type_=sa.BIGINT())\n', "may not change")


def test_rebinding_protected_names_fails():
    _fails("def upgrade():\n    op = None\n", "op must not be rebound")
    _fails("def upgrade():\n    for sa in []:\n        pass\n", "sa must not be rebound")


def test_adk_tables_are_off_limits_even_when_reviewed():
    sql = "DELETE FROM events"
    _fails(f'def upgrade():\n    op.execute("{sql}")\n', "names an ADK table", reviewed=_reviewed(sql))
    _fails('def downgrade():\n    helpers.drop_table_if_empty("sessions")\n', "names an ADK table")


def test_helpers_need_a_literal_app_table():
    _fails('def downgrade():\n    t = "users"\n    helpers.drop_table_if_empty(t)\n', "literal app-table name")
    _fails('def downgrade():\n    helpers.drop_table_if_empty("pg_class")\n', "literal app-table name")
    _fails("def downgrade():\n    helpers._require_app_table('users')\n", "is not allowed")
    assert _check('def downgrade():\n    helpers.drop_table_if_empty("users")\n') == []


def test_module_level_code_fails():
    _fails('op.execute("x")\n', "not allowed at module level")
    _fails("revision = make()\n", "revision metadata must be a literal")


def test_bindparams_on_a_reviewed_text_passes():
    sql = "INSERT INTO users (id) VALUES (:id)"
    assert _check(f'def upgrade():\n    op.execute(sa.text("{sql}").bindparams(id=1))\n',
                  reviewed=_reviewed(sql)) == []


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
