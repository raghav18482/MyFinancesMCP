import time
import logging
from typing import Optional

import pyotp
from SmartApi.smartConnect import SmartConnect

logger = logging.getLogger(__name__)


class AngelOneClient:
    def __init__(self, api_key: str, client_id: str, password: str, totp_secret: str):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_secret = totp_secret

        self.smart_api = SmartConnect(api_key=self.api_key)
        self._session_data: Optional[dict] = None
        self._login_time: Optional[float] = None
        self._feed_token: Optional[str] = None

    def ensure_session(self):
        if self._session_data and self._login_time:
            elapsed_hours = (time.time() - self._login_time) / 3600
            if elapsed_hours < 8:
                return
            try:
                self.smart_api.generateToken(self.smart_api.refresh_token)
                self._login_time = time.time()
                return
            except Exception:
                logger.warning("Token refresh failed, re-authenticating")

        self._authenticate()

    def _authenticate(self):
        totp = pyotp.TOTP(self.totp_secret)

        last_error = None
        for attempt in range(5):
            try:
                totp_code = totp.now()
                data = self.smart_api.generateSession(
                    self.client_id, self.password, totp_code
                )
                if data.get("status"):
                    self._session_data = data
                    self._login_time = time.time()
                    self._feed_token = (data.get("data") or {}).get("feedToken")
                    logger.info("Angel One session established for %s", self.client_id)
                    return
                last_error = data.get("message", "Unknown login error")
            except Exception as e:
                last_error = str(e)

            if attempt < 4:
                time.sleep(2)

        raise RuntimeError(f"Failed to authenticate after 5 attempts: {last_error}")

    @property
    def feed_token(self) -> Optional[str]:
        return self._feed_token

    # ── Portfolio ──────────────────────────────────────────────

    def get_profile(self) -> dict:
        return self.smart_api.getProfile(self.smart_api.refresh_token)

    def get_holdings(self) -> dict:
        return self.smart_api.holding()

    def get_all_holdings(self) -> dict:
        return self.smart_api.allholding()

    def get_positions(self) -> dict:
        return self.smart_api.position()

    # ── Orders & Trades ───────────────────────────────────────

    def get_order_book(self) -> dict:
        return self.smart_api.orderBook()

    def get_trade_book(self) -> dict:
        return self.smart_api.tradeBook()

    # ── Funds ─────────────────────────────────────────────────

    def get_funds(self) -> dict:
        return self.smart_api.rmsLimit()

    # ── Market Data ───────────────────────────────────────────

    def get_ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> dict:
        return self.smart_api.ltpData(exchange, tradingsymbol, symboltoken)

    def search_scrip(self, exchange: str, search_text: str) -> dict:
        return self.smart_api.searchScrip(exchange, search_text)

    # ── Trading ───────────────────────────────────────────────

    def place_order(self, order_params: dict):
        return self.smart_api.placeOrder(order_params)

    def modify_order(self, order_params: dict):
        return self.smart_api.modifyOrder(order_params)

    def cancel_order(self, order_id: str, variety: str = "NORMAL"):
        return self.smart_api.cancelOrder(order_id, variety)

    # ── Historical / Analytics ────────────────────────────────

    def get_candle_data(self, params: dict) -> dict:
        return self.smart_api.getCandleData(params)

    def get_market_data(self, mode: str, exchange_tokens: dict) -> dict:
        return self.smart_api.getMarketData(mode, exchange_tokens)
