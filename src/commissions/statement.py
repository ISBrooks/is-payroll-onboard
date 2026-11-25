"""The lines of a pay period's statement, as payroll is paid from them.

One source for both the statement workbook (scripts/export_xlsx.py) and the
totals the payroll run prints for the run card (scripts/report_payroll.py),
so the two can never disagree (found 2026-11-24: the run card added up raw
ledger rows, the workbook netted them, and a sale paid in week 1 and taken
back in week 2 showed in different weeks on each).

A rep's rows on one subscription in one period (an initial payment plus a
same-period split correction, a payment and its clawback) collapse into ONE
net line. The net line is a commission when it is >= 0, a deduction when
< 0, and falls in the week of its latest event (RULES §7). Net lines under
half a cent are dropped.
"""
from __future__ import annotations

from .periods import week_of

# column positions in each line (the statement workbook's detail columns are
# built from these)
REP, SUB, CUSTOMER_ID, CUSTOMER, AMOUNT, KIND = 0, 1, 2, 3, 4, 5
NOTE, EVENT_DATE = 19, 20

_SQL = """
    SELECT l.rep, l.subscription_id, sub.customer_id, cu.name,
           l.amount, l.kind, st.rate, sub.contract_value, sub.source,
           sub.service_type, sub.date_added, st.first_paid_date,
           st.qualifying_completed, st.coverage_paid, st.coverage_target,
           st.state, sub.date_cancelled, sub.cancel_reason,
           COALESCE(st.flags,''), l.note, l.event_date
    FROM ledger l
    LEFT JOIN subscriptions sub ON sub.subscription_id = l.subscription_id
    LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
    LEFT JOIN sub_status st ON st.subscription_id = l.subscription_id
    WHERE l.period_end = ? AND l.kind NOT IN ('vest','external','baseline')
    ORDER BY l.rep, l.amount DESC"""


def statement_lines(conn, period_end: str) -> list[tuple]:
    """The period's net lines, sorted by rep then largest amount first."""
    grouped: dict[tuple, list] = {}
    for r in conn.execute(_SQL, (period_end,)).fetchall():
        key = (r[REP], r[SUB])
        if key not in grouped:
            grouped[key] = list(r)
            continue
        g = grouped[key]
        g[AMOUNT] += r[AMOUNT]                                  # net amount
        if r[KIND] not in g[KIND]:
            g[KIND] = f"{g[KIND]}+{r[KIND]}"                    # combined kinds
        if r[NOTE] and (not g[NOTE] or r[NOTE] not in g[NOTE]):
            g[NOTE] = f"{g[NOTE]} | {r[NOTE]}" if g[NOTE] else r[NOTE]
        if r[EVENT_DATE] and (not g[EVENT_DATE] or r[EVENT_DATE] > g[EVENT_DATE]):
            g[EVENT_DATE] = r[EVENT_DATE]                       # latest event dates the net line
    lines = [tuple(v) for v in grouped.values() if abs(v[AMOUNT]) >= 0.005]
    lines.sort(key=lambda r: (r[REP], -r[AMOUNT]))
    return lines


def rep_totals(lines: list[tuple], period_end: str) -> dict[str, dict[str, float]]:
    """{rep: {'pos', 'neg', 'w1_pos', 'w1_neg', 'w2_pos', 'w2_neg'}} from the
    net lines: exactly what the statement's Summary shows."""
    out: dict[str, dict[str, float]] = {}
    for r in lines:
        t = out.setdefault(r[REP], dict.fromkeys(
            ("pos", "neg", "w1_pos", "w1_neg", "w2_pos", "w2_neg"), 0.0))
        side = "pos" if r[AMOUNT] >= 0 else "neg"
        t[side] += r[AMOUNT]
        t[f"w{week_of(r[EVENT_DATE], period_end)}_{side}"] += r[AMOUNT]
    return out
