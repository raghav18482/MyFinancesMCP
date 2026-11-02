# Token keys and data keys

Two key rings, never shared (`db/crypto.py`):

| Ring | Variables | Protects |
|---|---|---|
| Data keys | `ENCRYPTION_KEY` (version 1); optional `DATA_ENCRYPTION_KEYS="2:…"` and `DATA_ENCRYPTION_PRIMARY` | Broker credentials at rest |
| Token keys | `TOKEN_KEYS="t1:…"`; `TOKEN_PRIMARY` when there are several | The feedback form token and the pending OAuth consent request (ten minutes each) |

Production refuses to start without `TOKEN_KEYS`. It also refuses if a token
key equals a data key, or if a key or id is malformed. Error messages never
include a key.

Generate a key:

```bash
python -m db.crypto generate
```

## History: R8 and R9 (needs approval: an environment change)

- **R8** introduced `TOKEN_KEYS`. Before deploying it, generate a key and set
  `TOKEN_KEYS=t1:<key>` on the Render backend service (never `ENCRYPTION_KEY`).
  For one release it still accepted pre-R8 tokens (no kid, sealed with
  `ENCRYPTION_KEY`), logging `LEGACY_TOKEN_OPENED audience=…` for each.
- **R9** removes that path. Deploy it only when **both** hold:
  - R8 has been live for at least 1 hour (tokens live 10 minutes);
  - Render logs show no `LEGACY_TOKEN_OPENED` in the last 30 minutes.

  There is no time constant in the code, so restarts don't matter. A pre-R8
  token that turns up after R9 is refused: the visitor reloads the form, or
  restarts the connector's sign-in.

## Rotating the token key

1. Set `TOKEN_KEYS=t1:<old>,t2:<new>` and `TOKEN_PRIMARY=t2`, then redeploy.
2. Wait at least 10 minutes plus clock skew.
3. Set `TOKEN_KEYS=t2:<new>` and remove `TOKEN_PRIMARY`, then redeploy.

Nothing is re-encrypted. Tokens sealed with `t1` stop opening at step 3.

## Rotating the data key (later; not before the broker backfill)

1. Add `DATA_ENCRYPTION_KEYS=2:<new>`. Code that stores a key version can
   then use `DATA_ENCRYPTION_PRIMARY=2`.
2. Re-encrypt, and verify, every v1 ciphertext with v2.
3. Only then retire `ENCRYPTION_KEY`.

Decryption always uses exactly the stored version, never "try each key".
Today's credential columns are all version 1.

## Rollback

- **From R9 to R8:** safe; R8 opens everything R9 issues.
- **From R8 to R7:** tokens issued by R8 stop opening. At most 10 minutes of
  open forms and consent pages are affected.
