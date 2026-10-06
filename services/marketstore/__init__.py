"""Local market-data store: NSE bhavcopy history, the traded universe, and
point-in-time fundamental snapshots.

Why this exists: training previously pulled candles from Angel One per symbol,
which is rate-limited enough that 20 symbols over 30 days was the practical
ceiling. ``nselib.capital_market.bhav_copy_with_delivery`` returns the *entire*
NSE market for one day in a single call — including delivery quantity, which
Angel's candle API does not carry at all. One call per trading day gives the
whole universe, so the binding constraint becomes disk rather than rate limits.

Archive floor: the delivery-enriched bhavcopy only goes back to roughly January
2020. Earlier dates return "Data not found", and ``bhav_copy_equities`` returns
empty frames for 2018-2019. Treat 2020-01-01 as the earliest usable start date.
"""
from services.marketstore.store import (
    DAILY_ROOT,
    last_stored_date,
    read_range,
    stored_dates,
    write_day,
)
from services.marketstore.universe import (
    CAP_BANDS,
    load_universe,
    universe_symbols,
)

__all__ = [
    "CAP_BANDS",
    "DAILY_ROOT",
    "last_stored_date",
    "load_universe",
    "read_range",
    "stored_dates",
    "universe_symbols",
    "write_day",
]
