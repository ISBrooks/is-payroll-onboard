"""Settings loaded from .env. All rule constants live here so the engine
code stays a direct transcription of docs/COMMISSION_RULES.md."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    pestroutes_base_url: str
    pestroutes_auth_key: str
    pestroutes_auth_token: str
    office_ids: list[str]
    isa_roster: list[str]
    resend_api_key: str
    resend_from: str
    resend_to: str
    payroll_anchor_monday: str          # first payroll Monday, YYYY-MM-DD
    rate_standard: float
    rate_alt_source: float
    rate_no_payment_method: float
    alt_rate_sources: list[str]         # lowercased
    vesting_service_count: int
    db_path: Path = field(default=ROOT / "data" / "commissions.sqlite")
    # Everything first-paid on/before this date was settled by the manual
    # process (recorded as 'external' ledger rows, never re-paid by us).
    go_live_period_start: str = ""            # GO_LIVE_PERIOD_START in .env (required)
    # Payroll go-live (RULES §8c): Sunday start of the first pay period PAID
    # from this app (PAYROLL_LIVE_FROM). go_live_period_start above is
    # when the Moved / Re-signed / fee rules took effect — a different date.
    payroll_live_from: str = ""               # PAYROLL_LIVE_FROM in .env (required)
    backfill_since: str = ""                  # BACKFILL_SINCE in .env (required)
    # The IS Dashboard's database (read-only): the ISA rep -> FR employee-ID map and
    # the commission roster's start dates. Default: the dashboard installed beside this app.
    isadash_db: Path = field(default=ROOT.parent / "is-dash" / "data" / "isadash.sqlite")
    # Google Sheets hub: the Form-fed adjustments spreadsheet that also
    # receives the app's payroll output tabs.
    gsheet_id: str = ""
    google_sa_json: Path = field(default=ROOT / "secrets" / "google-sa.json")
    # RULES §1a: {rep: 'YYYY-MM-DD'} inside-sales start dates from .env
    # ISA_REP_START_DATES; the dashboard roster's start_date column overrides
    rep_start_dates: dict = field(default_factory=dict)
    # RULES §5: a real-money initial at/above max(initialQuote, this floor)
    # means the cancellation fee (= the initial-discount recapture) owed is $0
    full_initial_floor: float = 299.0
    # RULES §3 priority 0 (owner, 2026-10-20): commercial accounts pay 5%,
    # card or no card — customer card set to Commercial Property, or one of
    # these business-only plans (lowercased service type descriptions)
    rate_commercial: float = 0.05
    commercial_service_types: tuple[str, ...] = ()   # set COMMERCIAL_SERVICE_TYPES in .env
    # RULES §14 (owner, 2026-11-23): managers on the commission roster earn a
    # flat 5% once a sale's first service is done and paid — nothing else, no
    # deductions ever. {manager: 'YYYY-MM-DD'} = first day the app pays them.
    managers: list[str] = field(default_factory=list)
    manager_start_dates: dict = field(default_factory=dict)
    # outbound agents (roster role 'outbound'): named on split forms, paid by
    # the outbound process, never by this engine. Form intake needs them to
    # tell an outbound split from a misspelled ISA (2026-11-24: was a list of
    # names in form_intake.py, separate from the roster)
    outbound_agents: list[str] = field(default_factory=list)
    rate_manager: float = 0.05
    # where isa_roster/managers came from: 'dashboard' (Payroll -> Commission Roster,
    # the normal case) or 'env' (ISA_ROSTER; ROSTER_SOURCE=env, dev laptops)
    roster_source: str = "env"


def payees(s: "Settings") -> list[str]:
    """Everyone the engine credits sales to: inbound ISAs plus managers."""
    return list(s.isa_roster) + [m for m in s.managers if m not in s.isa_roster]


def _norm_name(n: str | None) -> str:
    return " ".join((n or "").split())


def _dashboard_roster(db: Path) -> list[tuple] | None:
    """Payroll -> Commission Roster roster rows (name, role, added_at, start_date)
    from the dashboard DB, read-only; None when it can't be read."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(commission_roster)")}
            if not cols:
                return None
            sd = "start_date" if "start_date" in cols else "NULL"
            return c.execute(f"SELECT name, role, added_at, {sd} "
                             f"FROM commission_roster").fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return None


