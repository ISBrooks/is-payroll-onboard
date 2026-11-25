"""Go-live cutover (RULES §8c): the manual payroll paid everything through the
day before PAYROLL_LIVE_FROM; the app pays from that day on.

build_baseline()      what the manual books show each rep holding on each
                      sale through the last manual period, plus each sale's state
                      and contract value at the cutover (the manual's own view of
                      FieldRoutes: the snapshot synced alongside its last pull).
seed_ledger()         archives the parallel-run ledger and starts a fresh one
                      holding exactly those amounts.
close_live_period()   a live pay period. A sale the manual books settled moves
                      money only on something that happened AFTER the cutover
                      (cancel, fee paid, reinstatement, contract-value change,
                      split change); its past differences stand. Every other
                      sale follows the normal rules (engine.close_period).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache

from .config import Settings
from .engine import EPS, SubFacts, _cents, _plabel_end, active_shares, close_period
from .store import connect

log = logging.getLogger(__name__)

@lru_cache(maxsize=None)
def _cut() -> tuple[str, str, str]:
    """(last manual day, its end of day, LIST_AT_UTC) for this install (RULES §8):
    the manual payroll paid everything through the day before PAYROLL_LIVE_FROM;
    upsells and splits booked in the app after CUTOVER_LIST_AT_UTC (the moment
    the manual's last adjustments list was made, UTC like created_at) are the
    app's to pay. Read after load_settings() has loaded .env."""
    import os
    live = (os.environ.get("PAYROLL_LIVE_FROM") or "").strip()
    at = (os.environ.get("CUTOVER_LIST_AT_UTC") or "").strip()
    if not live or not at:
        raise SystemExit("the go-live switch-over needs PAYROLL_LIVE_FROM and "
                         "CUTOVER_LIST_AT_UTC in .env (COMMISSION_RULES.md §8)")
    try:
        end = (date.fromisoformat(live) - timedelta(days=1)).isoformat()
        at = datetime.fromisoformat(at).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError as e:
        raise SystemExit(f"PAYROLL_LIVE_FROM / CUTOVER_LIST_AT_UTC in .env: {e}") from None
    return end, end + " 23:59:59", at


def first_live_end(s: Settings) -> str:
    return (date.fromisoformat(s.payroll_live_from) + timedelta(days=13)).isoformat()


# ------------------------------------------------------------------ baseline

class _Sales:
    """Who sold what to whom, for pinning manual rows to sales."""

    def __init__(self, conn: sqlite3.Connection, facts: dict[str, SubFacts]):
        self.cust: dict[str, str] = {}
        self.cv: dict[str, float] = {}
        self.day: dict[str, str] = {}
        self.cancel_day: dict[str, str] = {}
        self.first_paid: dict[str, str] = {}
        self.by_rep: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        self.by_cust: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for sid, cu, rep, da, cv, act, dc in conn.execute(
                "SELECT subscription_id, customer_id, rep, COALESCE(date_added,''), "
                "COALESCE(contract_value,0), active, date_cancelled FROM subscriptions"):
            cu = str(cu)
            f = facts.get(sid)
            fp = (f.first_paid_date or "") if f else ""
            # FR renews in place and resets dateAdded: a sale is at least as
            # old as its first payment
            d = min(x for x in (da[:10], fp[:10]) if x) if (da or fp) else ""
            self.cust[sid], self.cv[sid], self.day[sid] = cu, float(cv or 0), d
            self.first_paid[sid] = fp
            if not act and dc:
                self.cancel_day[sid] = dc[:10]
            self.by_cust[cu].append((d, sid))
            if rep:
                self.by_rep[(cu, rep)].append((d, sid))
        for sid, rep in conn.execute("SELECT subscription_id, rep FROM assignments"):
            cu = self.cust.get(sid)
            if cu and (self.day[sid], sid) not in self.by_rep[(cu, rep)]:
                self.by_rep[(cu, rep)].append((self.day[sid], sid))
        for v in list(self.by_rep.values()) + list(self.by_cust.values()):
            v.sort()

    def _pick(self, cand: list[tuple[str, str]], day: str,
              cv: float | None) -> dict[str, float] | None:
        cand = [(d, s_) for d, s_ in cand if d and d <= day]
        if not cand:
            return None
        last = cand[-1][0]
        same = [s_ for d, s_ in cand if d == last]
        if len(same) == 1:
            return {same[0]: 1.0}
        if cv:
            hits = [s_ for s_ in same if abs(self.cv[s_] - cv) <= 1.0]
            if len(hits) == 1:
                return {hits[0]: 1.0}
            tot = sum(self.cv[s_] for s_ in same)
            if tot > EPS and abs(tot - cv) <= 1.0:          # one line for both
                return {s_: self.cv[s_] / tot for s_ in same}
        trig = [s_ for s_ in same if self.first_paid[s_] and self.first_paid[s_][:10] <= day]
        if len(trig) == 1:
            return {trig[0]: 1.0}
        pool = trig or same
        tot = sum(self.cv[s_] for s_ in pool)
        return ({s_: self.cv[s_] / tot for s_ in pool} if tot > EPS
                else {s_: 1.0 / len(pool) for s_ in pool})

    def owner(self, cu: str, rep: str, day: str, cv: float | None = None):
        return self._pick(self.by_rep.get((cu, rep), []), day, cv)

    def owner_deduction(self, cu: str, rep: str, day: str):
        """A cancel deduction belongs to the rep's sale most recently
        cancelled on/before it (cust 900013)."""
        cand = [(self.cancel_day[s_], s_) for d, s_ in self.by_rep.get((cu, rep), [])
                if d and d <= day and self.cancel_day.get(s_, "9999") <= day]
        if cand:
            cand.sort()
            return {cand[-1][1]: 1.0}
        return self.owner(cu, rep, day)

    def owner_any_rep(self, cu: str, day: str):
        """Adjustments can name a rep FieldRoutes never credited (a split
        partner with no app assignment): fall back to the customer's sale."""
        return self._pick(self.by_cust.get(cu, []), day, None)


# a roster rep the manual sheets sometimes name differently: sheet name -> roster
# name. Add only cases verified one by one (a similar name can be another person).
REP_ALIASES: dict[str, str] = {
    # "Sam": "Samantha Example",
}


def _manual_rows(conn: sqlite3.Connection):
    """Every manual-book row as (kind, customer, rep, period_end, amount, cv).
    kind: G paid, A adjustment (non-upsell), T CV true-down, D deduction."""
    al = REP_ALIASES.get
    for per, rep, cu, cv, amt in conn.execute(
            "SELECT period, rep, customer_id, contract_value, commission FROM manual_paid"):
        yield "G", str(cu), al(rep, rep), _plabel_end(per or ""), float(amt or 0), cv
    # an adjustment entered twice by accident (same account, rep, period,
    # amount and note) counts once — owner 2026-10-15 (900024: one D2D split
    # on the sheet twice). Two payments only for two subscriptions.
    for pe, rep, acct, amt, note in conn.execute(
            "SELECT DISTINCT period_end, rep, account, amount, note FROM manual_adjustments"):
        if "upsell" in (note or "").lower():
            continue                      # upsell credits: matched separately
        yield "A", str(acct), al(rep, rep), pe, float(amt or 0), None
    for pe, sec, rep, cu, amt in conn.execute(
            "SELECT period_end, section, rep, customer_id, amount FROM manual_deducted"):
        yield (("T" if sec == "bd_renewal" else "D"), str(cu), al(rep, rep), pe,
               -abs(float(amt or 0)), None)


def _state(f: SubFacts | None, cutoff: str, manual_paid: bool, golive: str) -> str:
    """vested | excluded | half | claw | active | pending, as of cutoff."""
    if f is None:
        return "pending"
    if f.vested_date and f.vested_date <= cutoff:
        return "vested"
    if not f.active and f.date_cancelled and f.date_cancelled <= cutoff:
        if f.cancel_excluded:
            return "excluded"
        fee_ok = f.cxl_fee_paid and (
            (f.date_cancelled or "")[:10] < golive
            or (f.cxl_fee_paid_date or "9999") <= cutoff)
        return "half" if fee_ok else "claw"
    if (f.first_paid_date and f.first_paid_date <= cutoff) or manual_paid:
        return "active"
    return "pending"


def _assigned(conn: sqlite3.Connection, sid: str, at_utc: str | None) -> dict[str, float]:
    """Booked assignment shares {rep: stored share} existing at at_utc (None =
    now); empty when the sale has none."""
    q = ("SELECT rep, share FROM assignments WHERE subscription_id=? "
         "AND (removed_at IS NULL" + (" OR removed_at > ?) AND created_at <= ?"
                                      if at_utc else ")"))
    out: dict[str, float] = {}
    for rep, share in conn.execute(q, [sid, at_utc, at_utc] if at_utc else [sid]):
        out[rep] = round(out.get(rep, 0.0) + float(share), 6)
    return out


def _shares_at(conn: sqlite3.Connection, f: SubFacts, at_utc: str | None) -> dict[str, float]:
    """App attribution {rep: stored share} from assignments existing at
    at_utc (None = now); soldBy rep at 100% when there are none. Stored
    shares, never recomputed from today's rate, so only a booked split change
    reads as a change."""
    q = ("SELECT rep, share, type FROM assignments WHERE subscription_id=? "
         "AND (removed_at IS NULL" + (" OR removed_at > ?)" if at_utc else ")"))
    args: list = [f.subscription_id]
    if at_utc:
        q += " AND created_at <= ?"
        args += [at_utc, at_utc]
    out: dict[str, float] = {}
    for rep, share, _typ in conn.execute(q, args):
        out[rep] = out.get(rep, 0.0) + float(share)
    if not out and f.rep:
        out[f.rep] = 1.0
    return {r: round(v, 6) for r, v in out.items()}


def build_baseline(conn: sqlite3.Connection, s: Settings, facts: list[SubFacts],
                   snapshot: str, notpaid_live: set[tuple[str, str, str]],
                   settle: set[str] = frozenset()) -> dict:
    """Fill cutover_sale / cutover_baseline / cutover_legacy (replacing them).
    `snapshot` = path of a copy of this app's DB synced when the manual payroll
    pulled its data for its last period (RULES §8c).
    `notpaid_live` = (customer, rep, date sold) of the manual "not paid" list
    (manual_not_paid): the manual would have paid those once the customer
    paid, so they are timing, not misses."""
    fx = {f.subscription_id: f for f in facts}
    sales = _Sales(conn, fx)
    hold: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"G": 0.0, "A": 0.0, "T": 0.0, "D": 0.0, "N": 0.0})
    legacy: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0])
    cv_paid: dict[str, float] = {}           # CV the sheet paid the sale on
    cv_paid_at: dict[str, str] = {}
    for kind, cu, rep, pe, amt, cv in _manual_rows(conn):
        if abs(amt) < 0.0005:
            continue
        if pe == "0000":
            own = None                    # unreadable period label
        elif kind == "G":
            own = sales.owner(cu, rep, pe, float(cv) if cv else None)
        elif kind == "D":
            own = sales.owner_deduction(cu, rep, pe)
        elif kind == "A":
            # a transfer can sit on a sheet that ends just before FieldRoutes'
            # sale date (900040): allow two weeks
            late = (date.fromisoformat(pe) + timedelta(days=14)).isoformat()
            own = (sales.owner(cu, rep, pe) or sales.owner_any_rep(cu, pe)
                   or sales.owner(cu, rep, late) or sales.owner_any_rep(cu, late))
        else:
            own = sales.owner(cu, rep, pe)
        if not own:
            legacy[(rep, kind)][0] += amt
            legacy[(rep, kind)][1] += 1
            continue
        for sid, w in own.items():
            hold[(sid, rep)][kind] += amt * w
            if kind == "G" and cv and float(cv) > EPS and pe >= cv_paid_at.get(sid, ""):
                cv_paid[sid], cv_paid_at[sid] = float(cv), pe

    # a deduction pinned to a sale the rep holds nothing on (its payment went
    # to a sibling sale of the same customer) moves onto that sibling, so a
    # later cancel never takes more than the rep holds on the customer
    by_cr: dict[tuple[str, str], list[str]] = defaultdict(list)
    for (sid, rep) in hold:
        by_cr[(sales.cust.get(sid, ""), rep)].append(sid)
    moved = 0
    for (cu, rep), sids in by_cr.items():
        if len(sids) < 2:
            continue
        for sid in sids:
            h = hold[(sid, rep)]
            if h["G"] + h["A"] > 0.005 or h["T"] + h["D"] > -0.005:
                continue
            sib = max((x for x in sids if x != sid),
                      key=lambda x: sum(hold[(x, rep)].values()))
            hs = hold[(sib, rep)]
            if sum(hs.values()) <= 0.005:
                continue
            take = h["T"] + h["D"]
            h["N"] -= take
            hs["N"] += take
            moved += 1

    held = {k: round(sum(v.values()), 6) for k, v in hold.items()}
    # paid = money went out on the sale; split transfers between reps net to
    # zero and are NOT a payment (900036: Hayden -7.17 / Riley +7.17 before
    # the commission itself was paid — it still pays when it is earned)
    paid_net = defaultdict(float)
    for (sid, rep), v in hold.items():
        paid_net[sid] += v["G"] + v["A"]
    manual_paid = {sid for sid, v in paid_net.items() if v > 0.005}

    # ---- the cutover snapshot: the manual's own view of FieldRoutes ----
    snap = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    snap_sub = {r[0]: r[1:] for r in snap.execute(
        "SELECT subscription_id, active, date_cancelled, cancel_reason, "
        "contract_value FROM subscriptions")}
    snap.close()
    upsell_inc_cut = defaultdict(float)
    for sid, inc in conn.execute(
            "SELECT subscription_id, COALESCE(acv_increase,0) FROM upsells "
            "WHERE created_at <= ? AND (removed_at IS NULL OR removed_at > ?)",
            (_cut()[2], _cut()[2])):
        upsell_inc_cut[sid] += float(inc)
    upd = dict(conn.execute("SELECT subscription_id, date_updated FROM subscriptions"))
    not_paid = defaultdict(list)
    for cu, rep, ds in notpaid_live:
        not_paid[(str(cu), rep)].append((ds or "")[:10])

    golive = s.go_live_period_start
    rows_sale, rows_base = [], []
    counts = defaultdict(int)
    with_rows = {sid for (sid, _r) in hold}
    disp_of: dict[str, str] = {}
    cv_cut_of: dict[str, float] = {}
    for f in facts:
        sid = f.subscription_id
        if f.state == "excluded_prestart":
            continue
        mp = sid in manual_paid
        changed = (upd.get(sid) or "") > _cut()[1] and sid in snap_sub
        fc = f
        if changed:
            # record touched after the cutover: its active/cancel facts at the
            # cutover come from the snapshot (a reinstatement or a late-entered
            # cancel is a post-cutover event)
            s_act, s_dc, s_reason, _s_cv = snap_sub[sid]
            if bool(s_act) != bool(f.active) or (s_dc or "") != (f.date_cancelled or ""):
                fc = SubFacts(sid, f.rep, f.contract_value, f.source, f.card_on_file,
                              s_act, s_dc, s_reason)
                fc.first_paid_date, fc.vested_date = f.first_paid_date, f.vested_date
                fc.cxl_fee_paid, fc.cxl_fee_paid_date = f.cxl_fee_paid, f.cxl_fee_paid_date
                r_ = (s_reason or "").strip().lower()
                fc.cancel_excluded = (r_ == "renewal" or r_.startswith("moved")) \
                    if f.active else f.cancel_excluded
        st = _state(fc, _cut()[1], mp, golive)
        earned = mp or bool(fc.first_paid_date and fc.first_paid_date <= _cut()[1])
        if st in ("claw", "half", "excluded") and not earned:
            st = "pending"     # SKEPTIC FIX A: never earned at cutover -> normal rules
        if st == "pending" and sid not in with_rows:
            continue                       # nothing happened yet: normal rules
        cv_raw = f.contract_value if not changed else float(snap_sub[sid][3] or 0)
        cv_cut = max(cv_raw - upsell_inc_cut.get(sid, 0.0), 0.0)
        if st == "pending":
            disp, note = "app", "not yet earned at cutover: normal rules from go-live"
        elif sid in settle:
            disp, note = "settle", "settled to the current rules in the first live period (the owner)"
        elif (st == "active" and not mp and f.rep
              and any(abs((date.fromisoformat(d) - date.fromisoformat(
                  (f.date_added or "")[:10])).days) <= 3
                  for d in not_paid.get((sales.cust.get(sid, ""), f.rep), [])
                  if d and f.date_added)):
            disp, note = "app", "on the manual Not-Paid list (owed once paid): normal rules"
        else:
            disp = "stand"
            note = ("manual paid" if mp else
                    "never paid by the manual books: stays at $0" if st == "active"
                    else f"{st} at cutover")
        counts[(disp, st)] += 1
        disp_of[sid] = disp
        cv_cut_of[sid] = cv_cut
        shares = _shares_at(conn, f, _cut()[2])
        rows_sale.append((sid, sales.cust.get(sid, ""), st, round(cv_cut, 2),
                          json.dumps(shares, sort_keys=True), int(mp), disp, note))
    # a sale not yet earned at the cutover carries only payments/transfers into
    # the live ledger; an old deduction on it stands in legacy (it must not be
    # refunded through the payment when the sale is finally earned)
    for (sid, rep), v in hold.items():
        if disp_of.get(sid) == "app":
            for k in ("T", "D", "N"):
                if abs(v[k]) >= 0.0005:
                    legacy[(rep, k)][0] += v[k]
                    legacy[(rep, k)][1] += 1
                    v[k] = 0.0
        # the rate this rep was paid on the sale (contract-value true-ups use it)
        basis = cv_paid.get(sid) or cv_cut_of.get(sid) or 0.0
        paid_rep = max(v["G"] + v["A"], 0.0)
        rate = round(paid_rep / basis, 6) if basis > EPS else None
        rows_base.append((sid, rep, round(v["G"], 6), round(v["A"], 6), round(v["T"], 6),
                          round(v["D"] + v["N"], 6), round(sum(v.values()), 6), rate))

    conn.execute("DELETE FROM cutover_sale")
    conn.execute("DELETE FROM cutover_baseline")
    conn.execute("DELETE FROM cutover_legacy")
    conn.executemany("""INSERT INTO cutover_sale(subscription_id, customer_id,
        state_cut, cv_cut, shares_cut, manual_paid, disposition, note, built_at)
        VALUES(?,?,?,?,?,?,?,?,datetime('now'))""", rows_sale)
    conn.executemany("""INSERT INTO cutover_baseline(subscription_id, rep, g, a,
        t, d, held, rate) VALUES(?,?,?,?,?,?,?,?)""", rows_base)
    conn.executemany("INSERT INTO cutover_legacy(rep, kind, amount, rows) VALUES(?,?,?,?)",
                     [(r, k, round(v[0], 6), v[1]) for (r, k), v in legacy.items()])
    conn.commit()
    return {"sales": len(rows_sale), "baseline_rows": len(rows_base),
            "legacy_rows": sum(v[1] for v in legacy.values()),
            "netted": moved, "by_disposition_state": {f"{d}/{st}": n for (d, st), n
                                                       in sorted(counts.items())}}


