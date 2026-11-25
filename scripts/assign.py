"""Manage split / rescue commission assignments (RULES §11).

  python scripts/assign.py add 12345 "Rowan Brook=0.5" "Finley Lake=0.5" --type split --note "..."
  python scripts/assign.py add 67890 "Harper Shaw=1" --type rescue --note "D2D reschedule 8/25"
  python scripts/assign.py remove 67890            # soft-remove all active rows for the sub
  python scripts/assign.py remove 67890 --rep "Harper Shaw"
  python scripts/assign.py list [--sub 12345]

Active assignments REPLACE the soldBy attribution for that subscription.
Removing after a commission was paid claws it back on the next payroll run
(that is what removing the credit means); removing before anything was paid
is a clean no-op.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commissions.config import load_settings
from commissions.store import connect


def canonical_rep(name: str, roster: list[str]) -> str:
    m = [r for r in roster if r.casefold() == name.strip().casefold()]
    if not m:
        near = [r for r in roster if name.strip().casefold() in r.casefold()]
        hint = f" Did you mean: {', '.join(near)}?" if near else ""
        raise SystemExit(f"'{name}' is not on the ISA roster.{hint}")
    return m[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("subscription_id")
    a.add_argument("shares", nargs="+", help='e.g. "Rowan Brook=0.5"')
    a.add_argument("--type", choices=["split", "rescue"], required=True)
    a.add_argument("--note", default=None)

    r = sub.add_parser("remove")
    r.add_argument("subscription_id")
    r.add_argument("--rep", default=None)

    ls = sub.add_parser("list")
    ls.add_argument("--sub", default=None)
    ls.add_argument("--all", action="store_true", help="include removed rows")

    up = sub.add_parser("upsell", help="one-time upsell credit (RULES §12)")
    up.add_argument("subscription_id")
    up.add_argument("rep")
    up.add_argument("amount", type=float)
    up.add_argument("--acv", type=float, default=None,
                    help="ACV increase (default: amount / 9%%)")
    up.add_argument("--date", required=True, help="upsell date YYYY-MM-DD")
    up.add_argument("--d2d-rep", default=None,
                    help="original D2D seller (year-end finance report)")
    up.add_argument("--note", default=None)

    mn = sub.add_parser("manual", help="raw one-time ledger adjustment")
    mn.add_argument("rep")
    mn.add_argument("amount", type=float)
    mn.add_argument("--period-end", required=True)
    mn.add_argument("--note", required=True)

    args = ap.parse_args()
    s = load_settings()
    conn = connect(s.db_path)

    if args.cmd == "add":
        pairs = []
        for spec in args.shares:
            if "=" not in spec:
                raise SystemExit(f'bad share "{spec}" — use "Rep Name=0.5"')
            name, frac = spec.rsplit("=", 1)
            pairs.append((canonical_rep(name, s.isa_roster), float(frac)))
        total = sum(f for _, f in pairs)
        if not (0 < total <= 1.0001):
            raise SystemExit(f"shares sum to {total:g}; must be in (0, 1]")
        # replace any existing active rows for this sub
        conn.execute("UPDATE assignments SET removed_at=datetime('now') "
                     "WHERE subscription_id=? AND removed_at IS NULL",
                     (args.subscription_id,))
        for rep, frac in pairs:
            conn.execute("INSERT INTO assignments(subscription_id,rep,share,type,note) "
                         "VALUES(?,?,?,?,?)",
                         (args.subscription_id, rep, frac, args.type, args.note))
        conn.commit()
        print(f"assigned {args.subscription_id} ({args.type}): "
              + ", ".join(f"{rep} {frac:.0%}" for rep, frac in pairs))
        print("takes effect on the next payroll run / status refresh")

    elif args.cmd == "remove":
        q = "UPDATE assignments SET removed_at=datetime('now') " \
            "WHERE subscription_id=? AND removed_at IS NULL"
        p: list = [args.subscription_id]
        if args.rep:
            q += " AND rep=?"
            p.append(canonical_rep(args.rep, s.isa_roster))
        n = conn.execute(q, p).rowcount
        conn.commit()
        print(f"removed {n} active assignment(s) for {args.subscription_id}")
        if n:
            print("if commission was already paid on this sub, the next run claws it back")

    elif args.cmd == "upsell":
        rep = canonical_rep(args.rep, s.isa_roster)
        acv = args.acv if args.acv is not None else round(args.amount / 0.09, 2)
        conn.execute("INSERT INTO upsells(subscription_id,rep,amount,acv_increase,"
                     "date,d2d_rep,note) VALUES(?,?,?,?,?,?,?)",
                     (args.subscription_id, rep, args.amount, acv, args.date,
                      args.d2d_rep, args.note))
        conn.commit()
        print(f"upsell: {rep} +${args.amount:,.2f} on {args.subscription_id} "
              f"(ACV +${acv:,.2f}{', D2D: ' + args.d2d_rep if args.d2d_rep else ''})"
              " — pays on the next payroll run")

    elif args.cmd == "manual":
        rep = canonical_rep(args.rep, s.isa_roster)
        conn.execute("INSERT INTO ledger(subscription_id,rep,period_end,amount,"
                     "kind,note) VALUES('-',?,?,?,'manual',?)",
                     (rep, args.period_end, args.amount, args.note))
        conn.commit()
        print(f"manual: {rep} {args.amount:+,.2f} in period ending {args.period_end}")

    else:
        q = ("SELECT subscription_id, rep, share, type, note, created_at, removed_at "
             "FROM assignments")
        cond, p = [], []
        if args.sub:
            cond.append("subscription_id=?"); p.append(args.sub)
        if not args.all:
            cond.append("removed_at IS NULL")
        if cond:
            q += " WHERE " + " AND ".join(cond)
        rows = conn.execute(q + " ORDER BY created_at", p).fetchall()
        if not rows:
            print("no assignments")
        for row in rows:
            sid, rep, share, typ, note, created, removed = row
            status = f"REMOVED {removed[:10]}" if removed else "active"
            print(f"{sid:>10}  {typ:<7} {rep:<22} {share:>5.0%}  {status:<18} "
                  f"{created[:10]}  {note or ''}")


if __name__ == "__main__":
    main()
