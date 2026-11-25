"""The history upload: what a company types into the four CSV files must arrive
in the four tables exactly as the engine reads them."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

import load_history
from commissions import cutover
from commissions.engine import _plabel_end
from commissions.store import connect, set_meta

TEMPLATES = Path(__file__).resolve().parents[1] / "history_templates"


def run(monkeypatch, capsys, folder: Path, db: Path, *flags: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["load_history.py", "--dir", str(folder), "--db", str(db), *flags])
    code = load_history.main()
    return code, capsys.readouterr().out


def write(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


@pytest.fixture()
def folder(tmp_path):
    f = tmp_path / "history"
    write(f, "paid.csv", "period_end,rep,customer_id,contract_value,commission\n"
                         "2026-08-29,Avery Stone,900101,948.00,85.32\n"
                         "8/29/2026,blake  rivers,900102,\"1,200.00\",$108.00\n"
                         "2026-08-15,Robin Vale,900107,500,15.00\n")
    write(f, "adjustments.csv", "period_end,rep,customer_id,amount,note\n"
                                "2026-08-29,Blake Rivers,900101,42.66,Split sale\n"
                                "2026-08-29,Avery Stone,900101,-42.66,Split sale\n"
                                "2026-08-29,Casey Flint,900103,30.00,Upsell credit\n")
    write(f, "deductions.csv", "period_end,rep,customer_id,amount,kind\n"
                               "2026-08-29,Avery Stone,900104,61.20,cancel\n"
                               "2026-08-29,Blake Rivers,900102,(12.15),cv_change\n")
    write(f, "not_paid.csv", "customer_id,rep,date_sold\n900105,Avery Stone,2026-08-20\n")
    return f


def test_the_shipped_templates_pass_the_check(monkeypatch, capsys, tmp_path):
    code, out = run(monkeypatch, capsys, TEMPLATES, tmp_path / "c.sqlite")
    assert code == 0, out
    assert "check only: nothing was written" in out


def test_check_only_writes_nothing(monkeypatch, capsys, folder, tmp_path):
    db = tmp_path / "c.sqlite"
    code, out = run(monkeypatch, capsys, folder, db)
    assert code == 0, out
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert "manual_paid" not in tables


def test_totals_per_rep_are_shown_for_comparison(monkeypatch, capsys, folder, tmp_path):
    _code, out = run(monkeypatch, capsys, folder, tmp_path / "c.sqlite")
    avery = next(line for line in out.splitlines() if line.startswith("Avery Stone"))
    # paid 85.32, adjustments -42.66, cv true-downs 0, deductions -61.20, net -18.54
    assert avery.split()[2:] == ["85.32", "-42.66", "0.00", "-61.20", "-18.54"]
    assert "Robin Vale" in out and "not on the roster" in out


def test_loaded_rows_are_what_the_engine_reads(monkeypatch, capsys, folder, tmp_path):
    db = tmp_path / "c.sqlite"
    code, out = run(monkeypatch, capsys, folder, db, "--write")
    assert code == 0, out
    conn = connect(db)
    # the paid period label must parse back to the period end
    labels = {r[0] for r in conn.execute("SELECT period FROM manual_paid")}
    assert {_plabel_end(x) for x in labels} == {"2026-08-29", "2026-08-15"}
    # names are stored as spelled on the roster; money is read with $ , and ( )
    assert conn.execute("SELECT rep, contract_value, commission FROM manual_paid WHERE customer_id='900102'"
                        ).fetchone() == ("Blake Rivers", 1200.0, 108.0)
    # the engine's own reader: G paid, A adjustment (upsell notes are skipped), T true-down, D deduction
    rows = sorted((k, cu, rep, pe, round(amt, 2)) for k, cu, rep, pe, amt, _cv in cutover._manual_rows(conn))
    assert rows == [
        ("A", "900101", "Avery Stone", "2026-08-29", -42.66),
        ("A", "900101", "Blake Rivers", "2026-08-29", 42.66),
        ("D", "900104", "Avery Stone", "2026-08-29", -61.2),
        ("G", "900101", "Avery Stone", "2026-08-29", 85.32),
        ("G", "900102", "Blake Rivers", "2026-08-29", 108.0),
        ("G", "900107", "Robin Vale", "2026-08-15", 15.0),
        ("T", "900102", "Blake Rivers", "2026-08-29", -12.15),
    ]
    assert conn.execute("SELECT customer_id, rep, date_sold FROM manual_not_paid").fetchall() == [
        ("900105", "Avery Stone", "2026-08-20")]


def test_loading_again_replaces_rather_than_doubles(monkeypatch, capsys, folder, tmp_path):
    db = tmp_path / "c.sqlite"
    run(monkeypatch, capsys, folder, db, "--write")
    run(monkeypatch, capsys, folder, db, "--write")
    assert connect(db).execute("SELECT COUNT(*) FROM manual_paid").fetchone()[0] == 3


def test_a_bad_row_blocks_the_load_and_names_the_line(monkeypatch, capsys, folder, tmp_path):
    db = tmp_path / "c.sqlite"
    write(folder, "deductions.csv", "period_end,rep,customer_id,amount,kind\n"
                                    "2026-08-29,Avery Stone,900104,61.20,refund\n")
    code, out = run(monkeypatch, capsys, folder, db, "--write")
    assert code == 1
    assert "deductions.csv line 2" in out and "kind must be" in out
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert "manual_paid" not in tables


def test_a_date_that_is_not_a_period_end_is_flagged(monkeypatch, capsys, folder, tmp_path):
    write(folder, "paid.csv", "period_end,rep,customer_id,contract_value,commission\n"
                              "2026-08-27,Avery Stone,900101,948.00,85.32\n")
    _code, out = run(monkeypatch, capsys, folder, tmp_path / "c.sqlite")
    assert "not the last day of a pay period" in out and "2026-08-27" in out


def test_history_cannot_be_replaced_after_the_baseline_is_built(monkeypatch, capsys, folder, tmp_path):
    db = tmp_path / "c.sqlite"
    run(monkeypatch, capsys, folder, db, "--write")
    conn = connect(db)
    set_meta(conn, "cutover_seeded_at", "2026-09-01T00:00:00")
    conn.commit()
    conn.close()
    code, out = run(monkeypatch, capsys, folder, db, "--write")
    assert code == 1 and "baseline was already built" in out


def test_a_failed_load_leaves_the_old_history(monkeypatch, capsys, folder, tmp_path):
    db = tmp_path / "c.sqlite"
    code, out = run(monkeypatch, capsys, folder, db, "--write")
    assert code == 0, out
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM manual_paid").fetchone()[0]
    real_connect = load_history.connect

    class Breaks:
        """A connection whose last insert fails, after the tables were dropped and refilled."""
        def __init__(self, c):
            self._c = c

        def executemany(self, sql, rows):
            if "manual_not_paid" in sql:
                raise sqlite3.OperationalError("disk full (made up)")
            return self._c.executemany(sql, rows)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr(load_history, "connect", lambda p: Breaks(real_connect(p)))
    with pytest.raises(sqlite3.OperationalError):
        run(monkeypatch, capsys, folder, db, "--write")
    after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM manual_paid").fetchone()[0]
    assert after == before > 0
