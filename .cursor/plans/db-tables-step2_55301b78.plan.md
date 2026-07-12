---
name: db-tables-step2
overview: Add a minimal SQLModel-on-Postgres persistence layer with three tables (User, Schedule, Log), Fernet-encrypted credentials, a shared engine, and tables auto-created on app startup. No repository pattern, no Alembic yet, just enough to store who needs what and when.
todos:
  - id: deps
    content: Add sqlmodel, psycopg[binary], cryptography to requirements.txt
    status: completed
  - id: envexample
    content: Add DATABASE_URL and ENCRYPTION_KEY (with key-generation hint) to .env.example
    status: completed
  - id: crypto_module
    content: Create db/crypto.py with Fernet encrypt_value / decrypt_value and a __main__ 'generate' command
    status: completed
  - id: engine_module
    content: Create db/engine.py with shared engine, get_session, and init_db (calls SQLModel.metadata.create_all)
    status: completed
  - id: models_module
    content: Create db/models.py with User, Schedule (index on next_run), and Log SQLModel tables
    status: completed
  - id: package_init
    content: Create db/__init__.py that re-exports engine, get_session, init_db, models, and crypto helpers
    status: completed
  - id: lifespan_wire
    content: Call init_db() once inside main.py _lifespan startup block
    status: completed
  - id: smoke_test
    content: "Run the 4-step verification: generate key, init_db, insert User+Schedule, round-trip decrypt"
    status: completed
isProject: false
---

## Goal

> "I can store who needs what and when."

Three tables, encrypted secrets, one engine, tables created on startup. No repository pattern, no Alembic yet (but structured so it slots in cleanly).

## Architecture

```mermaid
flowchart LR
    env[".env\nDATABASE_URL\nENCRYPTION_KEY"] --> engine[db.engine.engine\nSQLModel + psycopg]
    engine --> initdb[init_db on app startup\nSQLModel.metadata.create_all]
    initdb --> tables[(users / schedules / logs)]
    crypto[db.crypto.encrypt_value\ndecrypt_value\nFernet] --> users[users\npassword + totp encrypted]
    sched[scheduler later phase] --> users
    sched --> schedules
    sched --> logs
```

## Files to add

- New folder [db/](db/) with:
  - [db/__init__.py](db/__init__.py) - re-exports `engine`, `get_session`, `init_db`, models, crypto helpers.
  - [db/engine.py](db/engine.py) - reads `DATABASE_URL`, builds the shared `engine`, exposes `get_session()` and `init_db()`.
  - [db/crypto.py](db/crypto.py) - Fernet `encrypt_value` / `decrypt_value` reading `ENCRYPTION_KEY` from env, plus a `__main__` that prints a fresh key for `.env` setup.
  - [db/models.py](db/models.py) - `User`, `Schedule`, `Log` SQLModel classes.

## Files to change

