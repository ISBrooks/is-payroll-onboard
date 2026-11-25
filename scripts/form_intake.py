"""Form-intake pipeline: adjustment submissions -> verified bookings.

Two intake sources, one booking brain:

1. 'Form Responses 1' on the hub spreadsheet (the Google Form) — free-text
   rows that need parsing and nickname resolution.
2. The intake_queue table (the dashboard's Payroll -> Submit Adjustment
   form) — already structured: exact roster names from dropdowns, a typed
   adjustment kind, often the exact subscription picked in the UI.

Both are verified against the engine database (freshly synced from
PestRoutes); unambiguous rows book, everything else HOLDS for the owner and
retries next run. Nothing ambiguous ever books silently.

  python scripts/form_intake.py              # queue + Google Form rows
  python scripts/form_intake.py --queue-only # dashboard queue only (no network
                                             # to Google; used by the dashboard
                                             # to book right after a submit)
  python scripts/form_intake.py --dry-run    # verify + report only
  python scripts/form_intake.py --since "2026-01-03 23:59:59"

Run scripts/sync.py first so verification sees current PestRoutes data.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# status glyphs (✓/⚠) must survive a cp1252 console or redirected log file
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from commissions.config import is_commercial, load_settings
from commissions.store import connect



def default_watermark(s) -> str:
    """Form rows on or before this were booked by the manual payroll (they are in
    its loaded history): FORM_INTAKE_SINCE in .env, else the end of the last
    manual pay period (the day before PAYROLL_LIVE_FROM)."""
    from datetime import date, timedelta
    v = (os.environ.get("FORM_INTAKE_SINCE") or "").strip()
    if v:
        return v
    return (date.fromisoformat(s.payroll_live_from) - timedelta(days=1)).isoformat() + " 23:59:59"
SPLIT_WINDOW_DAYS = 60       # a split's sub must be sold within this window


# ------------------------------------------------------------- parsing -----
def parse_ts(v) -> str | None:
    """Sheet timestamp ('M/D/YYYY H:MM:SS') -> sortable 'YYYY-MM-DD HH:MM:SS'."""
    s = str(v or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def parse_acv(v) -> float | None:
    """'165', '165 difference in acv', 192.28 -> float."""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return float(m.group()) if m else None


def split_agents(field: str) -> list[str]:
    """'Rowan and Kendall' / 'Hayden/Rowan' / 'Kendall, Lennon' / 'A & B' -> tokens."""
    return [t.strip() for t in
            re.split(r"\band\b|&|/|,|\+", str(field or ""), flags=re.I) if t.strip()]


def resolver(roster: list[str], outbound: list[str]):
    """token -> (full_name, kind) where kind in {'inbound','outbound'};
    None when unknown, raises nothing, never guesses on ambiguity."""
    def resolve(token: str):
        t = re.sub(r"\s+", " ", token).strip().casefold()
        if not t:
            return None
        for pool, kind in ((roster, "inbound"), (outbound, "outbound")):
            exact = [n for n in pool if n.casefold() == t]
            if exact:
                return exact[0], kind
        # first-name (or any-prefix) match, unique across both pools
        hits = []
        for pool, kind in ((roster, "inbound"), (outbound, "outbound")):
            for n in pool:
                first = n.split()[0].casefold()
                if first == t or first.startswith(t) or n.casefold().startswith(t):
                    hits.append((n, kind))
        uniq = {h[0]: h for h in hits}
        return next(iter(uniq.values())) if len(uniq) == 1 else None
    return resolve


# ---------------------------------------------------------- verification ---
def candidate_subs(conn, cust: str, since_days_of: str) -> list[dict]:
    """Customer's subscriptions from the engine DB, newest first, with state."""
    rows = conn.execute("""
        SELECT sub.subscription_id, sub.rep, sub.service_type, sub.source,
               sub.contract_value, sub.date_added, sub.active,
               sub.date_cancelled, st.state, st.rate, st.first_paid_date,
               st.vested_date, sub.sold_by, COALESCE(cu.commercial, 0)
        FROM subscriptions sub LEFT JOIN sub_status st USING(subscription_id)
        LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
        WHERE sub.customer_id = ?
        ORDER BY sub.date_added DESC""", (cust,)).fetchall()
    keys = ("subscription_id", "rep", "service_type", "source", "cv",
            "date_added", "active", "date_cancelled", "state", "rate",
            "first_paid", "vested", "sold_by", "commercial")
    return [dict(zip(keys, r)) for r in rows]


def upsell_rate(s, sub: dict) -> float:
    """RULES §12: 9% of the ACV increase; 5% on a commercial account (§3)."""
    return (s.rate_commercial if is_commercial(s, sub.get("service_type"),
                                               sub.get("commercial"))
            else 0.09)


_D2D_CACHE: dict[str, str | None] = {}


