"""One-off + idempotent: fill customers.commercial (RULES §3 priority 0,
owner 2026-10-20) for every customer in the DB from FieldRoutes'
commercialAccount flag (the card's Commercial Property toggle). Every sync
keeps it current for open sales afterwards. Safe to re-run.

  python scripts/backfill_commercial.py            # update the DB
  python scripts/backfill_commercial.py --dry-run  # report only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commissions.config import load_settings
from commissions.fieldroutes import FieldRoutesClient
from commissions.store import connect


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    s = load_settings()
    conn = connect(s.db_path)                     # adds the column if missing
    custs = [r[0] for r in conn.execute("SELECT customer_id FROM customers")]
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        recs = fr.get_customers(custs)
    flag = {str(c["customerID"]): 1 if str(c.get("commercialAccount") or "0").strip() == "1"
            else 0 for c in recs}
    missing = [c for c in custs if c not in flag]
    before = dict(conn.execute("SELECT customer_id, commercial FROM customers"))
    changed = [(v, c) for c, v in flag.items() if before.get(c) != v]
    print(f"customers {len(custs)}, fetched {len(recs)}, commercial {sum(flag.values())}, "
          f"changing {len(changed)}, not returned by FieldRoutes {len(missing)}")
    if args.dry_run:
        return
    conn.executemany("UPDATE customers SET commercial=? WHERE customer_id=?", changed)
    conn.commit()
    print("updated")


if __name__ == "__main__":
    main()
