#!/usr/bin/env bash
# Run the PostgreSQL-only tests (tests/*_pg.py) against a throwaway server.
#
# Starts a disposable postgres container bound to 127.0.0.1 on a random port,
# exports TEST_POSTGRES_URL, runs every tests/*_pg.py script, and removes the
# container on exit, whatever happened. Nothing here can reach a real database:
# DATABASE_URL is unset for the test processes.
#
# Usage:  scripts/test_pg.sh [tests/test_x_pg.py ...]
# Env:    PG_IMAGE (default postgres:16), PYTHON (default: repo .venv, else python3)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${PG_IMAGE:-postgres:16}"

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
else
  PY="python3"
fi

command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker daemon is not running" >&2; exit 2; }

NAME="myfinances-test-pg-$$"
PASSWORD="$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --rm --name "$NAME" \
  -e POSTGRES_PASSWORD="$PASSWORD" \
  -p 127.0.0.1::5432 \
  "$IMAGE" >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U postgres -q 2>/dev/null; then break; fi
  sleep 1
done
docker exec "$NAME" pg_isready -U postgres -q || { echo "postgres did not become ready" >&2; exit 1; }

PORT="$(docker port "$NAME" 5432/tcp | head -1 | sed 's/.*://')"
export TEST_POSTGRES_URL="postgresql+psycopg://postgres:${PASSWORD}@127.0.0.1:${PORT}/postgres"

if [[ $# -gt 0 ]]; then
  TESTS=("$@")
else
  TESTS=()
  for f in "$REPO"/tests/*_pg.py; do [[ -e "$f" ]] && TESTS+=("$f"); done
fi

# Smoke check: the harness itself can connect.
env -u DATABASE_URL "$PY" - <<'EOF'
import os
from sqlalchemy import create_engine, text
with create_engine(os.environ["TEST_POSTGRES_URL"]).connect() as c:
    print("connected:", c.execute(text("SHOW server_version")).scalar())
EOF

if [[ ${#TESTS[@]} -eq 0 ]]; then
  echo "no tests/*_pg.py found; harness check only"
  exit 0
fi

failed=0
for t in "${TESTS[@]}"; do
  echo "== $t"
  if ! (cd "$REPO" && env -u DATABASE_URL "$PY" "$t"); then
    failed=$((failed + 1))
  fi
done

echo "pg tests: $(( ${#TESTS[@]} - failed )) passed, $failed failed"
[[ $failed -eq 0 ]]