def match_upsells(conn: sqlite3.Connection) -> list[tuple[int, float, str]]:
    """(upsell id, amount the manual paid, note) for every upsell booked
    before the manual's last adjustments list. Matched to the manual
    adjustments by customer, rep and amount; unmatched ones stand at $0."""
    cust = dict(conn.execute("SELECT subscription_id, customer_id FROM subscriptions"))
    paid = defaultdict(list)
    for acct, rep, amt, pe in conn.execute(
            "SELECT account, rep, amount, period_end FROM manual_adjustments "
            "WHERE LOWER(COALESCE(note,'')) LIKE '%upsell%'"):
        paid[(str(acct), rep)].append([float(amt or 0), pe])
    out = []
    for uid, sid, rep, amt, created in conn.execute(
            "SELECT id, subscription_id, rep, amount, created_at FROM upsells "
            "WHERE created_at <= ? ORDER BY id", (_cut()[2],)):
        pool = paid.get((str(cust.get(sid, "")), rep), [])
        hit = next((p for p in pool if abs(p[0] - float(amt)) <= 0.02), None)
        if hit:
            pool.remove(hit)
            out.append((uid, round(hit[0], 2), f"paid by the manual sheet ({hit[1]})"))
        else:
            out.append((uid, 0.0, "not on any manual sheet: stands at $0 (pre-go-live)"))
    return out


