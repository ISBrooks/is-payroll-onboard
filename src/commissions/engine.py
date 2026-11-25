"""The commission engine — a direct transcription of docs/COMMISSION_RULES.md.

Two passes:
  compute_status()  derives per-subscription facts (trigger date, qualifying
                    services, dollar coverage, rate, expected, vesting) into
                    sub_status. Pure derivation — no money moves.
  close_period()    appends ledger rows so that, for events dated on or
                    before the period end, each subscription's ledger sum
                    equals its expected commission (RULES §6). The ledger is
                    the authoritative money record.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .config import Settings, is_commercial, payees, rep_start_dates
from .periods import local_from_utc
from .store import active_shares

log = logging.getLogger(__name__)
EPS = 0.01
# RULES §5: a Cancellation Charge belongs to the cancellation invoiced between
# 30 days before and 60 days after it; an ad-hoc fee ticket within 14 days
CXL_FEE_WINDOW = (-30, 60)
ADHOC_FEE_WINDOW = (-14, 14)


def _day(x: str | None) -> date | None:
    try:
        return date.fromisoformat(str(x)[:10]) if x else None
    except ValueError:
        return None


def _cents(x: float) -> float:
    """Round half away from zero, like the payroll sheets: 40.005 -> 40.01
    (float round() gives 40.0 because 40.005 is stored as 40.00499...)."""
    return float(Decimal(repr(round(x, 6))).quantize(Decimal("0.01"), ROUND_HALF_UP))


# RULES §4 (CFO 2026-11-09): a service done up to this many days before the
# sold date is data-entry lag and counts; anything older belongs to the
# subscription's earlier life (a re-sold subscription restarts its count).
# Observed lag on real new sales is <= 14 days; re-sales are months or years.
RESOLD_LAG_DAYS = 14
_CV_NOTE = re.compile(r"cv=\$([\d,]+(?:\.\d+)?)")


def _paid_cv(conn: sqlite3.Connection, sid: str, period_end: str) -> float | None:
    """The contract value a live sale was last paid or trued on, read from its
    latest statement line before this period (sales paid before cv_basis
    existed). None = never paid by the app."""
    row = conn.execute(
        "SELECT note FROM ledger WHERE subscription_id=? AND period_end<? AND "
        "kind IN ('initial','repay','trueup') AND note LIKE 'state=%' "
        "ORDER BY id DESC LIMIT 1", (sid, period_end)).fetchone()
    m = _CV_NOTE.search(row[0]) if row and row[0] else None
    return float(m.group(1).replace(",", "")) if m else None


def _review(conn: sqlite3.Connection, period_end: str, sid: str, note: str) -> None:
    """Put a line on the statement's Review tab (once per sale and note)."""
    try:
        if conn.execute("SELECT 1 FROM live_flags WHERE subscription_id=? AND note=? "
                        "AND period_end<? LIMIT 1", (sid, note, period_end)).fetchone():
            return
        conn.execute("INSERT INTO live_flags(period_end, subscription_id, note) "
                     "VALUES(?,?,?)", (period_end, sid, note))
    except sqlite3.OperationalError:
        pass


def _event_date(f: "SubFacts", kind: str, claw_state: bool, fee_ok: bool) -> str | None:
    """RULES §7 weekly split: the FieldRoutes-dated event a ledger line
    settles, so the statement can place it in week 1 or week 2 of the
    period. initial: the first paid service; repay: that, or the later
    reinstatement that re-earned it; clawback: the cancellation, or the
    change that took the credit away (soldBy, split); trueup: the paid
    cancellation fee (half-keep) or the change to the subscription
    (contract value, rate, split). The amounts never depend on this."""
    changed = max((x for x in (f.date_updated, f.assign_changed) if x), default=None)
    if kind == "initial":
        return f.first_paid_date
    if kind == "repay":
        return max((x for x in (f.first_paid_date, changed) if x), default=None)
    if kind == "clawback":
        return f.date_cancelled if claw_state else changed
    if kind == "trueup" and claw_state and fee_ok and f.cxl_fee_paid_date:
        return max(f.cxl_fee_paid_date, f.date_cancelled or "")
    return changed


def _plabel_end(label: str) -> str:
    """'08/30/26 - 09/12/26' -> '2026-09-12' (unparseable -> '0000')."""
    try:
        m, d, y = label.split("-")[-1].strip().split("/")
        return f"20{int(y):02d}-{int(m):02d}-{int(d):02d}"
    except (ValueError, IndexError):
        return "0000"


@dataclass
class SubFacts:
    subscription_id: str
    rep: str | None                          # soldBy-derived; None on rescues
    contract_value: float
    source: str
    card_on_file: int
    active: int
    date_cancelled: str | None
    cancel_reason: str | None
    cv_effective: float = 0.0                # CV minus upsell increments (§12)
    first_service_date: str | None = None    # completion of 1st qualifying svc
    first_paid_date: str | None = None       # RULES §2 trigger (later of the two)
    qualifying_completed: int = 0
    coverage_paid: float = 0.0
    coverage_target: float = 0.0
    vested_date: str | None = None
    rate: float = 0.0
    expected: float = 0.0
    cxl_fee_paid: bool = False               # THIS cancel's fee paid (§5)
    cxl_fee_paid_date: str | None = None     # when it was paid (event date)
    date_added: str | None = None            # sold date (§8 per-sale history)
    cancel_excluded: bool = False            # renewal/moved/re-signed (§5)
    commercial: bool = False                 # §3 priority 0: always 5%
    rep_prior: str | None = None             # ISA whose credit soldBy took away
    restarted: bool = False                  # services before the sold date dropped
    date_updated: str | None = None          # FR dateUpdated: the sub last changed (§7)
    assign_changed: str | None = None        # last booked split change, office time (§7)
    state: str = "not_started"
    flags: list[str] = field(default_factory=list)


def _rate(s: Settings, card_on_file: int, source: str | None,
          one_time: bool = False, mosq_upsell: bool = False,
          commercial: bool = False) -> float:
    if commercial:
        # owner 2026-10-20: commercial accounts always pay 5%, card or no
        # card — ahead of every other rule
        return s.rate_commercial                 # §3 priority 0
    if not card_on_file:
        return s.rate_no_payment_method          # §3 priority 1
    src = (source or "").strip().lower()
    if src in s.alt_rate_sources or "renewal" in src:
        # owner 2026-10-14: ANY renewal source an ISA sells pays 5%
        # (Renewal - In/Out of Contract, ZOLD - Renewal, ...)
        return s.rate_alt_source                 # §3 priority 2
    if one_time or mosq_upsell:
        # CFO ruling 2026-10-05: one-time Initial Service jobs pay 5%, and
        # 'Upsell'-sourced 8/12-month mosquito subscriptions pay 5%
        return s.rate_alt_source
    return s.rate_standard                       # §3 priority 3


