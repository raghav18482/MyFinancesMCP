# RCA: fundamentals and AI summary unavailable in production (2026-10-11)

## Summary

Every fundamentals request on agentfolio.in failed, for every stock. Users saw
"No data found for SUZLON.NS", even for long-listed companies. The Fundamentals
tab (web and mobile) and the AI "plain English" summary were both down. Charts,
technicals and predictions use Angel One data and were not affected.

The **same code worked from a developer laptop**: SUZLON came back with P/E
16.5, ROE 39.4%, score 83 (A). Only production failed.

## Impact

* **What:** `GET /api/research/fundamental` and
  `POST /api/research/fundamental/summary`, for every symbol.
* **Seen as:** the web showed a page of "—" values. The mobile app showed "Not
  available for this stock", then, after PR #15, "Couldn't reach Yahoo Finance".
* **Duration:** unknown start, still ongoing when this RCA was written.
  Fundamentals may never have worked from Render.
* **Data:** nothing was lost or corrupted.

## Root cause

There are three causes, which stacked on top of each other.

### 1. Yahoo refuses requests from the production server's IP (primary)

Yahoo Finance has no official API. yfinance reads Yahoo's web endpoints,
which rate limit and block **by outbound IP**. Render's outbound IPs are shared
by many tenants, and cloud ranges are routinely refused with `429 Too Many
Requests` or `401 Unauthorized / Invalid Crumb`. This is widely reported for
Render, Streamlit Cloud, Colab and AWS (yfinance issues #2411, #2422, #2518).

Evidence:

* The identical code, yfinance version (1.7.0, pinned) and symbols work from a
  residential IP and fail from Render.
* After PR #15, production returned the new "Couldn't reach Yahoo Finance"
  path. That path is only taken when Yahoo answered with an error, or with an
  empty `info` for a symbol it quotes. It is never taken for "no such
  symbol".
* It failed on a **freshly deployed** process that had made almost no Yahoo
  calls of its own. That points at the IP's reputation, not only our volume.

### 2. yfinance hid the error, so the failure looked like "no data" (masking)

yfinance defaults to `config.debug.hide_exceptions = True`. On an HTTP error it
logs `str(e) + response.text` to its own `yfinance` logger and returns an empty
`info`. `fundamental_service` read an empty `info` as "Yahoo has no data for
this symbol", returned a 200 with `error: "No data found"`, and **cached that
for 6 hours**. So:

* the real 401/429 never reached our logs as a failure;
* the API told clients "no data", which is permanent, and not "try again";
* one refusal blanked a stock for 6 hours, even after Yahoo recovered.

### 3. Our own traffic made the refusals worse and longer (contributing)

Five code paths called Yahoo with no shared limit:

| Path | Requests | Concurrency |
|---|---|---|
| `fundamental_service` | 4 per stock (info, financials, balance sheet, holders) | 4 threads |
| `peer_service` | up to 24 peers × 4 | 8 threads → ~32 in flight |
| `estimates_service` | 6 per stock | 6 threads |
| `ownership_service` | 2 per stock | serial |
| agent tool `get_stock_fundamentals` | 4 per stock | 4 threads |

A single peer comparison could burst about 100 requests from one IP. Retrying
after a 429 (PR #15 added one retry) only extends the ban.

Nothing survived a deploy either. The in-memory cache and the point-in-time
snapshots (`data/pit/`, on the container's ephemeral disk) were wiped on each
deploy, so every deploy restarted cold and went straight back to Yahoo.

### Found while fixing: a latent crash in predictions

`services/features/fundamental.py` joins fundamentals snapshots onto the
feature panel with `pd.merge_asof`. Snapshots read back from parquet are
`datetime64[ms]`, while panel dates are `[us]` or `[ns]`. The join raised
`MergeError: incompatible merge keys` **as soon as one real snapshot
existed**. Production never had one, because every fetch failed. Once the
fundamentals fix made fetches succeed, `prediction_service.build_panel` (which
serves the Direction outlook) would have started to crash. This was reproduced
locally, and a regression test now covers it.

## Timeline (2026-10-11, IST)

* **~07:37** The mobile app's Stock Detail shows "No data found for
  SUZLON.NS". SUZLON, LGEINDIA and HDFCGOLD all fail the same way.
* **~09:00** PR #15 merged. It separates "no data" from "Yahoo failed" (503,
  retryable).
* **~09:31** After the deploy, production returns "Couldn't reach Yahoo
  Finance for SUZLON.NS". The same code run locally returns full data. The
  cause is narrowed to Yahoo refusing the server.
* **~23:00** In-depth investigation, this fix and this RCA.

## Fix (this PR)

1. **`services/yahoo_gateway.py`: one door to Yahoo.** Every yfinance call
   goes through it:
   * **Pacing:** at most 2 requests in flight and 0.4s between starts,
     process-wide (`YAHOO_MAX_CONCURRENT`, `YAHOO_MIN_INTERVAL_SEC`).
   * **Circuit breaker:** on a 429 or a refusal, stop calling Yahoo for 2
     minutes, doubling on each repeat up to 30. Callers fail fast with a real
     `Retry-After`. Timeouts and 5xx do not trip it.
   * **Real causes:** a handler on the `yfinance` logger captures the error
     yfinance hid, on the calling thread. Every failure now carries a cause
     (`rate_limited`, `blocked`, `timeout`, `network`, `upstream_error`) and
     Yahoo's own words, e.g. "401 … Invalid Crumb".
   * **Proxy:** `YAHOO_PROXY_URL` routes all Yahoo traffic through a proxy.
     This is the fix for cause 1 if Render stays blocked.
2. **`services/fundamental_store.py`: last-known-good in Postgres**
   (`fundamental_snapshots`, created by `init_db`). It survives deploys.
   * Read first: a snapshot under 24h old is served with **no Yahoo call**.
   * Written after every successful fetch.
   * When Yahoo fails, a snapshot up to 45 days old is served marked
     `stale: true` with its `as_of` date, so users see real figures, not an
     error.
3. **`scripts/refresh_fundamentals.py`** fills the store from any machine
   Yahoo answers (a laptop works), against the production database. It is
   paced, and stops if Yahoo refuses. Fundamentals change quarterly, so a
   weekly run is enough.
4. **`fundamental_service`:**
   * reads the store, then goes through the gateway;
   * never retries a 429 or 403 (one retry only for timeout, network or 5xx);
   * fetches statements one after another, not in a 4-request burst;
   * adds `as_of`, `source` (`live` / `store` / `stale`), and for failures
     `cause` and `retry_after`.
5. **Other Yahoo callers** (estimates, ownership, peers) go through the
   gateway. Peers use 4 workers instead of 8, and most peers now come from the
   store. Estimates no longer caches failures.
6. **API:**
   * the 503 for a Yahoo failure carries `cause` and the breaker's real
     `Retry-After`;
   * `/api/research/estimates` returns 503 instead of caching a failure;
   * new login-required `GET /api/research/data-health` shows the gateway
     (last failure cause and detail, breaker, proxy) and the store (count,
     newest), so the cause can be confirmed in production without logs.
7. **Prediction crash:** both `merge_asof` keys are normalised to
   `datetime64[ns]`.

The successful response shape is unchanged; only fields are added. The mobile
app needs no change: it already shows a 503 with Retry and ignores unknown
fields.

## Verification

* **Backend tests:** 194 across 14 files, all passing.
  * `test_fundamental_service.py` (18, new): a hidden 401 is reported as
    `blocked` with Yahoo's text; a 429 is not retried and opens the breaker,
    and further calls are short-circuited; the cooldown doubles and caps; a
    timeout is retried and doesn't trip the breaker; a fresh snapshot is
    served with no Yahoo call; stale is served on failure; snapshots over 45
    days are not served; recovery after the cooldown; cause classification;
    pacing; the API returns 200 for no-data and 503 with `cause` and
    `Retry-After`; data-health.
  * `test_feature_contract.py`: a regression test writes a real parquet
    snapshot and builds the panel. It fails on `main` with the production
    `MergeError` and passes here.
* **Live, against real Yahoo** from a residential IP, with a throwaway
  database:
  * the refresh script saved SUZLON, TCS and HDFCGOLD, and reported a fake
    symbol as "no data";
  * the store served SUZLON in 0.19s with 0 Yahoo calls;
  * with Yahoo refusing (simulated), the stored figures were served marked
    stale;
  * with nothing stored, the API answered `rate_limited` with
    `retry_after: 120`.

## After deploy: to restore service now

1. Deploy. `init_db` creates `fundamental_snapshots` automatically.
2. Fill the store from a machine Yahoo answers:
   `DATABASE_URL=<production url> python scripts/refresh_fundamentals.py --universe`
   (NIFTY 500, about 15 minutes), or pass specific symbols. The app then
   serves real fundamentals and AI summaries straight away, even while Render
   is blocked.
3. Confirm the cause: signed in, open `/api/research/data-health`.
   `last_failure_detail` shows Yahoo's own response (expect 401 Invalid Crumb
   or 429). Render logs also show `ERROR:yfinance` lines with the same text.
4. For live refreshes from production, set `YAHOO_PROXY_URL` to a residential
   or rotating proxy. Without one, re-run step 2 weekly.

## Follow-ups (not in this PR)

* Show "Figures as of <date>" in the web and mobile apps when `stale` is
  true. Clients already receive `as_of`.
* Consider a licensed fundamentals source (or NSE filings) for production, so
  the app doesn't depend on an unofficial API.
* Every deploy signs all users out. This is unrelated but visible during this
  incident; worth its own fix.
