"""Partition maintenance for `template_counts`.

Two jobs, both idempotent, meant to run from cron once a day:

  - create partitions ahead of the month rollover. A missing partition is not a
    slow insert, it is an insert that fails, so they are made in advance rather
    than discovered at midnight on the first
  - drop partitions past the retention window. A DROP, not a DELETE — that is
    the whole reason the table is partitioned

`anomalies` is never pruned. At the measured flag rate it grows by a few hundred
thousand rows a year, and it is the only durable record of what the system
actually found.

Usage:
    python tools/retention.py --dry-run
    python tools/retention.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "collector"))

from store import Store  # noqa: E402

DEFAULT_DSN = "postgresql://anomaly:anomaly@localhost:5432/anomaly_watch"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--keep-months", type=int, default=12, help="retention window for template_counts")
    parser.add_argument("--months-ahead", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true", help="report what would be dropped, drop nothing")
    args = parser.parse_args()

    store = Store(args.dsn)
    store.ensure_schema()

    created = store.ensure_partitions(months_ahead=args.months_ahead)
    print(f"partitions present : {', '.join(created)}")

    if args.dry_run:
        print(f"dry run: would drop partitions older than {args.keep_months} months")
        return 0

    dropped = store.drop_old_partitions(keep_months=args.keep_months)
    print(f"dropped            : {', '.join(dropped) if dropped else 'nothing older than the window'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
