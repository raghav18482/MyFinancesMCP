"""Personal access tokens for the read-only MCP endpoint.

These are custom bearer tokens, separate from OAuth. A signed-in web user creates
one (re-entering their app password), pastes it into their MCP client's
``Authorization: Bearer`` header, and can revoke it at any time. They are for
clients that can send a header (Claude Code, Cursor, ``mcp-remote``); clients
that need OAuth, such as claude.ai custom connectors, go through
:mod:`services.oauth_provider` instead. Both kinds verify at the same endpoint.

Properties:

- **High entropy.** ``mfm_`` + 32 random bytes (``secrets.token_urlsafe``).
- **Never stored or logged raw.** Only a SHA-256 hash is persisted; logs use the
  row id or the short display prefix. The raw value is returned exactly once.
- **Expiring and revocable.** Every verification checks ``expires_at`` and
  ``revoked_at`` in the database, so revocation takes effect on the next request.
- **Owner-scoped.** List and revoke only ever touch the caller's own rows.
- **Bound to an active user.** A deactivated user's tokens stop verifying.
- **Rate limited, without shared-IP lockouts.** See "Verification limiter" below.
  Creation is limited per user and per IP.

Verification limiter
--------------------
Tokens carry 256 random bits, so guessing one is hopeless; the limiter exists
to stop a flood of guesses from turning into a flood of database lookups. It
must not lock out real users, and claude.ai users all arrive from Anthropic's
shared egress addresses, so a plain per-IP lockout would let one noisy client
(or many legitimately expired tokens) shut out everyone behind the same IP.
So:

- Only *unknown* tokens count: well-formed, but matching no row. A malformed
  token is rejected without a lookup and costs nothing; a token that exists but
  is expired or revoked is a real client that needs a refresh or a new token,
  not a guess.
- Unknown lookups are capped per IP and globally. Past either cap, a token is
  refused without a lookup **unless** its hash was issued or verified recently
  by this process. Those still go to the database (so expiry and revocation
  are always checked), which keeps real users working while guesses from the
  same addresses are dropped cheaply.

The limiter is in-memory and per process, like :mod:`services.login_throttle`;
behind several workers the effective limit multiplies by the worker count.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import select

from db import get_session
from db.models import McpToken, User, utcnow

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "mfm_"
SCOPE_PORTFOLIO_READ = "portfolio:read"
DEFAULT_SCOPES = (SCOPE_PORTFOLIO_READ,)

DEFAULT_TTL_DAYS = 30
MAX_TTL_DAYS = 90
MAX_ACTIVE_TOKENS_PER_USER = 10

_DISPLAY_PREFIX_LEN = 10  # "mfm_" + 6 chars: enough to recognise, useless to guess
_MIN_TOKEN_LEN = len(TOKEN_PREFIX) + 40
_MAX_TOKEN_LEN = 128
_LAST_USED_RESOLUTION_SEC = 60

# Verification limiter (see the module docstring). Unknown-token lookups only.
_VERIFY_WINDOW_SEC = 15 * 60
_MAX_UNKNOWN_PER_IP = 300
_MAX_UNKNOWN_GLOBAL = 5000
# Hashes issued or verified recently bypass the caps (they are still checked).
_KNOWN_TTL_SEC = 24 * 3600
_MAX_KNOWN = 50_000
# Token creation.
_ISSUE_WINDOW_SEC = 60 * 60
_MAX_ISSUE_PER_USER = 10
_MAX_ISSUE_PER_IP = 20

_lock = threading.Lock()
_unknown_by_ip: dict[str, list[float]] = {}
_unknown_global: collections.deque[float] = collections.deque()
_known: collections.OrderedDict[str, float] = collections.OrderedDict()
_issue_by_user: dict[str, list[float]] = {}
_issue_by_ip: dict[str, list[float]] = {}


class TokenError(Exception):
    """A token request the caller should be told about (status code attached)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class TooManyRequests(TokenError):
    def __init__(self, retry_after_sec: int) -> None:
        super().__init__("Too many requests. Try again later.", 429)
        self.retry_after_sec = retry_after_sec


