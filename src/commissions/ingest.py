"""Pull the commission-relevant slice of FieldRoutes into SQLite.

Backfill: subscriptions sold by ISA reps since BACKFILL_SINCE, then the
full chain for each — customer (card on file), appointments, tickets,
payments (embedded applications give paid dates), cancel-reason notes.

Incremental sync: re-discover new subs by dateAdded, then re-refresh the
chain for every subscription not yet in a terminal state. Brute-force but
bounded and misses nothing (card flips, reactivations, late payments).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from .config import Settings, payees
from .fieldroutes import FieldRoutesClient, chunks
from .store import get_meta, set_meta

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- rep map

def load_rep_map(s: Settings) -> dict[str, str]:
    """{fr_employee_id: canonical roster name} seeded from the ISA
    dashboard's agent_map (on_roster=1). Roster = inbound ISAs + managers."""
    out: dict[str, str] = {}
    if not s.isadash_db.exists():
        log.warning("isadash db not found at %s — rep map empty", s.isadash_db)
        return out
    conn = sqlite3.connect(f"file:{s.isadash_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT genesys_name, fr_employee_id FROM agent_map "
            "WHERE on_roster=1 AND fr_employee_id IS NOT NULL").fetchall()
    finally:
        conn.close()
    roster_cf = {n.casefold(): n for n in payees(s)}
    for name, ids in rows:
        canonical = roster_cf.get((name or "").casefold())
        if canonical is None:      # dashboard roster, but not on OUR payroll
            continue
        for eid in (ids or "").split(","):
            eid = eid.strip()
            if eid:
                out[eid] = canonical
    return out


def clear_offroster_reps(conn: sqlite3.Connection, s: Settings) -> None:
    """NULL the rep tag on stored subs whose rep left the payroll roster
    (their rows aren't re-upserted, so they'd otherwise stay forever).
    Nothing is paid or deducted for them after that (RULES §1): removing a
    rep from the roster takes them off the statement."""
    names = payees(s)
    ph = ",".join("?" * len(names))
    # rep_off_roster remembers who sold it: rules that look at ANOTHER sale's
    # seller (the Bad-Debt-Renewal re-sell, §5) must not change for the other
    # reps just because this one left the roster
    n = conn.execute(f"UPDATE subscriptions SET rep_off_roster=rep, rep=NULL "
                     f"WHERE rep IS NOT NULL AND rep NOT IN ({ph})", names).rowcount
    conn.commit()
    if n:
        log.info("cleared rep tag on %d subs sold by off-roster reps", n)


def resolve_unknown_soldby(fr: FieldRoutesClient, s: Settings,
                           sold_by_ids: set[str],
                           rep_map: dict[str, str]) -> dict[str, str]:
    """employee/get any soldBy ids not in the map; add roster name matches."""
    unknown = [i for i in sold_by_ids if i and i != "0" and i not in rep_map]
    if not unknown:
        return rep_map
    roster_cf = {n.casefold(): n for n in payees(s)}
    names = fr.get_employees(unknown)
    added = 0
    for eid, name in names.items():
        canonical = roster_cf.get(name.casefold())
        if canonical:
            rep_map[eid] = canonical
            added += 1
    if added:
        log.info("resolved %d additional FR employee ids to roster reps", added)
    return rep_map


# ------------------------------------------------------------ service types

def refresh_service_types(fr: FieldRoutesClient, conn: sqlite3.Connection) -> None:
    got = fr.request("serviceType", "get",
                     {"typeIDs": ",".join(str(i) for i in range(1, 200))})
    rows = [(str(t["typeID"]), (t.get("description") or "").strip(),
             int(t.get("reservice") or 0), int(t.get("initial") or 0),
             str(t.get("officeID")))
            for t in got.get("serviceTypes") or []]
    conn.executemany(
        "INSERT INTO service_types(type_id,description,reservice,is_initial,office_id) "
        "VALUES(?,?,?,?,?) ON CONFLICT(type_id) DO UPDATE SET "
        "description=excluded.description, reservice=excluded.reservice, "
        "is_initial=excluded.is_initial, office_id=excluded.office_id", rows)
    conn.commit()
    log.info("service types refreshed: %d", len(rows))


# ------------------------------------------------------------- discovery

def discover_subscription_ids(fr: FieldRoutesClient, s: Settings,
                              since: str, until: str) -> list[str]:
    """All subscription ids added in [since, until], deduped across offices."""
    ids: list[str] = []
    for oid in s.office_ids:
        got = fr.search_ids("subscription", oid, "subscriptionIDs",
                            dateAdded={"operator": "BETWEEN", "value": [since, until]})
        log.info("office %s: %d subs added %s..%s", oid, len(got), since, until)
        ids.extend(got)
    return list(dict.fromkeys(ids))


