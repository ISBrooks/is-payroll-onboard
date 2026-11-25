"""Incremental FieldRoutes refresh: discover newly-added subscriptions and
re-pull the full chain for every subscription not yet in a terminal state.

  python scripts/sync.py               # new subs since last_sync minus 2 days
  python scripts/sync.py --since 2026-08-25
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commissions.config import load_settings
from commissions.engine import compute_status
from commissions.fieldroutes import FieldRoutesClient
from commissions.ingest import sync
from commissions.store import connect, get_meta, set_meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="discover subs added since this date (default: last_sync - 2d)")
    ap.add_argument("--rpm", type=int, default=400)
    args = ap.parse_args()

    s = load_settings()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    conn = connect(s.db_path)
    # refresh sub_status first so the terminal-state skip list is current
    compute_status(conn, s)
    until = date.today().isoformat()
    since = args.since or (
        date.fromisoformat(get_meta(conn, "last_sync") or s.backfill_since)
        - timedelta(days=2)).isoformat()
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token, max_reads_per_min=args.rpm) as fr:
        sync(fr, conn, s, since, until)
        print(f"API calls used: {fr.calls}")
    compute_status(conn, s)
    set_meta(conn, "last_sync", until)


if __name__ == "__main__":
    main()
