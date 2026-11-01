# Admin API: enabling, using and rotating

The admin API (`/api/admin/*`) can list users and create or change them, including their broker credentials. It is therefore off in production unless deliberately switched on, and only answers from inside the instance.

## Rules the code enforces

| Rule | Where |
|---|---|
| In production the router is mounted only when `ADMIN_API_ENABLED=1`. Otherwise every admin path is a 404. | `web/app.create_app`, `security_config.admin_api_enabled` |
| Requests must come from loopback (`127.0.0.1` or `::1`) with no `X-Forwarded-For`, `Forwarded` or `X-Real-IP` header. Public traffic always reaches the app through Render's proxy, so this means a Render Shell session on the instance. Anything else is a 404. | `security_config.admin_request_source` |
| Remote access also needs `ADMIN_ALLOW_REMOTE=1`, an `ADMIN_ALLOWED_SOURCES` CIDR list, and a verified `CLIENT_IP_MODE` (not `shadow`) whose trusted address is inside the list. A forwarded header alone never decides. | same |
| `ADMIN_API_KEY` must be at least 32 characters, or every admin request is a 503 and `ADMIN_KEY_MISCONFIGURED` is logged. | `adminApi.router.check_admin_key` |
| A correct key always succeeds. Wrong keys are capped at 10 per 15 minutes per source (then 429 for that source only). More than 50 failures an hour logs `ADMIN_AUTH_FAILURES_HIGH` and slows failed responses by 1 s. | `adminApi.guard` |
| Responses never contain the Angel API key, PIN, TOTP secret or tokens, and 422 errors never echo request values. | `adminApi.service.public_view`, `web.app._sanitized_validation_error` |

At startup one line is logged: `ADMIN_API status=<enabled|disabled> key=<ok|missing|too_short> remote=<off|on|misconfigured>`. It never contains the key or its length.

Set log alerts on `ADMIN_KEY_MISCONFIGURED`, `ADMIN_AUTH_FAILURES_HIGH` and `ADMIN_REMOTE_MISCONFIGURED`.

## Using it

1. Generate a fresh key on your machine:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
2. In Render, set `ADMIN_API_KEY` to it and set `ADMIN_API_ENABLED=1`. Render restarts the service.
3. Open a Render Shell on the instance and read the key without echoing it or saving it in shell history:
   ```bash
   read -s ADMIN_KEY
   ```
   then call the API over loopback:
   ```bash
   curl -s -H "X-Admin-Key: $ADMIN_KEY" "http://127.0.0.1:$PORT/api/admin/users"
   ```
4. When finished, remove `ADMIN_API_ENABLED` (Render restarts), and replace `ADMIN_API_KEY` with a new value. Rotate the key after every use: the API is off between uses, so there is never a second key to keep valid.

Never paste the key into chat, tickets, commit messages or a shell command line.

## Remote access (exceptional)

Only after `CLIENT_IP_MODE` has been verified (see `web/client_ip.py`):

1. Set `ADMIN_ALLOW_REMOTE=1` and `ADMIN_ALLOWED_SOURCES` to the narrowest CIDR that covers you.
2. Check the startup line says `remote=on`. `remote=misconfigured` means remote access is still off.
3. Remove both variables as soon as you're done, and rotate the key.
