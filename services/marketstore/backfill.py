"""Resumable NSE bhavcopy backfill.

    python -m services.marketstore.backfill --from 2020-01-01
    python -m services.marketstore.backfill --days 30          # recent window
    python -m services.marketstore.backfill --status           # what's on disk

One HTTP call per trading day pulls the entire market, so a full 2020-to-now
build is roughly 1,600 calls. With the default delay that is a few hours;
it is resumable, so interrupt it freely.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta

from services.marketstore.bhavcopy import ARCHIVE_FLOOR, NoDataForDate, fetch_day, parse_day
from services.marketstore.store import DAILY_ROOT, stored_dates, store_summary, write_day

logger = logging.getLogger(__name__)

# NSE is not a high-volume API and this is a bulk read of public files.
# Be polite: a third of a second between days still finishes a full backfill
# inside an afternoon.
DEFAULT_DELAY_SEC = 0.35


def trading_day_candidates(start: date, end: date) -> list[date]:
    """Every weekday in the range. Exchange holidays fall out when NSE 404s."""
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def backfill(
    start: date,
    end: date | None = None,
    *,
    root: str = DAILY_ROOT,
    delay: float = DEFAULT_DELAY_SEC,
    skip_existing: bool = True,
) -> dict:
    end = end or date.today()
    if start < ARCHIVE_FLOOR:
        logger.warning("start %s is before the archive floor; clamping to %s",
                       start, ARCHIVE_FLOOR)
        start = ARCHIVE_FLOOR

    already = stored_dates(root) if skip_existing else set()
    candidates = [d for d in trading_day_candidates(start, end) if d not in already]

    logger.info("backfill %s -> %s: %d candidate days (%d already stored)",
                start, end, len(candidates), len(already))

    counts = {"written": 0, "rows": 0, "skipped": 0, "failed": 0}

    for i, day in enumerate(candidates, 1):
        try:
            df = fetch_day(day)
        except NoDataForDate:
            # Holiday or weekend-adjacent closure. Expected, not an error.
            counts["skipped"] += 1
            continue
        except Exception as e:
            logger.warning("%s: fetch failed (%s)", day, e)
            counts["failed"] += 1
            continue

        rows = write_day(df, root)
        counts["written"] += 1
        counts["rows"] += rows

        if i % 25 == 0 or i == len(candidates):
            logger.info("  [%d/%d] %s  written=%d rows=%d skipped=%d failed=%d",
                        i, len(candidates), day, counts["written"], counts["rows"],
                        counts["skipped"], counts["failed"])

        if delay:
            time.sleep(delay)

    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", help="start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="end", help="end date (YYYY-MM-DD), default today")
    parser.add_argument("--days", type=int, help="backfill the last N calendar days")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SEC,
                        help=f"seconds between calls (default {DEFAULT_DELAY_SEC})")
    parser.add_argument("--root", default=DAILY_ROOT, help="store root")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch days already stored")
    parser.add_argument("--status", action="store_true",
                        help="print what is on disk and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if args.status:
        summary = store_summary(args.root)
        for k, v in summary.items():
            print(f"{k:14} {v}")
        return 0

    end = parse_day(args.end) if args.end else date.today()
    if args.days:
        start = end - timedelta(days=args.days)
    elif args.start:
        start = parse_day(args.start)
    else:
        parser.error("one of --from, --days or --status is required")
        return 2

    counts = backfill(start, end, root=args.root, delay=args.delay,
                      skip_existing=not args.force)

    print(f"\ndays written : {counts['written']}")
    print(f"rows written : {counts['rows']}")
    print(f"non-trading  : {counts['skipped']}")
    print(f"failed       : {counts['failed']}")

    summary = store_summary(args.root)
    print(f"\nstore now holds {summary['rows']} rows over {summary['trading_days']} "
          f"trading days ({summary['first_day']} -> {summary['last_day']}), "
          f"{summary['symbols']} symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