class _FeeIndex:
    """RULES §5: which cancellation fee belongs to which cancellation.

    FieldRoutes fee tickets carry no subscription/appointment link
    (subscriptionID 0), so the link is by date on the same customer: the fee
    is invoiced on/after the sale date, within the window of the cancel, and
    no OTHER subscription on the account (ISA or not — customer_subs) was
    cancelled closer to the invoice. Paid = balance $0, no write-off, and no
    coupon dollars (a couponed fee is a waived fee)."""

    def __init__(self, conn: sqlite3.Connection):
        cxl = {r[0] for r in conn.execute(
            "SELECT type_id FROM service_types WHERE description LIKE '%ancellation%'")}
        self.tix: dict[str, list] = defaultdict(list)
        for cu, inv, amt, bal, paid, cpn, wo, svc, appt, autogen in conn.execute("""
                SELECT customer_id, invoice_date,
                       COALESCE(NULLIF(sub_total, 0), total), balance, paid_date,
                       COALESCE(coupon_paid, 0), COALESCE(writeoff_tainted, 0),
                       service_id, COALESCE(appointment_id, '0'), autogen_sub_id
                FROM tickets WHERE active = 1 AND total > 0.01
                  AND invoice_date IS NOT NULL"""):
            is_cxl = svc in cxl
            if not is_cxl and not (appt == "0" and autogen is None):
                continue
            self.tix[str(cu)].append((_day(inv), amt or 0.0, bal, paid, cpn, wo,
                                      "cxl" if is_cxl else "adhoc"))
        self.cancels: dict[str, list] = defaultdict(list)
        for sid, cu, dc in conn.execute("""
                SELECT subscription_id, customer_id, date_cancelled FROM subscriptions
                WHERE active = 0 AND date_cancelled IS NOT NULL
                UNION
                SELECT subscription_id, customer_id, date_cancelled FROM customer_subs
                WHERE active = 0 AND date_cancelled IS NOT NULL"""):
            d = _day(dc)
            if d:
                self.cancels[str(cu)].append((str(sid), d))
        self.recap = {str(sid): disc for sid, disc in conn.execute(
            "SELECT subscription_id, initial_discount FROM subscriptions "
            "WHERE initial_discount IS NOT NULL")}

    def _owns(self, cu: str, sid: str, inv: date, dc: date, amt: float) -> bool:
        """This cancellation is the one closest to the invoice. On a tie
        (cancelled the same day), the fee belongs to the cancellation whose
        initial-discount recapture it matches — one fee never half-protects
        two reps (900026: Lennon's $291 recapture vs Rowan's $0-discount
        mosquito); if neither or both match, every tied one keeps it."""
        mine = abs((inv - dc).days)
        ties = []
        for s2, d2 in self.cancels.get(cu, []):
            if s2 == sid:
                continue
            dist = abs((inv - d2).days)
            if dist < mine:
                return False
            if dist == mine:
                ties.append(s2)
        if not ties:
            return True
        match = lambda s: s in self.recap and abs((self.recap[s] or 0) - amt) <= 1.0  # noqa: E731
        if match(sid):
            return True
        return not any(match(s2) for s2 in ties)

    def paid_date(self, sid: str, cu: str, date_added: str | None,
                  date_cancelled: str | None, post_golive: bool,
                  adhoc_amounts: list[float]) -> tuple[str | None, str | None]:
        """(paid date, route) for THIS cancellation's fee. route is
        'cxl-ticket' / 'adhoc-ticket' when paid, 'billed' when a fee was
        billed for this cancel but is unpaid or waived, else None."""
        cu = str(cu)
        dc, da = _day(date_cancelled), _day(date_added)
        if dc is None:
            return None, None
        best, billed = None, False
        for inv, amt, bal, paid, cpn, wo, kind in self.tix.get(cu, []):
            if inv is None or (da and inv < da):
                continue                     # an older cancellation's fee
            lo, hi = CXL_FEE_WINDOW if kind == "cxl" else ADHOC_FEE_WINDOW
            if not lo <= (inv - dc).days <= hi:
                continue
            if not self._owns(cu, sid, inv, dc, amt):
                continue                     # belongs to another cancellation
            if kind == "adhoc":
                # an untyped / plan-type ticket is a fee only when it bills
                # the initial-discount recapture amount (post-go-live only)
                if not post_golive or not any(abs(amt - a) <= 1.0
                                              for a in adhoc_amounts):
                    continue
            else:
                billed = True
            if bal is None or abs(bal) >= EPS or wo or cpn >= EPS or not paid:
                continue                     # unpaid / written off / couponed
            if best is None or paid < best[0]:
                best = (paid, kind)
        if best:
            return best[0], ("cxl-ticket" if best[1] == "cxl" else "adhoc-ticket")
        return None, ("billed" if billed else None)


def prestart_subs(conn: sqlite3.Connection, s: Settings,
                  shares: dict[str, list[tuple[str, float, str]]]) -> dict[str, str]:
    """RULES §1a: {subscription_id: start date} for subs sold before the
    soldBy rep's inside-sales start date (e.g. Indigo Vale's retention-era
    renewals). Kept as normal accounts: a sub an assignment credits to
    someone else, and a pre-start sale the manual process actually paid
    (a manual payment dated on/after that sale, no later sale to the same
    customer by the rep)."""
    starts = rep_start_dates(s)
    if not starts:
        return {}
    reps = list(starts)
    ph = ",".join("?" * len(reps))
    rows = conn.execute(
        f"SELECT subscription_id, customer_id, rep, COALESCE(date_added,'') "
        f"FROM subscriptions WHERE rep IN ({ph})", reps).fetchall()
    # which sale each manual payment (paid sheet or adjustments sheet) was
    # for: the rep's latest sale to that customer dated on/before it
    sales: dict[tuple[str, str], list] = defaultdict(list)
    for sid, cu, rep, da in rows:
        sales[(str(cu), rep)].append((da[:10], sid))
    for v in sales.values():
        v.sort()
    paid_sids: set[str] = set()
    pays: list = []
    try:
        pays += [(cu, rep, _plabel_end(per)) for cu, rep, per in conn.execute(
            f"SELECT customer_id, rep, period FROM manual_paid WHERE rep IN ({ph})",
            reps)]
        pays += [(cu, rep, pe) for cu, rep, pe in conn.execute(
            f"SELECT account, rep, period_end FROM manual_adjustments WHERE rep IN ({ph}) "
            f"AND amount > 0 AND LOWER(COALESCE(note,'')) NOT LIKE '%upsell%'", reps)]
    except sqlite3.OperationalError:
        pass
    for cu, rep, pe in pays:
        cand = [sid for d, sid in sales.get((str(cu), rep), []) if d <= pe]
        if cand:
            paid_sids.add(cand[-1])
    out: dict[str, str] = {}
    for sid, cu, rep, da in rows:
        start = starts[rep]
        if not da or da[:10] >= start:
            continue
        if any(r_ != rep for r_, _sh, _t in shares.get(sid, [])):
            continue                 # an assignment credits someone else (§11)
        if sid in paid_sids:
            continue                 # the manual process paid THIS sale
        out[sid] = start
    return out


def _per_service_value(recurring_charge: float, frequency: int | None,
                       billing_frequency: int | None) -> float:
    """RULES §4b / DISCOVERY billing-frequency semantics."""
    rc = recurring_charge or 0.0
    if billing_frequency and billing_frequency > 0 and frequency and frequency > 0:
        return rc * frequency / billing_frequency
    return rc


