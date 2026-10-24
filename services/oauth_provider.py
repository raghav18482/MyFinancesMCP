"""OAuth 2.1 authorization server for the read-only MCP endpoint.

This is what lets claude.ai (Settings → Connectors → Add custom connector) and
other OAuth-capable MCP clients connect without a pasted token. It implements
the MCP SDK's ``OAuthAuthorizationServerProvider`` protocol; the HTTP endpoints
are the SDK's own handlers, mounted at the domain root by
:mod:`web.routers.oauth` (FastMCP itself is mounted at ``/mcp``, so the routes
it would add for itself would land under ``/mcp`` instead of where clients look).

The flow:

1. The client registers itself (Dynamic Client Registration, RFC 7591). Only
   allow-listed redirect URIs are accepted: claude.ai's and claude.com's
   callbacks, loopback addresses for local clients (RFC 8252), and anything in
   ``OAUTH_EXTRA_REDIRECT_URIS``.
2. ``/oauth/authorize`` validates the request (client, exact redirect URI,
   scope, PKCE S256, resource) and hands the browser to ``/oauth/consent`` with
   the request sealed in a Fernet token, so nothing is stored for abandoned
   attempts and nothing in it can be altered.
3. The user signs in there with their WhatsApp number and app password (the
   same check, and the same throttle, as the web login) and approves
   read-only access. That issues a single-use code that lives five minutes.
4. ``/oauth/token`` swaps the code (with the PKCE verifier) for an access token
   (one hour) and a refresh token (30 days, rotated on every use).

Properties:

- **Scope** ``portfolio:read`` only, the same scope personal access tokens carry.
- **Resource-bound** (RFC 8707). Every grant records the MCP endpoint it is for,
  and the bearer middleware refuses a token whose resource is anything else.
- **Hashed at rest.** Client secrets, codes, access and refresh tokens are stored
  as SHA-256 hashes. Raw values appear only in the response that issues them.
- **Single-use codes.** A code is consumed the moment it is presented, so even a
  wrong PKCE verifier burns it. Presenting it again revokes whatever the first
  exchange issued (RFC 6749 §10.5).
- **Refresh rotation with reuse detection.** A refresh token presented after it
  was rotated revokes the whole grant, except within a short grace window so
  that a client retrying a refresh whose response it lost is not disconnected.
- **Revocable** through ``/oauth/revoke`` (RFC 7009) or the web app; revoking any
  token revokes its whole grant. Deactivated users' tokens stop working.
- **Rate limited** through the same unknown-token limiter as personal access
  tokens (:mod:`services.mcp_tokens`), which does not lock out users who share
  an egress IP.

Identity reaches the tools as ``AccessToken.subject == "user:<id>"``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import unquote, urlencode, urlparse

import anyio
from pydantic import AnyHttpUrl, AnyUrl, ValidationError
from sqlalchemy import update
from sqlmodel import select
from starlette.requests import Request

from mcp.server.auth.middleware.client_auth import AuthenticationError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from db import get_session, sign_token, unsign_token
from db.models import OAuthAuthCode, OAuthClient, OAuthGrant, User, utcnow
from db.models import OAuthToken as OAuthTokenRow
from services import mcp_tokens

logger = logging.getLogger(__name__)

SCOPE = mcp_tokens.SCOPE_PORTFOLIO_READ
SUPPORTED_SCOPES = [SCOPE]

#: Where the MCP endpoint lives, relative to the public base URL (see ``main.py``).
MCP_RESOURCE_PATH = "/mcp/mcp"

AUTHORIZE_PATH = "/oauth/authorize"
CONSENT_PATH = "/oauth/consent"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
REVOKE_PATH = "/oauth/revoke"

CODE_PREFIX = "mfc_"
ACCESS_PREFIX = "mfa_"
REFRESH_PREFIX = "mfr_"

CODE_TTL_SEC = 5 * 60
ACCESS_TTL_SEC = 60 * 60
REFRESH_TTL_SEC = 30 * 24 * 3600
PENDING_TTL_SEC = 10 * 60
REFRESH_REUSE_GRACE_SEC = 30
MAX_ACTIVE_GRANTS_PER_USER = 20

CLAUDE_REDIRECT_URIS = frozenset({
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_AUTH_METHODS = ("client_secret_post", "client_secret_basic", "none")
_CODE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # base64url(SHA-256), unpadded
_MAX_CLIENT_NAME = 80

# Registration happens from the client's servers (Anthropic's, for claude.ai),
# so this per-IP cap is generous: it bounds table growth, not legitimate use.
_REGISTER_WINDOW_SEC = 60 * 60
_MAX_REGISTER_PER_IP = 300
_register_lock = threading.Lock()
_register_by_ip: dict[str, list[float]] = {}


# ── Configuration ──────────────────────────────────────────────────────────


def _normalize(url: str) -> str:
    """URL compared as a URL (case, default port) with any trailing slash dropped."""
    try:
        return str(AnyHttpUrl(url)).removesuffix("/")
    except ValidationError:
        return ""


def same_resource(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = _normalize(a or ""), _normalize(b or "")
    return bool(na) and na == nb


def _extra_redirect_uris() -> set[str]:
    raw = os.environ.get("OAUTH_EXTRA_REDIRECT_URIS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def _loopback_redirects_allowed() -> bool:
    return os.environ.get("OAUTH_ALLOW_LOOPBACK_REDIRECTS", "1").strip() != "0"


def redirect_uri_allowed(uri: str) -> bool:
    """Whether a client may register this redirect URI. Matching at
    ``/authorize`` is then exact, against what the client registered."""
    s = str(uri)
    if s in CLAUDE_REDIRECT_URIS or s in _extra_redirect_uris():
        return True
    p = urlparse(s)
    if p.fragment or p.username or p.password:
        return False
    return (
        _loopback_redirects_allowed()
        and p.scheme == "http"
        and (p.hostname or "") in _LOOPBACK_HOSTS
    )


@dataclass(frozen=True)
class OAuthUrls:
    base: str

    @property
    def issuer(self) -> str:
        return self.base

    @property
    def resource(self) -> str:
        return self.base + MCP_RESOURCE_PATH

    def endpoint(self, path: str) -> str:
        return self.base + path


def authorization_server_metadata(urls: OAuthUrls) -> OAuthMetadata:
    """RFC 8414 metadata, served at ``/.well-known/oauth-authorization-server``."""
    return OAuthMetadata(
        issuer=AnyHttpUrl(urls.issuer),
        authorization_endpoint=AnyHttpUrl(urls.endpoint(AUTHORIZE_PATH)),
        token_endpoint=AnyHttpUrl(urls.endpoint(TOKEN_PATH)),
        registration_endpoint=AnyHttpUrl(urls.endpoint(REGISTER_PATH)),
        revocation_endpoint=AnyHttpUrl(urls.endpoint(REVOKE_PATH)),
        revocation_endpoint_auth_methods_supported=list(_AUTH_METHODS),
        scopes_supported=SUPPORTED_SCOPES,
        response_types_supported=["code"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=list(_AUTH_METHODS),
        code_challenge_methods_supported=["S256"],
        service_documentation=AnyHttpUrl(urls.endpoint("/connect")),
    )


def protected_resource_metadata(urls: OAuthUrls) -> ProtectedResourceMetadata:
    """RFC 9728 metadata for the MCP endpoint."""
    return ProtectedResourceMetadata(
        resource=AnyHttpUrl(urls.resource),
        authorization_servers=[AnyHttpUrl(urls.issuer)],
        scopes_supported=SUPPORTED_SCOPES,
        resource_name="AgentFolio — Angel One portfolio (read-only)",
        resource_documentation=AnyHttpUrl(urls.endpoint("/connect")),
    )


# ── Small helpers ──────────────────────────────────────────────────────────


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _subject(user_id: int) -> str:
    return f"user:{int(user_id)}"


def _clean_name(name: Optional[str]) -> Optional[str]:
    """A client-chosen display name, stripped of control/format characters."""
    if not name:
        return None
    cleaned = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    cleaned = " ".join(cleaned.split())[:_MAX_CLIENT_NAME]
    return cleaned or None


def check_registration_rate(client_ip: str) -> Optional[int]:
    """Record a registration attempt; return seconds to wait if over the cap."""
    now = time.time()
    with _register_lock:
        hits = [t for t in _register_by_ip.get(client_ip, []) if now - t < _REGISTER_WINDOW_SEC]
        if len(hits) >= _MAX_REGISTER_PER_IP:
            _register_by_ip[client_ip] = hits
            return int(_REGISTER_WINDOW_SEC - (now - hits[0])) + 1
        hits.append(now)
        _register_by_ip[client_ip] = hits
        return None


def reset_rate_limits() -> None:
    with _register_lock:
        _register_by_ip.clear()


# ── Token models handed to the SDK (no secrets in them) ────────────────────


class StoredAuthorizationCode(AuthorizationCode):
    row_id: int
    user_id: int


class StoredRefreshToken(RefreshToken):
    row_id: int
    grant_id: int


class StoredAccessToken(AccessToken):
    grant_id: int


@dataclass(frozen=True)
class PendingAuthorization:
    """A validated ``/authorize`` request waiting for the user's consent."""

    client_id: str
    client_name: Optional[str]
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    state: Optional[str]
    scopes: tuple[str, ...]
    resource: str

    @property
    def redirect_host(self) -> str:
        return urlparse(self.redirect_uri).hostname or ""

    @property
    def is_claude(self) -> bool:
        return self.redirect_uri in CLAUDE_REDIRECT_URIS

    @property
    def is_loopback(self) -> bool:
        return self.redirect_host in _LOOPBACK_HOSTS