@dataclass(frozen=True)
class VerifiedToken:
    """What a successful verification yields. Holds no secret."""

    token_id: int
    user_id: int
    scopes: tuple[str, ...]
    expires_at: int  # unix seconds


def mcp_session_key(user_id: int) -> str:
    """The in-memory broker session key for a token-authenticated MCP user."""
    return f"mcp-user-{int(user_id)}"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fresh(store: dict[str, list[float]], key: str, now: float, window: float) -> list[float]:
    return [t for t in store.get(key, []) if now - t < window]


def _check_and_record(store: dict[str, list[float]], key: str, cap: int, window: float) -> None:
    now = time.time()
    with _lock:
        hits = _fresh(store, key, now, window)
        if len(hits) >= cap:
            raise TooManyRequests(int(window - (now - hits[0])) + 1)
        hits.append(now)
        store[key] = hits


def lookup_allowed(client_ip: str, digest: str) -> bool:
    """May a well-formed token with this hash be looked up in the database?

    True unless an unknown-token cap is exceeded and the hash is not one this
    process issued or verified recently. Shared by PATs and OAuth access tokens.
    """
    now = time.time()
    with _lock:
        seen = _known.get(digest)
        if seen is not None and now - seen < _KNOWN_TTL_SEC:
            return True
        while _unknown_global and now - _unknown_global[0] >= _VERIFY_WINDOW_SEC:
            _unknown_global.popleft()
        hits = _fresh(_unknown_by_ip, client_ip, now, _VERIFY_WINDOW_SEC)
        if hits:
            _unknown_by_ip[client_ip] = hits
        else:
            _unknown_by_ip.pop(client_ip, None)
        return len(hits) < _MAX_UNKNOWN_PER_IP and len(_unknown_global) < _MAX_UNKNOWN_GLOBAL


def record_unknown(client_ip: str) -> None:
    """A well-formed token matched no row."""
    now = time.time()
    with _lock:
        hits = _fresh(_unknown_by_ip, client_ip, now, _VERIFY_WINDOW_SEC)
        hits.append(now)
        _unknown_by_ip[client_ip] = hits
        _unknown_global.append(now)
        # The IP comes from X-Forwarded-For, which a client can vary, so drop
        # idle entries rather than let the map grow without bound.
        if len(_unknown_by_ip) > _MAX_UNKNOWN_GLOBAL * 2:
            for ip in [k for k, v in _unknown_by_ip.items() if not v or now - v[-1] >= _VERIFY_WINDOW_SEC]:
                del _unknown_by_ip[ip]


def remember_known(digest: str) -> None:
    """This hash belongs to a real token: let it past the caps for a while."""
    now = time.time()
    with _lock:
        _known[digest] = now
        _known.move_to_end(digest)
        while len(_known) > _MAX_KNOWN:
            _known.popitem(last=False)


def reset_rate_limits() -> None:
    """Forget all limiter state (tests, or an operator clearing a lockout)."""
    with _lock:
        _unknown_by_ip.clear()
        _unknown_global.clear()
        _known.clear()
        _issue_by_user.clear()
        _issue_by_ip.clear()


def _row_view(row: McpToken) -> dict:
    """Public view of a token row. Never includes the hash or the raw token."""
    now = utcnow()
    expires = _aware(row.expires_at)
    revoked = row.revoked_at is not None
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.token_prefix,
        "scopes": row.scopes.split(),
        "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
        "expires_at": expires.isoformat() if expires else None,
        "last_used_at": _aware(row.last_used_at).isoformat() if row.last_used_at else None,
        "revoked": revoked,
        "active": (not revoked) and bool(expires and expires > now),
    }