def compute_status(conn: sqlite3.Connection, s: Settings) -> list[SubFacts]:
    initial_type_ids = {r[0] for r in conn.execute(
        "SELECT type_id FROM service_types WHERE is_initial=1")}
    # §4: recurring PLAN service types — any type some subscription uses as
    # its plan, minus reservices, general services, inspections and fees.
    # A sub whose plan changed (e.g. Bimonthly -> Quarterly) keeps every
    # service done under the earlier plan (owner, 2026-10-14).
    plan_type_ids = {r[0] for r in conn.execute("""
        SELECT DISTINCT y.type_id FROM service_types y
        JOIN subscriptions s ON s.service_id = y.type_id
        WHERE y.reservice = 0 AND y.is_initial = 0
          AND y.description NOT IN ('General Service', 'Inspection',
                                    'Service Fee', 'Initial Service')""")}
    shares = active_shares(conn)
    # §7 weekly split: when each sub's booked split last changed (the table
    # stamps UTC) — dates the ledger lines a split change produces
    assign_changed: dict[str, str | None] = {}
    for sid_, c_, r_ in conn.execute(
            "SELECT subscription_id, MAX(created_at), MAX(COALESCE(removed_at, '')) "
            "FROM assignments GROUP BY subscription_id"):
        assign_changed[sid_] = local_from_utc(max(c_ or "", r_ or "") or None)
    subs = conn.execute("""
        SELECT s.subscription_id, s.rep, s.contract_value, s.source, s.service_id,
               s.frequency, s.billing_frequency, s.recurring_charge, s.active,
               s.date_cancelled, s.cancel_reason, s.initial_appointment_id,
               COALESCE(c.card_on_file, 0), s.date_added, s.service_type,
               s.customer_id, s.initial_quote, s.initial_discount,
               s.soldby_rep_now, COALESCE(c.commercial, 0), s.rep_prior, s.sold_by,
               s.date_updated
        FROM subscriptions s
        LEFT JOIN customers c ON c.customer_id = s.customer_id
        WHERE s.rep IS NOT NULL OR s.subscription_id IN
              (SELECT subscription_id FROM assignments WHERE removed_at IS NULL)
           OR s.rep_prior IN (%s)
        """ % ",".join("?" * len(s.isa_roster)), list(s.isa_roster)).fetchall()
    prestart = prestart_subs(conn, s, shares)          # §1a
    managers = set(s.managers)                           # §14 flat 5%
    # §5 Bad-Debt-Renewal re-sell rule (CFO 2026-10-05): per customer, the
    # newer 'Bad Debt Renewal' subs and their reps, to catch exclusions on
    # the older sub that must flip to a deduction when ANOTHER agent is paid.
    # A pre-start sale is not an ISA sale (§1a) and never triggers it.
    bd_renewals: dict[str, list[tuple[str, str | None, str]]] = {}
    for sid_, cust_, rep_, added_ in conn.execute(
            "SELECT subscription_id, customer_id, COALESCE(rep, rep_off_roster), "
            "COALESCE(date_added,'') "
            "FROM subscriptions WHERE LOWER(TRIM(COALESCE(source,''))) = "
            "'bad debt renewal'"):
        if sid_ not in prestart:
            bd_renewals.setdefault(cust_, []).append((sid_, rep_, added_))
    # §5 "a different agent IS PAID on the re-sell": the BD subs are computed
    # first so their trigger is known when the older subs are judged
    bd_sids = {x[0] for lst in bd_renewals.values() for x in lst}
    bd_paid: set[str] = set()
    # a re-sell sold by someone since REMOVED from the roster is not computed
    # any more (its rep tag was cleared): its last computed trigger still
    # counts, so removing that rep never changes the original rep's money
    in_run = {r[0] for r in subs}
    off = [x for x in bd_sids if x not in in_run]
    for i in range(0, len(off), 900):
        chunk = off[i:i + 900]
        bd_paid |= {r[0] for r in conn.execute(
            "SELECT subscription_id FROM sub_status WHERE first_paid_date IS NOT NULL "
            "AND subscription_id IN (%s)" % ",".join("?" * len(chunk)), chunk)}
    subs.sort(key=lambda r: r[0] not in bd_sids)
    # §4 vesting is permanent: the first vest date the engine ever saw, plus
    # 'vested' freezes from earlier ledger closes (they seed the lock)
    try:
        vest_locks = dict(conn.execute(
            "SELECT subscription_id, vested_date FROM vest_lock"))
    except sqlite3.OperationalError:
        vest_locks = None                    # pre-migration DB: no locking
    ledger_vested = dict(conn.execute(
        "SELECT subscription_id, MIN(period_end) FROM ledger "
        "WHERE kind='vest' AND note='vested' GROUP BY subscription_id"))
    new_locks: list[tuple[str, str, str]] = []
    # who the LEDGER says was actually paid on each sub of those customers —
    # survives FieldRoutes soldBy rewrites (the 900008 Marlow→Sage case)
    ledger_payees: dict[str, set[str]] = {}
    if bd_renewals:
        for sid_, rep_ in conn.execute("""
                SELECT DISTINCT l.subscription_id, l.rep FROM ledger l
                JOIN subscriptions s2 ON s2.subscription_id = l.subscription_id
                WHERE s2.customer_id IN (%s) AND l.kind != 'vest'
                  AND l.amount > 0""" % ",".join(
                    "?" * len(bd_renewals)), list(bd_renewals)):
            ledger_payees.setdefault(sid_, set()).add(rep_)
    out: list[SubFacts] = []
    # §12: upsell ACV increments credited to another rep are excluded from
    # the account's own commission math while inside the 3-service window
    upsell_inc = {r[0]: r[1] for r in conn.execute(
        "SELECT subscription_id, SUM(COALESCE(acv_increase,0)) FROM upsells "
        "WHERE removed_at IS NULL GROUP BY subscription_id")}
    fees = _FeeIndex(conn)                              # §5, per subscription
    # §4 (CFO 2026-11-09): a re-sold subscription restarts its service count.
    # Sales the manual books settled keep the count they were settled on.
    try:
        stand_sids = {r[0] for r in conn.execute(
            "SELECT subscription_id FROM cutover_sale WHERE disposition='stand'")}
    except sqlite3.OperationalError:
        stand_sids = set()
    app_paid = {r[0] for r in conn.execute(
        "SELECT DISTINCT subscription_id FROM ledger "
        "WHERE kind IN ('initial','repay') AND amount > 0")}
    sub_customer = {r[0]: r[1] for r in conn.execute(
        "SELECT subscription_id, customer_id FROM subscriptions")}
    # (customer, rep) pairs whose actual paid amount is on record in the
    # manual books — a pre-go-live claw on these is CONFIRMED, not uncertain
    # (§8: the claw uses the actual manual net, not the rate snapshot)
    manual_last_pay: dict[tuple[str, str], str] = {}
    try:
        manual_confirmed = {(str(r[0]), r[1]) for r in conn.execute(
            "SELECT DISTINCT customer_id, rep FROM manual_paid")}
        for cu_, rep_, per_ in conn.execute(
                "SELECT DISTINCT customer_id, rep, period FROM manual_paid"):
            k_, e_ = (str(cu_), rep_), _plabel_end(per_ or "")
            if e_ != "0000" and e_ > manual_last_pay.get(k_, ""):
                manual_last_pay[k_] = e_
        # customer-level: a single manual rep (reassigned accounts) or the
        # Not-Paid list (never paid -> claw $0) also settles the amount (§8)
        confirmed_custs = {str(r[0]) for r in conn.execute(
            "SELECT customer_id FROM manual_paid GROUP BY customer_id "
            "HAVING COUNT(DISTINCT rep) = 1")}
        confirmed_custs |= ({str(r[0]) for r in conn.execute(
            "SELECT customer_id FROM manual_not_paid")}
            - {c for c, _ in manual_confirmed})
    except sqlite3.OperationalError:
        manual_confirmed, confirmed_custs = set(), set()
    # a replacement only counts for the cancellation it was found against:
    # added -14..+90 days of the CURRENT cancel date (a stale row from an
    # earlier cancel must not protect a later deductible re-cancel)
    resigned_at = {r[0]: r[1] for r in conn.execute(
        "SELECT subscription_id, replacement_added FROM replacements "
        "WHERE replacement_sub_id IS NOT NULL")}
    for (sid, rep, cv, source, service_id, freq, billfreq, rc, active,
         date_cancelled, cancel_reason, initial_appt, card, date_added,
         service_type, cust_id, initial_quote, initial_discount,
         soldby_rep_now, cust_commercial, rep_prior, sold_by, date_updated) in subs:
        # FR renews subscriptions IN PLACE (dateAdded reset, years of service
        # history still attached) — for RENEWAL-source subs, only services
        # after the sub's own sold date count for trigger/vesting. Normal
        # sales are exempt: a service entered a day or two before the
        # subscription paperwork (data-entry lag) is legitimate.
        # §4 (CFO 2026-11-09): the same holds for ANY subscription re-sold on
        # an old record (900003, 900091): its earlier services and payments do
        # not count, so it is not vested the moment it is sold. A service up
        # to RESOLD_LAG_DAYS before the paperwork is still data-entry lag.
        added_floor = ""
        renewal_src = "renewal" in (source or "").lower()
        if date_added and (renewal_src or sid not in stand_sids):
            from datetime import datetime as _dt, timedelta as _td
            try:
                if renewal_src:
                    added_floor = (_dt.fromisoformat(date_added) - _td(days=1)
                                   ).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    added_floor = (_dt.fromisoformat(date_added[:10])
                                   - _td(days=RESOLD_LAG_DAYS)
                                   ).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                added_floor = ""
        f = SubFacts(sid, rep, cv or 0.0, source or "", card, active,
                     date_cancelled, cancel_reason)
        f.cv_effective = max((cv or 0.0) - upsell_inc.get(sid, 0.0), 0.0)
        f.date_added = date_added
        f.rep_prior = rep_prior
        f.date_updated = date_updated
        f.assign_changed = assign_changed.get(sid)
        if rep is None and rep_prior and not shares.get(sid):
            # §1 (owner, 2026-11-03): FR moved soldBy to someone off the roster
            # after we credited an ISA — the credit goes with it. No share is
            # left for the ISA, so close_period takes the payment back.
            f.flags.append(f"soldby-moved-off-roster: credit removed from "
                           f"{rep_prior} (PestRoutes soldBy now employee {sold_by})")
        if sid in prestart:
            # §1a: sold before the rep joined inside sales — not an ISA sale:
            # no commission, no true-up, no deduction, never booked at cutover
            f.state = "excluded_prestart"
            f.flags.append(f"sold-before-isa-start-{prestart[sid]}")
            out.append(f)
            continue

        # ---- qualifying services (§4, §4a) ----
        all_appts = conn.execute("""
            SELECT appointment_id, type, date_completed, ticket_id, is_initial
            FROM appointments WHERE subscription_id=? AND status_text='Completed'
              AND date_completed IS NOT NULL
              ORDER BY date_completed""", (sid,)).fetchall()
        appts = [a for a in all_appts if a[2] >= added_floor]
        if len(appts) < len(all_appts):
            # services from before this sale were set aside: the count restarts
            f.restarted = True
            if not renewal_src:
                f.flags.append("service-count-restarted")
        qualifying = [a for a in appts
                      if a[4] == 1 or a[1] in initial_type_ids
                      or a[1] == service_id or a[1] in plan_type_ids]
        f.qualifying_completed = len(qualifying)

        # ---- linked, fully-paid, non-writeoff invoices ----
        tickets = conn.execute("""
            SELECT t.ticket_id, t.appointment_id, t.total, t.balance, t.paid_date,
                   t.writeoff_tainted, t.invoice_date, COALESCE(t.coupon_paid,0)
            FROM tickets t JOIN appointments a ON a.appointment_id = t.appointment_id
            WHERE a.subscription_id = ? AND t.active=1
            UNION
            SELECT t.ticket_id, t.appointment_id, t.total, t.balance, t.paid_date,
                   t.writeoff_tainted, t.invoice_date, COALESCE(t.coupon_paid,0)
            FROM tickets t WHERE t.autogen_sub_id = ? AND t.active=1""",
            (sid, sid)).fetchall()
        # write-offs count as paid (waived-but-active rule, RULES §13);
        # tickets written off before paid_date was tracked fall back to invoice date
        paid_ok = {}
        wo_tickets = set()
        for tid, _a, total, bal, paid, wo, inv, cpn in tickets:
            if bal is None or abs(bal) >= EPS:
                continue
            if wo:
                wo_tickets.add(tid)
                paid = paid or inv
            # §4b: coupon dollars are not real payment — only the cash-paid
            # portion of an invoice counts toward coverage. The FIRST service's
            # invoice is exempt by construction (owner, 2026-10-01, §2/§4b):
            # a couponed initial still fires the trigger (balance $0 + paid
            # date, coupon applications included) and the initial's ticket is
            # excluded from the coverage sum below, so this reduction only
            # ever bites recurring service invoices and monthly bills.
            paid_ok[tid] = (max((total or 0) - (cpn or 0), 0.0), paid)
        by_appt = {a: (tid, total, bal, paid, wo)
                   for tid, a, total, bal, paid, wo, _inv, _c in tickets
                   if a and a != "0"}

        # ---- §2 trigger: first qualifying service completed AND paid ----
        if qualifying:
            first = qualifying[0]
            f.first_service_date = first[2]
            if first[4] != 1 and first[1] not in initial_type_ids:
                f.flags.append("first-service-not-flagged-initial")   # §4a
            tk = by_appt.get(first[0])
            if tk is None:
                tk_by_id = None
                if first[3] and first[3] != "0":
                    row = conn.execute(
                        "SELECT ticket_id,total,balance,paid_date,writeoff_tainted "
                        "FROM tickets WHERE ticket_id=?", (first[3],)).fetchone()
                    if row:
                        tk_by_id = (row[0], row[1], row[2], row[3], row[4])
                tk = tk_by_id
            if tk is None:
                f.flags.append("no-invoice-on-first-service")
            else:
                tid, total, bal, paid, wo = tk
                if wo and bal is not None and abs(bal) < EPS:
                    # waived-but-active counts as paid (RULES §13)
                    f.flags.append("first-invoice-writeoff")
                    got = paid_ok.get(tid)
                    if got and got[1]:
                        f.first_paid_date = max(first[2], got[1])
                elif total is not None and abs(total) < EPS:
                    # $0 invoice — paid at completion, treated like any other
                    # account (free initials are specially approved; confirmed
                    # by owner 2026-10-02, review flag retired)
                    f.first_paid_date = max(first[2], first[2])
                elif bal is not None and abs(bal) < EPS and paid:
                    f.first_paid_date = max(first[2], paid)

        if (not f.first_paid_date and f.restarted and sid in app_paid
                and sid in ledger_vested):
            # paid (and treated as vested) at the re-sale under the earlier
            # rule: the payment stays, but the 3-service count restarts — it
            # is deducted if it cancels before 3 new paid services
            f.first_paid_date = date_added
            f.flags.append("paid-at-resale")

        # ---- §4b coverage: paid dollars beyond the first service's invoice ----
        first_ticket_id = None
        if qualifying:
            tk = by_appt.get(qualifying[0][0])
            first_ticket_id = tk[0] if tk else (qualifying[0][3]
                                                if qualifying[0][3] not in (None, "0") else None)
        psv = _per_service_value(rc or 0.0, freq, billfreq)
        f.coverage_target = 2.0 * psv
        cum = 0.0
        coverage_reached_date = None
        cover_events = sorted(
            ((paid, total) for tid, (total, paid) in paid_ok.items()
             if tid != first_ticket_id and total and total > EPS and paid
             and paid >= added_floor),
            key=lambda x: x[0])
        for paid, total in cover_events:
            cum += total
            if coverage_reached_date is None and cum >= f.coverage_target - EPS:
                coverage_reached_date = paid
        f.coverage_paid = cum
        if psv <= EPS:
            f.flags.append("no-recurring-charge")

        # ---- vesting (§4b) ----
        if (f.qualifying_completed >= s.vesting_service_count
                and f.first_paid_date
                and (f.coverage_target <= EPS or coverage_reached_date)):
            third = qualifying[s.vesting_service_count - 1][2]
            f.vested_date = max(d for d in
                                (third, f.first_paid_date, coverage_reached_date)
                                if d)
        # the coverage test above uses the CURRENT plan, so a price or plan
        # change after vesting must not un-vest (900014: vested 7/24 at
        # $89/service, re-planned 9/16 to $135 quarterly). The lock covers
        # only that drift: the 3 qualifying services and the trigger must
        # still hold, so an engine fix to either still applies.
        lock = vest_locks.get(sid) if vest_locks is not None else None
        services_hold = bool(f.first_paid_date
                             and f.qualifying_completed >= s.vesting_service_count)
        if f.vested_date:
            if lock and lock < f.vested_date:
                f.vested_date = lock
            elif not lock and vest_locks is not None:
                new_locks.append((sid, f.vested_date, "computed"))
        elif services_hold and lock:
            f.vested_date = lock
            f.flags.append("vest-locked")
        elif services_hold and sid in ledger_vested and vest_locks is not None:
            f.vested_date = min(ledger_vested[sid] + " 23:59:59",
                                max(qualifying[s.vesting_service_count - 1][2],
                                    f.first_paid_date))
            new_locks.append((sid, f.vested_date, "ledger"))
            f.flags.append("vest-locked")
        if sid in bd_sids and f.first_paid_date:
            bd_paid.add(sid)

        # ---- rate & expected (§3, §6) ----
        stype = (service_type or "").strip().lower()
        # one-time: an Initial Service plan, or no recurring charge and no
        # service frequency (FR stores -1 on some one-off plans, e.g. 900039)
        one_time = (stype == "initial service"
                    or ((rc or 0) == 0 and (freq or 0) <= 0))
        mosq_upsell = ((source or "").strip().lower() == "upsell"
                       and "month mosquito" in stype)
        f.commercial = is_commercial(s, service_type, cust_commercial)
        f.rate = _rate(s, card, source, one_time=one_time,
                       mosq_upsell=mosq_upsell, commercial=f.commercial)
        if one_time and f.first_paid_date and not f.vested_date:
            # §5 (owner, 2026-10-23): a one-time job has no later services to
            # wait for — kept once its service is done and paid. Cancelling it
            # afterwards (often just to close the account) never deducts.
            f.vested_date = f.first_paid_date
        if rep in managers and not shares.get(sid):
            # §14 (owner, 2026-11-23): a manager's sale pays a flat 5% once its
            # first service is done and paid (§2), and that is all — no card,
            # source or commercial rates, no 3-service vesting, and never a
            # deduction: a later cancel, a lower contract value or an unpaid
            # fee changes nothing. Kept from the first payment (like a
            # one-time job), so close_period pays it once and freezes it.
            f.rate = s.rate_manager
            f.flags.append("manager-flat-5%")
            start = s.manager_start_dates.get(rep, "")
            if f.first_paid_date and f.first_paid_date[:10] < start:
                # payable before the app started paying this manager: paid
                # outside the app (or not at all) — never booked here
                f.state = "excluded_prestart"
                f.flags.append(f"manager-paid-before-app-start-{start}")
            elif f.first_paid_date:
                f.vested_date = f.first_paid_date
                f.expected = f.rate * f.cv_effective
                f.state = "vested"
            else:
                f.state = "pending" if f.qualifying_completed or appts else "not_started"
            out.append(f)
            continue
        cancelled = (not active) and date_cancelled
        reason = (cancel_reason or "").strip().lower()
        # renewal exclusion is retroactive; moved / re-signed apply only to
        # cancels dated after go-live (manual settled older ones its own way)
        post_golive = bool(cancelled and date_cancelled >= s.go_live_period_start)

        # ---- §5 paid-cancellation-fee exception, per subscription ----
        if cancelled:
            init_real = None             # real money paid on the first invoice
            if first_ticket_id:
                ft = conn.execute(
                    "SELECT COALESCE(NULLIF(sub_total, 0), total), balance, "
                    "COALESCE(coupon_paid, 0), COALESCE(writeoff_tainted, 0) "
                    "FROM tickets WHERE ticket_id = ?", (first_ticket_id,)).fetchone()
                if ft and ft[1] is not None and abs(ft[1]) < EPS and not ft[3]:
                    init_real = (ft[0] or 0.0) - (ft[2] or 0.0)
            recapture = [a for a in (
                initial_discount,
                (initial_quote - init_real)
                if initial_quote is not None and init_real is not None else None)
                if a is not None and a > 1.0]
            fee_date, route = fees.paid_date(sid, cust_id, date_added,
                                             date_cancelled, post_golive, recapture)
            if (fee_date is None and route != "billed" and post_golive
                    and not one_time and initial_quote is not None
                    and init_real is not None and f.first_paid_date
                    and init_real >= max(initial_quote, s.full_initial_floor) - EPS):
                # the fee is the initial-discount recapture; a customer who
                # paid the FULL initial with real money owes $0, so no fee is
                # billed — that counts as paid (900025, owner 2026-10-14)
                fee_date, route = f.first_paid_date, "full-price-initial"
            if fee_date:
                f.cxl_fee_paid, f.cxl_fee_paid_date = True, fee_date
                if route != "cxl-ticket":
                    f.flags.append(f"cxl-fee-{route}")

        is_renewal_cancel = cancelled and reason == "renewal"
        # §5: any reason BEGINNING with 'Moved' counts (incl. 'Moved OOC');
        # the reason stored is always the LATEST cancellation's note
        is_moved = cancelled and post_golive and reason.startswith("moved")
        ra, dc_ = _day(resigned_at.get(sid)), _day(date_cancelled)
        is_resigned = bool(cancelled and post_golive and not is_renewal_cancel
                           and not is_moved and ra and dc_
                           and -14 <= (ra - dc_).days <= 90)
        # §5 Bad-Debt-Renewal re-sell (CFO 2026-10-05): a renewal/re-signed
        # exclusion does NOT protect the old sub when a DIFFERENT agent is
        # paid on the customer's newer 'Bad Debt Renewal' sub — the original
        # commission is deducted instead.
        if (is_renewal_cancel or is_resigned) and rep:
            cust_ = sub_customer.get(sid, "")
            for bd_sid, bd_rep, bd_added in bd_renewals.get(cust_, []):
                if bd_sid == sid or not bd_added or bd_added <= (date_added or ""):
                    continue
                if not (bd_sid in bd_paid or ledger_payees.get(bd_sid)
                        or manual_last_pay.get((str(cust_), bd_rep), "") >= bd_added[:10]):
                    # the re-sell never paid anyone (cancelled before its
                    # first payment): the old sub's exclusion stands
                    # (900011: Devon Rivers' BD renewal never paid)
                    continue
                paid_reps = ledger_payees.get(sid, set())
                if bd_rep and (bd_rep != rep
                               or (paid_reps and bd_rep not in paid_reps)):
                    # a different agent is paid on the BD-renewal re-sell —
                    # judged against soldBy AND against who the ledger
                    # actually paid, so a soldBy rewrite cannot hide it
                    is_renewal_cancel = is_resigned = False
                    f.flags.append("bd-renewal-paid-to-other-rep")
                    break
                # pre-app-era accounts have no ledger rows: the manual books
                # know who was really paid — flag for the owner's review
                manual_reps = {r_ for c_, r_ in manual_confirmed if c_ == cust_}
                if bd_rep and manual_reps and bd_rep not in manual_reps:
                    f.flags.append("bd-renewal-check-original-payee")
                    break
        f.cancel_excluded = bool(is_renewal_cancel or is_moved or is_resigned)
        excl_state = ("excluded_renewal" if is_renewal_cancel else
                      "excluded_moved" if is_moved else "excluded_resigned")

        if is_renewal_cancel and not f.first_paid_date:
            f.flags.append("renewal-cancel-before-first-payment")     # §5 anomaly
        if (cancelled and not f.cancel_excluded and not f.vested_date
                and f.first_paid_date and f.first_paid_date < s.go_live_period_start
                and f.rate < 0.089
                and (str(sub_customer.get(sid, "")), rep) not in manual_confirmed
                and str(sub_customer.get(sid, "")) not in confirmed_custs):
            # claw of a manual-era commission at today's <9% rate with NO
            # record in the manual books to confirm what was actually paid.
            # When manual_paid has the account, the §8 close-time lookup claws
            # the actual amount and the claw is confirmed — no flag.
            f.flags.append("claw-rate-uncertain-pre-golive-card-state")

        if f.vested_date or f.cancel_excluded:
            # vested or exclusion-protected: expected frozen at current calc —
            # close_period freezes it permanently via the ledger
            f.expected = f.rate * f.cv_effective if f.first_paid_date else 0.0
            f.state = "vested" if f.vested_date else excl_state
        elif cancelled:
            if f.cxl_fee_paid and f.first_paid_date:
                # paid cancellation fee: rep keeps HALF (§5)
                f.expected = 0.5 * f.rate * f.cv_effective
                f.state = "half_kept_fee_paid"
            else:
                f.expected = 0.0
                f.state = "clawed_back" if f.first_paid_date else "cancelled_unpaid"
        elif f.first_paid_date:
            f.expected = f.rate * f.cv_effective
            f.state = "paid"
        else:
            f.state = "pending" if f.qualifying_completed or appts else "not_started"
        out.append(f)

    for f in out:
        # rescue-pending flag: assigned but the first service hasn't paid yet —
        # the accounts the owner reviews and may release back to the pool
        for rep, share, typ in shares.get(f.subscription_id, []):
            if typ == "rescue" and not f.first_paid_date:
                f.flags.append("rescue-pending-first-service")
        if f.rep is None and f.subscription_id in shares:
            f.rep = shares[f.subscription_id][0][0]   # display rep for status row

    rows = [(f.subscription_id, f.rep, f.first_paid_date, f.qualifying_completed,
             round(f.coverage_paid, 2), round(f.coverage_target, 2), f.rate,
             round(f.expected, 2), f.vested_date, f.state,
             ";".join(f.flags) or None) for f in out]
    conn.executemany("""
        INSERT INTO sub_status(subscription_id,rep,first_paid_date,
          qualifying_completed,coverage_paid,coverage_target,rate,expected,
          vested_date,state,flags,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(subscription_id) DO UPDATE SET rep=excluded.rep,
          first_paid_date=excluded.first_paid_date,
          qualifying_completed=excluded.qualifying_completed,
          coverage_paid=excluded.coverage_paid,
          coverage_target=excluded.coverage_target, rate=excluded.rate,
          expected=excluded.expected, vested_date=excluded.vested_date,
          state=excluded.state, flags=excluded.flags,
          updated_at=excluded.updated_at""", rows)
    if new_locks:
        conn.executemany("INSERT OR IGNORE INTO vest_lock(subscription_id, "
                         "vested_date, source) VALUES(?,?,?)", new_locks)
    conn.commit()
    return out


