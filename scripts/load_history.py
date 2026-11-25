"""Load your manual payroll history into the engine (before go-live).

The engine takes over from a manual payroll process. To do that without paying
anything twice or deducting what was never paid, it must know what the manual
process already did. That history goes in four plain CSV files (templates with
one example row each are in history_templates/):

  paid.csv         one row per commission the manual payroll PAID
                   period_end, rep, customer_id, contract_value, commission
  adjustments.csv  one row per manual adjustment (split transfers, rescues,
                   one-off corrections, upsell credits)
                   period_end, rep, customer_id, amount, note
  deductions.csv   one row per amount the manual payroll TOOK BACK
                   period_end, rep, customer_id, amount, kind
                   kind = cancel     (a cancellation claw-back)
                        = cv_change  (a true-down because the contract value dropped)
  not_paid.csv     accounts the manual process was waiting to pay until the
                   customer paid ("owed once paid")
                   customer_id, rep, date_sold

    python scripts/load_history.py                      # check only: nothing is written
    python scripts/load_history.py --dir data/history   # (the default folder)
    python scripts/load_history.py --write              # replace the loaded history

Without --write it only validates and prints each rep's totals, so they can be
compared with the manual books first. --write replaces all four tables in one
step (so a corrected file can simply be loaded again) and is refused once the
go-live baseline has been seeded from them (build_cutover.py --apply).

Conventions:
  * period_end is the LAST day of the pay period the row was paid/deducted in
    (YYYY-MM-DD or M/D/YYYY). It must be a real period end for your pay calendar.
  * rep is the name exactly as on the commission roster (the dashboard's Payroll ->
    Commission Roster). People the manual sheet paid
    who are not on the roster are loaded but stay outside the app.
  * customer_id is the FieldRoutes customer (account) number.
  * commission and adjustment amounts carry their sign as paid (a negative
    adjustment took money away). Deduction amounts may be written either way;
    they are always treated as money taken back.
  * an adjustment whose note contains the word "upsell" is an upsell credit and
    is matched to the app's own upsell records instead of to a sale.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commissions.config import load_settings, payees
from commissions.periods import period_for
from commissions.store import connect, get_meta

ROOT = Path(__file__).resolve().parents[1]

FILES = {
    "paid": ("paid.csv", ["period_end", "rep", "customer_id", "contract_value", "commission"]),
    "adjustments": ("adjustments.csv", ["period_end", "rep", "customer_id", "amount", "note"]),
    "deductions": ("deductions.csv", ["period_end", "rep", "customer_id", "amount", "kind"]),
    "not_paid": ("not_paid.csv", ["customer_id", "rep", "date_sold"]),
}

# The four tables exactly as the engine reads them (engine.py, cutover.py).
DDL = """
DROP TABLE IF EXISTS manual_paid;
DROP TABLE IF EXISTS manual_adjustments;
DROP TABLE IF EXISTS manual_deducted;
DROP TABLE IF EXISTS manual_not_paid;
CREATE TABLE manual_paid (period TEXT, rep TEXT, customer_id TEXT, contract_value REAL, commission REAL);
CREATE TABLE manual_adjustments (period_end TEXT, rep_raw TEXT, rep TEXT, account TEXT, amount REAL,
                                 note TEXT, d2d_rep TEXT, serviced INTEGER, source TEXT);
