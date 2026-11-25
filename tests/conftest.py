"""Shared test setup: no real settings, no network.

The engine reads its settings from .env at the top of the project. Tests must
behave the same on every machine, so a real .env is never read: the few settings
the engine needs are set here, and they describe a made-up company."""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False            # never read a real .env

ROSTER = ["Avery Stone", "Blake Rivers", "Casey Flint"]
TEST_ENV = {
    "PESTROUTES_BASE_URL": "https://placeholder.invalid",
    "PESTROUTES_AUTH_KEY": "placeholder",
    "PESTROUTES_AUTH_TOKEN": "placeholder",
    "PESTROUTES_OFFICE_IDS": "1,2",
    "ISA_ROSTER": ",".join(ROSTER),
    # the engine normally reads its roster from the dashboard (Payroll -> Commission
    # Roster); tests use its .env mode. tests/test_roster.py covers the dashboard mode.
    "ROSTER_SOURCE": "env",
    "OUTBOUND_AGENTS": "Robin Vale",
    # a made-up company's go-live (COMMISSION_RULES.md §8): pay periods end every
    # other Saturday from 8/29; the app calculates from 8/16 and pays from 9/27
    "PAYROLL_ANCHOR_MONDAY": "2026-08-31",
    "BACKFILL_SINCE": "2025-08-01",
    "GO_LIVE_PERIOD_START": "2026-08-16",
    "PAYROLL_LIVE_FROM": "2026-09-27",
    "CUTOVER_LIST_AT_UTC": "2026-09-28 17:00:00",
    "OFFICE_TIMEZONE": "America/Chicago",
}
for _k in ("GSHEET_ID", "ISADASH_DB", "RATE_MANAGER", "RESEND_API_KEY", "ISA_REP_START_DATES",
           "COMMERCIAL_SERVICE_TYPES", "FORM_INTAKE_SINCE"):
    os.environ.pop(_k, None)
os.environ.update(TEST_ENV)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Anything that tries to leave the machine fails the test."""
    def refuse(self, address, *a, **k):
        raise AssertionError(f"test tried to open a network connection to {address!r}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
