"""OAuth 2.1 for the MCP endpoint (claude.ai custom connectors).

Drives the real combined app (``main.create_app``) through registration,
authorization, consent, token exchange, refresh, revocation and MCP calls:
PKCE S256 is required and checked, redirect URIs match exactly and only
allow-listed ones register, codes are short-lived and single use (a replay
revokes what the first exchange issued), refresh tokens rotate with reuse
detection, revocation kills the whole grant, tokens are bound to the MCP
resource, each user only ever sees their own data, nothing secret is stored
raw, personal access tokens keep working, the verify limiter does not lock out
users who share an IP, and every tool is annotated read-only. The last test
connects the MCP SDK's own OAuth client over a real loopback socket.

Fake broker and temporary SQLite only. Run directly or under pytest.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import secrets
import sys
import threading
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _security_harness import PASSWORD, USERS, harness, run_all  # noqa: E402
from test_mcp_auth import _INIT, _free_port, _issue, _mcp_app, _payload, _rpc, mcp_harness  # noqa: E402

CLAUDE_AI = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_COM = "https://claude.com/api/mcp/auth_callback"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _base():
    from web import security_config

    return security_config.public_base_url().rstrip("/")


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _register(http, redirect_uris=(CLAUDE_AI, CLAUDE_COM), method="client_secret_post", **extra):
    body = {"redirect_uris": list(redirect_uris), "token_endpoint_auth_method": method,
            "client_name": "Claude", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], **extra}
    return http.post("/oauth/register", json=body)


def _client(http, **kw):
    res = _register(http, **kw)
    assert res.status_code == 201, res.text
    return res.json()


def _authorize(http, client, challenge, *, redirect=CLAUDE_AI, state="st-1", resource="default", **extra):
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
              "scope": "portfolio:read", **extra}
    if resource == "default":
        params["resource"] = _base() + "/mcp/mcp"
    elif resource is not None:
        params["resource"] = resource
    return http.get("/oauth/authorize", params=params, follow_redirects=False)


def _blob(res):
    assert res.status_code == 302, (res.status_code, res.text[:300])
    loc = urlparse(res.headers["location"])
    assert loc.path == "/oauth/consent", res.headers["location"]
    return parse_qs(loc.query)["request"][0]


def _consent(http, blob, who="A", *, decision="allow", password=PASSWORD):
    u = USERS[who]
    return http.post("/oauth/consent", data={"request": blob, "decision": decision,
                                             "country_code": u["dial"], "whatsapp_number": u["national"],
                                             "app_password": password}, follow_redirects=False)


def _code(res, state="st-1"):
    assert res.status_code == 303, (res.status_code, res.text[:300])
    q = parse_qs(urlparse(res.headers["location"]).query)
    assert q["state"] == [state]
    return q["code"][0]


def _token(http, client, **form):
    data = {"client_id": client["client_id"], **form}
    if client.get("client_secret") and "client_secret" not in form:
        data["client_secret"] = client["client_secret"]
    return http.post("/oauth/token", data=data, headers=FORM)


def _exchange(http, client, code, verifier, redirect=CLAUDE_AI):
    return _token(http, client, grant_type="authorization_code", code=code,
                  code_verifier=verifier, redirect_uri=redirect)


def _connect(http, who="A", client=None):
    """Full flow for ``who``; returns ``(client, token_json)``."""
    client = client or _client(http)
    verifier, challenge = _pkce()
    code = _code(_consent(http, _blob(_authorize(http, client, challenge)), who))
    res = _exchange(http, client, code, verifier)
    assert res.status_code == 200, res.text
    return client, res.json()


def _holdings(http, access):
    res = _rpc(http, "tools/call", {"name": "get_holdings", "arguments": {}}, access)
    if res.status_code != 200:
        return res.status_code, ""
    result = _payload(res)["result"]
    return 200, "".join(c.get("text", "") for c in result.get("content", []))


def test_metadata_is_published_at_the_domain_root():
    with mcp_harness() as h:
        base = _base()
        for path in ("/.well-known/oauth-protected-resource/mcp/mcp", "/.well-known/oauth-protected-resource"):
            prm = h.http.get(path)
            assert prm.status_code == 200, path
            body = prm.json()
            assert body["resource"] == base + "/mcp/mcp", body
            assert [s.rstrip("/") for s in body["authorization_servers"]] == [base]
            assert body["scopes_supported"] == ["portfolio:read"]

        meta = h.http.get("/.well-known/oauth-authorization-server").json()
        assert meta["issuer"].rstrip("/") == base
        assert meta["authorization_endpoint"] == base + "/oauth/authorize"
        assert meta["token_endpoint"] == base + "/oauth/token"
        assert meta["registration_endpoint"] == base + "/oauth/register"
        assert meta["revocation_endpoint"] == base + "/oauth/revoke"
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert meta["scopes_supported"] == ["portfolio:read"]
        assert set(meta["grant_types_supported"]) == {"authorization_code", "refresh_token"}

        # Nothing was left under the /mcp mount by mistake for clients to find instead.
        assert h.http.get("/mcp/.well-known/oauth-authorization-server").status_code == 404

        res = _rpc(h.http, "initialize", _INIT)
        assert res.status_code == 401
        www = res.headers["www-authenticate"]
        assert f'resource_metadata="{base}/.well-known/oauth-protected-resource/mcp/mcp"' in www, www
    print("ok  protected-resource + authorization-server metadata at the root; 401 points at it")


def test_registration_only_accepts_allow_listed_redirects():
    from db.models import OAuthClient
    from sqlmodel import select

    with mcp_harness() as h:
        made = _register(h.http)
        assert made.status_code == 201, made.text
        info = made.json()
        assert info["scope"] == "portfolio:read" and info["client_secret"]
        with h.db() as s:
            row = s.exec(select(OAuthClient).where(OAuthClient.client_id == info["client_id"])).first()
        assert row.client_secret_hash and info["client_secret"] not in row.metadata_json
        assert row.client_secret_hash != info["client_secret"]

        for bad in (["https://evil.example/callback"], ["https://claude.ai/api/mcp/other"],
                    ["http://claude.ai/api/mcp/auth_callback"], [CLAUDE_AI, "https://evil.example/cb"],
                    ["javascript:alert(1)"], ["http://localhost:9999/cb#frag"]):
            res = _register(h.http, redirect_uris=bad)
            assert res.status_code == 400, (bad, res.text)
        assert _register(h.http, method="private_key_jwt").status_code == 400
        assert _register(h.http, scope="portfolio:write").status_code == 400
        assert _register(h.http, grant_types=["authorization_code", "refresh_token",
                                              "client_credentials"]).status_code == 400

        # Local clients (Claude Code, Inspector) register loopback callbacks, public or not.
        assert _register(h.http, redirect_uris=["http://127.0.0.1:33418/callback"], method="none").status_code == 201
        assert _register(h.http, redirect_uris=["http://localhost:6274/oauth/callback"]).status_code == 201

        # Client-chosen names are cleaned before they are ever displayed.
        weird = _register(h.http, client_name="Cla‮ude\x00 " + "x" * 200).json()
        assert "‮" not in weird["client_name"] and len(weird["client_name"]) <= 80
    print("ok  DCR: claude.ai/claude.com/loopback only, secret hashed, scope forced, names cleaned")


def test_full_flow_with_pkce_reaches_the_users_portfolio():
    from db.models import OAuthAuthCode, OAuthToken
    from sqlmodel import select

    with mcp_harness() as h:
        client = _client(h.http)
        verifier, challenge = _pkce()
        auth = _authorize(h.http, client, challenge)
        assert auth.headers["cache-control"] == "no-store"
        blob = _blob(auth)

        page = h.http.get("/oauth/consent", params={"request": blob})
        assert page.status_code == 200
        assert "Read-only portfolio access" in page.text and "claude.ai" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"

        code = _code(_consent(h.http, blob, "A"))
        assert code.startswith("mfc_")
        res = _exchange(h.http, client, code, verifier)
        assert res.status_code == 200, res.text
        assert res.headers["cache-control"] == "no-store"
        tok = res.json()
        assert tok["token_type"] == "Bearer" and tok["scope"] == "portfolio:read"
        assert tok["access_token"].startswith("mfa_") and tok["refresh_token"].startswith("mfr_")
        assert 0 < tok["expires_in"] <= 3600

        assert _rpc(h.http, "initialize", _INIT, tok["access_token"]).status_code == 200
        status, text = _holdings(h.http, tok["access_token"])
        assert status == 200 and "INFY-EQ" in text and "TCS-EQ" not in text, text

        with h.db() as s:
            hashes = [r.token_hash for r in s.exec(select(OAuthToken)).all()]
            code_rows = s.exec(select(OAuthAuthCode)).all()
        assert len(hashes) == 2 and tok["access_token"] not in hashes and tok["refresh_token"] not in hashes
        assert hashlib.sha256(tok["access_token"].encode()).hexdigest() in hashes
        assert code_rows[0].code_hash != code and code_rows[0].used_at is not None
        with open(h.engine.url.database, "rb") as fh:
            db_bytes = fh.read()
        for raw in (tok["access_token"], tok["refresh_token"], code, client["client_secret"]):
            assert raw.encode() not in db_bytes
    print("ok  register -> authorize -> consent -> token (PKCE S256) -> MCP reads A's holdings; all hashed")


def test_pkce_is_required_and_a_wrong_verifier_burns_the_code():
    with mcp_harness() as h:
        client = _client(h.http)
        verifier, challenge = _pkce()

        # No challenge, a plain challenge, or a non-S256 method: refused before consent.
        res = h.http.get("/oauth/authorize", params={
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": CLAUDE_AI,
            "state": "s"}, follow_redirects=False)
        assert "error=invalid_request" in res.headers.get("location", ""), res.text
        assert "error=invalid_request" in _authorize(h.http, client, "short").headers["location"]
        res = _authorize(h.http, client, challenge, code_challenge_method="plain")
        assert "error=invalid_request" in res.headers["location"]

        code = _code(_consent(h.http, _blob(_authorize(h.http, client, challenge)), "A"))
        wrong = _exchange(h.http, client, code, secrets.token_urlsafe(48))
        assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant", wrong.text
        right = _exchange(h.http, client, code, verifier)
        assert right.status_code == 400 and right.json()["error"] == "invalid_grant", right.text
    print("ok  PKCE S256 required; a wrong verifier fails and the code cannot be retried")


def test_redirect_uris_must_match_exactly():
    with mcp_harness() as h:
        client = _client(h.http, redirect_uris=[CLAUDE_AI])
        verifier, challenge = _pkce()
        for bad in (CLAUDE_COM, CLAUDE_AI + "/", CLAUDE_AI + "?x=1", "https://evil.example/cb"):
            res = _authorize(h.http, client, challenge, redirect=bad)
            # Never redirected to an unregistered URI: answered directly.
            assert res.status_code == 400 and "location" not in res.headers, (bad, res.status_code)

        # The token request must name the same redirect URI as the authorization request.
        code = _code(_consent(h.http, _blob(_authorize(h.http, client, challenge)), "A"))
        res = _exchange(h.http, client, code, verifier, redirect=CLAUDE_COM)
        assert res.status_code == 400 and res.json()["error"] == "invalid_request", res.text

        # Unknown client: no redirect either.
        res = _authorize(h.http, {"client_id": "nope"}, challenge)
        assert res.status_code == 400 and "location" not in res.headers
    print("ok  unregistered or altered redirect URIs are refused without a redirect; token step re-checks")


def test_codes_are_single_use_and_replay_revokes_the_grant():
    from db.models import OAuthAuthCode
    from db.models import utcnow
    from sqlmodel import select

    with mcp_harness() as h:
        client = _client(h.http)
        verifier, challenge = _pkce()
        code = _code(_consent(h.http, _blob(_authorize(h.http, client, challenge)), "A"))
        first = _exchange(h.http, client, code, verifier).json()
        assert _holdings(h.http, first["access_token"])[0] == 200

        replay = _exchange(h.http, client, code, verifier)
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
        assert _holdings(h.http, first["access_token"])[0] == 401, "replayed code did not revoke its tokens"
        res = _token(h.http, client, grant_type="refresh_token", refresh_token=first["refresh_token"])
        assert res.status_code == 400 and res.json()["error"] == "invalid_grant"

        # Another client cannot redeem someone else's code (and does not burn it).
        other = _client(h.http)
        verifier2, challenge2 = _pkce()
        code2 = _code(_consent(h.http, _blob(_authorize(h.http, client, challenge2)), "A"))
        assert _exchange(h.http, other, code2, verifier2).json()["error"] == "invalid_grant"
        assert _exchange(h.http, client, code2, verifier2).status_code == 200

        # Expired codes are refused.
        verifier3, challenge3 = _pkce()
        code3 = _code(_consent(h.http, _blob(_authorize(h.http, client, challenge3)), "A"))
        with h.db() as s:
            for row in s.exec(select(OAuthAuthCode).where(OAuthAuthCode.used_at == None)).all():  # noqa: E711
                row.expires_at = utcnow() - timedelta(seconds=1)
                s.add(row)
            s.commit()
        assert _exchange(h.http, client, code3, verifier3).json()["error"] == "invalid_grant"
    print("ok  codes: single use, replay revokes the first exchange's tokens, client-bound, expire")


def test_refresh_rotates_and_reuse_revokes_the_grant():
    from db.models import OAuthToken
    from db.models import utcnow
    from services import oauth_provider
    from sqlmodel import select

    with mcp_harness() as h:
        client, tok = _connect(h.http)
        res = _token(h.http, client, grant_type="refresh_token", refresh_token=tok["refresh_token"])
        assert res.status_code == 200, res.text
        new = res.json()
        assert new["refresh_token"] != tok["refresh_token"] and new["access_token"] != tok["access_token"]
        assert _holdings(h.http, new["access_token"])[0] == 200

        # A retry within the grace window (lost response) is not treated as theft...
        retry = _token(h.http, client, grant_type="refresh_token", refresh_token=tok["refresh_token"])
        assert retry.status_code == 200, retry.text

        # ...but presenting a rotated token after it revokes the whole grant.
        with h.db() as s:
            row = s.exec(select(OAuthToken).where(
                OAuthToken.token_hash == hashlib.sha256(tok["refresh_token"].encode()).hexdigest())).first()
            row.used_at = utcnow() - timedelta(seconds=oauth_provider.REFRESH_REUSE_GRACE_SEC + 5)
            s.add(row)
            s.commit()
        stolen = _token(h.http, client, grant_type="refresh_token", refresh_token=tok["refresh_token"])
        assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_grant"
        assert _holdings(h.http, new["access_token"])[0] == 401
        after = _token(h.http, client, grant_type="refresh_token", refresh_token=new["refresh_token"])
        assert after.status_code == 400

        # A refresh token only works for the client it was issued to, and only for portfolio:read.
        client2, tok2 = _connect(h.http)
        other = _client(h.http)
        res = _token(h.http, other, grant_type="refresh_token", refresh_token=tok2["refresh_token"])
        assert res.status_code == 400
        res = _token(h.http, client2, grant_type="refresh_token", refresh_token=tok2["refresh_token"],
                     scope="portfolio:write")
        assert res.status_code == 400 and res.json()["error"] == "invalid_scope"
    print("ok  refresh: rotates, tolerates a quick retry, reuse later revokes everything, client-bound")


def test_revocation_endpoint_revokes_the_grant():
    with mcp_harness() as h:
        client, tok = _connect(h.http)
        other = _client(h.http)
        # Another client's revocation request is accepted (RFC 7009) but does nothing.
        res = h.http.post("/oauth/revoke", data={"client_id": other["client_id"],
                                                 "client_secret": other["client_secret"],
                                                 "token": tok["access_token"]}, headers=FORM)
        assert res.status_code == 200
        assert _holdings(h.http, tok["access_token"])[0] == 200

        # Wrong secret: 401.
        res = h.http.post("/oauth/revoke", data={"client_id": client["client_id"], "client_secret": "nope",
                                                 "token": tok["refresh_token"]}, headers=FORM)
        assert res.status_code == 401

        res = h.http.post("/oauth/revoke", data={"client_id": client["client_id"],
                                                 "client_secret": client["client_secret"],
                                                 "token": tok["refresh_token"],
                                                 "token_type_hint": "refresh_token"}, headers=FORM)
        assert res.status_code == 200
        assert _holdings(h.http, tok["access_token"])[0] == 401
        res = _token(h.http, client, grant_type="refresh_token", refresh_token=tok["refresh_token"])
        assert res.status_code == 400

        # Revoking by access token works too.
        client2, tok2 = _connect(h.http)
        h.http.post("/oauth/revoke", data={"client_id": client2["client_id"],
                                           "client_secret": client2["client_secret"],
                                           "token": tok2["access_token"]}, headers=FORM)
        assert _holdings(h.http, tok2["access_token"])[0] == 401
        assert _token(h.http, client2, grant_type="refresh_token",
                      refresh_token=tok2["refresh_token"]).status_code == 400
    print("ok  /oauth/revoke kills access and refresh together; only the owning client can revoke")


def test_client_authentication():
    with mcp_harness() as h:
        client = _client(h.http)
        verifier, challenge = _pkce()
        code = _code(_consent(h.http, _blob(_authorize(h.http, client, challenge)), "A"))
        res = _token(h.http, client, grant_type="authorization_code", code=code, code_verifier=verifier,
                     redirect_uri=CLAUDE_AI, client_secret="wrong")
        assert res.status_code == 401

        # client_secret_basic.
        basic = _client(h.http, method="client_secret_basic")
        verifier, challenge = _pkce()
        code = _code(_consent(h.http, _blob(_authorize(h.http, basic, challenge)), "A"))
        auth = base64.b64encode(f"{basic['client_id']}:{basic['client_secret']}".encode()).decode()
        res = h.http.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                "code_verifier": verifier, "redirect_uri": CLAUDE_AI,
                                                "client_id": basic["client_id"]},
                          headers={**FORM, "Authorization": f"Basic {auth}"})
        assert res.status_code == 200, res.text

        # Public client: PKCE alone.
        public = _client(h.http, method="none", redirect_uris=["http://127.0.0.1:33418/callback"])
        assert "client_secret" not in public or not public["client_secret"]
        verifier, challenge = _pkce()
        code = _code(_consent(h.http, _blob(_authorize(h.http, public, challenge,
                                                       redirect="http://127.0.0.1:33418/callback")), "A"))
        res = _exchange(h.http, public, code, verifier, redirect="http://127.0.0.1:33418/callback")
        assert res.status_code == 200, res.text
    print("ok  token endpoint: wrong secret 401; client_secret_basic and public PKCE clients work")


def test_consent_requires_the_users_password_and_can_be_declined():
    from db.models import OAuthAuthCode
    from sqlmodel import select

    with mcp_harness() as h:
        client = _client(h.http)
        _v, challenge = _pkce()
        blob = _blob(_authorize(h.http, client, challenge))

        res = _consent(h.http, blob, "A", password="not-the-password")
        assert res.status_code == 401 and "location" not in res.headers
        assert "don&#39;t match" in res.text or "don't match" in res.text
        with h.db() as s:
            assert s.exec(select(OAuthAuthCode)).first() is None

        res = _consent(h.http, blob, "A", decision="deny")
        assert res.status_code == 303
        q = parse_qs(urlparse(res.headers["location"]).query)
        assert res.headers["location"].startswith(CLAUDE_AI)
        assert q["error"] == ["access_denied"] and q["state"] == ["st-1"] and "code" not in q

        # Tampered or garbage request: no redirect anywhere.
        for bad in (blob[:-4] + "AAAA", "x", ""):
            assert h.http.get("/oauth/consent", params={"request": bad}).status_code == 400
            assert _consent(h.http, bad, "A").status_code == 400

        # The login throttle applies (5 per number per window).
        for _ in range(5):
            _consent(h.http, blob, "B", password="wrong-wrong-wrong")
        res = _consent(h.http, blob, "B")
        assert res.status_code == 429, res.status_code

        # An inactive user cannot approve.
        h.service.deactivate_user(h.ids["A"])
        assert _consent(h.http, blob, "A").status_code == 401
    print("ok  consent: password required (throttled), deny returns access_denied, tampering is refused")


def test_tokens_are_bound_to_the_mcp_resource():
    from db.models import OAuthGrant
    from sqlmodel import select

    with mcp_harness() as h:
        client = _client(h.http)
        _v, challenge = _pkce()
        res = _authorize(h.http, client, challenge, resource="https://evil.example/mcp")
        assert res.status_code == 302 and "error=invalid_request" in res.headers["location"]
        # Omitting the resource binds the grant to this server anyway.
        assert _authorize(h.http, client, challenge, resource=None).status_code == 302
        # A trailing slash is the same resource.
        assert "/oauth/consent" in _authorize(h.http, client, challenge,
                                              resource=_base() + "/mcp/mcp/").headers["location"]

        _c, tok = _connect(h.http, client=client)
        assert _holdings(h.http, tok["access_token"])[0] == 200
        with h.db() as s:
            g = s.exec(select(OAuthGrant)).first()
            g.resource = "https://other.example/mcp"
            s.add(g)
            s.commit()
        assert _holdings(h.http, tok["access_token"])[0] == 401
    print("ok  tokens carry the MCP resource; a token for any other resource is refused")


def test_each_user_sees_only_their_own_data_and_connections():
    with mcp_harness() as h:
        client = _client(h.http)
        _c, tok_a = _connect(h.http, "A", client)
        _c, tok_b = _connect(h.http, "B", client)
        _s, text_a = _holdings(h.http, tok_a["access_token"])
        _s, text_b = _holdings(h.http, tok_b["access_token"])
        assert "INFY-EQ" in text_a and "TCS-EQ" not in text_a
        assert "TCS-EQ" in text_b and "INFY-EQ" not in text_b

        web_a = h.login("A")
        web_b = h.login("B")
        conns_a = web_a.get("/api/oauth/connections").json()["connections"]
        conns_b = web_b.get("/api/oauth/connections").json()["connections"]
        assert len(conns_a) == 1 and len(conns_b) == 1 and conns_a[0]["id"] != conns_b[0]["id"]
        assert conns_a[0]["client_name"] == "Claude"
        assert web_a.delete(f"/api/oauth/connections/{conns_b[0]['id']}").status_code == 404
        assert _holdings(h.http, tok_b["access_token"])[0] == 200

        assert web_a.delete(f"/api/oauth/connections/{conns_a[0]['id']}").status_code == 200
        assert _holdings(h.http, tok_a["access_token"])[0] == 401
        assert _holdings(h.http, tok_b["access_token"])[0] == 200

        from fastapi.testclient import TestClient
        assert TestClient(h.app, base_url="https://testserver").get("/api/oauth/connections").status_code == 401
    print("ok  A's and B's OAuth tokens read only their own holdings; connections are owner-scoped")


def test_deactivation_and_account_deletion_end_oauth_access():
    from db.models import OAuthGrant, OAuthToken
    from sqlmodel import select

    with mcp_harness() as h:
        client, tok_b = _connect(h.http, "B")
        h.service.deactivate_user(h.ids["B"])
        assert _holdings(h.http, tok_b["access_token"])[0] == 401
        assert _token(h.http, client, grant_type="refresh_token",
                      refresh_token=tok_b["refresh_token"]).status_code == 400

        _c, tok_a = _connect(h.http, "A", client)
        web_a = h.login("A")
        res = web_a.request("DELETE", "/api/account", json={"app_password": PASSWORD})
        assert res.status_code == 200, res.text
        assert _holdings(h.http, tok_a["access_token"])[0] == 401
        with h.db() as s:
            assert s.exec(select(OAuthGrant).where(OAuthGrant.user_id == h.ids["A"])).first() is None
            assert len(s.exec(select(OAuthToken)).all()) == 2  # B's pair is untouched
    print("ok  a deactivated user's OAuth tokens stop working; account deletion removes the grants")


def test_personal_access_tokens_still_work_beside_oauth():
    with mcp_harness() as h:
        pat = _issue(h.ids["A"])
        _c, tok = _connect(h.http, "B")
        _s, text_pat = _holdings(h.http, pat)
        _s, text_oauth = _holdings(h.http, tok["access_token"])
        assert "INFY-EQ" in text_pat and "TCS-EQ" in text_oauth
        # A PAT is not an OAuth token: the revocation endpoint ignores it.
        res = h.http.post("/oauth/revoke", data={"token": pat, "client_id": _c["client_id"],
                                                 "client_secret": _c["client_secret"]}, headers=FORM)
        assert res.status_code == 200 and _holdings(h.http, pat)[0] == 200
    print("ok  personal access tokens and OAuth tokens work side by side")


def test_shared_ip_users_are_not_locked_out_by_expired_tokens_or_guesses():
    from db.models import OAuthToken
    from db.models import utcnow
    from services import mcp_tokens
    from sqlmodel import select

    with mcp_harness() as h:
        original = mcp_tokens._MAX_UNKNOWN_PER_IP
        mcp_tokens._MAX_UNKNOWN_PER_IP = 3
        try:
            _c, tok_a = _connect(h.http, "A")
            _c, tok_b = _connect(h.http, "B")
            with h.db() as s:
                row = s.exec(select(OAuthToken).where(OAuthToken.token_hash == hashlib.sha256(
                    tok_b["access_token"].encode()).hexdigest())).first()
                row.expires_at = utcnow() - timedelta(seconds=1)
                s.add(row)
                s.commit()
            # Many clients behind one IP retrying expired tokens: not counted as guesses.
            for _ in range(10):
                assert _holdings(h.http, tok_b["access_token"])[0] == 401
            assert _holdings(h.http, tok_a["access_token"])[0] == 200
            # Guesses from the same IP trip the cap...
            for _ in range(5):
                assert _holdings(h.http, "mfa_" + secrets.token_urlsafe(32))[0] == 401
            # ...yet a real user behind it keeps working, and a fresh connection works at once.
            assert _holdings(h.http, tok_a["access_token"])[0] == 200
            _c, tok_new = _connect(h.http, "B")
            assert _holdings(h.http, tok_new["access_token"])[0] == 200
        finally:
            mcp_tokens._MAX_UNKNOWN_PER_IP = original
    print("ok  limiter: expired tokens don't count, guesses are capped, real users behind the IP keep working")


def test_no_oauth_secret_reaches_the_logs():
    import logging

    from test_mcp_auth import _Capture

    cap = _Capture()
    root = logging.getLogger()
    prior = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    try:
        with mcp_harness() as h:
            client = _client(h.http)
            verifier, challenge = _pkce()
            code = _code(_consent(h.http, _blob(_authorize(h.http, client, challenge)), "A"))
            tok = _exchange(h.http, client, code, verifier).json()
            _holdings(h.http, tok["access_token"])
            new = _token(h.http, client, grant_type="refresh_token", refresh_token=tok["refresh_token"]).json()
            _holdings(h.http, tok["access_token"] + "x")
            h.http.post("/oauth/revoke", data={"client_id": client["client_id"],
                                               "client_secret": client["client_secret"],
                                               "token": new["refresh_token"]}, headers=FORM)
    finally:
        root.removeHandler(cap)
        root.setLevel(prior)
    # httpx's own request log shows client-side URLs; only server-side lines matter here,
    # and the token endpoint's body (code, verifier, secret) never appears in a URL.
    logs = "\n".join(cap.lines)
    for secret in (client["client_secret"], code, verifier, tok["access_token"], tok["refresh_token"],
                   new["access_token"], new["refresh_token"], PASSWORD):
        assert secret not in logs, f"{secret[:6]}… appeared in logs"
    print("ok  client secrets, codes, verifiers, tokens and the app password never reach the logs")


def test_every_tool_is_annotated_read_only():
    with mcp_harness() as h:
        _c, tok = _connect(h.http)
        tools = _payload(_rpc(h.http, "tools/list", {}, tok["access_token"]))["result"]["tools"]
        assert len(tools) == 15
        for t in tools:
            ann = t.get("annotations") or {}
            assert t.get("title") and ann.get("title") == t["title"], t["name"]
            assert ann.get("readOnlyHint") is True and ann.get("destructiveHint") is False, t["name"]
    print("ok  all 15 tools carry a title and readOnlyHint=true, destructiveHint=false")


@contextlib.contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _MemoryStorage:
    def __init__(self):
        self.tokens = None
        self.client_info = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


def test_real_mcp_sdk_oauth_client_connects_over_loopback():
    import httpx
    import uvicorn
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with _env(PUBLIC_BASE_URL=base), harness(app_factory=_mcp_app) as h:
        server = uvicorn.Server(uvicorn.Config(h.app, host="127.0.0.1", port=port,
                                               lifespan="on", log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"

        callback = "http://127.0.0.1:33419/callback"
        captured: dict = {}
        u = USERS["A"]

        async def redirect_handler(authorization_url: str) -> None:
            # Plays the user's browser: follow /authorize to the consent page, sign in, allow.
            async with httpx.AsyncClient(timeout=15) as browser:
                res = await browser.get(authorization_url)
                assert res.status_code == 302, res.text
                consent_url = res.headers["location"]
                assert consent_url.startswith(base + "/oauth/consent"), consent_url
                page = await browser.get(consent_url)
                assert page.status_code == 200 and "Read-only portfolio access" in page.text
                blob = parse_qs(urlparse(consent_url).query)["request"][0]
                res = await browser.post(base + "/oauth/consent", data={
                    "request": blob, "decision": "allow", "country_code": u["dial"],
                    "whatsapp_number": u["national"], "app_password": PASSWORD})
                assert res.status_code == 303, res.text
                captured["location"] = res.headers["location"]

        async def callback_handler():
            q = parse_qs(urlparse(captured["location"]).query)
            assert captured["location"].startswith(callback)
            return q["code"][0], q.get("state", [None])[0]

        storage = _MemoryStorage()
        auth = OAuthClientProvider(
            server_url=base + "/mcp/mcp",
            client_metadata=OAuthClientMetadata(
                client_name="SDK test client", redirect_uris=[callback],
                grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                token_endpoint_auth_method="client_secret_post",
            ),
            storage=storage, redirect_handler=redirect_handler, callback_handler=callback_handler,
        )

        async def session():
            async with httpx.AsyncClient(auth=auth, timeout=20) as hc:
                async with streamable_http_client(base + "/mcp/mcp", http_client=hc) as (read, write, _sid):
                    async with ClientSession(read, write) as s:
                        await s.initialize()
                        tools = await s.list_tools()
                        res = await s.call_tool("get_holdings", {})
                        return tools, res

        try:
            tools, res = asyncio.run(asyncio.wait_for(session(), timeout=60))
            text = "".join(getattr(c, "text", "") for c in res.content)
            assert "INFY-EQ" in text and "TCS-EQ" not in text, text
            assert all(t.annotations and t.annotations.readOnlyHint for t in tools.tools)
            assert storage.tokens.access_token.startswith("mfa_")
            assert storage.client_info.client_id
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    print("ok  the MCP SDK's OAuth client discovers, registers, signs in, and reads A's holdings")


if __name__ == "__main__":
    run_all(dict(globals()))