# ── Database operations (sync; the provider runs them in a worker thread) ──


def _client_info(row: OAuthClient) -> OAuthClientInformationFull:
    info = OAuthClientInformationFull.model_validate_json(row.metadata_json)
    # The secret is only ever compared by hash (``HashedSecretClientAuthenticator``).
    info.client_secret = None
    return info


def _get_client_row(client_id: str) -> Optional[OAuthClient]:
    if not client_id or len(client_id) > 64:
        return None
    with get_session() as db:
        return db.exec(select(OAuthClient).where(OAuthClient.client_id == client_id)).first()


def get_client_sync(client_id: str) -> Optional[OAuthClientInformationFull]:
    row = _get_client_row(client_id)
    return _client_info(row) if row else None


def _register_client_sync(info: OAuthClientInformationFull) -> None:
    uris = [str(u) for u in (info.redirect_uris or [])]
    if not uris:
        raise RegistrationError("invalid_redirect_uri", "At least one redirect_uri is required")
    bad = [u for u in uris if not redirect_uri_allowed(u)]
    if bad:
        raise RegistrationError(
            "invalid_redirect_uri",
            "Redirect URI not allowed: " + ", ".join(bad[:3])
            + ". Allowed: claude.ai / claude.com MCP callbacks and loopback (http://localhost) addresses.",
        )
    if info.token_endpoint_auth_method not in _AUTH_METHODS:
        raise RegistrationError(
            "invalid_client_metadata",
            "token_endpoint_auth_method must be one of: " + ", ".join(_AUTH_METHODS),
        )
    extra_grants = set(info.grant_types) - {"authorization_code", "refresh_token"}
    if extra_grants:
        raise RegistrationError("invalid_client_metadata",
                                "Unsupported grant_types: " + ", ".join(sorted(extra_grants)))

    # Whatever the client asked for, it gets the one scope there is.
    info.scope = SCOPE
    info.client_name = _clean_name(info.client_name)
    secret_hash = _hash(info.client_secret) if info.client_secret else None
    metadata = info.model_dump(mode="json", exclude={"client_secret"}, exclude_none=True)
    with get_session() as db:
        db.add(OAuthClient(
            client_id=info.client_id,
            client_secret_hash=secret_hash,
            metadata_json=json.dumps(metadata),
            client_name=info.client_name,
        ))
        db.commit()
    logger.info("OAuth client registered: %s (%s, redirect hosts %s)", info.client_id,
                info.token_endpoint_auth_method,
                sorted({urlparse(u).hostname or "" for u in uris}))


