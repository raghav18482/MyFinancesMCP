"""Deployment security settings shared by the web app and the combined server.

Production is detected from the environment: ``APP_ENV=production`` (or
``prod``), or ``RENDER=true``, which Render sets on every service. In
production the server refuses to start without the secrets and host list it
needs, the session cookie is ``Secure`` by default, and the interactive API
docs are off. Locally the same gaps only produce warnings, so a plain
``python main.py`` keeps working.
"""
from __future__ import annotations

import logging
import os
import secrets

logger = logging.getLogger(__name__)

MIN_SESSION_SECRET_CHARS = 32


class InsecureConfiguration(RuntimeError):
    """A production deployment is missing a security-critical setting."""


def is_production() -> bool:
    app_env = os.environ.get("APP_ENV", "").strip().lower()
    if app_env in ("production", "prod"):
        return True
    return os.environ.get("RENDER", "").strip().lower() == "true"


def allowed_hosts() -> list[str]:
    """Hostnames to answer to, from ``ALLOWED_HOSTS`` (comma-separated).

    Anything that reads the ``Host`` header — ``request.base_url`` on ``/connect``,
    absolute links, redirects — trusts whatever the client sent unless the host is
    validated. Unset means "accept any Host": refused in production, a warning
    locally.
    """
    raw = os.environ.get("ALLOWED_HOSTS", "").strip()
    if not raw:
        if is_production():
            raise InsecureConfiguration("ALLOWED_HOSTS must be set in production")
        logger.warning(
            "ALLOWED_HOSTS is not set — accepting any Host header. "
            "Set it to your deployment domain in production."
        )
        return ["*"]
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    # Loopback stays allowed so health checks and local curl keep working.
    for local in ("localhost", "127.0.0.1"):
        if local not in hosts:
            hosts.append(local)
    return hosts


def session_secret() -> str:
    """The cookie-signing key. Required, and long, in production."""
    value = os.environ.get("SESSION_SECRET", "").strip()
    if len(value) >= MIN_SESSION_SECRET_CHARS:
        return value
    if is_production():
        raise InsecureConfiguration(
            f"SESSION_SECRET must be set to at least {MIN_SESSION_SECRET_CHARS} characters in production"
        )
    if value:
        logger.warning("SESSION_SECRET is shorter than %d characters", MIN_SESSION_SECRET_CHARS)
        return value
    logger.warning("SESSION_SECRET is not set — using a random key; sessions end on restart")
    return secrets.token_hex(32)


def cookie_https_only() -> bool:
    """``Secure`` session cookie: on by default in production, opt-in locally."""
    raw = os.environ.get("SESSION_COOKIE_SECURE", "").strip()
    if raw:
        secure = raw == "1"
        if is_production() and not secure:
            raise InsecureConfiguration("SESSION_COOKIE_SECURE=0 is not allowed in production")
        return secure
    return is_production()


def api_docs_enabled() -> bool:
    """``/docs``, ``/redoc`` and ``/openapi.json``: never in production."""
    return not is_production()


def public_base_url() -> str:
    """Where this server is reachable, used for the MCP auth settings."""
    value = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if is_production():
        if not value.startswith("https://"):
            raise InsecureConfiguration("PUBLIC_BASE_URL must be set to an https:// URL in production")
        return value
    return value or "http://localhost:8000"


def validate_production_config() -> None:
    """Fail fast on a production deployment missing a security-critical setting."""
    if not is_production():
        return
    allowed_hosts()
    session_secret()
    cookie_https_only()
    public_base_url()
    if not os.environ.get("ENCRYPTION_KEY", "").strip():
        raise InsecureConfiguration("ENCRYPTION_KEY must be set in production")
    from web import client_ip

    try:
        client_ip.validate_config(production=True)
    except ValueError as e:
        raise InsecureConfiguration(str(e)) from None
    if not os.environ.get("ADMIN_API_KEY", "").strip():
        logger.warning("ADMIN_API_KEY is not set; the admin API refuses every request")


# ── Admin API ─────────────────────────────────────────────────────────────

MIN_ADMIN_KEY_CHARS = 32
_LOOPBACK = ("127.0.0.1", "::1")
_FORWARDING_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


def admin_api_enabled() -> bool:
    """Whether ``/api/admin/*`` is mounted: always locally, opt-in in production."""
    if not is_production():
        return True
    return os.environ.get("ADMIN_API_ENABLED", "").strip() == "1"


def admin_key_status() -> str:
    """``ok``, ``missing`` or ``too_short``. Never the key or its length."""
    key = os.environ.get("ADMIN_API_KEY", "").strip()
    if not key:
        return "missing"
    if len(key) < MIN_ADMIN_KEY_CHARS:
        return "too_short"
    return "ok"


def admin_key_usable() -> bool:
    return admin_key_status() == "ok"


def admin_allowed_networks() -> list:
    """``ADMIN_ALLOWED_SOURCES`` as networks; invalid entries are dropped and logged."""
    import ipaddress

    networks = []
    for raw in (os.environ.get("ADMIN_ALLOWED_SOURCES") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.error("ADMIN_ALLOWED_SOURCES entry %r is not a network; ignored", raw)
    return networks


def admin_remote_status() -> str:
    """``off``, ``on``, or ``misconfigured`` (remote asked for without what it needs)."""
    from web.client_ip import current_mode

    if os.environ.get("ADMIN_ALLOW_REMOTE", "").strip() != "1":
        return "off"
    if not admin_allowed_networks() or current_mode() == "shadow":
        return "misconfigured"
    return "on"


def admin_request_source(request) -> str | None:
    """Where an admin request came from, or ``None`` if that source is not allowed.

    Allowed by default only from inside the instance (Render Shell: a loopback
    peer with no forwarding headers, which public traffic through Render's
    proxy can never have). Remote access additionally needs
    ``ADMIN_ALLOW_REMOTE=1``, an ``ADMIN_ALLOWED_SOURCES`` allowlist, and a
    *verified* client IP (``web.client_ip``) inside it. A forgeable header
    never decides.
    """
    import ipaddress

    from web.client_ip import trusted_client_ip

    peer = request.client.host if request.client else None
    forwarded = any(request.headers.get(h) for h in _FORWARDING_HEADERS)
    if peer in _LOOPBACK and not forwarded:
        return "loopback"
    if admin_remote_status() != "on":
        return None
    ip = trusted_client_ip(request.headers, peer)
    if ip is None:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if any(addr in net for net in admin_allowed_networks()):
        return ip
    return None


def log_admin_status() -> None:
    """One startup line. Says nothing about the key except whether it is usable."""
    remote = admin_remote_status()
    logger.info(
        "ADMIN_API status=%s key=%s remote=%s",
        "enabled" if admin_api_enabled() else "disabled", admin_key_status(), remote,
    )
    if remote == "misconfigured":
        logger.error(
            "ADMIN_REMOTE_MISCONFIGURED: ADMIN_ALLOW_REMOTE=1 needs ADMIN_ALLOWED_SOURCES and a "
            "verified CLIENT_IP_MODE; remote admin access stays off"
        )
