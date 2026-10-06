"""Tests for the market store and the point-in-time snapshot layer.

The one that matters is ``test_pit_returns_none_rather_than_falling_back``: it
guards the look-ahead rule. ``read_as_of`` must return None when it has no
snapshot predating the date, because the tempting alternative — falling back to
today's restated figure — is exactly the bias that makes a backtest look
excellent and a live model fail.

Run directly (``python tests/test_marketstore.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from services.marketstore import bhavcopy, pit, store


# A verbatim row shape from NSE's bhavcopy, including the quirks that matter:
# padded column names, string numerics with commas, a non-EQ series, and the
# day-month-year date format.
RAW_FIXTURE = pd.DataFrame([
    {
        "SYMBOL": " RELIANCE ", "SERIES": "EQ", "DATE1": "01-Oct-2026",
        "PREV_CLOSE": "1,187.00", "OPEN_PRICE": "1,180.10", "HIGH_PRICE": "1,183.90",
        "LOW_PRICE": "1,160.80", "LAST_PRICE": "1,167.00", "CLOSE_PRICE": "1,167.70",
        "AVG_PRICE": "1,173.12", "TTL_TRD_QNTY": "16,771,221",
        "TURNOVER_LACS": "196,746.10", "NO_OF_TRADES": "279,428",
        "DELIV_QTY": "10,270,423", "DELIV_PER": "61.24",
    },
    {
        "SYMBOL": "SOMEBOND", "SERIES": "N1", "DATE1": "01-Oct-2026",
        "PREV_CLOSE": "100", "OPEN_PRICE": "100", "HIGH_PRICE": "100",
        "LOW_PRICE": "100", "LAST_PRICE": "100", "CLOSE_PRICE": "100",
        "AVG_PRICE": "100", "TTL_TRD_QNTY": "10", "TURNOVER_LACS": "0.01",
        "NO_OF_TRADES": "1", "DELIV_QTY": "10", "DELIV_PER": "100",
    },
    {
        "SYMBOL": "DELISTED", "SERIES": "EQ", "DATE1": "01-Oct-2026",
        "PREV_CLOSE": "-", "OPEN_PRICE": "-", "HIGH_PRICE": "-",
        "LOW_PRICE": "-", "LAST_PRICE": "-", "CLOSE_PRICE": "-",
        "AVG_PRICE": "-", "TTL_TRD_QNTY": "-", "TURNOVER_LACS": "-",
        "NO_OF_TRADES": "-", "DELIV_QTY": "-", "DELIV_PER": "-",
    },
])


def test_normalise_handles_nse_quirks():
    out = bhavcopy.normalise(RAW_FIXTURE, date(2026, 10, 1))

    assert list(out.columns) == bhavcopy.SCHEMA, "schema drifted"
    # The bond row is dropped by the series filter, the dashes row by the
    # close-price filter.
    assert len(out) == 1, f"expected only the EQ row with a price, got {len(out)}"

    row = out.iloc[0]
    assert row["symbol"] == "RELIANCE", "symbol should be stripped and upper-cased"
    assert row["close"] == 1167.70, "comma-separated numerics must parse"
    assert row["volume"] == 16771221
    assert row["deliv_pct"] == 61.24
    # Turnover arrives in lakhs and is stored in rupees.
    assert abs(row["turnover"] - 196746.10 * 100_000) < 1.0, "turnover unit conversion"
    assert pd.Timestamp(row["date"]).date() == date(2026, 10, 1)
    print("ok  normalise handles series filter, commas, dashes and turnover units")


def test_normalise_rejects_a_missing_column():
    broken = RAW_FIXTURE.drop(columns=["DELIV_PER"])
    try:
        bhavcopy.normalise(broken, date(2026, 10, 1))
    except ValueError as e:
        assert "DELIV_PER" in str(e)
        print("ok  normalise names the missing column instead of failing late")
        return
    raise AssertionError("a missing column should raise, not produce a partial frame")


def test_store_round_trip_and_idempotency():
    df = bhavcopy.normalise(RAW_FIXTURE, date(2026, 10, 1))
    other = df.copy()
    other["symbol"] = "TCS"

    with tempfile.TemporaryDirectory() as root:
        store.write_day(pd.concat([df, other]), root)
        assert len(store.read_range(root=root)) == 2

        # Re-writing the same day must replace it, not append. An interrupted
        # backfill is re-run routinely, so this is the normal case.
        store.write_day(pd.concat([df, other]), root)
        back = store.read_range(root=root)
        assert len(back) == 2, f"double write duplicated rows: {len(back)}"

        only = store.read_range(["RELIANCE"], root=root)
        assert len(only) == 1 and only.iloc[0]["symbol"] == "RELIANCE"

        assert store.last_stored_date(root) == date(2026, 10, 1)
        assert store.stored_dates(root) == {date(2026, 10, 1)}
        summary = store.store_summary(root)
        assert summary["rows"] == 2 and summary["symbols"] == 2
    print("ok  store round-trips, filters by symbol, and is idempotent per day")


def test_store_range_filter():
    rows = []
    for d in (date(2025, 6, 2), date(2026, 1, 5), date(2026, 10, 1)):
        day = bhavcopy.normalise(RAW_FIXTURE, d)
        rows.append(day)

    with tempfile.TemporaryDirectory() as root:
        for day in rows:
            store.write_day(day, root)

        assert store.stored_years(root) == [2025, 2026]
        mid = store.read_range(None, date(2026, 1, 1), date(2026, 6, 1), root=root)
        assert len(mid) == 1, "date range filter should select exactly one day"
        assert pd.Timestamp(mid.iloc[0]["date"]).date() == date(2026, 1, 5)
    print("ok  store filters by date range across year partitions")


# ── The look-ahead guard ───────────────────────────────────────────────────
def _snapshot(symbol: str, total: float) -> dict:
    return {
        "symbol": symbol,
        "valuation": {"pe_ratio": 24.0, "pb_ratio": 3.1},
        "health": {"roe": 15.0, "debt_to_equity": 40.0},
        "revenue_trend": [{"year": "2025", "value": 1_000.0}],
        "score": {"total": total, "confidence": 0.9, "pillars": [
            {"key": "valuation", "score": 70.0},
            {"key": "profitability", "score": 80.0},
        ]},
    }


def test_pit_returns_none_rather_than_falling_back():
    """The whole point of the point-in-time store.

    A caller asking for fundamentals as of a date *before* any snapshot exists
    must get None. Returning today's figures instead would silently inject
    knowledge of the future into every historical training row.
    """
    with tempfile.TemporaryDirectory() as root:
        pit.write_snapshot(_snapshot("RELIANCE", 71.0),
                           observed_at=date(2026, 6, 1), root=root)

        later = pit.read_as_of("RELIANCE", date(2026, 9, 1), root=root)
        assert later is not None, "a snapshot before the date should be found"
        assert later["score_total"] == 71.0

        earlier = pit.read_as_of("RELIANCE", date(2026, 1, 1), root=root)
        assert earlier is None, (
            "read_as_of must return None before the first snapshot — a fallback "
            "to current values is look-ahead bias"
        )

        unknown = pit.read_as_of("NOSUCHCO", date(2026, 9, 1), root=root)
        assert unknown is None
    print("ok  read_as_of returns None instead of leaking the present backwards")


def test_pit_picks_the_newest_snapshot_not_the_latest():
    """merge-asof semantics: newest *at or before* the date, never after."""
    with tempfile.TemporaryDirectory() as root:
        pit.write_snapshot(_snapshot("TCS", 60.0), observed_at=date(2026, 3, 1), root=root)
        pit.write_snapshot(_snapshot("TCS", 75.0), observed_at=date(2026, 7, 1), root=root)

        mid = pit.read_as_of("TCS", date(2026, 5, 1), root=root)
        assert mid["score_total"] == 60.0, "must not see the July snapshot in May"

        late = pit.read_as_of("TCS", date(2026, 9, 1), root=root)
        assert late["score_total"] == 75.0, "should take the newest available"
    print("ok  read_as_of takes the newest snapshot at or before the date")


def test_pit_symbol_normalisation_and_idempotency():
    with tempfile.TemporaryDirectory() as root:
        pit.write_snapshot(_snapshot("RELIANCE-EQ", 71.0),
                           observed_at=date(2026, 6, 1), root=root)
        # Same symbol, same day, written twice: one row, latest wins.
        pit.write_snapshot(_snapshot("RELIANCE-EQ", 72.0),
                           observed_at=date(2026, 6, 1), root=root)

        all_rows = pit.read_all(root)
        assert len(all_rows) == 1, f"same symbol+day should be one row, got {len(all_rows)}"
        assert all_rows.iloc[0]["symbol"] == "RELIANCE", "-EQ suffix should be stripped"
        assert all_rows.iloc[0]["score_total"] == 72.0

        cov = pit.coverage(root)
        assert cov["snapshots"] == 1 and cov["symbols"] == 1
    print("ok  snapshots normalise the symbol and are idempotent per day")


def test_pit_skips_failed_fetches():
    with tempfile.TemporaryDirectory() as root:
        wrote = pit.write_snapshot(
            {"symbol": "BROKEN", "error": "No data found", "score": None},
            observed_at=date(2026, 6, 1), root=root,
        )
        assert wrote is False, "a failed fetch carries nothing worth preserving"
        assert pit.read_all(root).empty
    print("ok  failed fetches are not recorded as snapshots")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in TESTS:
        fn()
    print(f"\n{len(TESTS)} passed")
