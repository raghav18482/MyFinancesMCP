# Staging

A separate copy of the backend and the WhatsApp service, built from each
repository's `staging` branch, on its own Render services and its own empty
database. Nothing here touches production.

Render sets `RENDER=true` on every service, so staging runs with production
rules: it needs its own secrets, and it starts only on a database at a
revision this release lists (`db/schema_version.py`).

## 1. Database (an empty Neon database, not a copy of production)

1. **Create a new Neon database or branch with no data.** Note both its
   pooled and its direct connection strings.
2. **Build the schema** from your own machine (not from Render; locally the
   migrations accept an empty database):
   ```bash
   pip install -r requirements.txt
   env -u RENDER -u APP_ENV MIGRATION_DATABASE_URL="<staging DIRECT url>" \
     alembic -x label=rehearsal upgrade head
   ```
   The label `rehearsal` means production's migration rules don't apply. Later
   merges into `staging` that add a revision just repeat this command.

## 2. WhatsApp service: Render web service `whatsapp-webhook-service-staging`

| Setting | Value |
|---|---|
| Repository / branch | `Whatsapp-webhook-service` / `staging` |
| Runtime | Node |
| Build command | `npm ci` (`node_modules` is no longer committed) |
| Start command | `node index.js` |
| Instances | 1. One WhatsApp session can't be shared. |
| Disk (optional) | mounted at `/var/data`. Without it, you re-scan the QR code after every restart. |

| Variable | Value |
|---|---|
| `WEBHOOK_TOKEN` | a new random value, 32+ characters (`node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"`) |
| `WEBHOOK_AUTH_MODE` | `enforce` |
| `AUTH_DIR` | `/var/data/auth_info` with a Disk, else leave unset |

After the first deploy, open the service's logs, scan the QR code with
WhatsApp (Settings → Linked devices). Staging becomes another linked device
and **sends from your own number**: enroll only yourself as a test user.

## 3. Backend: Render web service `myfinancesmcp-staging`

| Setting | Value |
|---|---|
| Repository / branch | `MyFinancesMCP` / `staging` |
| Runtime | Docker (the repository's `Dockerfile`), as production |
| Instances | 1, autoscaling off |

Use **new values for every secret**. Never copy production's.

| Variable | Value |
|---|---|
| `DATABASE_URL` | staging pooled URL |
| `MIGRATION_DATABASE_URL` | staging direct URL |
| `ENCRYPTION_KEY` | new: `python -m db.crypto generate` |
| `TOKEN_KEYS` | `t1:` + another new key from the same command (must differ from `ENCRYPTION_KEY`) |
| `SESSION_SECRET` | new, 32+ characters |
| `ALLOWED_HOSTS` | `myfinancesmcp-staging.onrender.com` (the service's actual host) |
| `PUBLIC_BASE_URL` | `https://myfinancesmcp-staging.onrender.com` |
| `WHATSAPP_WEBHOOK_URL` | `https://<whatsapp staging host>/send` (**required**: without it staging sends through production's service) |
| `WHATSAPP_WEBHOOK_TOKEN` | the same value as the webhook's `WEBHOOK_TOKEN` |
| `SCHEDULER_ENABLED` | `1` to test scheduled briefings, else unset |
| `CLIENT_IP_MODE` | unset (`shadow`) |
| LLM / market-data keys | whatever features you want to test, ideally separate keys |

Leave unset: `ADMIN_API_ENABLED`, `FEEDBACK_WHATSAPP_REPLY`, `IP_DIAG_*`.

## 4. Smoke test

1. `GET https://<backend>/api/auth/config` answers.
2. The startup log shows `init_db: schema at 0003_broker_connections` and
   `CLIENT_IP mode=shadow`.
3. Enroll yourself at `/enroll` with your Angel One credentials.
   **Caution (G-ANGEL, still unanswered):** if a new Angel One login ends
   older sessions, logging in on staging can log you out of production.
4. Send a manual briefing: it arrives on WhatsApp, the webhook's log shows
   `sent 1 message(s) to …NNNN auth=ok`, and
   `SELECT status FROM briefing_deliveries` shows `accepted`.

## The UI

`AgentfolioUI/vercel.json` sends `/api`, `/mcp`, `/oauth` and `/.well-known`
to the **production** backend. A Vercel preview of the UI's `staging` branch
would therefore talk to production. Until that address is made configurable,
test the UI against staging locally; its dev server proxies to
`AGENTFOLIO_BACKEND`:

```bash
cd AgentfolioUI && git checkout staging
AGENTFOLIO_BACKEND=https://myfinancesmcp-staging.onrender.com npm run dev
```
