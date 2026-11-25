"""Period-isolated payroll report (the parallel-run mode).

Re-simulates the whole ledger from scratch under CURRENT rules — as if every
prior period had been done right — then reports ONLY the requested period.
No catch-up rows ever appear: rule fixes and data corrections silently land
in the periods they belong to, and the report shows just this period's
activity. The production ledger is NOT touched.

  python scripts/report_payroll.py                       # latest closed period
  python scripts/report_payroll.py --period-end 2026-09-26

Run scripts/sync.py first so the data is fresh. Manual one-off ledger rows
(kind='manual', from assign.py) are carried into the simulation.

LIVE periods (ending on/after the first live period, RULES §8c): no
simulation — the production ledger (seeded at the cutover from the manual
books) is closed for the period, under a lock and after a DB backup. A
period can be re-run (its rows are replaced) until its payroll Monday has
passed; after that it is locked and later changes land in the next period.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commissions.config import load_settings
from commissions.engine import close_period, compute_status, seed_external
from commissions.periods import anchor_period_end, latest_closed_period, payroll_monday
from commissions.store import connect


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period-end", default=None, help="Saturday YYYY-MM-DD")
    ap.add_argument("--keep-sim", action="store_true",
                    help="keep the simulation DB for inspection")
    ap.add_argument("--preview", action="store_true",
                    help="live: close a period that has not ended yet (staging tests)")
    ap.add_argument("--finalize", action="store_true",
                    help="live: mark the period PAID (final) - it can never be re-run")
    args = ap.parse_args()

    s = load_settings()
    if args.period_end:
        end = date.fromisoformat(args.period_end)
    else:
        _, end = latest_closed_period(date.today(), s.payroll_anchor_monday)
    from commissions.cutover import first_live_end
    if end.isoformat() >= first_live_end(s):
        if args.finalize:
            raise SystemExit(finalize(s, end))
        raise SystemExit(live_close(s, end, args.preview))
    print(f"Pay period {end - timedelta(days=13)} .. {end} "
          f"(payroll Monday {payroll_monday(end)}) — period-isolated simulation "
          f"(PRE-GO-LIVE: the manual sheet paid this period)")

    sim = Path(tempfile.mkdtemp()) / f"resim_{end.isoformat()}.sqlite"
    src = sqlite3.connect(s.db_path)
    dst = sqlite3.connect(sim)
    src.backup(dst)
    src.close()
    # wipe the ledger but carry manual one-off rows for their periods
    manual_rows = dst.execute(
        "SELECT subscription_id, rep, period_end, amount, kind, note "
        "FROM ledger WHERE kind='manual'").fetchall()
    dst.execute("DELETE FROM ledger")
    dst.executemany("INSERT INTO ledger(subscription_id,rep,period_end,amount,"
                    "kind,note) VALUES(?,?,?,?,?,?)", manual_rows)
    dst.commit()
    dst.close()

    conn = connect(sim)
    facts = compute_status(conn, s)
    seed_external(conn, s, facts)
    # close every period from go-live through the requested one
    pend = anchor_period_end(s.payroll_anchor_monday)      # first period end
    while pend <= end:
        close_period(conn, s, facts,
                     (pend - timedelta(days=13)).isoformat(), pend.isoformat())
        pend += timedelta(days=14)

    # per-rep summary for the requested period only
    print(f"\n{'Rep':<24}{'Commissions':>14}{'Deductions':>14}{'Net':>12}")
    print("-" * 64)
    tot_p = tot_d = 0.0
    for rep, pos, neg in conn.execute("""
            SELECT rep, ROUND(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),2),
                   ROUND(SUM(CASE WHEN amount<0 THEN amount ELSE 0 END),2)
            FROM ledger WHERE period_end=? AND kind NOT IN ('vest','external')
            GROUP BY rep ORDER BY rep""", (end.isoformat(),)):
        tot_p += pos
        tot_d += neg
        print(f"{rep:<24}{pos:>14,.2f}{neg:>14,.2f}{pos + neg:>12,.2f}")
    print("-" * 64)
    print(f"{'TOTAL':<24}{tot_p:>14,.2f}{tot_d:>14,.2f}{tot_p + tot_d:>12,.2f}")

    # workbook via the standard exporter, pointed at the simulation
    import subprocess
    out = s.db_path.parent / "reports" / f"commission_statement_{end.isoformat()}.xlsx"
    subprocess.run([sys.executable, str(Path(__file__).parent / "export_xlsx.py"),
                    "--period-end", end.isoformat(), "--db", str(sim),
                    "--out", str(out)], check=True)
    print(f"\nworkbook: {out}")
    if args.keep_sim:
        print(f"simulation db kept: {sim}")


def _summary(conn, period_end: str) -> tuple[float, float]:
    """Per-rep totals for the period and for each week (RULES §7), from the
    statement's own net lines (commissions.statement), so the run card always
    shows what the statement workbook shows."""
    from commissions.periods import week_ranges
    from commissions.statement import rep_totals, statement_lines
    reps = rep_totals(statement_lines(conn, period_end), period_end)
    R = lambda v: round(v, 2)  # noqa: E731
    print(f"\n{'Rep':<24}{'Commissions':>14}{'Deductions':>14}{'Net':>12}")
    print("-" * 64)
    tot_p = tot_d = 0.0
    for rep in sorted(reps):
        pos, neg = R(reps[rep]["pos"]), R(reps[rep]["neg"])
        tot_p += pos
        tot_d += neg
        print(f"{rep:<24}{pos:>14,.2f}{neg:>14,.2f}{pos + neg:>12,.2f}")
    print("-" * 64)
    print(f"{'TOTAL':<24}{tot_p:>14,.2f}{tot_d:>14,.2f}{tot_p + tot_d:>12,.2f}")
    (w1s, w1e), (w2s, w2e) = week_ranges(period_end)
    md = lambda d: d.strftime("%m/%d")  # noqa: E731
    print(f"\nBy week (RULES 7): week 1 = {md(w1s)}-{md(w1e)}, week 2 = {md(w2s)}-{md(w2e)}")
    print(f"{'Rep':<24}{'Wk1 Comm':>12}{'Wk1 Ded':>12}{'Wk1 Net':>12}"
          f"{'Wk2 Comm':>12}{'Wk2 Ded':>12}{'Wk2 Net':>12}")
    print("-" * 96)
    wt = [0.0, 0.0, 0.0, 0.0]
    for rep in sorted(reps):
        t = reps[rep]
        p1, d1, p2, d2 = (R(t[k]) for k in ("w1_pos", "w1_neg", "w2_pos", "w2_neg"))
        wt[0] += p1; wt[1] += d1; wt[2] += p2; wt[3] += d2
        print(f"{rep:<24}{p1:>12,.2f}{d1:>12,.2f}{p1 + d1:>12,.2f}"
              f"{p2:>12,.2f}{d2:>12,.2f}{p2 + d2:>12,.2f}")
    print("-" * 96)
    print(f"{'ALL REPS':<24}{wt[0]:>12,.2f}{wt[1]:>12,.2f}{wt[0] + wt[1]:>12,.2f}"
          f"{wt[2]:>12,.2f}{wt[3]:>12,.2f}{wt[2] + wt[3]:>12,.2f}")
    print(f"Week 1 ({md(w1s)}-{md(w1e)}): commissions {wt[0]:,.2f} deductions {wt[1]:,.2f} "
          f"total {wt[0] + wt[1]:,.2f}")
    print(f"Week 2 ({md(w2s)}-{md(w2e)}): commissions {wt[2]:,.2f} deductions {wt[3]:,.2f} "
          f"total {wt[2] + wt[3]:,.2f}")
    return round(tot_p, 2), round(tot_d, 2)


def _flags(conn, period_end: str) -> None:
    rows = conn.execute("SELECT subscription_id, note FROM live_flags WHERE period_end=? "
                        "ORDER BY subscription_id", (period_end,)).fetchall()
    if rows:
        print(f"\nREVIEW ({len(rows)} items the close did not do automatically):")
        for sid, note in rows:
            print(f"  {sid}: {note}")


def finalize(s, end: date) -> int:
    """Mark a live period PAID: it can never be re-run; later changes land in
    the next period."""
    conn = connect(s.db_path)
    P = end.isoformat()
    row = conn.execute("SELECT status FROM payroll_runs WHERE period_end=?", (P,)).fetchone()
    if not row or row[0] not in ("draft", "final"):
        print(f"{P} has no completed run to finalize")
        return 1
    conn.execute("UPDATE payroll_runs SET status='final' WHERE period_end=?", (P,))
    conn.commit()
    print(f"{P} marked PAID (final)")
    return 0


def live_close(s, end: date, preview: bool = False) -> int:
    """Close a LIVE pay period on the production ledger (RULES 8c).

    Safe to re-run until the period is final: the period rows are replaced.
    If anything fails mid-close the DB is restored from the backup taken just
    before, so a half-written period never survives."""
    import os
    from datetime import datetime
    from commissions.cutover import close_live_period, first_live_end
    from commissions.store import get_meta
    P, start = end.isoformat(), (end - timedelta(days=13)).isoformat()
    print(f"Pay period {start} .. {P} (payroll Monday {payroll_monday(end)}) - LIVE")
    print(f"roster ({s.roster_source}): {len(s.isa_roster)} inside sales"
          + (f" + managers {', '.join(s.managers)}" if s.managers else ""))
    if end >= date.today() and not preview:
        print(f"{P} has not ended yet - nothing to close")
        return 1
    lock = open(s.db_path.parent / "payroll_close.lock", "w")
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("another payroll close is running - not starting")
        return 1
    conn = connect(s.db_path)
    if not get_meta(conn, "cutover_seeded_at"):
        print("the go-live cutover has not been seeded (scripts/build_cutover.py)")
        return 1
    later = conn.execute("SELECT period_end FROM payroll_runs WHERE period_end > ? "
                         "ORDER BY period_end LIMIT 1", (P,)).fetchone()
    if later or conn.execute(
            "SELECT 1 FROM ledger WHERE period_end > ? AND kind NOT IN "
            "('baseline','manual') LIMIT 1", (P,)).fetchone():
        print(f"a later period ({later[0] if later else 'ledger rows'}) is already closed "
              f"- {P} is locked")
        return 1
    prev_end = (end - timedelta(days=14)).isoformat()
    if P != first_live_end(s):
        pr = conn.execute("SELECT status FROM payroll_runs WHERE period_end=?",
                          (prev_end,)).fetchone()
        if not pr or pr[0] not in ("draft", "final"):
            print(f"close {prev_end} first - live periods close in order"
                  + (f" (its last run did not finish: {pr[0]})" if pr else ""))
            return 1
        if pr[0] == "draft":            # closing P means P-14 was paid
            conn.execute("UPDATE payroll_runs SET status='final' WHERE period_end=?",
                         (prev_end,))
            conn.commit()
    ran = conn.execute("SELECT status FROM payroll_runs WHERE period_end=?", (P,)).fetchone()
    if ran and ran[0] == "final":
        print(f"{P} was paid (final) - locked; later changes land in the next period")
        return 1
    if ran and ran[0] != "draft" and date.today() > payroll_monday(end):
        # an unfinished run (killed while exporting) is NOT a paid period:
        # never finalize it - its rows may not match the statement payroll used
        print(f"{P}: its last run did not finish ({ran[0]}) and payroll Monday has "
              f"passed - NOT finalizing and NOT re-running. Compare payroll_runs.totals "
              f"with the statement payroll used, then fix by hand (set it 'draft' and "
              f"--finalize if they match, else put back its rows from "
              f"data/backups/pre_close_{P}_*.sqlite)")
        return 1
    if ran and date.today() > payroll_monday(end):
        print(f"{P} was paid on {payroll_monday(end)} - locked; later changes "
              f"land in the next period")
        conn.execute("UPDATE payroll_runs SET status='final' WHERE period_end=?", (P,))
        conn.commit()
        return 1

    facts = compute_status(conn, s)                  # before touching the period
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = s.db_path.parent / "backups"
    bdir.mkdir(exist_ok=True)
    backup = bdir / f"pre_close_{P}_{stamp}.sqlite"
    bk = sqlite3.connect(backup)
    conn.backup(bk)
    bk.close()
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = s.db_path.parent / "reports" / f"commission_statement_{P}.xlsx"
    exported = False
    try:
        # ONE transaction from the delete to the period's last row: a run
        # killed anywhere before this commit leaves the DB exactly as it was.
        # It commits as 'running' and turns 'draft' only once the statement
        # is on disk, so a run killed while exporting blocks the next period
        # and is never finalized (a re-run replaces the period rows).
        conn.execute("DELETE FROM ledger WHERE period_end=? AND kind NOT IN "
                     "('baseline','manual')", (P,))
        conn.execute("DELETE FROM live_state WHERE period_end=?", (P,))
        conn.execute("""INSERT INTO payroll_runs(period_end, status, started_at)
            VALUES(?, 'running', ?) ON CONFLICT(period_end) DO UPDATE SET
            status='running', started_at=excluded.started_at, finished_at=NULL""",
                     (P, started))
        close_live_period(conn, s, facts, start, P, commit=False)
        pos, neg = _summary(conn, P)
        conn.execute("""UPDATE payroll_runs SET finished_at=?, data_as_of=?, totals=?
            WHERE period_end=?""",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      get_meta(conn, "last_sync"), f"{pos:.2f}/{neg:.2f}", P))
        conn.commit()
        _flags(conn, P)
        import subprocess
        tmp = out.with_name(out.stem + ".tmp.xlsx")
        subprocess.run([sys.executable, str(Path(__file__).parent / "export_xlsx.py"),
                        "--period-end", P, "--out", str(tmp)], check=True)
        os.replace(tmp, out)
        exported = True
        conn.execute("UPDATE payroll_runs SET status='draft' WHERE period_end=?", (P,))
        conn.commit()
    except BaseException:
        conn.close()
        if exported:
            # the statement on disk matches the committed rows: keep both;
            # the period stays 'running' until it is re-run
            print(f"close of {P} wrote its statement but was not marked done - re-run it")
            raise
        src = sqlite3.connect(backup)
        dst = sqlite3.connect(s.db_path)
        src.backup(dst)                              # put the DB back as it was
        dst.close()
        src.close()
        print(f"close FAILED - database restored from {backup}")
        raise
    print(f"\nworkbook: {out}")
    return 0


if __name__ == "__main__":
    main()
