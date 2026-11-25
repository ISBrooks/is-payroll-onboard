"""Backfill FieldRoutes history into data/commissions.sqlite.

  python scripts/backfill.py                     # since BACKFILL_SINCE (.env)
  python scripts/backfill.py --since 2026-08-01  # shorter window (smoke test)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commissions.config import load_settings
from commissions.fieldroutes import FieldRoutesClient
from commissions.ingest import backfill
from commissions.store import connect, set_meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="YYYY-MM-DD (default: BACKFILL_SINCE)")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--rpm", type=int, default=300, help="API reads/min pace")
    args = ap.parse_args()

    s = load_settings()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    since = args.since or s.backfill_since
    until = args.until or date.today().isoformat()
    conn = connect(s.db_path)
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token, max_reads_per_min=args.rpm) as fr:
        backfill(fr, conn, s, since, until)
        print(f"API calls used: {fr.calls}")
    set_meta(conn, "last_sync", until)


if __name__ == "__main__":
    main()
