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
    if not os.environ.get("ADMIN_API_KEY", "").strip():
        logger.warning("ADMIN_API_KEY is not set; the admin API refuses every request")