def _roster_from_dashboard(db: Path, anchor_monday: str
                           ) -> tuple[list[str], list[str], dict[str, str], list[str]]:
    """(inbound ISAs, managers, manager start dates, outbound agents) from the dashboard's
    commission roster — the list the owner keeps current (2026-11-23: Morgan Hale
    and Reese Frost were removed there but stayed on the statement because
    the engine only read .env ISA_ROSTER). A manager with no start date is
    paid from the first day of the pay period they were added in, so adding
    one never back-pays old sales."""
    from datetime import date
    from .periods import period_for
    rows = _dashboard_roster(db)
    if rows is None:
        raise SystemExit(
            f"commission roster not readable in {db} — set ISADASH_DB to the "
            "dashboard database, or ROSTER_SOURCE=env to use .env ISA_ROSTER")
    inbound = sorted({_norm_name(n) for n, role, *_ in rows
                      if role == "inbound" and _norm_name(n)})
    if not inbound:
        raise SystemExit(f"the dashboard commission roster in {db} has no inbound "
                         "agents — refusing to run with an empty roster")
    managers: list[str] = []
    starts: dict[str, str] = {}
    for n, role, added, sd in rows:
        n = _norm_name(n)
        if role != "manager" or not n:
            continue
        managers.append(n)
        if sd and str(sd).strip():
            starts[n] = date.fromisoformat(str(sd).strip()[:10]).isoformat()
        else:
            try:
                d = date.fromisoformat(str(added or "")[:10])
            except ValueError:
                d = date.today()
            starts[n] = period_for(d, anchor_monday)[0].isoformat()
    outbound = sorted({_norm_name(n) for n, role, *_ in rows
                       if role == "outbound" and _norm_name(n)})
    return inbound, sorted(managers), starts, outbound


def _parse_start_dates(raw: str) -> dict[str, str]:
    """'Indigo Vale=2026-07-10; Other Rep=2026-08-01' -> dict. A malformed
    date fails loudly — a silent bad value would exclude a rep's every sale."""
    from datetime import date
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"ISA_REP_START_DATES entry without '=': {part!r}")
        name, d = (x.strip() for x in part.split("=", 1))
        out[name] = date.fromisoformat(d).isoformat()   # raises on bad input
    return out


def _required(env, key: str) -> str:
    """A date setting with no safe default (rulebook §8): YYYY-MM-DD, or a clear stop."""
    from datetime import date
    v = (env.get(key) or "").strip()
    if not v:
        raise SystemExit(f"{key} is not set in .env: see .env.example and COMMISSION_RULES.md §8")
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise SystemExit(f"{key}={v!r} in .env is not a date (YYYY-MM-DD)") from None


def _check_calendar(s: "Settings") -> None:
    """The go-live dates must sit on the pay calendar and in order (rulebook §8):
    checked at every start, so a typo stops here and not at a payroll close."""
    import os
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    anchor = date.fromisoformat(s.payroll_anchor_monday)
    golive = date.fromisoformat(s.go_live_period_start)
    live = date.fromisoformat(s.payroll_live_from)
    since = date.fromisoformat(s.backfill_since)
    bad = []
    if golive + timedelta(days=15) != anchor:
        bad.append(f"PAYROLL_ANCHOR_MONDAY ({anchor}) must be the payroll Monday of the first "
                   f"period the app calculates: GO_LIVE_PERIOD_START ({golive}) + 15 days = "
                   f"{golive + timedelta(days=15)}, and GO_LIVE_PERIOD_START a Sunday")
    if (live - golive).days <= 0 or (live - golive).days % 14:
        bad.append(f"PAYROLL_LIVE_FROM ({live}) must start a pay period after "
                   f"GO_LIVE_PERIOD_START ({golive}): a whole number of 14-day periods later")
    if since >= golive:
        bad.append(f"BACKFILL_SINCE ({since}) must be before GO_LIVE_PERIOD_START ({golive}); "
                   "12 months or more before is best")
    if not s.office_ids:
        bad.append("PESTROUTES_OFFICE_IDS is empty: list your FieldRoutes office ids")
    tz = (os.environ.get("OFFICE_TIMEZONE") or "").strip()
    try:
        ZoneInfo(tz) if tz else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = ""
    if not tz:
        bad.append("OFFICE_TIMEZONE must be the time zone FieldRoutes shows your office "
                   "times in, e.g. America/Denver")
    if bad:
        raise SystemExit("settings in .env don't fit together:\n  - " + "\n  - ".join(bad))


