"""The run card's totals and the statement workbook come from the same net lines.

Found 2026-11-24: the payroll run added up raw ledger rows for the run card while
the workbook netted each rep's rows per subscription, so a sale paid in week 1 and
taken back in week 2 showed in different weeks on each. Both now use
commissions.statement.statement_lines.
"""
from __future__ import annotations

import importlib.util
import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

from openpyxl import load_workbook

from commissions.statement import rep_totals, statement_lines
from commissions.store import connect

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
END = "2026-10-10"                     # weeks: Sun 9/27 - Sat 10/3, Sun 10/4 - Sat 10/10


def script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ledger(tmp_path):
    db = tmp_path / "commissions.sqlite"
    c = connect(db)
    rows = [  # (subscription, rep, amount, kind, event date)
        ("S1", "Avery Stone", 90.00, "initial", "2026-09-29"),    # paid in week 1 ...
        ("S1", "Avery Stone", -90.00, "clawback", "2026-10-06"),  # ... taken back in week 2: nets to 0
        ("S2", "Avery Stone", 50.00, "initial", "2026-09-30"),
        ("S3", "Avery Stone", 40.00, "initial", "2026-09-28"),    # paid week 1, part back week 2
        ("S3", "Avery Stone", -15.00, "trueup", "2026-10-07"),    # -> one +25 line, in week 2
        ("S4", "Blake Rivers", -30.00, "clawback", None),         # no event date: week 2
        ("S5", "Blake Rivers", 12.00, "vest", "2026-09-29"),      # not a pay line
    ]
    for sid, rep, amt, kind, ev in rows:
        c.execute("INSERT INTO ledger (subscription_id, rep, period_end, amount, kind, event_date) "
                  "VALUES (?,?,?,?,?,?)", (sid, rep, END, amt, kind, ev))
    c.commit()
    return db, c


def test_net_lines_and_their_weeks(tmp_path):
    _db, c = ledger(tmp_path)
    lines = statement_lines(c, END)
    assert [(r[0], r[1], round(r[4], 2)) for r in lines] == [
        ("Avery Stone", "S2", 50.0), ("Avery Stone", "S3", 25.0), ("Blake Rivers", "S4", -30.0)]
    t = rep_totals(lines, END)
    assert {k: round(v, 2) for k, v in t["Avery Stone"].items()} == {
        "pos": 75.0, "neg": 0.0, "w1_pos": 50.0, "w1_neg": 0.0, "w2_pos": 25.0, "w2_neg": 0.0}
    assert round(t["Blake Rivers"]["w2_neg"], 2) == -30.0


def test_run_card_totals_match_the_workbook(tmp_path, monkeypatch):
    db, c = ledger(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        pos, neg = script("report_payroll")._summary(c, END)
    out = buf.getvalue()
    card = {int(n): (float(p.replace(",", "")), float(d.replace(",", "")))
            for n, p, d in re.findall(r"Week ([12]) \([\d/]+-[\d/]+\): commissions (-?[\d,.]+) "
                                      r"deductions (-?[\d,.]+)", out)}
    assert (pos, neg) == (75.0, -30.0)
    assert card == {1: (50.0, 0.0), 2: (25.0, -30.0)}

    xlsx = tmp_path / "statement.xlsx"
    monkeypatch.setattr(sys, "argv", ["export_xlsx.py", "--period-end", END, "--db", str(db), "--out", str(xlsx)])
    with redirect_stdout(io.StringIO()):
        script("export_xlsx").main()
    wb = load_workbook(xlsx)
    book = {1: [0.0, 0.0], 2: [0.0, 0.0]}
    for side, tab in enumerate(("Commissions", "Deductions")):
        ws = wb[tab]
        head = [x.value for x in ws[1]]
        a, w = head.index("Amount"), head.index("Week")
        for r in ws.iter_rows(min_row=2, values_only=True):
            if r[0]:
                book[int(r[w])][side] += r[a]
    assert {k: tuple(round(x, 2) for x in v) for k, v in book.items()} == card
