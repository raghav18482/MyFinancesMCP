"""Fill the last-known-good fundamentals store from a machine Yahoo answers.

Yahoo Finance refuses requests from the production host's IP (see
``services/yahoo_gateway.py``). Fundamentals change quarterly, so they do not
need to be fetched from production at all: run this from any machine Yahoo
does not block (a laptop works) with ``DATABASE_URL`` pointing at the
production database, and the app serves these figures with their date.

    # specific stocks (the broker's trading symbols)
    DATABASE_URL=postgresql+psycopg://... python scripts/refresh_fundamentals.py SUZLON-EQ TCS-EQ

    # every stock in the NIFTY 500 universe (about 15 minutes, paced)
    DATABASE_URL=postgresql+psycopg://... python scripts/refresh_fundamentals.py --universe

Re-run weekly, or after results season. Requests are paced by the same
gateway the app uses, and the run stops early if Yahoo starts refusing.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass


def _symbols(args: argparse.Namespace) -> list[str]:
    symbols = [s.strip().upper() for s in args.symbols if s.strip()]
    if args.universe:
        from services.marketstore.universe import load_universe

        universe = load_universe()
        column = "symbol" if "symbol" in universe.columns else universe.columns[0]
        symbols += [f"{s}-EQ" for s in universe[column].astype(str)]
    # Keep order, drop duplicates.
    return list(dict.fromkeys(symbols))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("symbols", nargs="*", help="trading symbols, e.g. SUZLON-EQ")
    parser.add_argument("--universe", action="store_true", help="every NIFTY 500 stock")
    args = parser.parse_args()

    symbols = _symbols(args)
    if not symbols:
        parser.error("give symbols or --universe")

    from db import init_db
    from db.engine import DATABASE_URL, _safe_url
    from services import fundamental_store
    from services.fundamental_service import get_stock_fundamentals
    from services.yahoo_gateway import status

    init_db()  # creates fundamental_snapshots if this is the first run
    print(f"Writing to {_safe_url(DATABASE_URL)}; {len(symbols)} symbol(s)")

    saved = no_data = failed = 0
    started = time.time()
    for i, symbol in enumerate(symbols, 1):
        result = get_stock_fundamentals(symbol, use_store=False)
        if result.get("source") == "live":
            saved += 1
            mark = "saved"
        elif result.get("retryable"):
            failed += 1
            mark = f"FAILED ({result.get('cause')})"
        else:
            no_data += 1
            mark = "no data on Yahoo"
        print(f"[{i}/{len(symbols)}] {symbol}: {mark}")
        if status()["breaker_open"]:
            print("Yahoo is refusing this machine too; stopping. Try later or another network.")
            break

    print(f"\nDone in {time.time() - started:.0f}s: {saved} saved, {no_data} with no data, "
          f"{failed} failed. Store: {fundamental_store.status()}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