def _shares_for(conn: sqlite3.Connection, f: SubFacts,
                shares: dict[str, list[tuple[str, float, str]]]) -> list[tuple[str, float]]:
    """Active assignment shares if any (RULES §11), else soldBy rep at 100%.

    type 'outbound' (split with an outbound agent, paid on a
    separate payroll at 3%): the ISA keeps rate−3% on 9%% accounts and their
    full rate on 5%%/3%% accounts — computed from the CURRENT rate."""
    rows = shares.get(f.subscription_id)
    if rows:
        out = []
        for rep, share, typ in rows:
            if typ == "outbound":
                share = (f.rate - 0.03) / f.rate if f.rate > 0.051 else 1.0
            out.append((rep, share))
        return out
    return [(f.rep, 1.0)] if f.rep else []


def seed_external(conn: sqlite3.Connection, s: Settings,
                  facts: list[SubFacts]) -> int:
    """RULES §8: subscriptions first-paid BEFORE go-live were paid by the
    manual process. Record their current expected as 'external' ledger rows
    (one per share) so we never re-pay them, while §6 keeps them subject to
    deduction."""
    seeded = 0
    shares = active_shares(conn)
    golive = s.go_live_period_start
    ext_period = golive  # label only
    for f in facts:
        if f.state == "excluded_prestart":
            continue             # §1a: never an ISA sale, nothing to book
        if not f.first_paid_date or f.first_paid_date[:10] >= golive:
            continue
        # Baseline = expected AS OF the instant before go-live. An account
        # already cancelled (non-renewal, unvested) back then was already
        # deducted by the manual process — its baseline is zero, so nothing
        # is seeded and no fresh claw-back appears at go-live. Only changes
        # dated after go-live move money.
        cancelled_pre = (not f.active and f.date_cancelled
                         and f.date_cancelled[:10] < golive)
        is_renewal = ((f.cancel_reason or "").strip().lower() == "renewal"
                      or f.cancel_excluded)
        vested_pre = f.vested_date and f.vested_date[:10] < golive
        half_keep = False
        if cancelled_pre and not is_renewal and not vested_pre:
            if f.cxl_fee_paid:
                half_keep = True     # manual settled these at half (§5)
            else:
                continue
        has = conn.execute("SELECT 1 FROM ledger WHERE subscription_id=? LIMIT 1",
                           (f.subscription_id,)).fetchone()
        if has:
            continue
        for rep, share in _shares_for(conn, f, shares):
            amount = round(share * f.rate * f.cv_effective * (0.5 if half_keep else 1.0), 2)
            conn.execute("""INSERT INTO ledger(subscription_id,rep,period_end,amount,
                            kind,note) VALUES(?,?,?,?,?,?)""",
                         (f.subscription_id, rep, ext_period, amount, "external",
                          f"settled by manual payroll (first paid {f.first_paid_date[:10]}"
                          + (f"; share {share:.0%}" if share != 1.0 else "") + ")"))
        seeded += 1
    conn.commit()
    return seeded