def archive_ledger(conn: sqlite3.Connection, archive_as: str) -> None:
    """Rename the parallel-run ledger out of the way and recreate an empty
    ledger (with its indexes) from the schema."""
    from .store import SCHEMA
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if archive_as in have:
        raise RuntimeError(f"{archive_as} already exists: ledger already archived")
    conn.execute(f"ALTER TABLE ledger RENAME TO {archive_as}")
    for (idx,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (archive_as,)).fetchall():
        if not idx.startswith("sqlite_"):
            conn.execute(f"DROP INDEX {idx}")
    conn.commit()
    conn.executescript(SCHEMA)
    conn.commit()


def seed_rows(conn: sqlite3.Connection) -> dict:
    n = 0
    for sid, rep, g, a, t, d, held in conn.execute(
            "SELECT subscription_id, rep, g, a, t, d, held FROM cutover_baseline").fetchall():
        g, a, t = round(g, 2), round(a, 2), round(t, 2)
        d = round(round(held, 2) - g - a - t, 2)      # rows sum to the rounded holding
        for key, amt, what in (("G", g, "commission paid"), ("A", a, "adjustments"),
                               ("T", t, "CV true-downs"), ("D", d, "deductions")):
            if abs(amt) >= 0.005:
                conn.execute("""INSERT INTO ledger(subscription_id, rep, period_end,
                    amount, kind, note, ref) VALUES(?,?,?,?,'baseline',?,?)""",
                             (sid, rep, _cut()[0], round(amt, 2),
                              f"manual books through {_cut()[0]}: {what}",
                              f"baseline:{key}:{sid}:{rep}"))
                n += 1
    u = 0
    for uid, amt, note in match_upsells(conn):
        sid, rep = conn.execute("SELECT subscription_id, rep FROM upsells WHERE id=?",
                                (uid,)).fetchone()
        conn.execute("""INSERT INTO ledger(subscription_id, rep, period_end, amount,
            kind, note, ref) VALUES(?,?,?,?,'baseline',?,?)""",
                     (sid, rep, _cut()[0], amt, "upsell credit " + note, f"upsell:{uid}"))
        u += 1
    conn.commit()
    return {"baseline_ledger_rows": n, "upsell_markers": u}