def d2d_host_tag(s, sold_by) -> str | None:
    """The host sub's seller as FieldRoutes labels them, IF they are a
    year-tagged D2D rep (e.g. 'R- Pat Example 2026', 'V- Sam Example 2026') —
    those sales fall under D2D rules, and upsells on them feed the year-end
    D2D finance report. None for ISA/office sellers. Employee names come
    from the dashboard's DB (read-only)."""
    sid = str(sold_by or "").strip()
    if not sid or sid == "0":
        return None
    if sid in _D2D_CACHE:
        return _D2D_CACHE[sid]
    import sqlite3 as _sq
    name = ""
    try:
        d = _sq.connect(f"file:{s.isadash_db.as_posix()}?mode=ro", uri=True)
        row = d.execute("SELECT name FROM employees WHERE fr_employee_id = ?",
                        (sid,)).fetchone()
        d.close()
        name = (row[0] if row else "") or ""
    except _sq.Error:
        return None                 # dashboard DB unavailable: no tag, no harm
    tag = name.strip() if re.search(r"\b20\d\d\b", name) else None
    _D2D_CACHE[sid] = tag
    return tag


def live_import_account(s, conn, cust: str) -> int:
    """Upsell on an account the engine doesn't track (e.g. D2D-sold): pull
    the customer's subscriptions straight from FieldRoutes and store them
    with rep=NULL (never ISA-paid; host only for the one-time upsell credit).
    Sync never deletes subscription rows, so the import is permanent."""
    import json as _json
    from commissions.fieldroutes import FieldRoutesClient
    fr = FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token, max_reads_per_min=300)
    try:
        sub_ids: list[str] = []
        for oid in s.office_ids:
            try:
                got = fr.request("subscription", "search",
                                 {"officeIDs": oid,
                                  "customerIDs": _json.dumps([int(cust)])})
                if any(k == "customerIDs" for k in (got.get("ignoredParams") or {})):
                    return 0        # never trust an unfiltered search
                sub_ids += [str(x) for x in (got.get("subscriptionIDs") or [])]
            except Exception:       # noqa: BLE001 — per-office failure is fine
                continue
        sub_ids = list(dict.fromkeys(sub_ids))
        if not sub_ids:
            return 0
        n = 0
        for r in fr.get_subscriptions(sub_ids):
            def _f(v):
                try: return float(v)
                except (TypeError, ValueError): return None
            def _i(v):
                try: return int(v)
                except (TypeError, ValueError): return None
            def _d(v):
                v = str(v or "")
                return None if not v or v.startswith("0000") else v
            conn.execute("""INSERT INTO subscriptions(subscription_id,customer_id,
                office_id,sold_by,rep,date_added,contract_value,source,service_id,
                service_type,frequency,billing_frequency,recurring_charge,
                initial_appointment_id,active,date_cancelled,date_updated)
                VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                  active=excluded.active, date_cancelled=excluded.date_cancelled,
                  date_updated=excluded.date_updated
                WHERE subscriptions.rep IS NULL AND subscriptions.subscription_id
                  NOT IN (SELECT subscription_id FROM assignments)""",
                (str(r["subscriptionID"]), str(r.get("customerID")),
                 str(r.get("officeID")), str(r.get("soldBy")),
                 _d(r.get("dateAdded")), _f(r.get("contractValue")),
                 r.get("source"), str(r.get("serviceID")), r.get("serviceType"),
                 _i(r.get("frequency")), _i(r.get("billingFrequency")),
                 _f(r.get("recurringCharge")), str(r.get("initialAppointmentID")),
                 1 if str(r.get("active")) == "1" else 0,
                 _d(r.get("dateCancelled")), _d(r.get("dateUpdated"))))
            n += 1
        for c in fr.get_customers([cust, cust]):
            if str(c.get("customerID")) != str(cust):
                continue
            card = 1 if (str(c.get("autoPayPaymentProfileID") or "") not in ("", "0")
                         or str(c.get("mostRecentCreditCardLastFour") or "")) else 0
            comm = 1 if str(c.get("commercialAccount") or "0").strip() == "1" else 0
            conn.execute("""INSERT INTO customers(customer_id,office_id,name,
                card_on_file,autopay,status,date_updated,commercial)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(customer_id) DO UPDATE SET
                commercial=excluded.commercial""",
                (str(c.get("customerID")), str(c.get("officeID")),
                 ((c.get("fname") or "") + " " + (c.get("lname") or "")).strip(),
                 card, str(c.get("aPay") or ""), str(c.get("status") or ""),
                 str(c.get("dateUpdated") or ""), comm))
        conn.commit()
        return n
    finally:
        fr.close()


def serviced(conn, sid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM appointments WHERE subscription_id=? "
        "AND date_completed IS NOT NULL LIMIT 1", (sid,)).fetchone() is not None


def active_assignment(conn, sid: str) -> list[tuple]:
    return conn.execute(
        "SELECT rep, share, type FROM assignments "
        "WHERE subscription_id=? AND removed_at IS NULL", (sid,)).fetchall()