class _ManualBasis:
    """RULES §8: what the manual process actually did on each SALE.

    Manual history is keyed by (customer, rep), so it is split per sale: a
    payment or deduction belongs to the rep's latest sale to that customer
    dated on/before it — an older sale's payment never inflates a newer
    sale's claw (900002: a 2025 payment on a renewed-away sale). Per sale:
      G = commission paid, T = CV true-downs taken, D = cancel deductions.
    (The CV true-downs were ingested under section 'bd_renewal'; 555 of 738
    rows equal rate x (CV paid - CV now) exactly — e.g. 900019: paid 84.06,
    trued down 23.76, deducted 60.30 at cancel.)"""

    def __init__(self, conn: sqlite3.Connection, period_start: str,
                 period_end: str, golive: str):
        self.sales: dict[tuple[str, str], list] = defaultdict(list)
        self.cancel_day = {r[0]: (r[1] or "")[:10] for r in conn.execute(
            "SELECT subscription_id, date_cancelled FROM subscriptions "
            "WHERE date_cancelled IS NOT NULL AND active = 0")}
        for sid, cu, rep, da in conn.execute(
                "SELECT subscription_id, customer_id, rep, COALESCE(date_added,'') "
                "FROM subscriptions WHERE rep IS NOT NULL"):
            self.sales[(str(cu), rep)].append((da[:10], sid))
        # sales credited by an assignment too: a D2D rescue's soldBy is the
        # D2D rep (rep NULL here) and the ISA is credited via the assignment,
        # so its 'D2D Split' payments must attach to the sale (900015 etc.)
        for sid, cu, rep, da in conn.execute("""
                SELECT a.subscription_id, s.customer_id, a.rep,
                       COALESCE(s.date_added,'')
                FROM assignments a JOIN subscriptions s USING(subscription_id)
                WHERE a.removed_at IS NULL"""):
            if (da[:10], sid) not in self.sales[(str(cu), rep)]:
                self.sales[(str(cu), rep)].append((da[:10], sid))
        for v in self.sales.values():
            v.sort()
        self.sale_cv = {r[0]: r[1] or 0.0 for r in conn.execute(
            "SELECT subscription_id, contract_value FROM subscriptions")}
        self.tie_pick: dict[tuple[str, str, str], str] = {}
        self._ties: dict[tuple[str, str, str], list[str]] = {}
        self.G: dict[str, float] = defaultdict(float)
        self.T: dict[str, float] = defaultdict(float)
        self.D: dict[str, float] = defaultdict(float)
        self.D_pre: dict[str, float] = defaultdict(float)   # before go-live
        self.by_cust: dict[str, list] = defaultdict(list)
        self.rep_pay_days: dict[str, set] = defaultdict(set)
        self.not_paid: set[str] = set()
        self.golive = golive
        self.ambiguous: set[str] = set()     # same-day sales: can't attribute
        self.cv_paid: dict[str, float] = {}  # CV the sheets paid this sale on
        self.any_pay: dict[tuple[str, str], list] = defaultdict(list)
        self.has_adj: set[str] = set()       # paid (partly) via adjustments
        self.ok = True
        try:
            pays = [(cu, rep, _plabel_end(per), amt, cv, False)
                    for cu, rep, per, amt, cv in conn.execute(
                        "SELECT customer_id, rep, period, SUM(commission), "
                        "MAX(contract_value) FROM manual_paid "
                        "GROUP BY customer_id, rep, period")]
            # split transfers and D2D-rescue credits paid on the adjustments
            # sheet are payments on the sale too (900027: Finley's $61.56 came
            # only via a 'D2D Split' row); upsell credits are not (§12)
            try:
                pays += [(cu, rep, pe, amt, None, True)
                         for cu, rep, pe, amt in conn.execute(
                             "SELECT account, rep, period_end, SUM(amount) "
                             "FROM manual_adjustments WHERE "
                             "LOWER(COALESCE(note,'')) NOT LIKE '%upsell%' "
                             "GROUP BY account, rep, period_end")]
            except sqlite3.OperationalError:
                pass
            for cu, rep, pe, _amt, cv, _adj in pays:       # settle same-day ties first
                if cv and pe <= period_end:
                    self._owner(str(cu), rep, pe, cv)
            for cu, rep, pe, amt, cv, adj in pays:
                self.rep_pay_days[rep].add(pe)
                self.any_pay[(str(cu), rep)].append(pe)     # all periods
                if pe > period_end:
                    continue
                cu = str(cu)
                self.by_cust[cu].append((rep, "G", pe, amt or 0.0))
                sid = self._owner(cu, rep, pe, cv)
                if sid:
                    self.G[sid] += amt or 0.0
                    if adj:
                        self.has_adj.add(sid)
                    elif cv and sid not in self.cv_paid:
                        self.cv_paid[sid] = float(cv)
            for cu, rep, sec, pe, amt in conn.execute(
                    "SELECT customer_id, rep, section, period_end, SUM(amount) "
                    "FROM manual_deducted WHERE period_end < ? "
                    "GROUP BY customer_id, rep, section, period_end", (period_start,)):
                cu, k, a = str(cu), ("T" if sec == "bd_renewal" else "D"), abs(amt or 0.0)
                self.by_cust[cu].append((rep, k, pe, a))
                sid = self._owner_ded(cu, rep, pe) if k == "D" else self._owner(cu, rep, pe)
                if sid:
                    (self.T if k == "T" else self.D)[sid] += a
                    if k == "D" and pe < golive:
                        self.D_pre[sid] += a
            # Not-Paid entries name a rep and a sold date: match on both, so
            # an entry for a different rep / sale on the account never zeroes
            # this sale's claw
            self.not_paid = {(str(cu), rep, (ds or "")[:10]) for cu, rep, ds in conn.execute(
                "SELECT customer_id, rep, date_sold FROM manual_not_paid")}
        except sqlite3.OperationalError:     # manual history not ingested
            self.ok = False
        for key, same in self._ties.items():
            if key not in self.tie_pick:
                self.ambiguous.update(same)

    def _owner(self, cu: str, rep: str, day: str, cv: float | None = None) -> str | None:
        cand = [(d, sid) for d, sid in self.sales.get((cu, rep), []) if d <= day]
        if not cand:
            return None
        last_day = cand[-1][0]
        same = [sid for d, sid in cand if d == last_day]
        if len(same) > 1:
            # two sales to the customer on the same day: the sheets' line goes
            # to the one sale whose contract value it was paid on (900033 /
            # 900034: paid on CV 531 = 900034). Unresolved ties use the
            # snapshot fallback.
            key = (cu, rep, last_day)
            if key not in self.tie_pick and cv:
                hits = [s_ for s_ in same if abs(self.sale_cv.get(s_, 0.0) - cv) <= 1.0]
                if len(hits) == 1:
                    self.tie_pick[key] = hits[0]
            self._ties[key] = same
            return self.tie_pick.get(key)
        return same[0]

    def _sibling_row(self, cu: str, rep: str, day: str, sid: str) -> bool:
        """The row was pinned to ANOTHER sale of a same-day tie."""
        cand = [(d, s_) for d, s_ in self.sales.get((cu, rep), []) if d <= day]
        if not cand:
            return False
        pick = self.tie_pick.get((cu, rep, cand[-1][0]))
        return bool(pick and pick != sid)

    def _owner_ded(self, cu: str, rep: str, day: str) -> str | None:
        """A cancel deduction belongs to the rep's sale most recently
        CANCELLED on/before it — not simply the newest sale (cust 900013: the
        3/12 sale's deduction must not land on the still-active 3/21 sale)."""
        cand = [(self.cancel_day[sid], sid) for d, sid in self.sales.get((cu, rep), [])
                if d <= day and self.cancel_day.get(sid, "9999") <= day]
        if cand:
            cand.sort()
            return cand[-1][1]
        return self._owner(cu, rep, day)

    def basis(self, sid: str, cu: str, rep: str, sale_day: str
              ) -> tuple[float, float, float, float, str | None]:
        """(G, T, D, D_pre, paid_under) for this sale. paid_under names
        another rep when the sheets paid the account (on/after this sale)
        under exactly one different rep — a reassigned account."""
        if self.G.get(sid, 0.0) > 0.005:
            return (self.G[sid], self.T.get(sid, 0.0), self.D.get(sid, 0.0),
                    self.D_pre.get(sid, 0.0), None)
        others: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
        for r_, k, pe, amt in self.by_cust.get(cu, []):
            if r_ == rep or pe < sale_day:
                continue
            if any(d <= pe for d, _s in self.sales.get((cu, r_), [])):
                # that rep's OWN sale on the account — never a reassignment
                # of this one (900012: Logan's payment on his own sale)
                continue
            others[r_]["GTD".index(k)] += amt
            if k == "D" and pe < self.golive:
                others[r_][3] += amt
        paid = {r_: v for r_, v in others.items() if v[0] > 0.005}
        if len(paid) == 1:
            r_, (g, t, d, dp) = next(iter(paid.items()))
            return g, t, d, dp, r_
        return 0.0, 0.0, 0.0, 0.0, None

    def commission_now(self, sid: str, g: float, t: float, cv_now: float) -> float:
        """The sale's commission on TODAY's contract value at the rate the
        sheets actually paid (§8b: CV changes are trued at the as-paid
        implied rate) — so a half-keep halves the right amount even when the
        sheets never booked the CV change (900035: 934 -> 780). Falls back to
        paid minus true-downs when the paid CV is unknown or the sale was
        paid through adjustments."""
        cvp = self.cv_paid.get(sid)
        if cvp and cvp > EPS and sid not in self.has_adj and cv_now > EPS:
            return round(g / cvp * cv_now, 2)
        return round(g - t, 2)

    def never_paid(self, cu: str, rep: str, sale_day: str,
                   first_paid: str | None, sid: str = "") -> bool:
        """Nothing to take back: the sale is on the manual Not-Paid list (same
        rep, same sold date ±3 days), or the manual payroll WAS paying this
        rep in the 75 days after the sale's first payment yet never paid
        anything on this customer after the sale. Any manual row for this
        (customer, rep) after the sale means it may have been paid — then
        this is NOT a never-paid finding (snapshot fallback instead) —
        unless that row was pinned to the other sale of a same-day pair."""
        sd = _day(sale_day)
        if any(r_ == rep and pe >= sale_day and not self._sibling_row(cu, rep, pe, sid)
               for r_, _k, pe, _a in self.by_cust.get(cu, [])):
            return False
        # a payment on/after the sale in ANY period (even after the period
        # being closed) means it was paid — re-simulating an earlier period
        # must not declare it never-paid and close it permanently at $0
        if any(pe >= sale_day and not self._sibling_row(cu, rep, pe, sid)
               for pe in self.any_pay.get((cu, rep), [])):
            return False
        for c_, r_, ds in self.not_paid:
            if c_ == cu and r_ == rep and sd and _day(ds) and abs((_day(ds) - sd).days) <= 3:
                return True
        fp = _day(first_paid)
        if fp is None:
            return False
        lo, hi = fp.isoformat(), date.fromordinal(fp.toordinal() + 75).isoformat()
        return any(lo <= pe <= hi for pe in self.rep_pay_days.get(rep, ()))