def _revoke_grant(db, grant_id: int, now: datetime) -> None:
    grant = db.get(OAuthGrant, grant_id)
    if grant is not None and grant.revoked_at is None:
        grant.revoked_at = now
        db.add(grant)
    db.execute(
        update(OAuthTokenRow)
        .where(OAuthTokenRow.grant_id == grant_id, OAuthTokenRow.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def _issue_pair(db, grant: OAuthGrant, now: datetime) -> tuple[str, str]:
    access, refresh = _new_secret(ACCESS_PREFIX), _new_secret(REFRESH_PREFIX)
    db.add(OAuthTokenRow(grant_id=grant.id, kind="access", token_hash=_hash(access),
                         created_at=now, expires_at=now + timedelta(seconds=ACCESS_TTL_SEC)))
    db.add(OAuthTokenRow(grant_id=grant.id, kind="refresh", token_hash=_hash(refresh),
                         created_at=now, expires_at=now + timedelta(seconds=REFRESH_TTL_SEC)))
    return access, refresh


def _token_response(grant: OAuthGrant, access: str, refresh: str) -> OAuthToken:
    mcp_tokens.remember_known(_hash(access))
    return OAuthToken(
        access_token=access,
        token_type="Bearer",
        expires_in=ACCESS_TTL_SEC,
        scope=grant.scopes,
        refresh_token=refresh,
    )


def _user_active(db, user_id: int) -> bool:
    user = db.get(User, user_id)
    return bool(user and user.is_active)


def _consume_code_sync(client_id: str, raw: str) -> Optional[StoredAuthorizationCode]:
    code = (raw or "").strip()
    if not mcp_tokens.well_formed(code, CODE_PREFIX):
        return None
    digest = _hash(code)
    now = utcnow()
    with get_session() as db:
        row = db.exec(select(OAuthAuthCode).where(OAuthAuthCode.code_hash == digest)).first()
        if row is None or not hmac.compare_digest(row.code_hash, digest) or row.client_id != client_id:
            return None
        if row.used_at is not None:
            if row.grant_id is not None:
                _revoke_grant(db, row.grant_id, now)
                db.commit()
            logger.warning("OAuth code replayed for client %s; grant %s revoked",
                           client_id, row.grant_id)
            return None
        if _aware(row.expires_at) <= now:
            return None
        # Consume now, before PKCE is checked: a code gets exactly one attempt.
        claimed = db.execute(
            update(OAuthAuthCode)
            .where(OAuthAuthCode.id == row.id, OAuthAuthCode.used_at.is_(None))
            .values(used_at=now)
        )
        db.commit()
        if claimed.rowcount != 1:
            return None
        return StoredAuthorizationCode(
            code="redacted",
            scopes=row.scopes.split(),
            expires_at=_aware(row.expires_at).timestamp(),
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
            resource=row.resource,
            subject=_subject(row.user_id),
            row_id=row.id,
            user_id=row.user_id,
        )


def _exchange_code_sync(client_id: str, code: StoredAuthorizationCode) -> OAuthToken:
    now = utcnow()
    with get_session() as db:
        if not _user_active(db, code.user_id):
            raise TokenError("invalid_grant", "The account is not active")
        grant = OAuthGrant(user_id=code.user_id, client_id=client_id,
                           scopes=" ".join(code.scopes), resource=code.resource or "",
                           created_at=now)
        db.add(grant)
        db.flush()
        access, refresh = _issue_pair(db, grant, now)
        row = db.get(OAuthAuthCode, code.row_id)
        if row is not None:
            row.grant_id = grant.id
            db.add(row)

        active = db.exec(
            select(OAuthGrant)
            .where(OAuthGrant.user_id == code.user_id, OAuthGrant.revoked_at.is_(None))
            .order_by(OAuthGrant.created_at.desc(), OAuthGrant.id.desc())
        ).all()
        for old in active[MAX_ACTIVE_GRANTS_PER_USER:]:
            _revoke_grant(db, old.id, now)
        db.commit()
        db.refresh(grant)
        logger.info("OAuth grant %s created for user id=%s, client %s", grant.id, code.user_id, client_id)
        return _token_response(grant, access, refresh)


def _load_refresh_sync(client_id: str, raw: str) -> Optional[StoredRefreshToken]:
    token = (raw or "").strip()
    if not mcp_tokens.well_formed(token, REFRESH_PREFIX):
        return None
    digest = _hash(token)
    now = utcnow()
    with get_session() as db:
        row = db.exec(select(OAuthTokenRow).where(OAuthTokenRow.token_hash == digest,
                                                  OAuthTokenRow.kind == "refresh")).first()
        if row is None or not hmac.compare_digest(row.token_hash, digest):
            return None
        grant = db.get(OAuthGrant, row.grant_id)
        if grant is None or grant.client_id != client_id:
            return None
        if grant.revoked_at is not None or row.revoked_at is not None:
            return None
        used = _aware(row.used_at)
        if used is not None and (now - used).total_seconds() > REFRESH_REUSE_GRACE_SEC:
            _revoke_grant(db, grant.id, now)
            db.commit()
            logger.warning("OAuth refresh token reused for grant %s; grant revoked", grant.id)
            return None
        if _aware(row.expires_at) <= now or not _user_active(db, grant.user_id):
            return None
        return StoredRefreshToken(
            token="redacted",
            client_id=grant.client_id,
            scopes=grant.scopes.split(),
            expires_at=int(_aware(row.expires_at).timestamp()),
            resource=grant.resource,
            subject=_subject(grant.user_id),
            row_id=row.id,
            grant_id=grant.id,
        )


def _exchange_refresh_sync(rt: StoredRefreshToken) -> OAuthToken:
    now = utcnow()
    with get_session() as db:
        rotated = db.execute(
            update(OAuthTokenRow)
            .where(OAuthTokenRow.id == rt.row_id, OAuthTokenRow.used_at.is_(None))
            .values(used_at=now)
        )
        if rotated.rowcount != 1:
            # Already rotated. ``_load_refresh_sync`` only lets that through inside
            # the grace window; re-check here in case it was rotated in between.
            row = db.get(OAuthTokenRow, rt.row_id)
            used = _aware(row.used_at) if row else None
            if used is None or (now - used).total_seconds() > REFRESH_REUSE_GRACE_SEC:
                _revoke_grant(db, rt.grant_id, now)
                db.commit()
                raise TokenError("invalid_grant", "refresh token has already been used")
        grant = db.get(OAuthGrant, rt.grant_id)
        if grant is None or grant.revoked_at is not None or not _user_active(db, grant.user_id):
            db.rollback()
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        access, refresh = _issue_pair(db, grant, now)
        db.commit()
        db.refresh(grant)
        return _token_response(grant, access, refresh)


def _lookup_access(db, digest: str, now: datetime) -> tuple[Optional[OAuthTokenRow], Optional[OAuthGrant]]:
    row = db.exec(select(OAuthTokenRow).where(OAuthTokenRow.token_hash == digest,
                                              OAuthTokenRow.kind == "access")).first()
    if row is None or not hmac.compare_digest(row.token_hash, digest):
        return None, None
    grant = db.get(OAuthGrant, row.grant_id)
    return row, grant


def _access_token_model(row: OAuthTokenRow, grant: OAuthGrant) -> StoredAccessToken:
    return StoredAccessToken(
        token="redacted",
        client_id=grant.client_id,
        scopes=grant.scopes.split(),
        expires_at=int(_aware(row.expires_at).timestamp()),
        resource=grant.resource,
        subject=_subject(grant.user_id),
        grant_id=grant.id,
    )


def _access_valid(db, row, grant, now) -> bool:
    return (
        row is not None and grant is not None
        and row.revoked_at is None and grant.revoked_at is None
        and _aware(row.expires_at) > now
        and _user_active(db, grant.user_id)
    )


def _load_access_sync(raw: str) -> Optional[StoredAccessToken]:
    """Look up an access token without the limiter (revocation endpoint)."""
    token = (raw or "").strip()
    if not mcp_tokens.well_formed(token, ACCESS_PREFIX):
        return None
    now = utcnow()
    with get_session() as db:
        row, grant = _lookup_access(db, _hash(token), now)
        return _access_token_model(row, grant) if _access_valid(db, row, grant, now) else None


def verify_access_token(raw: Optional[str], client_ip: str = "unknown") -> Optional[StoredAccessToken]:
    """Verify an OAuth access token for the MCP endpoint. None on any failure.

    Goes through the shared unknown-token limiter (:mod:`services.mcp_tokens`).
    """
    token = (raw or "").strip()
    if not mcp_tokens.well_formed(token, ACCESS_PREFIX):
        return None
    digest = _hash(token)
    if not mcp_tokens.lookup_allowed(client_ip, digest):
        logger.warning("OAuth access token verification throttled for ip=%s", client_ip)
        return None
    now = utcnow()
    try:
        with get_session() as db:
            row, grant = _lookup_access(db, digest, now)
            if row is None:
                mcp_tokens.record_unknown(client_ip)
                return None
            if not _access_valid(db, row, grant, now):
                return None
            last = _aware(grant.last_used_at)
            if last is None or (now - last).total_seconds() >= 60:
                grant.last_used_at = now
                db.add(grant)
                db.commit()
                db.refresh(grant)
                db.refresh(row)
            mcp_tokens.remember_known(digest)
            return _access_token_model(row, grant)
    except Exception:
        logger.exception("OAuth access token verification failed closed")
        return None


def _revoke_grant_sync(grant_id: int) -> None:
    with get_session() as db:
        _revoke_grant(db, grant_id, utcnow())
        db.commit()
    logger.info("OAuth grant %s revoked by its client", grant_id)


# ── Consent ────────────────────────────────────────────────────────────────


def load_pending(blob: str) -> Optional[PendingAuthorization]:
    """Open a sealed ``/authorize`` request, re-checking it against the client.

    None if it is tampered with, older than ten minutes, or its client or
    redirect URI is no longer registered.
    """
    try:
        data = unsign_token(blob or "", PENDING_TTL_SEC)
    except Exception:
        return None
    if data.get("v") != 1:
        return None
    info = get_client_sync(str(data.get("cid") or ""))
    if info is None:
        return None
    ru = str(data.get("ru") or "")
    if ru not in {str(u) for u in (info.redirect_uris or [])}:
        return None
    return PendingAuthorization(
        client_id=info.client_id,
        client_name=info.client_name,
        redirect_uri=ru,
        redirect_uri_provided_explicitly=bool(data.get("rue")),
        code_challenge=str(data.get("cc") or ""),
        state=data.get("st"),
        scopes=tuple(data.get("sc") or ()),
        resource=str(data.get("rs") or ""),
    )


def approve(pending: PendingAuthorization, user_id: int) -> str:
    """Issue a code for ``user_id`` (already authenticated) and return the redirect."""
    raw = _new_secret(CODE_PREFIX)
    now = utcnow()
    with get_session() as db:
        if not _user_active(db, user_id):
            raise PermissionError("account is not active")
        db.add(OAuthAuthCode(
            code_hash=_hash(raw),
            client_id=pending.client_id,
            user_id=user_id,
            scopes=" ".join(pending.scopes),
            resource=pending.resource,
            code_challenge=pending.code_challenge,
            redirect_uri=pending.redirect_uri,
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            created_at=now,
            expires_at=now + timedelta(seconds=CODE_TTL_SEC),
        ))
        db.commit()
    logger.info("OAuth consent given by user id=%s to client %s", user_id, pending.client_id)
    return construct_redirect_uri(pending.redirect_uri, code=raw, state=pending.state)


def deny(pending: PendingAuthorization) -> str:
    return construct_redirect_uri(
        pending.redirect_uri,
        error="access_denied",
        error_description="The user declined to connect their portfolio",
        state=pending.state,
    )


# ── Connected apps, for the web UI ─────────────────────────────────────────


def list_grants(user_id: int) -> list[dict]:
    with get_session() as db:
        grants = db.exec(
            select(OAuthGrant)
            .where(OAuthGrant.user_id == user_id, OAuthGrant.revoked_at.is_(None))
            .order_by(OAuthGrant.created_at.desc())
        ).all()
        names = {}
        for g in grants:
            if g.client_id not in names:
                c = db.exec(select(OAuthClient).where(OAuthClient.client_id == g.client_id)).first()
                names[g.client_id] = (c.client_name if c else None) or "OAuth app"
        return [
            {
                "id": g.id,
                "client_name": names[g.client_id],
                "scopes": g.scopes.split(),
                "created_at": _aware(g.created_at).isoformat() if g.created_at else None,
                "last_used_at": _aware(g.last_used_at).isoformat() if g.last_used_at else None,
            }
            for g in grants
        ]


def revoke_grant_for_user(user_id: int, grant_id: int) -> bool:
    """Revoke one of ``user_id``'s own grants. False if there is no such grant for them."""
    with get_session() as db:
        grant = db.get(OAuthGrant, grant_id)
        if grant is None or grant.user_id != user_id:
            return False
        _revoke_grant(db, grant_id, utcnow())
        db.commit()
    logger.info("OAuth grant %s revoked by user id=%s", grant_id, user_id)
    return True


def purge_expired(older_than_sec: int = 24 * 3600) -> int:
    """Delete codes and tokens that expired more than ``older_than_sec`` ago."""
    cutoff = utcnow() - timedelta(seconds=older_than_sec)
    with get_session() as db:
        n = 0
        for model in (OAuthAuthCode, OAuthTokenRow):
            for row in db.exec(select(model).where(model.expires_at < cutoff)).all():
                db.delete(row)
                n += 1
        db.commit()
    return n


# ── The SDK provider and client authenticator ──────────────────────────────


class McpOAuthProvider:
    """``OAuthAuthorizationServerProvider`` backed by the app database.

    ``base_url`` is a callable so the public URL is read when it is needed,
    not frozen at import.
    """

    def __init__(self, base_url: Callable[[], str]) -> None:
        self._base_url = base_url

    @property
    def urls(self) -> OAuthUrls:
        return OAuthUrls(self._base_url().rstrip("/"))

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return await anyio.to_thread.run_sync(get_client_sync, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await anyio.to_thread.run_sync(_register_client_sync, client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        urls = self.urls
        if params.resource and not same_resource(params.resource, urls.resource):
            raise AuthorizeError("invalid_request", f"Unknown resource; this server is {urls.resource}")
        if not _CODE_CHALLENGE_RE.fullmatch(params.code_challenge or ""):
            raise AuthorizeError("invalid_request", "code_challenge must be an S256 PKCE challenge")
        if not redirect_uri_allowed(str(params.redirect_uri)):
            raise AuthorizeError("invalid_request", "redirect_uri is not allowed")
        scopes = params.scopes or [SCOPE]
        if any(s not in SUPPORTED_SCOPES for s in scopes):
            raise AuthorizeError("invalid_scope", f"Only {SCOPE} is available")
        blob = sign_token({
            "v": 1,
            "cid": client.client_id,
            "ru": str(params.redirect_uri),
            "rue": params.redirect_uri_provided_explicitly,
            "cc": params.code_challenge,
            "st": params.state,
            "sc": sorted(set(scopes)),
            "rs": urls.resource,
        })
        return urls.endpoint(CONSENT_PATH) + "?" + urlencode({"request": blob})

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[StoredAuthorizationCode]:
        return await anyio.to_thread.run_sync(_consume_code_sync, client.client_id, authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: StoredAuthorizationCode
    ) -> OAuthToken:
        return await anyio.to_thread.run_sync(_exchange_code_sync, client.client_id, authorization_code)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[StoredRefreshToken]:
        return await anyio.to_thread.run_sync(_load_refresh_sync, client.client_id, refresh_token)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: StoredRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        return await anyio.to_thread.run_sync(_exchange_refresh_sync, refresh_token)

    async def load_access_token(self, token: str) -> Optional[StoredAccessToken]:
        return await anyio.to_thread.run_sync(_load_access_sync, token)

    async def revoke_token(self, token: StoredAccessToken | StoredRefreshToken) -> None:
        grant_id = getattr(token, "grant_id", None)
        if grant_id is not None:
            await anyio.to_thread.run_sync(_revoke_grant_sync, grant_id)


class HashedSecretClientAuthenticator:
    """Client authentication for ``/oauth/token`` and ``/oauth/revoke``.

    Same contract as the SDK's ``ClientAuthenticator``, but compares a hash of
    the presented secret with the stored hash, since raw secrets are not kept.
    """

    def __init__(self, provider: McpOAuthProvider) -> None:
        self.provider = provider

    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        form = await request.form()
        client_id = form.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise AuthenticationError("Missing client_id")
        row = await anyio.to_thread.run_sync(_get_client_row, client_id)
        if row is None:
            raise AuthenticationError("Invalid client_id")
        info = _client_info(row)

        presented: Optional[str] = None
        method = info.token_endpoint_auth_method
        if method == "client_secret_basic":
            header = request.headers.get("authorization", "")
            if not header.lower().startswith("basic "):
                raise AuthenticationError("Missing or invalid Basic authentication in Authorization header")
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
                basic_id, presented = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                raise AuthenticationError("Invalid Basic authentication header") from None
            if unquote(basic_id) != client_id:
                raise AuthenticationError("Client ID mismatch in Basic auth")
            presented = unquote(presented)
        elif method == "client_secret_post":
            value = form.get("client_secret")
            presented = value if isinstance(value, str) else None

        if row.client_secret_hash:
            if not presented or not hmac.compare_digest(row.client_secret_hash, _hash(presented)):
                raise AuthenticationError("Invalid client credentials")
            if info.client_secret_expires_at and info.client_secret_expires_at < int(time.time()):
                raise AuthenticationError("Client secret has expired")
        return info