# ---------------------------------------------------------------- booking --
def book_assignment(conn, sid: str, pairs: list[tuple[str, float]],
                    typ: str, note: str) -> str:
    """Returns the booking's reference ('assign:<id>,<id>')."""
    conn.execute("UPDATE assignments SET removed_at=datetime('now') "
                 "WHERE subscription_id=? AND removed_at IS NULL", (sid,))
    ids = []
    for rep, frac in pairs:
        ids.append(conn.execute(
            "INSERT INTO assignments(subscription_id,rep,share,type,note) "
            "VALUES(?,?,?,?,?)", (sid, rep, frac, typ, note)).lastrowid)
    return "assign:" + ",".join(str(i) for i in ids)


def book_upsell(conn, sid: str, rep: str, amount: float, acv: float,
                date: str, d2d: str | None, note: str,
                d2d_host: str | None = None) -> str:
    """Returns the booking's reference ('upsell:<id>')."""
    uid = conn.execute("INSERT INTO upsells(subscription_id,rep,amount,acv_increase,"
                       "date,d2d_rep,d2d_host,note) VALUES(?,?,?,?,?,?,?,?)",
                       (sid, rep, amount, acv, date, d2d, d2d_host, note)).lastrowid
    return f"upsell:{uid}"


# ------------------------------------------- dashboard intake queue --------
# The dashboard's Submit Adjustment form inserts rows here (status NEW);
# this script books them and writes the outcome back. HELD rows retry on
# every run, exactly like held Google Form rows.
QUEUE_DDL = """CREATE TABLE IF NOT EXISTS intake_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at TEXT DEFAULT (datetime('now','localtime')),
    submitted_by TEXT,
    type TEXT NOT NULL,          -- split | outbound | rescue | upsell | tech_upsell
    account TEXT NOT NULL,
    agent1 TEXT,                 -- the (first) inbound ISA rep
    agent2 TEXT,                 -- split: 2nd ISA; outbound: the outbound agent
    tech TEXT,                   -- tech_upsell: tech; rescue/upsell: D2D rep tag
    branch TEXT,
    acv REAL,                    -- upsell types: the ACV change
    sub_id_hint TEXT,            -- subscription picked in the dashboard UI
    note TEXT,
    status TEXT DEFAULT 'NEW',   -- NEW | BOOKED ... | ALREADY BOOKED | HELD | DELETED
    detail TEXT, est_amount REAL, rep TEXT, sub_id TEXT,
    processed_at TEXT,
    booked_ref TEXT)             -- 'assign:<id>,..' / 'upsell:<id>' it booked"""

QUEUE_LABEL = {"split": "Inside sales split", "outbound": "Outbound split",
               "rescue": "D2D rescue", "upsell": "Upsell",
               "tech_upsell": "Tech referral upsell"}