def load_settings() -> Settings:
    from datetime import date
    # override=True: the engine's own .env always wins. Scripts launched by
    # the dashboard (Lock & Run, Submit Adjustment) inherit the dashboard's
    # environment, which carries its own, different ISA_ROSTER — without
    # this the engine silently used the dashboard's roster (2026-10-21).
    load_dotenv(ROOT / ".env", override=True)
    env = os.environ
    isadash_db = Path(env["ISADASH_DB"]) if env.get("ISADASH_DB") else Settings.isadash_db
    anchor = _required(env, "PAYROLL_ANCHOR_MONDAY")
    roster = [_norm_name(n) for n in env.get("ISA_ROSTER", "").split(",") if n.strip()]
    managers: list[str] = []
    mgr_starts: dict[str, str] = {}
    outbound = [_norm_name(n) for n in env.get("OUTBOUND_AGENTS", "").split(",") if n.strip()]
    source = "env"
    if env.get("ROSTER_SOURCE", "dashboard").strip().lower() == "env":
        if not roster:
            raise SystemExit("ROSTER_SOURCE=env needs ISA_ROSTER in .env (comma-separated names)")
    else:
        # the dashboard roster (Payroll -> Commission Roster) is the source of truth
        roster, managers, mgr_starts, outbound = _roster_from_dashboard(isadash_db, anchor)
        source = "dashboard"
    s = Settings(
        pestroutes_base_url=env["PESTROUTES_BASE_URL"],
        pestroutes_auth_key=env["PESTROUTES_AUTH_KEY"],
        pestroutes_auth_token=env["PESTROUTES_AUTH_TOKEN"],
        office_ids=[o.strip() for o in env["PESTROUTES_OFFICE_IDS"].split(",") if o.strip()],
        isa_roster=roster,
        managers=managers,
        manager_start_dates=mgr_starts,
        outbound_agents=outbound,
        rate_manager=float(env.get("RATE_MANAGER", "0.05")),
        roster_source=source,
        resend_api_key=env.get("RESEND_API_KEY", ""),
        resend_from=env.get("RESEND_FROM", ""),
        resend_to=env.get("RESEND_TO", ""),
        payroll_anchor_monday=anchor,
        rate_standard=float(env.get("RATE_STANDARD", "0.09")),
        rate_alt_source=float(env.get("RATE_ALT_SOURCE", "0.05")),
        rate_no_payment_method=float(env.get("RATE_NO_PAYMENT_METHOD", "0.03")),
        alt_rate_sources=[x.strip().lower() for x in
                          env.get("ALT_RATE_SOURCES", "").split(",") if x.strip()],
        vesting_service_count=int(env.get("VESTING_SERVICE_COUNT", "3")),
        go_live_period_start=_required(env, "GO_LIVE_PERIOD_START"),
        payroll_live_from=_required(env, "PAYROLL_LIVE_FROM"),
        backfill_since=_required(env, "BACKFILL_SINCE"),
        isadash_db=isadash_db,
        gsheet_id=env.get("GSHEET_ID", ""),
        google_sa_json=(ROOT / env["GOOGLE_SA_JSON"]) if env.get("GOOGLE_SA_JSON")
                       else Settings.google_sa_json,
        rep_start_dates=_parse_start_dates(env.get("ISA_REP_START_DATES", "")),
        full_initial_floor=float(env.get("FULL_INITIAL_FLOOR", "299")),
        rate_commercial=float(env.get("RATE_COMMERCIAL", "0.05")),
        commercial_service_types=tuple(
            x.strip().lower() for x in env.get(
                "COMMERCIAL_SERVICE_TYPES", ",".join(Settings.commercial_service_types)
            ).split(",") if x.strip()),
    )
    _check_calendar(s)
    return s


def is_commercial(s: Settings, service_type: str | None,
                  customer_commercial: int | None) -> bool:
    """RULES §3 priority 0: the customer card says Commercial Property, or
    the subscription is a business-only plan."""
    return bool(customer_commercial) or (
        (service_type or "").strip().lower() in s.commercial_service_types)


def rep_start_dates(s: Settings) -> dict[str, str]:
    """RULES §1a: {canonical roster name: 'YYYY-MM-DD'}. The dashboard's
    commission_roster.start_date wins over .env; both are validated."""
    import logging
    import sqlite3
    from datetime import date
    norm = lambda n: " ".join((n or "").split()).casefold()   # noqa: E731
    roster = {norm(n): n for n in s.isa_roster}
    out: dict[str, str] = {}
    for name, d in s.rep_start_dates.items():
        if norm(name) in roster:
            out[roster[norm(name)]] = d
    try:
        c = sqlite3.connect(f"file:{s.isadash_db.as_posix()}?mode=ro", uri=True)
        try:
            rows = c.execute("SELECT name, start_date FROM commission_roster "
                             "WHERE COALESCE(TRIM(start_date),'') != ''").fetchall()
        finally:
            c.close()
    except sqlite3.Error:              # db or column not there yet: .env only
        rows = []
    for name, d in rows:
        if norm(name) in roster:
            val = date.fromisoformat(str(d)[:10]).isoformat()
            prev = out.get(roster[norm(name)])
            if prev and prev != val:
                logging.getLogger(__name__).warning(
                    "start date for %s: dashboard %s overrides .env %s",
                    name, val, prev)
            out[roster[norm(name)]] = val
    return out