# --------------------------------------------------------------- live close

def _holdings(conn: sqlite3.Connection, sid: str) -> dict[str, float]:
    return {r: float(v) for r, v in conn.execute(
        "SELECT rep, SUM(amount) FROM ledger WHERE subscription_id=? AND "
        "(ref IS NULL OR ref NOT LIKE 'upsell:%') AND kind != 'manual' "
        "GROUP BY rep", (sid,))}


def close_live_period(conn: sqlite3.Connection, s: Settings, facts: list[SubFacts],
                      period_start: str, period_end: str,
                      commit: bool = True) -> list[dict]:
    cutoff = period_end + " 23:59:59"
    golive = s.go_live_period_start
    stand = {r[0]: r for r in conn.execute(
        "SELECT subscription_id, state_cut, cv_cut, shares_cut, manual_paid "
        "FROM cutover_sale WHERE disposition='stand'")}
    base_d = defaultdict(float)
    base_a = defaultdict(float)
    base_gat = defaultdict(float)
    base_rate: dict[tuple[str, str], float | None] = {}
    base_reps: dict[str, set] = defaultdict(set)    # FIX P: no per-call scan
    for sid, rep, g, a, t, d, rate in conn.execute(
            "SELECT subscription_id, rep, g, a, t, d, rate FROM cutover_baseline"):
        base_d[(sid, rep)] = float(d or 0)
        base_a[(sid, rep)] = float(a or 0)
        base_gat[(sid, rep)] = float(g or 0) + float(a or 0) + float(t or 0)
        base_rate[(sid, rep)] = rate
        base_reps[sid].add(rep)
    conn.execute("DELETE FROM live_flags WHERE period_end=?", (period_end,))

    def flag(sid, note):
        if conn.execute("SELECT 1 FROM live_flags WHERE subscription_id=? AND note=? "
                        "AND period_end<? LIMIT 1", (sid, note, period_end)).fetchone():
            return                                  # already raised earlier
        conn.execute("INSERT INTO live_flags(period_end, subscription_id, note) "
                     "VALUES(?,?,?)", (period_end, sid, note))
        log.warning("review %s: %s", sid, note)

    def sale_rate(sid, hold, old_cv):
        """The rate the sale was paid at (sum of the reps' as-paid rates, at
        most 9%: a deduct-then-repay or a transfer never inflates it)."""
        rates = [base_rate.get((sid, r)) for r in set(hold) | base_reps[sid]
                 if base_rate.get((sid, r)) is not None]
        if rates and sum(rates) > EPS:
            return min(sum(rates), 0.09)
        held = sum(max(h, 0.0) for h in hold.values())
        return min(held / old_cv, 0.09) if old_cv > EPS else 0.0

    def trueup(sid, hold, old_cv, new_cv, why, st, comm_base=None, ev=None):
        """Contract-value change at the rate the sale was paid (§8b), shared by
        what each rep holds. The sale never moves past what is owed on the new
        value: a cut never takes it below rate x new value (a rep paid on a
        lower value than FieldRoutes showed keeps it), a raise never lifts it
        above (a past missed true-down is not topped up).
        comm_base (commercial sales, §3): the go-live contract value. Up to it
        the sale earns the as-paid rate; any value above it is new money and
        earns the commercial 5%."""
        pos = {r: h for r, h in hold.items() if h > 0.005}
        H = sum(pos.values())
        if H <= 0.005 or old_cv <= EPS:
            return
        # FIX A: the sale's holding counts a rep below zero down to what the
        # manual moved from them to a partner (split adjustment); a past
        # over-deduction does not count (it stands)
        Hs = H + sum(max(h, min(0.0, base_a[(sid, r)]))
                     for r, h in hold.items() if h < -0.005)
        rate = sale_rate(sid, hold, old_cv)

        def earns(cv_):
            if comm_base is None:
                return rate * cv_
            return (rate * min(cv_, comm_base)
                    + s.rate_commercial * max(cv_ - comm_base, 0.0))
        owed = earns(new_cv)
        delta = earns(new_cv) - earns(old_cv)
        if new_cv < old_cv:
            new_h = max(Hs + delta, min(Hs, owed))
        else:
            new_h = min(Hs + delta, max(Hs, owed))
        line = max(new_h - Hs, -H)
        how = f"at the rate the sale was paid ({rate:.2%})"
        if comm_base is not None and max(old_cv, new_cv) > comm_base + EPS:
            how += (f"; commercial: value above ${comm_base:,.2f} at "
                    f"{s.rate_commercial:.0%}")
        for rep, h in pos.items():
            book(sid, rep, line * h / H, "trueup",
                 f"contract value {old_cv:,.2f} -> {new_cv:,.2f} after go-live, "
                 f"{how} ({why})", st, ev)

    def _live(sid, rep, kinds):
        return float(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM ledger WHERE subscription_id=? AND rep=? "
            "AND kind IN (%s)" % ",".join("?" * len(kinds)), (sid, rep, *kinds)).fetchone()[0])

    TAKEN = ("clawback", "halfkeep", "restore", "feeback", "feeundo")

    def _sale_live(sid, kinds):
        out = {}
        for rep, v in conn.execute(
                "SELECT rep, COALESCE(SUM(amount),0) FROM ledger WHERE subscription_id=? "
                "AND kind IN (%s) GROUP BY rep" % ",".join("?" * len(kinds)),
                (sid, *kinds)):
            out[rep] = float(v)
        return out

    def _since_active(sid, kinds):
        """FIX C: live lines of these kinds booked since the sale was last
        active (= this cancellation only; an earlier cycle was settled by its
        reinstatement), per rep."""
        last = conn.execute(
            "SELECT COALESCE(MAX(period_end),'') FROM live_state WHERE subscription_id=? "
            "AND state='active' AND period_end<?", (sid, period_end)).fetchone()[0]
        out = {}
        for rep, v in conn.execute(
                "SELECT rep, COALESCE(SUM(amount),0) FROM ledger WHERE subscription_id=? "
                "AND period_end>? AND kind IN (%s) GROUP BY rep" % ",".join("?" * len(kinds)),
                (sid, last, *kinds)):
            out[rep] = float(v)
        return out

    def _restores(sid, reps, st_cut, frac, cv_now=None, shares=None):
        """SKEPTIC FIX B (sale level): what a reinstatement / late fee gives
        back. The manual books' deduction counts only when the sale was
        already cancelled at the cutover, never beyond the sale's commission
        (claw) or half of it (half): past over-deductions stand. The app's own
        deductions always count. A late fee gives back half of it, at most
        half of the commission on today's contract value. Shared by each rep's
        part of what was taken, or by a split booked since."""
        reps = set(reps) | base_reps[sid]
        # FIX D5: the cap on the manual deduction is the commission AT CUTOVER;
        # live true-ups are already inside the live claw-backs
        B = sum(base_gat.get((sid, r), 0.0) for r in reps)
        man = {r: max(-base_d[(sid, r)], 0.0) if st_cut in ("claw", "half") else 0.0
               for r in reps}
        man_tot = sum(man.values())
        cap = max(B, 0.0) * (1.0 if st_cut == "claw" else 0.5)
        scale = min(1.0, cap / man_tot) if man_tot > 0.005 else 0.0
        live = _sale_live(sid, TAKEN)
        took = {r: man[r] * scale - live.get(r, 0.0) for r in reps | set(live)}
        total = max(0.0, sum(took.values()))
        if cv_now is not None:
            hold_now = _holdings(conn, sid)
            total = min(total, sale_rate(sid, hold_now, cv_now or 1.0) * (cv_now or 0.0)) \
                if cv_now else total
        total *= frac
        if total < 0.005:
            return {}
        if shares:
            ssum = sum(shares.values()) or 1.0
            return {r: total * v / ssum for r, v in shares.items()}
        # FIX B: share by each rep's part of THIS cancellation's deductions;
        # the cumulative 'took' (incl. the manual deduction) only when the
        # app has taken nothing since the sale was last active
        cur = {r: -v for r, v in _since_active(sid, TAKEN).items() if -v > 0.005}
        posd = cur or {r: v for r, v in took.items() if v > 0.005}
        tsum = sum(posd.values())
        return {r: total * v / tsum for r, v in posd.items()} if tsum > 0.005 else {}

    prev = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        """SELECT subscription_id, state, cv, shares FROM live_state ls
           WHERE period_end = (SELECT MAX(period_end) FROM live_state
                               WHERE subscription_id = ls.subscription_id
                                 AND period_end < ?)""", (period_end,))}
    emitted: list[dict] = []
    states = []

    roster = set(s.isa_roster)

    def book(sid, rep, amount, kind, note, st, ev=None):
        # ev: the FR-dated event the line settles (RULES §7 weekly split)
        amount = _cents(amount)
        if abs(amount) < 0.01 or rep not in roster:
            return
        conn.execute("""INSERT INTO ledger(subscription_id, rep, period_end, amount,
            kind, note, event_date) VALUES(?,?,?,?,?,?,?)""",
                     (sid, rep, period_end, amount, kind, note, ev or None))
        emitted.append({"subscription_id": sid, "rep": rep, "amount": amount,
                        "kind": kind, "state": st, "flags": []})

    others = []
    for f in facts:
        sid = f.subscription_id
        if sid not in stand:
            if (not f.active and f.cancel_excluded and f.first_paid_date
                    and f.date_cancelled and f.first_paid_date > f.date_cancelled
                    and (f.date_cancelled or "") <= _cut()[1]
                    and not conn.execute(
                        "SELECT 1 FROM ledger WHERE subscription_id=? AND kind NOT IN "
                        "('baseline','vest','freeze') AND (ref IS NULL OR ref NOT LIKE "
                        "'upsell:%') LIMIT 1", (sid,)).fetchone()):    # FIX D7
                # §5 / owner 2026-10-15: excluded (Renewal) cancel before
                # go-live, first invoice paid only afterwards — never paid, so
                # never deducted either
                continue
            others.append(f)
            continue
        _sid, st_cut, cv_cut, shares_cut, mp = stand[sid]
        p_state, p_cv, p_shares = prev.get(sid, (st_cut, cv_cut, shares_cut))
        p_shares = json.loads(p_shares or "{}")
        # §3 commercial (owner, 2026-10-20): what the manual paid keeps its
        # rate; value above the go-live contract value is paid at 5%
        comm_base = None
        if f.commercial:
            comm_base = float(cv_cut) if (cv_cut or 0) > EPS else float(p_cv or 0)
        st = _state(f, cutoff, bool(mp), golive)
        cv = f.cv_effective
        if cv < 1.0 and p_cv and p_cv >= 1.0:
            # FieldRoutes shows $0: never true the commission down to nothing
            # (and never lose it for good) on that — keep the last value; a
            # live sale showing $0 goes on the Review tab
            if st == "active":
                flag(sid, f"contract value shows ${cv:,.2f} (was ${p_cv:,.2f}): "
                          "left as it was, not trued down")
            cv = p_cv
        if p_cv and cv > p_cv + 0.005:
            # §6 (CFO 2026-11-09): a raise never pays on its own — only a
            # submitted upsell does. The sale keeps resting on its last value
            # (so a later cut is still measured from there).
            if p_state == "active" and st in ("active", "vested") and cv - p_cv >= 0.5:
                flag(sid, f"contract value went up ${p_cv:,.2f} -> ${cv:,.2f} with no "
                          "upsell submitted: not paid")
            cv = p_cv
        # only a BOOKED split change moves money; a FieldRoutes soldBy rewrite
        # on a sale the manual books settled does not
        booked_now = _assigned(conn, sid, None)
        if booked_now:
            shares = booked_now
        elif _assigned(conn, sid, _cut()[2]) and f.rep:
            shares = {f.rep: 1.0}           # the split was removed after go-live
        else:
            # SKEPTIC FIX F: no booked split now -> the cutover owner
            shares = json.loads(shares_cut or "{}") or p_shares
        hold = _holdings(conn, sid)
        if not booked_now and st == "active":
            # §1 (owner, 2026-11-03): the credit follows PestRoutes' soldBy —
            # but a sale the manual books paid never moves on its own
            owners = set(json.loads(shares_cut or "{}") or p_shares)
            if f.rep is None and f.rep_prior:
                flag(sid, f"PestRoutes soldBy moved off the roster after go-live (was "
                          f"{f.rep_prior}): the manual books paid this sale, so nothing moves "
                          "on its own — remove the credit by hand if it should go")
            elif f.rep and owners and f.rep not in owners:
                flag(sid, f"PestRoutes soldBy is now {f.rep}, but the manual books paid "
                          f"{', '.join(sorted(owners))}: nothing moves on its own — book a "
                          "reassignment if the credit should move")
        why = f"pre-go-live sale (manual books); {p_state} -> {st}"
        if st == "pending":
            st = p_state                    # a reversed payment never re-opens it
        # SKEPTIC FIX C: a CV change is trued up before any transition out of
        # active (final true-up then freeze / halve), never dropped
        cv_basis = p_cv
        if (p_state == "active" and st in ("vested", "half") and p_cv
                and abs(cv - p_cv) >= 0.5):
            trueup(sid, hold, p_cv, cv, why, st, comm_base, f.date_updated)
            hold = _holdings(conn, sid)
            cv_basis = cv
        if (st in ("claw", "half") and p_state == "excluded"
                and (f.date_cancelled or "") <= _cut()[1]):
            # the cancellation itself is from before go-live (its reason or the
            # re-sell picture changed later): the past stands — review by hand
            flag(sid, f"pre-go-live cancel {(f.date_cancelled or '')[:10]} is no longer "
                      f"excluded ({f.cancel_reason or 'no reason'}); not deducted")
            st = p_state
        if st != p_state:
            new_split = (booked_now and booked_now != p_shares
                         and all(r in roster for r in booked_now))
            if st == "claw" and p_state == "half":
                fb = {r: v for r, v in _since_active(sid, ("feeback",)).items() if v > 0.005}
                if fb:
                    # fee refunded after a late fee gave half back: undo exactly that
                    for rep, v in fb.items():
                        book(sid, rep, -v, "feeback",
                             f"cancellation fee no longer paid: half given back is "
                             f"taken again ({why})", st, f.date_updated)
                else:
                    # SKEPTIC FIX K: fee refunded -> take the other half only
                    for rep, h in hold.items():
                        b_ = base_gat[(sid, rep)] + _live(sid, rep, ("trueup", "transfer"))
                        book(sid, rep, -min(max(h, 0.0), 0.5 * max(b_, 0.0)), "feeundo",
                             f"cancellation fee no longer paid: the other half ({why})", st,
                             f.date_updated)
            elif st == "claw" and p_state != "vested":
                for rep, h in hold.items():
                    book(sid, rep, -max(h, 0.0), "clawback",
                         f"cancelled {(f.date_cancelled or '')[:10]} "
                         f"({f.cancel_reason or 'no reason'}) after go-live: deduct what the "
                         f"rep holds ({why})", st, f.date_cancelled)
            elif st == "half" and p_state in ("active", "excluded", "pending"):
                for rep, h in hold.items():
                    book(sid, rep, -0.5 * max(h, 0.0), "halfkeep",
                         f"cancelled {(f.date_cancelled or '')[:10]}, cancellation fee "
                         f"paid: rep keeps half of what they hold ({why})", st,
                         max(f.date_cancelled or "", f.cxl_fee_paid_date or "") or None)
            elif st == "half" and p_state == "claw":
                fu = {r: v for r, v in _since_active(sid, ("feeundo",)).items() if v < -0.005}
                if fu:
                    # fee paid again after a refund took the other half: undo that
                    for rep, v in fu.items():
                        book(sid, rep, -v, "feeundo",
                             f"cancellation fee paid again: the other half given back "
                             f"({why})", st, f.cxl_fee_paid_date)
                elif not (st_cut == "half" and not _sale_live(sid, ("clawback",))):
                    for rep, amt in _restores(sid, list(hold), st_cut, 0.5, cv_now=cv,
                                              shares=None).items():
                        book(sid, rep, amt, "feeback",
                             f"cancellation fee paid {(f.cxl_fee_paid_date or '')[:10]} after "
                             f"the full deduction: half back ({why})", st, f.cxl_fee_paid_date)
            elif st == "active" and (p_state in ("claw", "half") or (
                    p_state == "excluded" and st_cut in ("claw", "half"))):
                # FIX E: a cutover deduction whose reason was edited to an
                # excluded one meanwhile is still given back on reinstatement
                for rep, amt in _restores(sid, list(hold), st_cut, 1.0,
                                          shares=booked_now if new_split else None).items():
                    book(sid, rep, amt, "restore",
                         f"reinstated after go-live: deduction given back ({why})", st,
                         f.date_updated)
                # FIX D1: p_shares is NOT replaced here — the transfer below
                # still re-divides what the reps hold (moves nothing on R7)
            elif st == "excluded" and p_state in ("claw", "half"):
                # the reason was changed to an excluded one (Renewal, Moved...):
                # the deduction is given back, a cutover deduction included
                # (owner, 2026-10-15), never more than the commission
                for rep, amt in _restores(sid, list(hold), st_cut, 1.0).items():
                    book(sid, rep, amt, "restore",
                         f"cancel reason now '{f.cancel_reason}': deduction given back ({why})", st,
                         f.date_updated)
            # vested / excluded / active<-excluded: no money moves
        elif st == "active" and p_cv and abs(cv - p_cv) >= 0.5:
            trueup(sid, hold, p_cv, cv, why, st, comm_base, f.date_updated)
        if (st == "active" and p_state != "active" and p_cv
                and abs(cv - p_cv) >= 0.5):
            # SKEPTIC FIX C: back to active with a CV that moved meanwhile
            trueup(sid, _holdings(conn, sid), p_cv, cv, why, st, comm_base, f.date_updated)
        if st == "active" and shares != p_shares and p_shares:
            hold = _holdings(conn, sid)
            off = [r for r in set(hold) | set(shares) | set(p_shares)
                   if r not in roster and (abs(hold.get(r, 0.0)) > 0.005
                                           or shares.get(r) or p_shares.get(r))]
            if off:
                # SKEPTIC FIX D: an off-payroll holder cannot be debited — never
                # pay a roster rep for it; review by hand
                # FIX D-flag: the note names the change, so a later different
                # change on the same sale is listed again
                flag(sid, f"split change {json.dumps(p_shares, sort_keys=True)} -> "
                          f"{json.dumps(shares, sort_keys=True)} involves someone off the ISA "
                          f"roster ({', '.join(sorted(off))}): not transferred automatically")
            else:
                # SKEPTIC FIX E: zero-sum; negative balances (past) stand
                total = sum(max(h, 0.0) for h in hold.values())
                share_sum = sum(shares.values()) or 1.0
                for rep in set(hold) | set(shares):
                    target = (shares.get(rep, 0.0) / share_sum * total
                              + min(hold.get(rep, 0.0), 0.0))
                    book(sid, rep, target - hold.get(rep, 0.0), "transfer",
                         f"split changed after go-live: {p_shares} -> {shares} ({why})", st,
                         f.assign_changed or f.date_updated)
        if st == "active" or cv_basis == cv:
            cv_basis = cv
        # SKEPTIC FIX G: a split booked while the sale is not active is
        # applied (transferred) when it is active again, so keep the old one
        stored = shares if st == "active" else p_shares
        states.append((period_end, sid, st, round(cv_basis, 2), json.dumps(stored, sort_keys=True)))

    seen = {f.subscription_id for f in facts}
    for sid in stand:
        if sid not in seen:
            hold = {r: h for r, h in _holdings(conn, sid).items() if abs(h) > 0.005}
            if any(r in roster for r in hold):
                flag(sid, "pre-go-live sale no longer in the ISA data (its assignment was "
                          f"removed or the seller changed); reps hold {hold} — review by hand")
    conn.executemany("INSERT OR REPLACE INTO live_state(period_end, subscription_id, "
                     "state, cv, shares) VALUES(?,?,?,?,?)", states)
    if commit:
        conn.commit()
    emitted += close_period(conn, s, others, period_start, period_end, live=True,
                            commit=commit)
    return emitted


def reconcile(conn: sqlite3.Connection) -> list[tuple[str, float, float, float]]:
    """Per rep: manual books net vs baseline + legacy (must tie to the cent)."""
    manual = defaultdict(float)
    for kind, _cu, rep, pe, amt, _cv in _manual_rows(conn):
        if abs(amt) >= 0.0005:
            manual[rep] += amt
    seeded = defaultdict(float)
    for rep, v in conn.execute("SELECT rep, SUM(held) FROM cutover_baseline GROUP BY rep"):
        seeded[rep] += float(v)
    for rep, v in conn.execute("SELECT rep, SUM(amount) FROM cutover_legacy GROUP BY rep"):
        seeded[rep] += float(v)
    return [(rep, round(manual[rep], 2), round(seeded[rep], 2),
             round(seeded[rep] - manual[rep], 2)) for rep in sorted(set(manual) | set(seeded))]
