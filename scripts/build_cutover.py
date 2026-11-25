"""Build the go-live cutover (RULES §8c) — run ONCE, before the first live
pay period (the one starting PAYROLL_LIVE_FROM) is closed.

  python scripts/build_cutover.py                # dry run
  python scripts/build_cutover.py --apply        # seed

Needs, in this order: the manual payroll history loaded (scripts/load_history.py
--write, its "not paid" list included), and the snapshot: right when the manual
payroll pulls its data for its last period, run scripts/sync.py and copy
data/commissions.sqlite to data/cutover/snapshot.sqlite (or pass --snapshot).

Dry run fills the cutover tables and prints the per-rep tie-out; --apply
also archives the parallel-run ledger and seeds the fresh one. --apply
refuses unless every rep ties to the manual books to the cent.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commissions.config import load_settings
from commissions.cutover import (archive_ledger, build_baseline, reconcile,
                                 seed_rows)
from commissions.engine import compute_status
from commissions.store import connect, get_meta, set_meta

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "ledger_parallel_archive"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=str(ROOT / "data" / "cutover" / "snapshot.sqlite"),
                    help="copy of this DB synced alongside the manual's last data pull")
    ap.add_argument("--db", default=None, help="alternate DB (staging copy)")
    ap.add_argument("--notpaid", default=None,
                    help="CSV (customer_id, rep, date_sold) instead of the loaded not-paid list")
    ap.add_argument("--settle", default="", help="comma-separated subscription ids")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    s = load_settings()
    conn = connect(Path(args.db) if args.db else s.db_path)
    if get_meta(conn, "cutover_seeded_at"):
        print(f"cutover already seeded at {get_meta(conn, 'cutover_seeded_at')} — refusing")
        return 1
    # the baseline is built FROM the manual payroll history: it must be loaded first
    try:
        loaded = conn.execute("SELECT COUNT(*) FROM manual_paid").fetchone()[0]
    except sqlite3.OperationalError:
        loaded = 0
    if not loaded:
        print("no manual payroll history is loaded: run scripts/load_history.py --write first "
              "(every true-down and deduction must be in it, or holdings would be overstated)")
        return 2
    if not Path(args.snapshot).is_file():
        print(f"no snapshot at {args.snapshot}: see the top of this script")
        return 2
    if args.notpaid:
        with open(args.notpaid, encoding="utf-8") as fh:
            notpaid = {(r["customer_id"], r["rep"], r["date_sold"]) for r in csv.DictReader(fh)}
    else:
        notpaid = {(str(c), r, (d or "")[:10]) for c, r, d in conn.execute(
            "SELECT customer_id, rep, date_sold FROM manual_not_paid")}
    settle = {x.strip() for x in args.settle.split(",") if x.strip()}

    facts = compute_status(conn, s)
    info = build_baseline(conn, s, facts, args.snapshot, notpaid, settle)
    print(json.dumps(info, indent=1))

    recon = reconcile(conn)
    off = [r for r in recon if abs(r[3]) >= 0.01]
    print(f"\n{'Rep':<24}{'Manual books':>14}{'Baseline':>14}{'Diff':>8}")
    for rep, man, base, diff in recon:
        if rep in s.isa_roster or abs(diff) >= 0.01:
            print(f"{rep:<24}{man:>14,.2f}{base:>14,.2f}{diff:>8.2f}")
    roster = [r for r in recon if r[0] in s.isa_roster]
    print(f"roster total: manual {sum(r[1] for r in roster):,.2f} "
          f"baseline {sum(r[2] for r in roster):,.2f}; reps off by >= 1c: {len(off)}")
    legacy = conn.execute("SELECT kind, ROUND(SUM(amount),2), SUM(rows) FROM cutover_legacy "
                          "WHERE rep IN (%s) GROUP BY kind" % ",".join("?" * len(s.isa_roster)),
                          s.isa_roster).fetchall()
    print("roster rows not pinned to a sale (legacy, never moves):", legacy)

    if not args.apply:
        print("\ndry run — ledger untouched")
        return 0
    if off:
        print("REFUSING to seed: tie-out off for", [r[0] for r in off])
        return 2
    archive_ledger(conn, ARCHIVE)
    print(json.dumps(seed_rows(conn), indent=1))
    set_meta(conn, "cutover_seeded_at", conn.execute("SELECT datetime('now')").fetchone()[0])
    print(f"cutover seeded; parallel-run ledger archived as {ARCHIVE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