def close_period(conn: sqlite3.Connection, s: Settings, facts: list[SubFacts],
                 period_start: str, period_end: str, live: bool = False,
                 commit: bool = True) -> list[dict]:
    """Append ledger rows for the pay period [period_start, period_end].
    live=True: a live pay period after the cutover (RULES §8c) — the ledger
    already holds what the manual books paid, so the §8 manual-history
    reconstruction is skipped and every upsell without a ledger row is paid.
    Only event-dated activity on/before period_end moves money; frozen subs
    (vested/excluded ledger already reconciled) never move again."""
    emitted: list[dict] = []
    shares = active_shares(conn)
    cutoff = period_end + " 23:59:59"
    # §1: only people on the commission roster (inbound ISAs and managers) get
    # lines. Someone removed from it is off the statement — nothing paid,
    # nothing deducted (Morgan Hale / Reese Frost, 2026-11-23).
    on_roster = set(payees(s))
    managers = set(s.managers)                           # §14
    # §6 (CFO 2026-11-09): a contract-value raise never pays on its own, only
    # a submitted upsell does. basis = the value each live sale's commission
    # rests on as of the previous close: set at first payment, lowered by cuts.
    basis: dict[str, float] = {}
    new_basis: list[tuple[str, str, float]] = []
    if live:
        basis = dict(conn.execute(
            """SELECT subscription_id, cv FROM cv_basis b WHERE period_end =
               (SELECT MAX(period_end) FROM cv_basis
                WHERE subscription_id = b.subscription_id AND period_end < ?)""",
            (period_end,)))

    # RULES §8: claw-backs, half-keeps and re-pays on pre-go-live accounts use
    # what the manual process ACTUALLY did on that sale (per-sale history),
    # never a rate snapshot. PERIOD-AWARE: payments on/before this close,
    # deductions from BEFORE this period only — a deduction on this same
    # period's manual sheet is concurrent agreement with our line.
    sub_cust = dict(conn.execute(
        "SELECT subscription_id, customer_id FROM subscriptions"))
    golive = s.go_live_period_start
    mb = None if live else _ManualBasis(conn, period_start, period_end, golive)
    for f in facts:
        if f.state == "excluded_prestart":
            continue             # §1a: never paid, never deducted
        frozen = conn.execute(
            "SELECT note FROM ledger WHERE subscription_id=? AND kind IN "
            "('vest','freeze') ORDER BY id DESC LIMIT 1", (f.subscription_id,)).fetchone()
        if frozen:
            # vesting is permanent; an EXCLUSION freeze holds only while that
            # cancellation stands — reinstated or re-cancelled for a
            # deductible reason, the sub is reconciled again (§5, 900016)
            note_ = str(frozen[0] or "")
            if note_ == "vested" and f.restarted and not f.vested_date:
                # §4: that vest counted the subscription's earlier life; the
                # count restarted at the re-sale, so it is reconciled again
                pass
            elif not (note_.startswith("excluded")
                      and (f.active or not f.cancel_excluded)):
                continue
            else:
                f.flags.append("exclusion-lapsed")

        # expected AS OF period end: trigger/cancel/vest honored by event date
        triggered = f.first_paid_date and f.first_paid_date <= cutoff
        cancelled = f.date_cancelled and f.date_cancelled <= cutoff and not f.active
        is_renewal = cancelled and f.cancel_excluded
        vested = f.vested_date and f.vested_date <= cutoff

        sub_shares = _shares_for(conn, f, shares)
        # reps with prior ledger rows but no current share (removed split/rescue,
        # rep reassignment) must be reconciled to zero
        prior_reps = [r[0] for r in conn.execute(
            "SELECT DISTINCT rep FROM ledger WHERE subscription_id=? "
            "AND (ref IS NULL OR ref NOT LIKE 'upsell:%') AND kind != 'manual'",
            (f.subscription_id,))]
        share_map = dict(sub_shares)
        for r in prior_reps:
            share_map.setdefault(r, 0.0)
        if not triggered and not prior_reps:
            continue
        if live and not triggered and not conn.execute(
                "SELECT 1 FROM ledger WHERE subscription_id=? AND kind NOT IN "
                "('baseline','vest','freeze') AND (ref IS NULL OR ref NOT LIKE "
                "'upsell:%') LIMIT 1", (f.subscription_id,)).fetchone():
            # not earned yet and the app has paid nothing on it: a manual
            # split transfer from before go-live waits for the payment
            # (900036: Hayden -7.17 / Riley +7.17) — never reversed early
            continue

        # §5 half-keep is EVENT-DATED by the fee's paid date for post-go-live
        # cancels: a fee paid after the cancel's period means a full claw now
        # and half back in the period it is paid (pre-go-live cancels stay as
        # the manual process settled them)
        fee_ok = f.cxl_fee_paid and (
            (f.date_cancelled or "")[:10] < golive
            or (f.cxl_fee_paid_date or "9999") <= cutoff)
        claw_state = bool(triggered and cancelled and not is_renewal and not vested)
        # §6 (CFO 2026-11-09): a raise in contract value never pays on its own
        cv_pay = f.cv_effective
        if live and triggered:
            b = basis.get(f.subscription_id)
            if b is None:
                b = _paid_cv(conn, f.subscription_id, period_end)
            if b is not None and cv_pay > b + 0.005:
                if cv_pay - b >= 0.5 and not cancelled:
                    _review(conn, period_end, f.subscription_id,
                            f"contract value went up ${b:,.2f} -> ${cv_pay:,.2f} with no "
                            "upsell submitted: not paid")
                cv_pay = b
            new_basis.append((f.subscription_id, period_end, round(cv_pay, 2)))
        if not triggered:
            expected_total = 0.0
        elif claw_state:
            # unvested cancel → claw back (§6), unless the cancellation fee
            # was paid in full → rep keeps half (§5)
            expected_total = (round(0.5 * f.rate * cv_pay, 2)
                              if fee_ok else 0.0)
        else:
            expected_total = round(f.rate * cv_pay, 2)  # vesting is permanent (§4)

        for rep, share in share_map.items():
            if rep not in on_roster:
                continue                     # §1: off the roster, off the statement
            paid_so_far = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM ledger "
                "WHERE subscription_id=? AND rep=? "
                "AND (ref IS NULL OR ref NOT LIKE 'upsell:%') AND kind != 'manual'",
                (f.subscription_id, rep)).fetchone()[0]
            expected = round(share * expected_total, 2)
            diff = round(expected - paid_so_far, 2)
            if abs(diff) < 0.02:   # ignore split-share rounding dust
                continue
            if diff < 0 and rep in managers:
                continue           # §14: a manager is never deducted, for any reason
            # look-ahead: the sub is already dead AS OF RUN TIME (cancelled
            # after period end, unvested, no exclusion) — never paid, so do
            # not pay now just to claw next period (owner, 2026-09-18)
            if (diff > 0 and abs(paid_so_far) < 0.02
                    and f.state in ("clawed_back", "cancelled_unpaid")):
                continue
            if paid_so_far == 0 and diff > 0:
                kind = "repay" if rep in prior_reps else "initial"
            elif expected == 0.0:
                kind = "clawback"
            elif paid_so_far <= 0 < diff:
                kind = "repay"
            else:
                kind = "trueup"
            note = (f"state={f.state}; rate={f.rate:.0%}"
                    + (" (commercial)" if f.commercial else "")
                    + f"; cv=${f.contract_value:,.2f}")
            if share != 1.0:
                note += f"; share {share:.0%}"
            if f.flags:
                note += "; flags=" + ",".join(f.flags)

            # §8 pre-go-live accounts: the line is what the manual process
            # ACTUALLY did on this sale, not the rate snapshot the external
            # seed was booked at. Target holding: $0 (claw), half of the
            # CV-adjusted commission (paid fee), or all of it (re-pay).
            if (not live and share == 1.0 and mb.ok and f.first_paid_date
                    and f.first_paid_date[:10] < golive
                    and f.subscription_id not in mb.ambiguous
                    and (claw_state or kind == "repay")):
                cust = str(sub_cust.get(f.subscription_id, ""))
                g, t, d, d_pre, paid_under = mb.basis(
                    f.subscription_id, cust, rep, (f.date_added or "")[:10])
                handled = None
                if g > 0.005:
                    # target basis: commission on today's CV at the as-paid rate
                    b = mb.commission_now(f.subscription_id, g, t, f.cv_effective)
                    app_visible = conn.execute(
                        "SELECT COALESCE(SUM(amount),0) FROM ledger WHERE "
                        "subscription_id=? AND rep=? AND kind IN "
                        "('clawback','repay','trueup','initial') AND ref IS NULL",
                        (f.subscription_id, rep)).fetchone()[0]
                    # what the rep holds now: the manual books' view and the
                    # app's own view (go-live state + app lines); a deduction
                    # taken on either side is one event, never two. Never
                    # below $0: a sale the sheets over-deducted (900007 twice,
                    # 900009 three times) is neither refunded nor deducted
                    # again — past wrong deductions stand (§8b)
                    held = max(0.0, min(g - t - d, g - t - d_pre + app_visible))
                    target = (0.0 if (claw_state and not fee_ok)
                              else _cents(0.5 * b) if claw_state
                              else _cents(b))
                    visible = _cents(target - held)
                    src = "; manual paid under " + paid_under if paid_under else ""
                    handled = ("visible", visible, note + "; amount=actual manual "
                               "paid on this sale (RULES §8)" + src)
                    if abs(visible) < 0.02:
                        handled = ("none", 0.0, "manual books already at the "
                                   "right amount for this sale — no visible line "
                                   "(RULES §8)")
                elif mb.never_paid(cust, rep, (f.date_added or "")[:10],
                                   f.first_paid_date, f.subscription_id):
                    # nothing was ever paid on this sale: nothing to deduct,
                    # nothing to keep half of, nothing to restore
                    handled = ("none", 0.0, "never paid by the manual process "
                               "on this sale — no deduction or re-pay (RULES §8)")
                if handled:
                    mode, visible, why = handled
                    if mode == "visible":
                        vkind = ("clawback" if target == 0 and visible < 0
                                 else "repay" if visible > 0 else "trueup")
                        conn.execute(
                            """INSERT INTO ledger(subscription_id,rep,period_end,
                               amount,kind,note,event_date) VALUES(?,?,?,?,?,?,?)""",
                            (f.subscription_id, rep, period_end, visible, vkind, why,
                             _event_date(f, vkind, claw_state, fee_ok)))
                        emitted.append({"subscription_id": f.subscription_id,
                                        "rep": rep, "amount": visible,
                                        "kind": vkind, "state": f.state,
                                        "flags": f.flags})
                    balance = round(diff - visible, 2)
                    if abs(balance) >= 0.005:
                        conn.execute(
                            """INSERT INTO ledger(subscription_id,rep,period_end,
                               amount,kind,note) VALUES(?,?,?,?,'external',?)""",
                            (f.subscription_id, rep, period_end, balance,
                             "balance to the rate-snapshot ledger: " + why))
                    continue

            conn.execute("""INSERT INTO ledger(subscription_id,rep,period_end,
                            amount,kind,note,event_date) VALUES(?,?,?,?,?,?,?)""",
                         (f.subscription_id, rep, period_end, diff, kind, note,
                          _event_date(f, kind, claw_state, fee_ok)))
            emitted.append({"subscription_id": f.subscription_id, "rep": rep,
                            "amount": diff, "kind": kind, "state": f.state,
                            "flags": f.flags})
        # freeze marker once vested or renewal-excluded (after reconciling)
        if vested or is_renewal:
            conn.execute("""INSERT INTO ledger(subscription_id,rep,period_end,
                            amount,kind,note) VALUES(?,?,?,0,'vest',?)""",
                         (f.subscription_id, f.rep or "-", period_end,
                          "vested" if vested else f"excluded: {f.state}"))

    if new_basis:
        conn.executemany("INSERT OR REPLACE INTO cv_basis(subscription_id, period_end, cv) "
                         "VALUES(?,?,?)", new_basis)

    # ---- §12 upsell credits: one-time payouts, idempotent via ledger.ref ----
    for uid, sid, rep, amount, date_, d2d, note, removed in conn.execute(
            "SELECT id, subscription_id, rep, amount, date, d2d_rep, note, "
            "removed_at FROM upsells"):
        ref = f"upsell:{uid}"
        if rep not in on_roster:
            continue                         # §1: off the roster, off the statement
        paid_row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM ledger WHERE ref IN (?,?)",
            (ref, ref + ":reversal")).fetchone()[0]
        booked = conn.execute("SELECT 1 FROM ledger WHERE ref=? LIMIT 1",
                              (ref,)).fetchone() if live else None
        if removed is None and date_ <= cutoff and abs(paid_row) < EPS and not booked:
            # pre-go-live upsells were paid by the manual process (live: the
            # cutover booked a marker for every upsell the manual books held)
            kind = ("upsell" if live or date_[:10] >= s.go_live_period_start
                    else "external")
            n = f"upsell credit (ACV increase){'; D2D acct: ' + d2d if d2d else ''}"
            if note:
                n += f"; {note}"
            conn.execute("""INSERT INTO ledger(subscription_id,rep,period_end,
                            amount,kind,note,ref,event_date) VALUES(?,?,?,?,?,?,?,?)""",
                         (sid, rep, period_end, round(amount, 2), kind, n, ref,
                          str(date_)[:19] if date_ else None))
            if kind == "upsell":
                emitted.append({"subscription_id": sid, "rep": rep,
                                "amount": round(amount, 2), "kind": kind,
                                "state": "upsell", "flags": []})
        elif removed is not None and paid_row > EPS:
            conn.execute("""INSERT INTO ledger(subscription_id,rep,period_end,
                            amount,kind,note,ref,event_date) VALUES(?,?,?,?,'trueup',
                            'upsell credit removed',?,?)""",
                         (sid, rep, period_end, -round(paid_row, 2),
                          ref + ":reversal", local_from_utc(removed)))
            emitted.append({"subscription_id": sid, "rep": rep,
                            "amount": -round(paid_row, 2), "kind": "trueup",
                            "state": "upsell-reversal", "flags": []})
    if commit:
        conn.commit()
    return emitted