def process_queue(conn, s, dry_run: bool) -> tuple[int, int]:
    """Book NEW/HELD dashboard submissions. Returns (booked, held)."""
    rows = conn.execute("""SELECT id, submitted_at, submitted_by, type, account,
        agent1, agent2, tech, branch, acv, sub_id_hint, note
        FROM intake_queue WHERE status IN ('NEW','HELD') ORDER BY id""").fetchall()
    booked = held = 0
    for (qid, ts, by, typ, acct, a1, a2, tech, branch, acv, hint, unote) in rows:
        status, detail, est, rep_out, sid_out, svc = "HELD", "", None, "", "", 0
        ref = None                        # what this row booked (for delete)
        a1, a2 = (a1 or "").strip(), (a2 or "").strip()
        tech = (tech or "").strip()
        subs = candidate_subs(conn, acct, ts)
        if (typ in ("upsell", "tech_upsell") and not dry_run
                and not any(x["active"] for x in subs)):
            # the host isn't tracked: the customer has no ISA sale here, or
            # only cancelled/frozen ones while the active plan was sold by
            # someone else (900031: active plan sold by a D2D rep)
            n = live_import_account(s, conn, acct)
            if n:
                subs = candidate_subs(conn, acct, ts)
                print(f"    · live-imported {n} subscription(s) for acct {acct}")
        hinted = [x for x in subs if hint and x["subscription_id"] == str(hint)]
        base_note = f"dashboard {ts} by {by or '?'}: {QUEUE_LABEL.get(typ, typ)}"
        if unote:
            base_note += f" — {unote}"

        def within_window(x) -> bool:
            try:
                return (datetime.strptime(ts[:10], "%Y-%m-%d")
                        - datetime.strptime(x["date_added"][:10], "%Y-%m-%d")
                        ).days <= SPLIT_WINDOW_DAYS
            except (TypeError, ValueError):
                return False

        if not subs:
            detail = ("account not in engine DB — not ISA-sold, or sold after "
                      "the last sync; will retry next run")
            if typ in ("upsell", "tech_upsell") and not dry_run:
                detail = ("account not found in FieldRoutes either — check the "
                          "account number")
        elif typ in ("split", "outbound", "rescue"):
            # a1 (and a split's a2) must be engine-paid ISAs; an outbound a2
            # is paid by the outbound process, so only the dashboard's roster
            # dropdown vouches for it
            missing = [a for a in ((a1, a2) if typ == "split" else (a1,))
                       if a and a not in s.isa_roster]
            if not a1 or (typ in ("split", "outbound") and not a2):
                detail = "missing agent name(s) on the submission"
            elif missing:
                detail = (f"{', '.join(missing)} not in the engine's ISA_ROSTER "
                          "(.env) — add them there, then this retries")
            else:
                if hinted:
                    cands = hinted
                elif typ == "rescue":      # D2D-sold: soldBy is not a roster rep
                    cands = [x for x in subs if within_window(x)]
                else:
                    cands = ([x for x in subs if x["rep"] == a1]
                             or [x for x in subs if x["rep"] in s.isa_roster])
                    cands = [x for x in cands if within_window(x)]
                if not cands:
                    detail = (f"no subscription sold within {SPLIT_WINDOW_DAYS} "
                              f"days" + ("" if typ == "rescue"
                                         else f" by {a1}") + " — check soldBy, "
                              "or pick the subscription on the form")
                elif len(cands) > 1 and not hinted:
                    detail = ("account has " + str(len(cands)) + " recent subs ("
                              + ", ".join(f"{x['subscription_id']} {x['service_type']}"
                                          for x in cands[:4])
                              + ") — pick the subscription on the form")
                else:
                    sub = cands[0]
                    sid_out = sub["subscription_id"]
                    svc = int(serviced(conn, sid_out))
                    existing = active_assignment(conn, sid_out)
                    rate = sub["rate"] or s.rate_standard
                    if existing:
                        status = "ALREADY BOOKED"
                        detail = "; ".join(f"{r} {sh:.0%} ({t})"
                                           for r, sh, t in existing)
                        rep_out = ", ".join(r for r, _, _ in existing)
                    elif typ == "split":
                        rep_out = f"{a1} / {a2}"
                        est = round(0.5 * rate * (sub["cv"] or 0), 2)
                        detail = (f"50/50 split on sub {sid_out} "
                                  f"(CV ${sub['cv'] or 0:,.0f}, soldBy {sub['rep']})")
                        status = "BOOKED split"
                        if not dry_run:
                            ref = book_assignment(conn, sid_out,
                                                  [(a1, 0.5), (a2, 0.5)], "split",
                                                  base_note)
                    elif typ == "outbound":
                        share = round((rate - s.rate_no_payment_method) / rate, 4) \
                            if rate > 0.051 else 1.0
                        rep_out = a1
                        est = round(share * rate * (sub["cv"] or 0), 2)
                        detail = (f"outbound split with {a2} on sub {sid_out} — "
                                  f"{a1} keeps {share:.0%} of {rate:.0%}")
                        status = "BOOKED outbound"
                        if not dry_run:
                            ref = book_assignment(conn, sid_out, [(a1, share)],
                                                  "outbound",
                                                  base_note + f" (outbound: {a2})")
                    else:                   # rescue
                        rep_out = a1
                        est = round(0.5 * rate * (sub["cv"] or 0), 2)
                        d2d_name = tech or d2d_host_tag(s, sub.get("sold_by")) or ""
                        detail = (f"D2D rescue on sub {sid_out}"
                                  + (f" (D2D: {d2d_name})" if d2d_name else "")
                                  + f" — {a1} gets 50%")
                        status = "BOOKED rescue"
                        if not dry_run:
                            ref = book_assignment(
                                conn, sid_out, [(a1, 0.5)], "rescue",
                                base_note + (f" (D2D: {d2d_name})" if d2d_name else ""))
        elif typ in ("upsell", "tech_upsell"):
            actives = [x for x in subs if x["active"]]
            if a1 not in s.isa_roster:
                detail = (f"'{a1}' not in the engine's ISA_ROSTER (.env) — "
                          "add them there, then this retries")
            elif not acv or acv <= 0:
                detail = "no usable ACV change on the submission"
            elif typ == "tech_upsell" and not tech:
                detail = "tech referral without a tech name — add the tech"
            elif hinted:
                sub = hinted[0]
            elif not actives:
                detail = "customer has no active subscription to host the upsell"
            elif len(actives) > 1:
                detail = ("customer has " + str(len(actives)) + " active subs ("
                          + ", ".join(f"{x['subscription_id']} {x['service_type']}"
                                      for x in actives[:4])
                          + ") — pick the subscription on the form")
            else:
                sub = actives[0]
            if not detail:
                rep_out = a1
                sid_out = sub["subscription_id"]
                svc = int(serviced(conn, sid_out))
                up_rate = upsell_rate(s, sub)
                comm_tag = " — commercial 5%" if up_rate < 0.09 else ""
                amount = round(up_rate * acv, 2)
                est = amount
                dup = conn.execute(
                    "SELECT 1 FROM upsells WHERE subscription_id=? AND rep=? "
                    "AND ABS(amount-?)<0.02 AND removed_at IS NULL",
                    (sid_out, rep_out, amount)).fetchone()
                acv_inc = acv if sub["state"] in ("paid", "pending",
                                                  "not_started") else 0.0
                host = d2d_host_tag(s, sub.get("sold_by"))
                if dup:
                    status = "ALREADY BOOKED"
                    detail = f"same upsell exists on sub {sid_out}"
                else:
                    who = f"tech referral: {tech}" if typ == "tech_upsell" \
                        else (f"D2D: {tech}" if tech else "")
                    detail = (f"+${amount:,.2f} ({up_rate:.0%} of ACV +${acv:,.0f}) to "
                              f"{rep_out} on sub {sid_out} ({sub['service_type']},"
                              f" state {sub['state']})"
                              + (f" — {who}" if who else "")
                              + (f" — D2D host: {host}" if host else "") + comm_tag)
                    status = ("BOOKED tech upsell" if typ == "tech_upsell"
                              else "BOOKED upsell")
                    if not dry_run:
                        ref = book_upsell(conn, sid_out, rep_out, amount, acv_inc,
                                          ts[:10], tech or None,
                                          base_note + (f" — {who}" if who else "")
                                          + (f" — branch {branch}" if branch else "")
                                          + comm_tag,
                                          d2d_host=host)
        else:
            detail = f"unknown adjustment type '{typ}' — book by hand"

        if not dry_run:
            conn.execute("""UPDATE intake_queue SET status=?, detail=?,
                est_amount=?, rep=?, sub_id=?, booked_ref=?,
                processed_at=datetime('now','localtime')
                WHERE id=?""", (status, detail, est, rep_out, sid_out, ref, qid))
            # mirror into form_intake so the Verified Adjustments history
            # stays one audit trail across both intake sources
            conn.execute("""INSERT OR REPLACE INTO form_intake
                (ts, account, type, agents, acv, other_rep, status, detail,
                 est_amount, rep, sub_id, serviced)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, acct, f"Dashboard #{qid}: {QUEUE_LABEL.get(typ, typ)}",
                 " / ".join(x for x in (a1, a2) if x), str(acv or ""),
                 tech, status, detail, est, rep_out, sid_out, svc))
        booked += int(status.startswith(("BOOKED", "ALREADY")))
        held += int(status == "HELD")
        flag = "✓" if status.startswith(("BOOKED", "ALREADY")) else "⚠"
        print(f"  {flag} queue#{qid} {ts} acct {acct:<8} {typ:<12} -> "
              f"{status}: {detail}")
    if not dry_run:
        conn.commit()
    return booked, held


def delete_queue_row(conn, qid: int, by: str) -> dict:
    """Delete one dashboard submission (owner, 2026-10-21). A held or
    unprocessed row just stops retrying. A booked row also takes its booking
    off, exactly like removing it by hand (RULES §11/§12): not paid yet,
    nothing moves; on a draft payroll, a re-run drops it; on a paid payroll,
    the next payroll takes it back. Returns {ok, message}."""
    from datetime import date, timedelta
    row = conn.execute(
        "SELECT submitted_at, submitted_by, type, account, status, detail, sub_id, "
        "booked_ref, processed_at FROM intake_queue WHERE id=?", (qid,)).fetchone()
    if not row:
        return {"ok": False, "message": f"submission #{qid} not found"}
    ts, sub_by, typ, acct, status, detail, sid, ref, processed = row
    status = status or ""
    if status == "DELETED":
        return {"ok": True, "message": "already deleted"}
    table, ids = None, []
    if status.startswith("BOOKED"):
        if ref and ":" in ref:
            kind, raw = ref.split(":", 1)
            table = "upsells" if kind == "upsell" else "assignments"
            ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        else:
            # booked before bookings were linked to their row: its note starts
            # with this submission's own stamp ("dashboard <time> by <who>:")
            table = "upsells" if typ in ("upsell", "tech_upsell") else "assignments"
            stamp = f"dashboard {ts} by {sub_by or '?'}:"
            ids = [r[0] for r in conn.execute(
                f"SELECT id FROM {table} WHERE subscription_id=? "
                "AND substr(COALESCE(note,''), 1, length(?)) = ?", (sid, stamp, stamp))]
    removed = []
    for i in ids:
        if conn.execute(f"UPDATE {table} SET removed_at=datetime('now') "
                        "WHERE id=? AND removed_at IS NULL", (i,)).rowcount:
            removed.append(i)

    # was it already on a payroll? (the go-live rows on 10/10 are the manual
    # payroll's payments: removing one of those also moves money next close)
    periods: list[str] = []
    if removed and table == "upsells":
        periods = [r[0] for r in conn.execute(
            "SELECT DISTINCT period_end FROM ledger WHERE ref IN (%s)"
            % ",".join("?" * len(removed)), [f"upsell:{i}" for i in removed])]
    elif removed:
        reps = [r[0] for r in conn.execute(
            "SELECT rep FROM assignments WHERE id IN (%s)" % ",".join("?" * len(removed)),
            removed)]
        periods = [r[0] for r in conn.execute(
            "SELECT DISTINCT period_end FROM ledger WHERE subscription_id=? AND rep IN (%s) "
            "AND period_end >= ? AND kind NOT IN ('vest','freeze','manual')"
            % ",".join("?" * len(reps)), [sid, *reps, (processed or ts or "")[:10]])]
    today = date.today()
    final, draft = [], []
    for p in sorted(periods):
        run = conn.execute("SELECT status FROM payroll_runs WHERE period_end=?",
                           (p,)).fetchone()
        paid_monday = date.fromisoformat(p) + timedelta(days=2)
        (final if (run and run[0] == "final") or today > paid_monday else draft).append(p)

    def md(p):
        return f"{int(p[5:7])}/{int(p[8:10])}"
    if status.startswith("BOOKED") and not ids:
        msg = "Deleted. Its booking was not found (removed by hand already?)."
    elif status.startswith("BOOKED") and not removed:
        msg = "Deleted. Its booking had already been removed."
    elif removed and final:
        msg = (f"Deleted, and the booking is removed. It was paid on the {md(final[-1])} "
               "payroll, so the next payroll takes it back.")
    elif removed and draft:
        msg = (f"Deleted, and the booking is removed. It is on the {md(draft[-1])} "
               "payroll draft: press Re-run on the payroll card to drop it.")
    elif removed:
        msg = "Deleted, and the booking is removed. It had not been paid."
    elif status == "ALREADY BOOKED":
        msg = ("Deleted. This submission booked nothing (the account already had it), "
               "so the earlier booking stays — delete that one if it is wrong.")
    else:
        msg = "Deleted. Nothing had been booked, so no money moves."
    stamp_now = datetime.now().strftime("%Y-%m-%d %H:%M")
    was = f"was {status or 'NEW'}" + (f": {detail}" if detail else "")
    new_detail = f"deleted by {by} {stamp_now} — {msg} ({was})"
    conn.execute("UPDATE intake_queue SET status='DELETED', detail=? WHERE id=?",
                 (new_detail, qid))
    conn.execute("UPDATE form_intake SET status='DELETED', detail=? "
                 "WHERE ts=? AND account=? AND type LIKE ?",
                 (new_detail, ts, acct, f"Dashboard #{qid}:%"))
    conn.commit()
    return {"ok": True, "message": msg}


# ------------------------------------------------------------------ main ---
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", default=None, help="override watermark")
    ap.add_argument("--queue-only", action="store_true",
                    help="process the dashboard intake_queue only (no Google)")
    ap.add_argument("--delete", type=int, metavar="ID",
                    help="delete dashboard submission #ID (and its booking), then exit")
    ap.add_argument("--by", default="dashboard", help="who deleted it (with --delete)")
    args = ap.parse_args()

    s = load_settings()
    # one intake at a time: a dashboard submit and a payroll run can both
    # spawn this, and two runs must never book the same queue row twice
    lock = open(s.db_path.parent / "intake.lock", "w")
    if os.name == "posix":
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)          # waits for the other run
    conn = connect(s.db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS form_intake (
        ts TEXT, account TEXT, type TEXT, agents TEXT, acv TEXT, other_rep TEXT,
        status TEXT, detail TEXT, est_amount REAL, rep TEXT, sub_id TEXT,
        serviced INTEGER, processed_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (ts, account, type))""")
    conn.execute(QUEUE_DDL)
    if "booked_ref" not in {r[1] for r in conn.execute("PRAGMA table_info(intake_queue)")}:
        conn.execute("ALTER TABLE intake_queue ADD COLUMN booked_ref TEXT")
    conn.commit()

    if args.delete is not None:
        # under the same lock as booking, so a delete never races a booking
        import json
        print(json.dumps(delete_queue_row(conn, args.delete, args.by)))
        return

    last_sync = (conn.execute("SELECT value FROM meta WHERE key='last_sync'")
                 .fetchone() or ["?"])[0]

    # dashboard submissions first — structured, no parsing needed
    qb, qh = process_queue(conn, s, args.dry_run)
    if qb or qh:
        print(f"dashboard queue: {qb} booked/confirmed · {qh} held · "
              f"engine data as of {last_sync}")
    if args.queue_only:
        print("(--queue-only: skipping the Google Form tab)")
        return

    wm_row = conn.execute("SELECT value FROM meta WHERE key='form_intake_watermark'").fetchone()
    watermark = args.since or (wm_row[0] if wm_row else default_watermark(s))
    resolve = resolver(s.isa_roster, s.outbound_agents)

    if not s.gsheet_id:
        print("(no GSHEET_ID: no Google Form to read; dashboard adjustments only)")
        return
    from commissions.gsheet import SheetHub   # not needed for --queue-only
    hub = SheetHub(s)
    rows = hub.read("Form Responses 1")
    if not rows:
        print("form tab is empty")
        return
    hdr = [str(h).lower() for h in rows[0]]

    def col(*needles):
        for i, h in enumerate(hdr):
            if all(n in h for n in needles):
                return i
        return None
    c_ts, c_acct = col("timestamp"), col("account")
    c_type, c_agent = col("type"), col("is agent")
    c_acv, c_other = col("acv"), col("tech")

    def cell(r, i):
        return r[i] if (i is not None and i < len(r)) else ""

    held_keys = {(a, b, c) for a, b, c in conn.execute(
        "SELECT ts, account, type FROM form_intake WHERE status='HELD'")}
    new, results = [], []
    for r in rows[1:]:
        ts = parse_ts(cell(r, c_ts))
        raw_acct = cell(r, c_acct)
        acct = (str(int(raw_acct)) if isinstance(raw_acct, (int, float))
                else str(raw_acct or "").strip())
        typ = str(cell(r, c_type) or "").strip()
        if not ts or not acct:
            continue
        is_retry = (ts, acct, typ) in held_keys
        if not is_retry:
            if ts <= watermark:
                continue
            if conn.execute("SELECT 1 FROM form_intake WHERE ts=? AND account=? AND type=?",
                            (ts, acct, typ)).fetchone():
                continue
        new.append((ts, acct, typ, str(cell(r, c_agent) or "").strip(),
                    cell(r, c_acv), str(cell(r, c_other) or "").strip()))

    print(f"watermark {watermark} · {len(new)} new submission(s) · "
          f"engine data as of {last_sync}" + (" · DRY RUN" if args.dry_run else ""))

    for ts, acct, typ, agents_raw, acv_raw, other in sorted(new):
        status, detail, est, rep_out, sid_out, svc = "HELD", "", None, "", "", 0
        subs = candidate_subs(conn, acct, ts)
        is_split = "split" in typ.lower()
        is_upsell = "upsell" in typ.lower()

        if is_upsell and not args.dry_run and not any(x["active"] for x in subs):
            # tech-referral/upsell on a non-tracked (e.g. D2D-sold) host —
            # no tracked sub, or only inactive ones: import the account live
            # from FieldRoutes, then proceed
            n = live_import_account(s, conn, acct)
            if n:
                subs = candidate_subs(conn, acct, ts)
                print(f"    · live-imported {n} subscription(s) for acct {acct}")

        if not subs:
            detail = ("account not in engine DB — not ISA-sold, or sold after "
                      f"the last sync ({last_sync}); will retry next run")
            if is_upsell and not args.dry_run:
                detail = ("account not found in FieldRoutes either — check the "
                          "account number")
        elif is_split:
            tokens = split_agents(agents_raw)
            resolved = [resolve(t) for t in tokens]
            if len(tokens) < 2 and not other:
                detail = f"only one agent named ('{agents_raw}') — who is the split with?"
            elif any(x is None for x in resolved):
                bad = [t for t, x in zip(tokens, resolved) if x is None]
                if len(tokens) == 2 or (len(tokens) == 1 and other):
                    detail = f"could not identify agent(s): {', '.join(bad)}"
                else:
                    detail = f"could not parse agents from '{agents_raw}'"
            else:
                inb = [n for n, k in resolved if k == "inbound"]
                outb = [n for n, k in resolved if k == "outbound"]
                # choose the sub: newest ISA-tracked sub sold near the submission
                cands = [x for x in subs if x["rep"] in inb] or \
                        [x for x in subs if x["rep"] in s.isa_roster]
                cands = [x for x in cands
                         if (datetime.strptime(ts[:10], "%Y-%m-%d")
                             - datetime.strptime(x["date_added"][:10], "%Y-%m-%d")
                             ).days <= SPLIT_WINDOW_DAYS]
                if not cands:
                    detail = (f"no subscription sold within {SPLIT_WINDOW_DAYS} days "
                              f"by {', '.join(inb) or 'a roster rep'} — check soldBy")
                else:
                    sub = cands[0]
                    sid_out, svc = sub["subscription_id"], int(serviced(conn, sub["subscription_id"]))
                    existing = active_assignment(conn, sid_out)
                    rate = sub["rate"] or s.rate_standard
                    note = f"form {ts}: {typ} '{agents_raw}'" + (f" / {other}" if other else "")
                    if existing:
                        status = "ALREADY BOOKED"
                        detail = "; ".join(f"{r} {sh:.0%} ({t})" for r, sh, t in existing)
                        rep_out = ", ".join(r for r, _, _ in existing)
                    elif len(inb) == 2:
                        rep_out = " / ".join(inb)
                        est = round(0.5 * rate * (sub["cv"] or 0), 2)
                        detail = (f"50/50 split on sub {sid_out} (CV ${sub['cv']:,.0f}, "
                                  f"soldBy {sub['rep']})")
                        status = "BOOKED split"
                        if not args.dry_run:
                            book_assignment(conn, sid_out,
                                            [(inb[0], 0.5), (inb[1], 0.5)], "split", note)
                    elif len(inb) == 1 and len(outb) == 1:
                        share = round((rate - s.rate_no_payment_method) / rate, 4) \
                            if rate > 0.051 else 1.0
                        rep_out = inb[0]
                        est = round(share * rate * (sub["cv"] or 0), 2)
                        detail = (f"outbound split with {outb[0]} on sub {sid_out} — "
                                  f"{inb[0]} keeps {share:.0%} of {rate:.0%}")
                        status = "BOOKED outbound"
                        if not args.dry_run:
                            book_assignment(conn, sid_out, [(inb[0], share)],
                                            "outbound", note + f" (outbound: {outb[0]})")
                    elif len(inb) == 1 and other:
                        rep_out = inb[0]
                        est = round(0.5 * rate * (sub["cv"] or 0), 2)
                        detail = (f"D2D/rescue split with '{other}' on sub {sid_out} — "
                                  f"{inb[0]} gets 50%")
                        status = "BOOKED rescue"
                        if not args.dry_run:
                            book_assignment(conn, sid_out, [(inb[0], 0.5)], "rescue",
                                            note)
                    else:
                        detail = (f"unhandled combination (inbound: {inb}, outbound: "
                                  f"{outb}, other: '{other}') — book by hand")
        elif is_upsell:
            got = resolve(agents_raw)
            acv = parse_acv(acv_raw)
            actives = [x for x in subs if x["active"]]
            if got is None or got[1] != "inbound":
                detail = f"could not identify ISA '{agents_raw}'"
            elif acv is None or acv <= 0:
                detail = f"no usable ACV change in '{acv_raw}'"
            elif not actives:
                detail = "customer has no active subscription to host the upsell"
            elif len(actives) > 1:
                detail = ("customer has " + str(len(actives)) + " active subs ("
                          + ", ".join(f"{x['subscription_id']} {x['service_type']}"
                                      for x in actives[:4])
                          + ") — which one was upsold?")
            else:
                sub = actives[0]
                rep_out = got[0]
                sid_out, svc = sub["subscription_id"], int(serviced(conn, sub["subscription_id"]))
                up_rate = upsell_rate(s, sub)
                comm_tag = " — commercial 5%" if up_rate < 0.09 else ""
                amount = round(up_rate * acv, 2)
                est = amount
                dup = conn.execute(
                    "SELECT 1 FROM upsells WHERE subscription_id=? AND rep=? "
                    "AND ABS(amount-?)<0.02 AND removed_at IS NULL",
                    (sid_out, rep_out, amount)).fetchone()
                # ACV joins the host's own math only while the host is unvested
                acv_inc = acv if sub["state"] in ("paid", "pending", "not_started") else 0.0
                host = d2d_host_tag(s, sub.get("sold_by"))
                if dup:
                    status, detail = "ALREADY BOOKED", f"same upsell exists on sub {sid_out}"
                else:
                    detail = (f"+${amount:,.2f} ({up_rate:.0%} of ACV +${acv:,.0f}) on sub "
                              f"{sid_out} ({sub['service_type']}, state {sub['state']})"
                              + (f" — D2D host: {host}" if host else "") + comm_tag)
                    status = "BOOKED upsell"
                    if not args.dry_run:
                        book_upsell(conn, sid_out, rep_out, amount, acv_inc,
                                    ts[:10], other or None,
                                    f"form {ts}: {typ}" + comm_tag, d2d_host=host)
        else:
            detail = f"unknown submission type '{typ}' — book by hand"

        if not args.dry_run:
            conn.execute("""INSERT OR REPLACE INTO form_intake
                (ts, account, type, agents, acv, other_rep, status, detail,
                 est_amount, rep, sub_id, serviced)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, acct, typ, agents_raw, str(acv_raw or ""), other, status,
                 detail, est, rep_out, sid_out, svc))
        results.append((ts, acct, typ, agents_raw, other, status, detail, est,
                        rep_out, sid_out, svc))
        flag = "✓" if status.startswith(("BOOKED", "ALREADY")) else "⚠"
        print(f"  {flag} {ts} acct {acct:<8} {typ:<38} -> {status}: {detail}")

    if not args.dry_run:
        if new:
            top = max(ts for ts, *_ in new)
            conn.execute("INSERT OR REPLACE INTO meta(key,value) "
                         "VALUES('form_intake_watermark', ?)", (top,))
        conn.commit()

        # rewrite Verified Adjustments with the full history, newest first
        hist = conn.execute("""SELECT ts, rep, account, est_amount, detail,
            other_rep, serviced, status, agents, type FROM form_intake
            ORDER BY ts DESC""").fetchall()
        out = [["Processed", "Rep", "Account", "Est. $", "Note", "D2D Rep / Tech",
                "Serviced?", "Status", "Form said", "Type"],
               [f"engine data as of {last_sync} · rewritten "
                f"{datetime.now():%Y-%m-%d %H:%M} · HELD rows need the owner"]]
        for ts, rep, acct, est, detail, other, svc, status, agents, typ in hist:
            out.append([ts, rep or "", acct, est if est is not None else "",
                        detail, other or "", "TRUE" if svc else "FALSE",
                        status, agents, typ])
        hub.write("Verified Adjustments", out)
        print(f"\nVerified Adjustments tab rewritten ({len(hist)} rows)")

    booked = sum(1 for x in results if x[5].startswith("BOOKED"))
    held = sum(1 for x in results if x[5] == "HELD")
    print(f"\n{booked} booked · {held} held · "
          f"{sum(1 for x in results if x[5] == 'ALREADY BOOKED')} already booked")
    hub.close()


if __name__ == "__main__":
    main()