def issue(
    user_id: int,
    name: str = "MCP client",
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
    client_ip: str = "unknown",
) -> tuple[str, dict]:
    """Create a token for ``user_id``. Returns ``(raw_token, public_view)``.

    The raw token is not recoverable after this returns. The caller must have
    already authenticated the user (the web route re-checks the app password).
    """
    _check_and_record(_issue_by_user, str(user_id), _MAX_ISSUE_PER_USER, _ISSUE_WINDOW_SEC)
    _check_and_record(_issue_by_ip, client_ip, _MAX_ISSUE_PER_IP, _ISSUE_WINDOW_SEC)

    try:
        ttl = int(ttl_days)
    except (TypeError, ValueError):
        raise TokenError("ttl_days must be a whole number of days") from None
    if not 1 <= ttl <= MAX_TTL_DAYS:
        raise TokenError(f"ttl_days must be between 1 and {MAX_TTL_DAYS}")
    label = " ".join((name or "").split())[:60] or "MCP client"

    now = utcnow()
    with get_session() as db:
        user = db.get(User, user_id)
        if not user or not user.is_active:
            raise TokenError("Account is not active", 403)
        active = [
            r for r in db.exec(
                select(McpToken).where(McpToken.user_id == user_id,
                                       McpToken.revoked_at == None)  # noqa: E711
            ).all()
            if _aware(r.expires_at) > now
        ]
        if len(active) >= MAX_ACTIVE_TOKENS_PER_USER:
            raise TokenError(
                f"You already have {MAX_ACTIVE_TOKENS_PER_USER} active tokens. Revoke one first.",
                409,
            )

        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        row = McpToken(
            user_id=user_id,
            name=label,
            token_prefix=raw[:_DISPLAY_PREFIX_LEN],
            token_hash=hash_token(raw),
            scopes=" ".join(DEFAULT_SCOPES),
            created_at=now,
            expires_at=now + timedelta(days=ttl),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        view = _row_view(row)
    remember_known(row.token_hash)

    logger.info("MCP token id=%s issued for user id=%s (expires in %d days)", row.id, user_id, ttl)
    return raw, view


def well_formed(token: str, prefix: str) -> bool:
    return token.startswith(prefix) and _MIN_TOKEN_LEN <= len(token) <= _MAX_TOKEN_LEN


def verify(raw: Optional[str], client_ip: str = "unknown") -> Optional[VerifiedToken]:
    """Return the verified token, or None for every kind of failure.

    Failures are deliberately indistinguishable to the caller. A malformed token
    is refused without a lookup; past the unknown-token caps, so is any token
    this process has not recently seen (see the module docstring).
    """
    token = (raw or "").strip()
    if not well_formed(token, TOKEN_PREFIX):
        return None

    digest = hash_token(token)
    if not lookup_allowed(client_ip, digest):
        logger.warning("MCP token verification throttled for ip=%s", client_ip)
        return None

    now = utcnow()
    try:
        with get_session() as db:
            row = db.exec(select(McpToken).where(McpToken.token_hash == digest)).first()
            if row is None or not hmac.compare_digest(row.token_hash, digest):
                record_unknown(client_ip)
                return None
            ok = row.revoked_at is None and _aware(row.expires_at) > now
            user = db.get(User, row.user_id) if ok else None
            if not ok or user is None or not user.is_active:
                return None

            last = _aware(row.last_used_at)
            if last is None or (now - last).total_seconds() >= _LAST_USED_RESOLUTION_SEC:
                row.last_used_at = now
                db.add(row)
                db.commit()
                db.refresh(row)

            remember_known(digest)
            return VerifiedToken(
                token_id=row.id,
                user_id=row.user_id,
                scopes=tuple(row.scopes.split()),
                expires_at=int(_aware(row.expires_at).timestamp()),
            )
    except Exception:
        # A database fault must deny, never allow.
        logger.exception("MCP token verification failed closed")
        return None


def list_for_user(user_id: int) -> list[dict]:
    with get_session() as db:
        rows = db.exec(
            select(McpToken).where(McpToken.user_id == user_id).order_by(McpToken.created_at.desc())
        ).all()
        return [_row_view(r) for r in rows]


def revoke(user_id: int, token_id: int) -> bool:
    """Revoke one of ``user_id``'s own tokens. False if there is no such token for them."""
    with get_session() as db:
        row = db.get(McpToken, token_id)
        if row is None or row.user_id != user_id:
            return False
        if row.revoked_at is None:
            row.revoked_at = utcnow()
            db.add(row)
            db.commit()
            logger.info("MCP token id=%s revoked by user id=%s", token_id, user_id)
        return True