# ------------------------------------------------------------ chain pulls

def _assigned_sub_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT subscription_id FROM assignments WHERE removed_at IS NULL")}


def upsert_subscriptions(conn: sqlite3.Connection, subs: list[dict],
                         rep_map: dict[str, str],
                         assigned: set[str] | None = None) -> list[dict]:
    """Store ISA-sold subscriptions (plus any with manual assignments, e.g.
    D2D rescues — rep stays NULL, attribution comes from the assignment);
    return the stored records.

    The credited rep is whoever FR's soldBy maps to NOW (RULES §1, the owner
    2026-11-03): a lead sold later by another rep moves with soldBy. A stored
    sub whose soldBy moves to someone OFF the roster loses its rep (set to
    NULL) but keeps being refreshed, with the ISA it was taken from kept in
    rep_prior so the engine reconciles that rep to $0 and the cancels,
    payments and edits on the sub are still seen (900029). soldby_rep_now
    keeps the current mapping ('' = nobody on the roster)."""
    assigned = assigned or set()
    ids = [str(r["subscriptionID"]) for r in subs]
    stored_rep: dict[str, str | None] = {}
    stored_prior: dict[str, str | None] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for sid_, rep_, prior_ in conn.execute(
                "SELECT subscription_id, rep, rep_prior FROM subscriptions WHERE "
                "subscription_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            stored_rep[sid_] = rep_
            stored_prior[sid_] = prior_
    kept = []
    rows = []
    moved = 0
    for r in subs:
        sid = str(r["subscriptionID"])
        rep_now = rep_map.get(str(r.get("soldBy")))
        rep = rep_now
        prior = stored_prior.get(sid)
        if rep is None and stored_rep.get(sid):
            # soldBy moved off the roster after we credited an ISA: the credit
            # goes with it; remember who held it so the payment is taken back
            prior = stored_rep[sid]
            moved += 1
        if rep is None and sid not in assigned and sid not in stored_rep:
            continue
        kept.append(r)
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        def _i(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        def _d(v):
            v = str(v or "")
            return None if not v or v.startswith("0000") else v
        rows.append((
            str(r["subscriptionID"]), str(r.get("customerID")), str(r.get("officeID")),
            str(r.get("soldBy")), rep, _d(r.get("dateAdded")),
            _f(r.get("contractValue")), r.get("source"),
            str(r.get("serviceID")), r.get("serviceType"),
            _i(r.get("frequency")), _i(r.get("billingFrequency")),
            _f(r.get("recurringCharge")), str(r.get("initialAppointmentID")),
            1 if str(r.get("active")) == "1" else 0,
            _d(r.get("dateCancelled")), _d(r.get("dateUpdated")),
            # '' = soldBy now maps to NO roster rep (NULL = not yet synced)
            _f(r.get("initialQuote")), _f(r.get("initialDiscount")), rep_now or "",
            prior,
        ))
    conn.executemany("""
        INSERT INTO subscriptions(subscription_id,customer_id,office_id,sold_by,rep,
          date_added,contract_value,source,service_id,service_type,frequency,
          billing_frequency,recurring_charge,initial_appointment_id,active,
          date_cancelled,date_updated,initial_quote,initial_discount,
          soldby_rep_now,rep_prior)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(subscription_id) DO UPDATE SET
          initial_quote=excluded.initial_quote,
          initial_discount=excluded.initial_discount,
          soldby_rep_now=excluded.soldby_rep_now,
          rep_prior=excluded.rep_prior,
          customer_id=excluded.customer_id, office_id=excluded.office_id,
          sold_by=excluded.sold_by, rep=excluded.rep,
          date_added=excluded.date_added, contract_value=excluded.contract_value,
          source=excluded.source, service_id=excluded.service_id,
          service_type=excluded.service_type, frequency=excluded.frequency,
          billing_frequency=excluded.billing_frequency,
          recurring_charge=excluded.recurring_charge,
          initial_appointment_id=excluded.initial_appointment_id,
          active=excluded.active,
          cancel_reason=CASE WHEN excluded.date_cancelled IS NOT
                                  subscriptions.date_cancelled
                             THEN NULL ELSE subscriptions.cancel_reason END,
          date_cancelled=excluded.date_cancelled,
          date_updated=excluded.date_updated""", rows)
    conn.commit()
    if moved:
        log.info("soldBy moved off the roster on %d stored subscription(s): "
                 "credit removed, payments reconciled at the next close", moved)
    return kept


def refresh_chain(fr: FieldRoutesClient, conn: sqlite3.Connection,
                  sub_records: list[dict]) -> None:
    """Given fresh subscription records (already ISA-filtered and stored),
    pull customers, appointments, tickets, payments for them."""
    sub_ids = {str(r["subscriptionID"]) for r in sub_records}
    cust_ids = list(dict.fromkeys(str(r.get("customerID")) for r in sub_records))
    appt_ids: set[str] = set()
    for r in sub_records:
        for k in ("appointmentIDs", "completedAppointmentIDs"):
            for a in str(r.get(k) or "").split(","):
                a = a.strip()
                if a and a != "0":
                    appt_ids.add(a)
        ia = str(r.get("initialAppointmentID") or "")
        if ia and ia != "0":
            appt_ids.add(ia)

    # customers: card-on-file + ticket/payment id lists
    ticket_ids: set[str] = set()
    payment_ids: set[str] = set()
    cust_rows = []
    for c in fr.get_customers(cust_ids):
        card = 1 if (str(c.get("autoPayPaymentProfileID") or "0").strip() not in ("", "0")
                     or (c.get("mostRecentCreditCardLastFour") or "").strip()) else 0
        apay = 0 if str(c.get("aPay") or "No").strip().lower() in ("no", "", "0") else 1
        name = f"{(c.get('fname') or '').strip()} {(c.get('lname') or '').strip()}".strip()
        # RULES §3: the card's property-type toggle (Commercial Property)
        comm = 1 if str(c.get("commercialAccount") or "0").strip() == "1" else 0
        cust_rows.append((str(c["customerID"]), str(c.get("officeID")), name, card, apay,
                          c.get("statusText"), str(c.get("dateUpdated") or "") or None, comm))
        for t in str(c.get("ticketIDs") or "").split(","):
            if t.strip():
                ticket_ids.add(t.strip())
        for p in str(c.get("paymentIDs") or "").split(","):
            if p.strip():
                payment_ids.add(p.strip())
    conn.executemany("""
        INSERT INTO customers(customer_id,office_id,name,card_on_file,autopay,status,
          date_updated,commercial)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(customer_id) DO UPDATE SET office_id=excluded.office_id,
          name=excluded.name, card_on_file=excluded.card_on_file,
          autopay=excluded.autopay, status=excluded.status,
          date_updated=excluded.date_updated,
          commercial=excluded.commercial""", cust_rows)
    conn.commit()

    # appointments
    appt_rows = []
    our_appts: set[str] = set()
    for a in fr.get_appointments(list(appt_ids)):
        sid = str(a.get("subscriptionID"))
        if sid not in sub_ids:
            continue
        our_appts.add(str(a["appointmentID"]))
        dc = str(a.get("dateCompleted") or "")
        appt_rows.append((str(a["appointmentID"]), sid, str(a.get("customerID")),
                          str(a.get("type")), a.get("statusText"), a.get("date"),
                          None if dc.startswith("0000") or not dc else dc,
                          str(a.get("ticketID") or "0"),
                          1 if str(a.get("isInitial")) == "1" else 0))
    conn.executemany("""
        INSERT INTO appointments(appointment_id,subscription_id,customer_id,type,
          status_text,date,date_completed,ticket_id,is_initial)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(appointment_id) DO UPDATE SET
          subscription_id=excluded.subscription_id, customer_id=excluded.customer_id,
          type=excluded.type, status_text=excluded.status_text, date=excluded.date,
          date_completed=excluded.date_completed, ticket_id=excluded.ticket_id,
          is_initial=excluded.is_initial""", appt_rows)
    conn.commit()

    # tickets: keep ones linked to our subs (via appointment or RECURRING
    # autogen), Cancellation Charge invoices, and AD-HOC customer-level
    # tickets (no appointment, not auto-generated) — CSRs sometimes bill the
    # cancellation fee as an untyped (serviceID 0) or plan-type ticket
    # (RULES §5; e.g. 900032 $291 serviceID 0). Fee-type tickets that are
    # never cancellation fees are skipped.
    cxl_type_ids = {r[0] for r in conn.execute(
        "SELECT type_id FROM service_types WHERE description LIKE '%ancellation%'")}
    not_fee_types = {r[0] for r in conn.execute(
        "SELECT type_id FROM service_types WHERE description IN "
        "('Bounced Check Fee','Reimbursement Fee','Collections Late Fee',"
        "'Service Fee','Overdraft Fee','Technician Tip','Collect Payment')")}
    kept_tickets: set[str] = set()
    ticket_rows = []
    for t in fr.get_tickets(list(ticket_ids)):
        tid = str(t["ticketID"])
        appt = str(t.get("appointmentID") or "0")
        autogen = str(t.get("autoGenerated") or "0")
        agt = t.get("autoGeneratedType")
        is_recurring = (agt == "RECURRING")
        svc = str(t.get("serviceID"))
        adhoc = (appt == "0" and agt not in ("RECURRING", "INITIAL")
                 and svc not in not_fee_types)
        linked = ((appt in our_appts) or (is_recurring and autogen in sub_ids)
                  or svc in cxl_type_ids or adhoc)
        if not linked:
            continue
        kept_tickets.add(tid)
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
        ticket_rows.append((tid, str(t.get("customerID")), appt,
                            autogen if is_recurring else None,
                            svc, _f(t.get("total")),
                            _f(t.get("balance")), t.get("invoiceDate"),
                            1 if str(t.get("active")) == "1" else 0,
                            _f(t.get("subTotal"))))
    conn.executemany("""
        INSERT INTO tickets(ticket_id,customer_id,appointment_id,autogen_sub_id,
          service_id,total,balance,invoice_date,active,sub_total)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticket_id) DO UPDATE SET customer_id=excluded.customer_id,
          appointment_id=excluded.appointment_id, autogen_sub_id=excluded.autogen_sub_id,
          service_id=excluded.service_id, total=excluded.total,
          balance=excluded.balance, invoice_date=excluded.invoice_date,
          active=excluded.active, sub_total=excluded.sub_total""", ticket_rows)
    conn.commit()

    # every other subscription on customers that carry a possible fee ticket,
    # so the fee is matched to the right cancellation (RULES §5)
    fee_custs = {str(t[1]) for t in ticket_rows
                 if t[4] in cxl_type_ids or (t[2] == "0" and t[3] is None)}
    if fee_custs:
        # skip only subs the sync keeps current (roster rep or an assignment);
        # rep-NULL rows (off-roster, live imports) go stale in subscriptions,
        # so their cancellations must come through customer_subs
        stored = {r[0] for r in conn.execute(
            "SELECT subscription_id FROM subscriptions WHERE rep IS NOT NULL "
            "OR rep_prior IS NOT NULL "
            "OR subscription_id IN (SELECT subscription_id FROM assignments "
            "WHERE removed_at IS NULL)")}
        other_ids = []
        for c in fr.get_customers(list(fee_custs)):
            for x in str(c.get("subscriptionIDs") or "").split(","):
                x = x.strip()
                if x and x != "0" and x not in stored:
                    other_ids.append(x)
        if other_ids:
            def _dd(v):
                v = str(v or "")
                return None if not v or v.startswith("0000") else v
            conn.executemany("""
                INSERT INTO customer_subs(subscription_id,customer_id,date_added,
                  date_cancelled,active) VALUES(?,?,?,?,?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                  customer_id=excluded.customer_id, date_added=excluded.date_added,
                  date_cancelled=excluded.date_cancelled, active=excluded.active""",
                [(str(r["subscriptionID"]), str(r.get("customerID")),
                  _dd(r.get("dateAdded")), _dd(r.get("dateCancelled")),
                  1 if str(r.get("active")) == "1" else 0)
                 for r in fr.get_subscriptions(list(dict.fromkeys(other_ids)))])
            conn.commit()

    # payments -> paid dates, write-off taint, coupon dollars for our tickets
    paid_date: dict[str, str] = {}
    tainted: set[str] = set()
    coupon: dict[str, float] = {}
    apps_by_ticket: dict[str, list[tuple[str, float]]] = defaultdict(list)
    total_by_ticket = {r[0]: r[5] for r in ticket_rows}
    payments = list(fr.get_payments(list(payment_ids)))
    # a VOIDED charge never happened: FR adds a negative twin noted 'Voided'
    # whose originalPaymentID is the charge. Drop both, so only money that
    # stuck can date an invoice (900042: two card charges voided on 9/12,
    # paid by check 9/24). Refunds and reversals are not voids — see below.
    by_pid = {str(p.get("paymentID")): p for p in payments}
    voided: set[str] = set()
    for p in payments:
        orig = str(p.get("originalPaymentID") or "")
        if (orig and orig in by_pid
                and str(p.get("notes") or "").strip().lower() == "voided"):
            voided |= {str(p.get("paymentID")), orig}
    for p in payments:
        if str(p.get("paymentID")) in voided:
            continue
        wo = str(p.get("writeOff") or "0") == "1"
        is_coupon = str(p.get("paymentMethod")) == "0"   # Coupon/credit (§4b)
        pdate = str(p.get("date") or "")
        for app in p.get("paymentApplications") or []:
            tid = str(app.get("ticketID"))
            if tid not in kept_tickets:
                continue
            if wo:
                tainted.add(tid)
            try:
                applied = float(app.get("appliedAmount") or 0)
            except (TypeError, ValueError):
                applied = 0.0
            if is_coupon:
                coupon[tid] = coupon.get(tid, 0.0) + applied
            da = str(app.get("dateApplied") or "")
            if not da or da.startswith("0000"):
                continue
            # the money is received when the customer pays, even if FR
            # applies it to an invoice created days later (a fee taken by
            # card at the cancel call, invoiced afterwards: 900037)
            when = min(da, pdate) if pdate and not pdate.startswith("0000") else da
            apps_by_ticket[tid].append((when, applied))
    # paid date = the FIRST moment the applications covered the invoice.
    # A later refund/coupon swap never moves it (900028: card 6/22, swapped
    # to a coupon 9/9 — paid 6/22), and a refund is never a payment date.
    for tid, apps in apps_by_ticket.items():
        apps.sort()
        total, cum, first_full, last_pos = total_by_ticket.get(tid) or 0.0, 0.0, None, None
        for when, amt in apps:
            cum += amt
            if amt > 0:
                last_pos = when
            if first_full is None and total > 0 and cum >= total - 0.01:
                first_full = when
        if first_full or last_pos:
            paid_date[tid] = first_full or last_pos
    conn.executemany("UPDATE tickets SET paid_date=? WHERE ticket_id=?",
                     [(d, t) for t, d in paid_date.items()])
    # every payment of these customers was fetched: no surviving application
    # means no paid date (a ticket whose only charges were voided)
    conn.executemany("UPDATE tickets SET paid_date=NULL WHERE ticket_id=?",
                     [(t,) for t in kept_tickets if t not in paid_date])
    conn.executemany("UPDATE tickets SET writeoff_tainted=1 WHERE ticket_id=?",
                     [(t,) for t in tainted])
    conn.executemany("UPDATE tickets SET coupon_paid=? WHERE ticket_id=?",
                     [(round(v, 2), t) for t, v in coupon.items()])
    # reset coupon_paid to 0 for refreshed tickets with no coupon applications
    if kept_tickets:
        no_coupon = [t for t in kept_tickets if t not in coupon]
        conn.executemany("UPDATE tickets SET coupon_paid=0 WHERE ticket_id=?",
                         [(t,) for t in no_coupon])
    conn.commit()
    log.info("chain: %d custs, %d appts, %d tickets (%d paid-dated, %d writeoff), %d payments",
             len(cust_rows), len(appt_rows), len(ticket_rows), len(paid_date),
             len(tainted), len(payment_ids))


# ------------------------------------------------------ re-signed check

def refresh_replacements(fr: FieldRoutesClient, conn: sqlite3.Connection,
                         s: Settings) -> None:
    """RULES §5 re-signed exclusion: for recently-cancelled, unvested,
    commissioned subs, look for an ACTIVE replacement subscription on the
    same customer (any seller) added within -14d..+90d of the cancel."""
    rows = conn.execute("""
        SELECT sub.subscription_id, sub.customer_id, sub.office_id,
               sub.date_cancelled
        FROM subscriptions sub
        JOIN sub_status st ON st.subscription_id = sub.subscription_id
        WHERE sub.active = 0 AND sub.date_cancelled >= ?
          AND st.first_paid_date IS NOT NULL AND st.vested_date IS NULL
          AND LOWER(COALESCE(sub.cancel_reason,'')) NOT IN ('renewal','moved')
          AND sub.date_cancelled >= date('now', '-120 days')
          AND sub.subscription_id NOT IN
              (SELECT r.subscription_id FROM replacements r
               JOIN subscriptions s2 ON s2.subscription_id = r.subscription_id
               WHERE r.replacement_sub_id IS NOT NULL
                 -- found against THIS cancellation; a row from an earlier
                 -- cancel is re-checked after a reinstate + re-cancel
                 AND julianday(r.replacement_added)
                     BETWEEN julianday(substr(s2.date_cancelled,1,10)) - 14
                         AND julianday(substr(s2.date_cancelled,1,10)) + 90)""",
        (s.go_live_period_start,)).fetchall()
    if not rows:
        return
    log.info("re-signed check: %d cancelled subs", len(rows))
    found = 0
    for sid, cid, oid, dc in rows:
        try:
            ids = fr.search_ids("subscription", oid, "subscriptionIDs",
                                customerIDs=cid)
        except Exception as e:
            log.warning("replacement search failed for %s: %s", sid, e)
            continue
        others = [i for i in ids if i != sid]
        repl = None
        if others:
            lo = (date.fromisoformat(dc[:10]) - timedelta(days=14)).isoformat()
            hi = (date.fromisoformat(dc[:10]) + timedelta(days=90)).isoformat()
            for r in fr.get_subscriptions(others):
                added = str(r.get("dateAdded") or "")[:10]
                if str(r.get("active")) == "1" and lo <= added <= hi:
                    repl = r
                    break
        conn.execute("""INSERT INTO replacements(subscription_id,
            replacement_sub_id, replacement_added, replacement_soldby, checked_at)
            VALUES(?,?,?,?,datetime('now'))
            ON CONFLICT(subscription_id) DO UPDATE SET
            replacement_sub_id=excluded.replacement_sub_id,
            replacement_added=excluded.replacement_added,
            replacement_soldby=excluded.replacement_soldby,
            checked_at=excluded.checked_at""",
            (sid, str(repl["subscriptionID"]) if repl else None,
             str(repl.get("dateAdded"))[:10] if repl else None,
             str(repl.get("soldBy")) if repl else None))
        if repl:
            found += 1
    conn.commit()
    log.info("re-signed: %d replacements found", found)


# ------------------------------------------------------- cancel reasons

def refresh_cancel_reasons(fr: FieldRoutesClient, conn: sqlite3.Connection) -> None:
    """For our cancelled subs missing a reason, find the 'Subscription Cancel'
    note (typeID 16, referenceID = subscriptionID) via per-customer note
    search windowed on the cancel date."""
    rows = conn.execute("""
        SELECT s.subscription_id, s.customer_id, s.office_id, s.date_cancelled
        FROM subscriptions s
        WHERE s.date_cancelled IS NOT NULL AND s.cancel_reason IS NULL
          AND (s.rep IS NOT NULL OR s.rep_prior IS NOT NULL OR s.subscription_id IN
               (SELECT subscription_id FROM assignments WHERE removed_at IS NULL))""").fetchall()
    if not rows:
        return
    log.info("fetching cancel reasons for %d subscriptions", len(rows))
    # group by (office, customer) so one search covers all a customer's cancels
    by_cust: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for sid, cid, oid, dc in rows:
        by_cust[(oid, cid)].append((sid, dc))
    found = 0
    for (oid, cid), subs in by_cust.items():
        days = [dc[:10] for _, dc in subs if dc]
        lo, hi = min(days), max(days)
        # widen by a day each side; FR datetimes are office-local
        lo = (date.fromisoformat(lo) - timedelta(days=1)).isoformat()
        hi = (date.fromisoformat(hi) + timedelta(days=1)).isoformat()
        try:
            note_ids = fr.search_ids("note", oid, "noteIDs", customerIDs=cid,
                                     dateAdded={"operator": "BETWEEN", "value": [lo, hi]})
        except Exception as e:
            log.warning("note search failed for customer %s: %s", cid, e)
            continue
        if not note_ids:
            continue
        want = {sid for sid, _ in subs}
        latest: dict[str, dict] = {}
        for n in fr.get_notes(note_ids):
            if str(n.get("typeID")) != "16":
                continue
            ref = str(n.get("referenceID") or "")
            if ref in want and _note_key(n) > _note_key(latest.get(ref)):
                latest[ref] = n           # several cancels in the window: latest
        for ref, n in latest.items():
            _set_reason(conn, ref, n)
            found += 1
        conn.commit()
    log.info("cancel reasons resolved: %d/%d", found, len(rows))


def _note_key(n: dict | None) -> tuple:
    if not n:
        return ("", 0)
    try:
        nid = int(n.get("noteID") or 0)
    except (TypeError, ValueError):
        nid = 0
    return (str(n.get("dateAdded") or ""), nid)


def _set_reason(conn: sqlite3.Connection, sid: str, n: dict) -> None:
    conn.execute("UPDATE subscriptions SET cancel_reason=?, cancel_note_id=?, "
                 "cancel_note_added=? WHERE subscription_id=?",
                 ((n.get("cancellationReason") or "").strip() or "(none)",
                  str(n.get("noteID") or ""), str(n.get("dateAdded") or ""), sid))


def sweep_cancel_notes(fr: FieldRoutesClient, conn: sqlite3.Connection,
                       s: Settings, since: str, until: str) -> bool:
    """Pick up 'Subscription Cancel' notes ADDED or EDITED since the last
    sweep (RULES §5: the latest cancellation's reason governs). Catches a
    reason corrected after the fact and a sub reinstated and cancelled
    again (900016: Renewal 6/03 -> reinstated 10/01 -> Out of Contract 10/05).
    An edit to an OLDER cancellation's note never overrides a newer one.
    Returns False if any office's search failed (the caller then keeps the
    sweep watermark where it was, so those edits are searched again)."""
    stored: dict[str, str] = {}
    for sid, added, reason, dc in conn.execute(
            "SELECT subscription_id, cancel_note_added, cancel_reason, date_cancelled "
            "FROM subscriptions WHERE date_cancelled IS NOT NULL"):
        if added:
            stored[sid] = added
        elif reason:
            # reason set before note ids were tracked: only a note from about
            # the current cancellation onward may replace it
            d = _day_str(dc, -1)
            stored[sid] = d
        else:
            stored[sid] = ""                 # no reason yet: any note fills it
    note_ids: list[str] = []
    ok = True
    for oid in s.office_ids:
        try:
            note_ids += fr.search_ids(
                "note", oid, "noteIDs", typeIDs=[16],
                dateUpdated={"operator": "BETWEEN", "value": [since, until]})
        except Exception as e:      # noqa: BLE001
            ok = False
            log.warning("cancel-note sweep failed for office %s: %s", oid, e)
    if not note_ids:
        return ok
    latest: dict[str, dict] = {}
    for n in fr.get_notes(list(dict.fromkeys(note_ids))):
        if str(n.get("typeID")) != "16":
            continue
        ref = str(n.get("referenceID") or "")
        if ref in stored and _note_key(n) > _note_key(latest.get(ref)):
            latest[ref] = n
    changed = 0
    for ref, n in latest.items():
        if str(n.get("dateAdded") or "") >= stored[ref]:
            before = conn.execute("SELECT cancel_reason FROM subscriptions "
                                  "WHERE subscription_id=?", (ref,)).fetchone()[0]
            _set_reason(conn, ref, n)
            new = (n.get("cancellationReason") or "").strip() or "(none)"
            if before != new:
                changed += 1
                log.info("cancel reason %s: %r -> %r (note %s)", ref, before,
                         new, n.get("noteID"))
    conn.commit()
    log.info("cancel-note sweep: %d notes touched, %d reasons changed",
             len(latest), changed)
    return ok


def _day_str(dt: str | None, offset_days: int) -> str:
    """'2026-10-05 10:15:41', -1 -> '2026-10-04' ('' when unparseable)."""
    try:
        return (date.fromisoformat(str(dt)[:10]) + timedelta(days=offset_days)).isoformat()
    except ValueError:
        return ""


# ------------------------------------------------------------- top level

def backfill(fr: FieldRoutesClient, conn: sqlite3.Connection, s: Settings,
             since: str, until: str) -> None:
    refresh_service_types(fr, conn)
    rep_map = load_rep_map(s)
    log.info("rep map: %d FR employee ids across %d reps",
             len(rep_map), len(set(rep_map.values())))
    clear_offroster_reps(conn, s)
    assigned = _assigned_sub_ids(conn)
    ids = list(dict.fromkeys(
        discover_subscription_ids(fr, s, since, until) + sorted(assigned)))
    log.info("total candidate subscriptions: %d", len(ids))
    kept_total = 0
    for batch in chunks(ids, 2000):
        subs = fr.get_subscriptions(batch)
        rep_map = resolve_unknown_soldby(
            fr, s, {str(r.get("soldBy")) for r in subs}, rep_map)
        kept = upsert_subscriptions(conn, subs, rep_map, assigned)
        kept_total += len(kept)
        if kept:
            refresh_chain(fr, conn, kept)
        log.info("progress: %d ISA subs stored so far (api calls: %d)",
                 kept_total, fr.calls)
    refresh_cancel_reasons(fr, conn)
    log.info("backfill complete: %d ISA-sold subscriptions", kept_total)


def sync(fr: FieldRoutesClient, conn: sqlite3.Connection, s: Settings,
         new_since: str, until: str) -> None:
    """Incremental: discover new subs, then re-refresh every non-terminal sub."""
    refresh_service_types(fr, conn)
    rep_map = load_rep_map(s)
    clear_offroster_reps(conn, s)
    assigned = _assigned_sub_ids(conn)
    ids = discover_subscription_ids(fr, s, new_since, until)
    # late attribution: a sub sold under a non-ISA employee and re-assigned to
    # an ISA rep AFTER its discovery window passed would otherwise never be
    # seen (acct 900038: soldBy set to Kendall 3 hours after the 10/12 sync).
    # Re-check every sub added in the last 60 days that we don't store yet.
    from datetime import date as _date, timedelta as _td
    lookback = (_date.fromisoformat(until[:10]) - _td(days=60)).isoformat()
    have = {r[0] for r in conn.execute("SELECT subscription_id FROM subscriptions")}
    have |= set(ids)
    late = [i for i in discover_subscription_ids(fr, s, lookback, until)
            if i not in have]
    log.info("late-attribution recheck: %d unstored subs added since %s",
             len(late), lookback)
    ids = ids + late
    open_ids = [r[0] for r in conn.execute("""
        SELECT s.subscription_id FROM subscriptions s
        LEFT JOIN sub_status st ON st.subscription_id = s.subscription_id
        WHERE (s.rep IS NOT NULL OR s.rep_prior IS NOT NULL OR s.subscription_id IN
               (SELECT subscription_id FROM assignments WHERE removed_at IS NULL))
          AND COALESCE(st.state,'') NOT IN ('vested','excluded_renewal')""")]
    all_ids = list(dict.fromkeys(ids + open_ids + sorted(assigned)))
    log.info("sync: %d new-window + %d open = %d subscriptions to refresh",
             len(ids), len(open_ids), len(all_ids))
    for batch in chunks(all_ids, 2000):
        subs = fr.get_subscriptions(batch)
        rep_map = resolve_unknown_soldby(
            fr, s, {str(r.get("soldBy")) for r in subs}, rep_map)
        kept = upsert_subscriptions(conn, subs, rep_map, assigned)
        if kept:
            refresh_chain(fr, conn, kept)
    # lightweight pass (subscription record only — no customer/card/ticket
    # refresh, so a frozen commission is never re-rated): vested subs whose
    # active/cancel/CV state must not go stale (Padron/Tran cases), plus EVERY
    # renewal-excluded sub, active or not — FR can reinstate it and cancel it
    # again for a deductible reason (900016), and the exclusion only holds
    # while that Renewal cancellation stands (RULES §5).
    light_ids = [r[0] for r in conn.execute(
        """SELECT s.subscription_id FROM subscriptions s
           JOIN sub_status st USING(subscription_id)
           WHERE (st.state = 'vested' AND s.active = 1)
              OR st.state = 'excluded_renewal'""")]
    if light_ids:
        before = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT subscription_id, active, date_cancelled FROM subscriptions")}
        recs = fr.get_subscriptions(light_ids)
        upsert_subscriptions(conn, recs, rep_map, {str(r["subscriptionID"]) for r in recs})
        # reinstated or re-cancelled since we last looked: pull its tickets,
        # payments and appointments too, so a fee paid or a service done
        # after the reinstatement is seen in this same run (900020)
        changed = [r for r in recs if before.get(str(r["subscriptionID"])) != tuple(
            conn.execute("SELECT active, date_cancelled FROM subscriptions "
                         "WHERE subscription_id=?", (str(r["subscriptionID"]),)).fetchone() or ())]
        if changed:
            refresh_chain(fr, conn, changed)
        log.info("light-refreshed %d vested / renewal-excluded subs (%d changed, "
                 "full refresh)", len(recs), len(changed))
    # reasons AFTER every upsert, so a re-cancel found by the light pass gets
    # its reason in this same run; then the edited/added-note sweep from its
    # own watermark (advanced only when every office's search succeeded)
    refresh_cancel_reasons(fr, conn)
    sweep_from = min(get_meta(conn, "cancel_note_sweep_since") or new_since, new_since)
    if sweep_cancel_notes(fr, conn, s, sweep_from, until):
        set_meta(conn, "cancel_note_sweep_since", until[:10])
    # re-signed scan LAST: it needs the current reasons — run before them, a
    # brand-new Renewal cancel (reason not yet known) would get a re-signed
    # row that later protects a deductible re-cancel
    refresh_replacements(fr, conn, s)