CREATE TABLE manual_deducted (period_end TEXT, tab TEXT, section TEXT, rep TEXT, customer_id TEXT, amount REAL);
CREATE TABLE manual_not_paid (customer_id TEXT, rep TEXT, date_sold TEXT, balance REAL, completed_services REAL);
CREATE INDEX manual_paid_cust ON manual_paid(customer_id, rep);
CREATE INDEX manual_deducted_cust ON manual_deducted(customer_id, rep);
"""


def parse_day(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def parse_money(raw: str) -> float | None:
    t = (raw or "").strip().replace("$", "").replace(",", "")
    if not t:
        return None
    neg = t.startswith("(") and t.endswith(")")
    try:
        v = float(t.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def period_label(end: date) -> str:
    """The label the engine parses back into a period end ('08/16/26 - 08/29/26')."""
    start = end - timedelta(days=13)
    return f"{start:%m/%d/%y} - {end:%m/%d/%y}"


def read_file(path: Path, columns: list[str]) -> tuple[list[dict], list[str]]:
    """Rows as dicts keyed by the expected column names (header matched loosely)."""
    if not path.exists():
        return [], [f"{path.name}: file not found (an empty file with just the header row is fine)"]
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = [h.strip().lower().replace(" ", "_") for h in next(reader)]
        except StopIteration:
            return [], [f"{path.name}: empty file (it needs at least the header row)"]
        lost = [c for c in columns if c not in header]
        if lost:
            return [], [f"{path.name}: missing column(s) {', '.join(lost)} (found: {', '.join(header)})"]
        idx = {c: header.index(c) for c in columns}
        rows = []
        for n, r in enumerate(reader, start=2):
            if not any((x or "").strip() for x in r):
                continue
            rows.append({"_line": n, **{c: (r[i].strip() if i < len(r) else "") for c, i in idx.items()}})
    return rows, []


def main() -> int:
    ap = argparse.ArgumentParser(description="Load the manual payroll history (check only unless --write).")
    ap.add_argument("--dir", default=str(ROOT / "data" / "history"))
    ap.add_argument("--db", default=None, help="alternate database (a staging copy)")
    ap.add_argument("--write", action="store_true", help="replace the loaded history with these files")
    ap.add_argument("--force", action="store_true",
                    help="allow --write even after the go-live baseline was built (you must rebuild it)")
    args = ap.parse_args()

    s = load_settings()
    folder = Path(args.dir)
    conn = connect(Path(args.db) if args.db else s.db_path)
    roster = {" ".join(n.split()).casefold(): n for n in payees(s)}
    known_customers = {str(r[0]) for r in conn.execute("SELECT customer_id FROM customers")}

    problems: list[str] = []
    warnings: list[str] = []
    out: dict[str, list[tuple]] = {k: [] for k in FILES}
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    off_roster: dict[str, int] = defaultdict(int)
    unknown_customers: set[str] = set()
    odd_periods: dict[str, int] = defaultdict(int)

    def rep_name(raw: str, where: str) -> str | None:
        key = " ".join((raw or "").split()).casefold()
        if not key:
            problems.append(f"{where}: no rep")
            return None
        if key in roster:
            return roster[key]
        off_roster[" ".join(raw.split())] += 1
        return " ".join(raw.split())

    def customer(raw: str, where: str) -> str | None:
        cu = (raw or "").strip()
        if not cu.isdigit():
            problems.append(f"{where}: customer_id {cu!r} is not a number")
            return None
        if known_customers and cu not in known_customers:
            unknown_customers.add(cu)
        return cu

    def period_end(raw: str, where: str) -> date | None:
        d = parse_day(raw)
        if not d:
            problems.append(f"{where}: can't read the date {raw!r} (use YYYY-MM-DD)")
            return None
        if period_for(d, s.payroll_anchor_monday)[1] != d:
            odd_periods[d.isoformat()] += 1
        return d

    for kind, (fname, columns) in FILES.items():
        rows, errs = read_file(folder / fname, columns)
        problems += errs
        for r in rows:
            where = f"{fname} line {r['_line']}"
            rep = rep_name(r["rep"], where)
            cu = customer(r["customer_id"], where)
            if kind == "not_paid":
                ds = parse_day(r["date_sold"])
                if not ds:
                    problems.append(f"{where}: can't read date_sold {r['date_sold']!r}")
                if rep and cu and ds:
                    out[kind].append((cu, rep, ds.isoformat(), None, None))
                continue
            pe = period_end(r["period_end"], where)
            amount = parse_money(r["commission" if kind == "paid" else "amount"])
            if amount is None:
                problems.append(f"{where}: can't read the amount")
            if not (rep and cu and pe and amount is not None):
                continue
            if kind == "paid":
                cv = parse_money(r["contract_value"])
                if amount < 0:
                    warnings.append(f"{where}: a negative commission in paid.csv (should it be in deductions.csv?)")
                out[kind].append((period_label(pe), rep, cu, cv, amount))
                totals[rep]["paid"] += amount
            elif kind == "adjustments":
                out[kind].append((pe.isoformat(), r["rep"], rep, cu, amount, r["note"], None, None, "upload"))
                totals[rep]["adjustments"] += amount
            else:
                k = (r["kind"] or "").strip().lower().replace("-", "_").replace(" ", "_")
                if k not in ("cancel", "cv_change"):
                    problems.append(f"{where}: kind must be 'cancel' or 'cv_change', not {r['kind']!r}")
                    continue
                section = "bd_renewal" if k == "cv_change" else "deduction"
                out[kind].append((pe.isoformat(), "upload", section, rep, cu, abs(amount)))
                totals[rep]["cv true-downs" if k == "cv_change" else "deductions"] -= abs(amount)

    print(f"history folder: {folder}")
    for kind, (fname, _c) in FILES.items():
        print(f"  {fname:<18}{len(out[kind]):>8,} rows")
    if totals:
        cols = ["paid", "adjustments", "cv true-downs", "deductions"]
        print(f"\n{'Rep':<26}" + "".join(f"{c:>15}" for c in cols) + f"{'net held':>15}")
        for rep in sorted(totals, key=lambda r: (r not in roster.values(), r)):
            t = totals[rep]
            mark = "" if rep in roster.values() else "  (not on the roster)"
            print(f"{rep:<26}" + "".join(f"{t[c]:>15,.2f}" for c in cols) + f"{sum(t.values()):>15,.2f}{mark}")
        grand = {c: sum(t[c] for t in totals.values()) for c in cols}
        print(f"{'ALL':<26}" + "".join(f"{grand[c]:>15,.2f}" for c in cols) + f"{sum(grand.values()):>15,.2f}")
        print("\nCompare each rep's line with your manual books before loading. 'net held' is what the\n"
              "manual process has paid that rep and not taken back.")
    if off_roster:
        warnings.append("names not on the commission roster (loaded, but outside the app; check for "
                        "misspellings): "
                        + ", ".join(f"{n} ({c} rows)" for n, c in sorted(off_roster.items())))
    if unknown_customers:
        warnings.append(f"{len(unknown_customers)} customer id(s) are not in the synced FieldRoutes data "
                        f"(typos, or customers older than BACKFILL_SINCE), e.g. "
                        + ", ".join(sorted(unknown_customers)[:8]))
    if odd_periods:
        warnings.append("dates that are not the last day of a pay period for PAYROLL_ANCHOR_MONDAY="
                        f"{s.payroll_anchor_monday}: "
                        + ", ".join(f"{d} ({c} rows)" for d, c in sorted(odd_periods.items())[:8]))
    if not known_customers:
        warnings.append("the database has no customers yet: run scripts/backfill.py first so customer "
                        "numbers can be checked")
    for w in warnings:
        print("WARNING:", w)
    for p in problems[:40]:
        print("PROBLEM:", p)
    if len(problems) > 40:
        print(f"... and {len(problems) - 40} more problems")

    if not args.write:
        print("\ncheck only: nothing was written" + (" (fix the problems above first)" if problems else
                                                      ". Run again with --write to load."))
        return 1 if problems else 0
    if problems:
        print("\nnot loaded: fix the problems above first")
        return 1
    seeded = get_meta(conn, "cutover_seeded_at")
    if seeded and not args.force:
        print(f"\nnot loaded: the go-live baseline was already built from the history ({seeded}).")
        return 1
    # one transaction: the old tables are dropped and the new ones filled together,
    # so a failure part-way leaves the history exactly as it was
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in (x.strip() for x in DDL.split(";")):
            if stmt:
                conn.execute(stmt)
        conn.executemany("INSERT INTO manual_paid VALUES (?,?,?,?,?)", out["paid"])
        conn.executemany("INSERT INTO manual_adjustments VALUES (?,?,?,?,?,?,?,?,?)", out["adjustments"])
        conn.executemany("INSERT INTO manual_deducted VALUES (?,?,?,?,?,?)", out["deductions"])
        conn.executemany("INSERT INTO manual_not_paid VALUES (?,?,?,?,?)", out["not_paid"])
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    print("\nloaded: the four history tables were replaced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
