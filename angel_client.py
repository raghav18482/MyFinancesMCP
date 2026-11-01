import time
import logging
import uuid
from typing import Optional

import pyotp
import requests

from integrations.smartapi import new_smart_connect
from services import secret_registry

logger = logging.getLogger(__name__)

#: Total login attempts. Only network errors are retried.
LOGIN_ATTEMPTS = 3
#: Waits between network-error retries.
LOGIN_BACKOFF_SEC = (0.5, 1.0)
#: Per-request timeout for SmartAPI calls (SmartConnect's own default is 7 s).
LOGIN_TIMEOUT_SEC = 5


class AngelAuthError(RuntimeError):
    """An Angel One login that did not produce a session.

    ``kind`` is ``"credentials"`` when Angel One answered and refused (wrong PIN,
    TOTP or key), and ``"unavailable"`` when it could not be reached. A
    ``RuntimeError`` so existing callers that catch that keep working.
    """

    def __init__(self, kind: str, message: str, *, errorcode: Optional[str] = None) -> None:
        super().__init__(f"Angel One login failed ({kind}): {message}")
        self.kind = kind
        self.errorcode = errorcode


class AngelOneClient:
    """Read-only Angel One SmartAPI client: portfolio, orders/trades history, market data.

    Every credential and token this client holds is registered with
    :mod:`services.secret_registry`, so the log filter redacts it even where it
    appears without a key name. :meth:`release_secrets` drops them again; the
    session manager calls it when the session ends or the login fails.
    """

    def __init__(self, api_key: str, client_id: str, password: str, totp_secret: str):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_secret = totp_secret

        self._secret_owner = f"angel:{uuid.uuid4().hex}"
        secret_registry.register(self._secret_owner, api_key, password, totp_secret)

        self._smart_api = new_smart_connect(api_key=self.api_key, timeout=LOGIN_TIMEOUT_SEC)
        self._session_data: Optional[dict] = None
        self._login_time: Optional[float] = None
        self._feed_token: Optional[str] = None

    def __repr__(self) -> str:
        # Never the API key, PIN, TOTP secret or tokens.
        return f"AngelOneClient(client_id={self.client_id!r})"

    def release_secrets(self) -> None:
        secret_registry.release(self._secret_owner)

    def _register_tokens(self) -> None:
        data = (self._session_data or {}).get("data") or {}
        values = [
            data.get("jwtToken"),
            data.get("refreshToken"),
            data.get("feedToken"),
            getattr(self._smart_api, "access_token", None),
            getattr(self._smart_api, "refresh_token", None),
            getattr(self._smart_api, "feed_token", None),
        ]
        # SmartAPI prefixes the JWT it returns with "Bearer ".
        values = [v.removeprefix("Bearer ") if isinstance(v, str) else v for v in values]
        secret_registry.register(self._secret_owner, *values)

    def ensure_session(self):
        if self._session_data and self._login_time:
            elapsed_hours = (time.time() - self._login_time) / 3600
            if elapsed_hours < 8:
                return
            try:
                self._smart_api.generateToken(self._smart_api.refresh_token)
                self._login_time = time.time()
                self._register_tokens()
                return
            except Exception as e:
                logger.warning("Angel One token refresh failed (%s), re-authenticating",
                               type(e).__name__)

        self._authenticate()

    def _authenticate(self):
        """Log in once; retry only when the request may never have reached Angel One.

        A rejection (``status: false``, or an API error such as a bad PIN or
        TOTP) is never retried: repeating a wrong PIN counts toward Angel One's
        account lockout. Network failures are retried up to
        ``LOGIN_ATTEMPTS - 1`` more times with a short backoff, which bounds a
        failing login to roughly ``LOGIN_ATTEMPTS * LOGIN_TIMEOUT_SEC`` plus the
        backoff.
        """
        totp = pyotp.TOTP(self.totp_secret)

        for attempt in range(1, LOGIN_ATTEMPTS + 1):
            try:
                data = self._smart_api.generateSession(
                    self.client_id, self.password, totp.now()
                )
            except (requests.exceptions.RequestException, OSError) as e:
                # Never reached Angel One, or the answer was lost: safe to retry.
                logger.warning(
                    "Angel One login attempt %d/%d failed for %s: %s",
                    attempt, LOGIN_ATTEMPTS, self.client_id, type(e).__name__,
                )
                if attempt < LOGIN_ATTEMPTS:
                    time.sleep(LOGIN_BACKOFF_SEC[attempt - 1])
                    continue
                raise AngelAuthError("unavailable", "Could not reach Angel One.") from None
            except Exception as e:
                # SmartAPI raises its own exceptions for API errors (bad input,
                # token or data errors): a real answer, so not retried.
                logger.warning(
                    "Angel One login rejected for %s: %s: %s",
                    self.client_id, type(e).__name__, e,
                )
                raise AngelAuthError("credentials", str(e) or type(e).__name__) from None

            if data.get("status"):
                self._session_data = data
                self._login_time = time.time()
                self._feed_token = (data.get("data") or {}).get("feedToken")
                self._register_tokens()
                logger.info("Angel One session established for %s", self.client_id)
                return

            message = data.get("message") or "Unknown login error"
            # SmartAPI's own log of this failure is silenced (it included the
            # PIN and TOTP); this is the record of it instead.
            logger.warning(
                "Angel One login rejected for %s: errorcode=%s message=%s",
                self.client_id, data.get("errorcode"), message,
            )
            raise AngelAuthError("credentials", message, errorcode=data.get("errorcode"))

        raise AngelAuthError("unavailable", "Could not reach Angel One.")  # pragma: no cover

    @property
    def feed_token(self) -> Optional[str]:
        return self._feed_token

    @property
    def auth_token(self) -> Optional[str]:
        """JWT from the active session, required by SmartWebSocketV2 headers."""
        if not self._session_data:
            return None
        return (self._session_data.get("data") or {}).get("jwtToken")

    @property
    def client_code(self) -> str:
        return self.client_id

    # ── Portfolio ──────────────────────────────────────────────

    def get_profile(self) -> dict:
        return self._smart_api.getProfile(self._smart_api.refresh_token)

    def get_holdings(self) -> dict:
        return self._smart_api.holding()

    def get_all_holdings(self) -> dict:
        return self._smart_api.allholding()

    def get_positions(self) -> dict:
        return self._smart_api.position()

    # ── Orders & Trades ───────────────────────────────────────

    def get_order_book(self) -> dict:
        return self._smart_api.orderBook()

    def get_trade_book(self) -> dict:
        return self._smart_api.tradeBook()

    # ── Funds ─────────────────────────────────────────────────

    def get_funds(self) -> dict:
        return self._smart_api.rmsLimit()

    # ── Market Data ───────────────────────────────────────────

    def get_ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> dict:
        return self._smart_api.ltpData(exchange, tradingsymbol, symboltoken)

    def search_scrip(self, exchange: str, search_text: str) -> dict:
        return self._smart_api.searchScrip(exchange, search_text)

    # There are deliberately no order methods. This application is a read-only
    # assistant: placing, modifying or cancelling orders is not something any
    # code path here can do. The SmartConnect object is private so nothing
    # outside this class reaches its order endpoints either.

    # ── Historical / Analytics ────────────────────────────────

    def get_candle_data(self, params: dict) -> dict:
        return self._smart_api.getCandleData(params)

    def get_market_data(self, mode: str, exchange_tokens: dict) -> dict:
        return self._smart_api.getMarketData(mode, exchange_tokens)
