"""Verify that a request comes from this repo's fundamentals workflow on GitHub.

GitHub signs a short-lived OpenID Connect token for any workflow run that asks
for one (``permissions: id-token: write``). The token names the repository, the
branch and the workflow file, and GitHub's published keys prove it is genuine.
So the fundamentals job can post to the app with no shared secret to set up or
leak: only ``refresh-fundamentals.yml`` running on this repo's ``main`` passes.
Pull requests from forks never get such a token.
"""
from __future__ import annotations

import jwt

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "myfinancesmcp-fundamentals"
REPOSITORY = "raghav18482/MyFinancesMCP"
# Checked as well as the name: a repo created later under a reused name gets a new id.
REPOSITORY_ID = "1169306941"
WORKFLOW = ".github/workflows/refresh-fundamentals.yml"
REF = "refs/heads/main"

# Fetches GitHub's signing keys on first use and caches them.
_jwks = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks", lifespan=3600)


class NotOurWorkflow(Exception):
    """The token is missing, forged, expired, or from another repo, branch or workflow."""


def _signing_key(token: str):
    return _jwks.get_signing_key_from_jwt(token).key


def verify(token: str, *, ref: str = REF) -> dict:
    """Return the token's claims, or raise :class:`NotOurWorkflow`."""
    try:
        claims = jwt.decode(
            token,
            _signing_key(token),
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=60,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except Exception as e:  # bad signature, expired, wrong audience, keys unreachable
        raise NotOurWorkflow(f"invalid token ({type(e).__name__}: {e})") from e

    expected = {"repository": REPOSITORY, "repository_id": REPOSITORY_ID, "ref": ref}
    for claim, value in expected.items():
        if str(claims.get(claim)) != value:
            raise NotOurWorkflow(f"{claim} is {claims.get(claim)!r}, expected {value!r}")
    if not str(claims.get("workflow_ref", "")).startswith(f"{REPOSITORY}/{WORKFLOW}@"):
        raise NotOurWorkflow(f"workflow_ref is {claims.get('workflow_ref')!r}")
    return claims