- [requirements.txt](requirements.txt) - append `sqlmodel`, `psycopg[binary]`, `cryptography`.
- [.env.example](.env.example) - add `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/myfinance` and `ENCRYPTION_KEY=` (with a comment showing how to generate it).
- [main.py](main.py) - call `init_db()` once inside the existing `_lifespan` startup block (right where the cleanup task is created, around [main.py:43](main.py#L43)).

That's it. No changes to `web_app.py`, `mcp_server.py`, `session_manager.py`, or `services/daily_briefing.py` in this step. (Wiring those up to Postgres is Step 3.)

## Schema

### `users`

```python
class User(SQLModel, table=True):
    __tablename__ = "users"
    id: int | None = Field(default=None, primary_key=True)
    whatsapp_number: str | None = Field(default=None, index=True, unique=True)
    angel_api_key: str
    angel_client_id: str = Field(index=True, unique=True)
    angel_password_encrypted: str
    angel_totp_secret_encrypted: str
    angel_access_token: str | None = None
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

- `angel_api_key` and `angel_client_id` stay plaintext (per your spec).
- `angel_password_encrypted` and `angel_totp_secret_encrypted` hold Fernet ciphertext (a base64 string).
- `angel_access_token` left as plain TEXT - it's short-lived and gets refreshed; encryption is optional and we skip it for simplicity.

### `schedules`

```python
class Schedule(SQLModel, table=True):
    __tablename__ = "schedules"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    kind: str = Field(default="daily_briefing")          # forward-compat for other job types
    interval_minutes: int                                # 5 -> every 5 min, 1440 -> daily
    next_run: datetime = Field(index=True)               # the index you asked for
    last_run: datetime | None = None
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

The single scheduler loop (Step 3+) will simply do:

```python
session.exec(
    select(Schedule).where(Schedule.enabled == True, Schedule.next_run <= utcnow())
)
```

The `(enabled, next_run)` access pattern is exactly what your `index=True` on `next_run` supports.

### `logs`

```python
class Log(SQLModel, table=True):
    __tablename__ = "logs"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    schedule_id: int | None = Field(default=None, foreign_key="schedules.id")
    status: str                                          # "success" | "failed" | "skipped"
    message: str | None = None                           # WhatsApp text on success, error on failure
    duration_ms: int | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
```

Plain text `status` (not an enum) keeps migration painless.

## Encryption helpers - [db/crypto.py](db/crypto.py)

```python
import os, base64
from cryptography.fernet import Fernet, InvalidToken

def _fernet() -> Fernet:
    key = (os.environ.get("ENCRYPTION_KEY") or "").strip()
    if not key:
        raise RuntimeError("ENCRYPTION_KEY missing. Generate one with: python -m db.crypto generate")
    return Fernet(key.encode() if isinstance(key, str) else key)

def encrypt_value(raw: str) -> str:
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii")

def decrypt_value(enc: str) -> str:
    return _fernet().decrypt(enc.encode("ascii")).decode("utf-8")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        print(Fernet.generate_key().decode())
```

- `encrypt_value` / `decrypt_value` are the only two helpers exposed - matches your spec.
- Decryption only happens when building an `AngelOneClient` (Step 3). The decrypted string is never logged.
- `python -m db.crypto generate` prints a fresh Fernet key for `.env`.

## Engine + startup - [db/engine.py](db/engine.py)

```python
import os
from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/myfinance")
engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

def init_db() -> None:
    """Create all tables. Idempotent. Replace with Alembic when schema starts evolving."""
    from . import models  # noqa: F401  - ensures models are imported before create_all
    SQLModel.metadata.create_all(engine)

def get_session() -> Session:
    return Session(engine)
```

- One shared `engine`. No connection pool tuning yet.
- `pool_pre_ping=True` so a stale Postgres connection doesn't kill the scheduler later.
- `init_db` is idempotent - running it on every startup is fine.

## Startup wiring - [main.py](main.py)

Inside the existing `_lifespan` (currently around [main.py:40-51](main.py#L40)), add one line before the cleanup task is started:

```python
@asynccontextmanager
async def _lifespan(_app: Starlette):
    from db import init_db
    init_db()
    task = asyncio.create_task(_cleanup_loop())
    ...
```

Lazy import avoids importing SQLModel at module load if someone runs a script that doesn't need the DB.

## .env additions

```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/myfinance
ENCRYPTION_KEY=<paste output of: python -m db.crypto generate>
```

I'll add these to `.env.example` only. You'll paste the real values into `.env` yourself (since editing your live `.env` from a tool is risky given the existing TODO comment block in there).

## Verification

A small smoke test (run by hand once Postgres is up):

```bash
.venv/bin/python -m db.crypto generate                    # paste output into .env as ENCRYPTION_KEY
.venv/bin/python -c "from db import init_db; init_db()"   # creates the 3 tables
psql $DATABASE_URL -c "\dt"                               # should show users / schedules / logs
.venv/bin/python -c "
from datetime import datetime, timedelta
from sqlmodel import select
from db import get_session
from db.models import User, Schedule
from db.crypto import encrypt_value, decrypt_value
with get_session() as s:
    u = User(
        whatsapp_number='+919999999999',
        angel_api_key='Z0hsKZYf',
        angel_client_id='PPSU16696',
        angel_password_encrypted=encrypt_value('8107'),
        angel_totp_secret_encrypted=encrypt_value('FP4O3EVUYXZ5Q3WTF6I7JR772Y'),
    )
    s.add(u); s.commit(); s.refresh(u)
    sch = Schedule(user_id=u.id, interval_minutes=1440, next_run=datetime.utcnow()+timedelta(hours=1))
    s.add(sch); s.commit(); s.refresh(sch)
    print('user', u.id, 'schedule', sch.id, 'next_run', sch.next_run)
    print('decrypt round-trip:', decrypt_value(u.angel_password_encrypted))
"
```

Pass criteria: 3 tables present, one user + one schedule round-tripped, decrypted password matches input.

## Out of scope (deliberately)

- No repository pattern, no service layer wrappers - direct `Session(engine)` + `select(...)` per your spec.
- No Alembic - structure is Alembic-ready (single `SQLModel.metadata`); add it the day the schema first changes.
- No `users.role` / `users.email` / `users.timezone` - we'll add columns as the scheduler needs them.
- No migration of `[session_manager.py](session_manager.py)` to read from DB - that's Step 3 ("read encrypted creds from users -> build AngelOneClient on demand").
- No FastAPI route to create users from the web UI yet - for now, insert via the smoke-test script or `psql`.
- WhatsApp number is unique but not validated (no E.164 regex). Keep it simple.