"""Which of YOUR FieldRoutes labels the rules recognise by name (run after the backfill).

Some rules are matched on FieldRoutes text, written the way the original company's
FieldRoutes account spells it. If your account spells a thing differently, the rule
silently doesn't apply (for example, a cancel reason "Customer Moved" is not treated
as "Moved", so the rep is deducted when they shouldn't be). This lists every source,
cancel reason and service type in your data, with how often each occurs and which
rule (if any) recognises it, so you can compare with how your company uses them.

    python scripts/check_labels.py                # reads data/commissions.sqlite, read-only

What is matched, and where (COMMISSION_RULES.md):
  sources        contains "renewal"            -> the alternate rate (§3), also ALT_RATE_SOURCES
                 exactly "bad debt renewal"    -> a re-sell of a bad-debt account (§5)
                 exactly "upsell" + a service type containing "month mosquito" -> alternate rate (§3)
  cancel reasons exactly "renewal"             -> a renewal, not a real cancel (§5)
                 begins with "moved"           -> moved away: no deduction (§5)
  service types  exactly "initial service"     -> a one-time job (§3)
                 contains "ancellation"        -> a cancellation-fee invoice (§5)
                 one of NOT_FEE_TYPES below    -> a fee that is never a cancellation fee
                 in COMMERCIAL_SERVICE_TYPES   -> commercial rate (§3)
                 type ids above 199 are never read from FieldRoutes (ingest.refresh_service_types)
(All compared without case.) Changing a match is a code change: record the decision
in onboarding/DECISIONS.md and change the rulebook with it.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

NOT_FEE_TYPES = {"bounced check fee", "reimbursement fee", "collections late fee", "service fee",
                 "overdraft fee", "technician tip", "collect payment"}


def source_rule(src: str, alt: set[str]) -> str:
    s = src.strip().lower()
    out = []
    if s in alt:
        out.append("alternate rate (ALT_RATE_SOURCES)")
    if "renewal" in s:
        out.append("alternate rate (contains 'renewal')")
    if s == "bad debt renewal":
        out.append("bad-debt re-sell (§5)")
    if s == "upsell":
        out.append("alternate rate on 'month mosquito' plans")
    return "; ".join(out)


def reason_rule(reason: str) -> str:
    r = reason.strip().lower()
    if r == "renewal":
        return "renewal: not a real cancel (§5)"
    if r.startswith("moved"):
        return "moved: no deduction (§5)"
    return ""


def type_rule(desc: str, commercial: set[str]) -> str:
    d = desc.strip().lower()
    out = []
    if d == "initial service":
        out.append("one-time job (§3)")
    if "ancellation" in d:
        out.append("cancellation-fee invoice (§5)")
    if d in NOT_FEE_TYPES:
        out.append("never a cancellation fee")
    if "month mosquito" in d:
        out.append("'month mosquito' (with source 'upsell')")
    if d in commercial:
        out.append("commercial rate (COMMERCIAL_SERVICE_TYPES)")
    return "; ".join(out)


def report(conn, alt: set[str], commercial: set[str], out=print) -> None:
    def table(title, rows, rule):
        out(f"\n{title}")
        out(f"  {'count':>7}  {'label':<40} recognised as")
        for label, n in rows:
            out(f"  {n:>7}  {(label or '(blank)'):<40} {rule(label or '') or '-'}")
    table("Sources on subscriptions", conn.execute(
        "SELECT source, COUNT(*) FROM subscriptions GROUP BY source ORDER BY COUNT(*) DESC"),
        lambda x: source_rule(x, alt))
    table("Cancel reasons (latest cancellation note per subscription)", conn.execute(
        "SELECT cancel_reason, COUNT(*) FROM subscriptions WHERE cancel_reason IS NOT NULL "
        "AND cancel_reason != '' GROUP BY cancel_reason ORDER BY COUNT(*) DESC"), reason_rule)
    table("Service types on subscriptions", conn.execute(
        "SELECT service_type, COUNT(*) FROM subscriptions GROUP BY service_type ORDER BY COUNT(*) DESC"),
        lambda x: type_rule(x, commercial))
    try:
        types = conn.execute("SELECT description, COUNT(*) FROM service_types "
                             "GROUP BY description ORDER BY description").fetchall()
        top = conn.execute("SELECT MAX(CAST(type_id AS INTEGER)) FROM service_types").fetchone()[0]
    except sqlite3.OperationalError:
        types, top = [], None
    table("Every service type in the account (incl. fee and invoice types)", types,
          lambda x: type_rule(x, commercial))
    if top is not None and top >= 199:
        out(f"\nWARNING: your highest service type id read is {top}: ids above 199 are never "
            "read (src/commissions/ingest.py refresh_service_types). Widen that range.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "commissions.sqlite"))
    args = ap.parse_args()
    db = Path(args.db)
    if not db.is_file():
        print(f"no database at {db}: run scripts/backfill.py first")
        return 2
    from commissions.config import load_settings
    s = load_settings()
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        report(conn, set(s.alt_rate_sources), set(s.commercial_service_types))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
