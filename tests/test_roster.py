"""Who the engine pays comes from the dashboard's Commission Roster (Payroll ->
Commission Roster): inbound reps and managers, read-only from the dashboard's
database. That is how it runs in production, so it is tested on a made-up roster."""
from __future__ import annotations

import sqlite3

import pytest

from commissions.config import load_settings, payees


def roster_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE commission_roster (name TEXT PRIMARY KEY, role TEXT NOT NULL, "
              "added_at TEXT NOT NULL DEFAULT (datetime('now')), start_date TEXT)")
    c.executemany("INSERT INTO commission_roster (name, role, added_at, start_date) VALUES (?,?,?,?)", rows)
    c.commit()
    c.close()


def test_the_dashboard_roster_decides_who_is_paid(tmp_path, monkeypatch):
    db = tmp_path / "isadash.sqlite"
    roster_db(db, [("Avery Stone", "inbound", "2026-08-01 10:00:00", None),
                   ("Blake  Rivers", "inbound", "2026-08-01 10:00:00", None),
                   ("Robin Vale", "outbound", "2026-08-01 10:00:00", None),
                   ("Morgan Hale", "manager", "2026-09-02 10:00:00", None),
                   ("Riley Frost", "manager", "2026-09-02 10:00:00", "2026-10-11")])
    monkeypatch.setenv("ROSTER_SOURCE", "dashboard")
    monkeypatch.setenv("ISADASH_DB", str(db))
    s = load_settings()
    assert s.roster_source == "dashboard"
    assert s.isa_roster == ["Avery Stone", "Blake Rivers"]          # outbound reps are not paid here
    assert s.managers == ["Morgan Hale", "Riley Frost"]
    # a manager with no start date is paid from the start of the pay period they were added in
    assert s.manager_start_dates == {"Morgan Hale": "2026-08-30", "Riley Frost": "2026-10-11"}
    assert payees(s) == ["Avery Stone", "Blake Rivers", "Morgan Hale", "Riley Frost"]
    # outbound agents are not paid, but form intake needs their names (one list: the roster)
    assert s.outbound_agents == ["Robin Vale"]


def test_the_dashboard_mode_needs_no_isa_roster_setting(tmp_path, monkeypatch):
    db = tmp_path / "isadash.sqlite"
    roster_db(db, [("Avery Stone", "inbound", "2026-08-01 10:00:00", None)])
    monkeypatch.setenv("ROSTER_SOURCE", "dashboard")
    monkeypatch.setenv("ISADASH_DB", str(db))
    monkeypatch.delenv("ISA_ROSTER")
    monkeypatch.delenv("OUTBOUND_AGENTS")
    s = load_settings()
    assert s.isa_roster == ["Avery Stone"] and s.outbound_agents == []


def test_an_unreadable_dashboard_roster_stops_the_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSTER_SOURCE", "dashboard")
    monkeypatch.setenv("ISADASH_DB", str(tmp_path / "missing.sqlite"))
    with pytest.raises(SystemExit, match="commission roster not readable"):
        load_settings()


def test_an_empty_dashboard_roster_stops_the_engine(tmp_path, monkeypatch):
    db = tmp_path / "isadash.sqlite"
    roster_db(db, [("Morgan Hale", "manager", "2026-09-02 10:00:00", None)])
    monkeypatch.setenv("ROSTER_SOURCE", "dashboard")
    monkeypatch.setenv("ISADASH_DB", str(db))
    with pytest.raises(SystemExit, match="no inbound"):
        load_settings()
