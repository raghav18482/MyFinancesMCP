# Client IP mode

`CLIENT_IP_MODE` decides which `X-Forwarded-For` entry the app believes for
per-IP **security** limits (login, account deletion, MCP token issue, OAuth
consent, remote admin). See `web/client_ip.py`.

| Mode | Trusts | Use when |
|---|---|---|
| `shadow` (default) | nothing; per-IP security limits are off, per-identifier and global caps still apply | before measuring, and as the rollback |
| `render_last` | the last entry | the measurement shows Render appends the visitor address |
| `render_first` | the first entry | the measurement shows Render overwrites the header |
| `edge_secret` | the first entry when `X-Edge-Auth` matches `EDGE_AUTH_SECRET` (≥ 32 chars), else the last | Vercel Routing Middleware adds the header |

In production an unknown mode, or `edge_secret` without a strong secret, stops
startup. The startup log says `CLIENT_IP mode=<mode> per_ip_security_limits=on|off`.

## Measuring (needs a release from R2 to R5; R6 removed the endpoint)

1. Set `IP_DIAG_TOKEN` to a random value and `IP_DIAG_UNTIL` to a UTC time
   under 2 hours ahead.
2. Call `GET /__diag/ip?k=<token>` four ways: through `https://agentfolio.in`
   and through the Render host, each with and without
   `-H 'X-Forwarded-For: 6.6.6.6'`.
3. Pick the mode in which the forged `6.6.6.6` is never the entry the mode
   trusts, and your real address always is, on both hosts. If no mode satisfies
   both hosts, stay on `shadow` and consider `edge_secret`.
4. Unset `IP_DIAG_TOKEN` and `IP_DIAG_UNTIL`. Record the four results.

## Enforcing

1. Deploy R6 with `CLIENT_IP_MODE` still `shadow`.
2. Set the measured mode (approval needed). Check the startup line.
3. Watch for false `429`s on login and account deletion for 48 hours.

Rollback: set `CLIENT_IP_MODE=shadow`. No redeploy of code is needed.
