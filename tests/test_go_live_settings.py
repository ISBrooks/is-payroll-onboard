"""This company's go-live comes from its own settings, never from the original
company's dates (COMMISSION_RULES.md §8)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from commissions import cutover
from commissions.config import load_settings

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture(autouse=True)
def fresh_cut():
    cutover._cut.cache_clear()
    yield
    cutover._cut.cache_clear()


@pytest.mark.parametrize("key", ["PAYROLL_LIVE_FROM", "PAYROLL_ANCHOR_MONDAY",
                                 "GO_LIVE_PERIOD_START", "BACKFILL_SINCE"])
def test_each_go_live_date_is_required(monkeypatch, key):
    monkeypatch.delenv(key)
    with pytest.raises(SystemExit, match=key):
        load_settings()


def test_a_go_live_date_must_be_a_date(monkeypatch):
    monkeypatch.setenv("PAYROLL_LIVE_FROM", "next month")
    with pytest.raises(SystemExit, match="not a date"):
        load_settings()


def test_the_last_manual_day_is_the_day_before_go_live(monkeypatch):
    monkeypatch.setenv("PAYROLL_LIVE_FROM", "2026-09-27")
    monkeypatch.setenv("CUTOVER_LIST_AT_UTC", "2026-09-28T17:05:00")
    assert cutover._cut() == ("2026-09-26", "2026-09-26 23:59:59", "2026-09-28 17:05:00")


def test_the_switch_over_needs_the_list_time(monkeypatch):
    monkeypatch.delenv("CUTOVER_LIST_AT_UTC")
    with pytest.raises(SystemExit, match="CUTOVER_LIST_AT_UTC"):
        cutover._cut()


def test_form_intake_starts_after_the_last_manual_period(monkeypatch):
    spec = importlib.util.spec_from_file_location("form_intake", SCRIPTS / "form_intake.py")
    fi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fi)
    s = load_settings()
    assert fi.default_watermark(s) == "2026-09-26 23:59:59"
    monkeypatch.setenv("FORM_INTAKE_SINCE", "2026-09-01 00:00:00")
    assert fi.default_watermark(s) == "2026-09-01 00:00:00"


def test_isa_roster_is_only_needed_in_env_mode(monkeypatch):
    monkeypatch.delenv("ISA_ROSTER")
    with pytest.raises(SystemExit, match="ISA_ROSTER"):
        load_settings()                       # ROSTER_SOURCE=env in the tests


@pytest.mark.parametrize("key, value, says", [
    ("PAYROLL_ANCHOR_MONDAY", "2026-09-14", "PAYROLL_ANCHOR_MONDAY"),   # a later Monday: the parallel run would start late
    ("PAYROLL_LIVE_FROM", "2026-09-28", "PAYROLL_LIVE_FROM"),           # a Monday, off the grid
    ("PAYROLL_LIVE_FROM", "2026-08-16", "PAYROLL_LIVE_FROM"),           # not after the calculated start
    ("BACKFILL_SINCE", "2026-09-01", "BACKFILL_SINCE"),
    ("PESTROUTES_OFFICE_IDS", " , ", "PESTROUTES_OFFICE_IDS"),
    ("OFFICE_TIMEZONE", "Mountain", "OFFICE_TIMEZONE"),
])
def test_settings_that_dont_fit_the_pay_calendar_stop_the_engine(monkeypatch, key, value, says):
    monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit, match=says):
        load_settings()
