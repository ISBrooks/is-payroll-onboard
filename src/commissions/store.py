"""SQLite store. Raw FieldRoutes mirrors + the commission ledger.

All datetimes stored as FieldRoutes gives them: office-local
(OFFICE_TIMEZONE) 'YYYY-MM-DD HH:MM:SS' strings (lexicographically sortable). The ledger is
append-only; a rep's paycheck line is SUM(amount) for a period.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  subscription_id   TEXT PRIMARY KEY,
  customer_id       TEXT,
  office_id         TEXT,
  sold_by           TEXT,
  rep               TEXT,             -- canonical roster name; NULL = not ISA-sold
  date_added        TEXT,
  contract_value    REAL,
  source            TEXT,
  service_id        TEXT,             -- recurring serviceType id
  service_type      TEXT,
  frequency         INTEGER,          -- days between services
  billing_frequency INTEGER,          -- days between invoices; <=0 bill-at-service
  recurring_charge  REAL,             -- per BILLING period
  initial_appointment_id TEXT,
  active            INTEGER,
  date_cancelled    TEXT,
  cancel_reason     TEXT,             -- from the LATEST Subscription Cancel note
  date_updated      TEXT,
  initial_quote     REAL,             -- FR initialQuote: undiscounted initial
                                      -- price (§5 cancellation-fee basis)
  initial_discount  REAL,             -- FR initialDiscount (§5 fee = recapture)
  soldby_rep_now    TEXT,             -- roster rep FR's CURRENT soldBy maps to;
                                      -- differs from rep after a soldBy move
  rep_prior         TEXT              -- ISA whose credit was removed when FR's
                                      -- soldBy moved to someone off the roster
                                      -- (keeps the sub in the engine so the
                                      -- payment is taken back, RULES §1)
);
CREATE INDEX IF NOT EXISTS idx_subs_rep ON subscriptions(rep);
CREATE INDEX IF NOT EXISTS idx_subs_cust ON subscriptions(customer_id);

CREATE TABLE IF NOT EXISTS customers (
  customer_id   TEXT PRIMARY KEY,
  office_id     TEXT,
  name          TEXT,
  card_on_file  INTEGER,              -- payment method saved (RULES §3)
  autopay       INTEGER,
  status        TEXT,
  date_updated  TEXT,
  commercial    INTEGER               -- card says Commercial Property (§3)
);

CREATE TABLE IF NOT EXISTS appointments (
  appointment_id  TEXT PRIMARY KEY,
  subscription_id TEXT,
  customer_id     TEXT,
  type            TEXT,               -- serviceType id
  status_text     TEXT,
  date            TEXT,
  date_completed  TEXT,
  ticket_id       TEXT,
  is_initial      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_appts_sub ON appointments(subscription_id);

CREATE TABLE IF NOT EXISTS tickets (
  ticket_id      TEXT PRIMARY KEY,
  customer_id    TEXT,
  appointment_id TEXT,                -- '0' when not service-tied
  autogen_sub_id TEXT,                -- subscriptionID for RECURRING invoices
  service_id     TEXT,
  total          REAL,
  balance        REAL,
  invoice_date   TEXT,
  active         INTEGER,
  paid_date      TEXT,                -- last dateApplied when balance hit 0
  writeoff_tainted INTEGER DEFAULT 0, -- any applying payment was a write-off
  coupon_paid    REAL DEFAULT 0,      -- dollars applied via coupon (method 0);
                                      -- excluded from §4b coverage
  sub_total      REAL                 -- pre-tax amount (FR subTotal)
);
CREATE INDEX IF NOT EXISTS idx_tickets_cust ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_autogen ON tickets(autogen_sub_id);
CREATE INDEX IF NOT EXISTS idx_tickets_appt ON tickets(appointment_id);

CREATE TABLE IF NOT EXISTS service_types (
  type_id     TEXT PRIMARY KEY,
  description TEXT,
  reservice   INTEGER,
  is_initial  INTEGER,
  office_id   TEXT
);

-- Immutable commission ledger. One row per adjustment.
CREATE TABLE IF NOT EXISTS ledger (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_id TEXT NOT NULL,
  rep             TEXT NOT NULL,
  period_end      TEXT NOT NULL,      -- Saturday YYYY-MM-DD the row settles in
  amount          REAL NOT NULL,      -- + pay, - deduction
  kind            TEXT NOT NULL,      -- initial|trueup|clawback|repay|vest|
                                      -- external|upsell|manual
  note            TEXT,
  ref             TEXT,               -- idempotency key (e.g. 'upsell:7')
  created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_sub ON ledger(subscription_id);
CREATE INDEX IF NOT EXISTS idx_ledger_period ON ledger(period_end, rep);
CREATE INDEX IF NOT EXISTS idx_ledger_sub_rep ON ledger(subscription_id, rep);

-- Derived per-subscription commission state (recomputed, not authoritative;
-- the ledger is authoritative for money).
CREATE TABLE IF NOT EXISTS sub_status (
  subscription_id     TEXT PRIMARY KEY,
  rep                 TEXT,
  first_paid_date     TEXT,           -- trigger date (RULES §2)
  qualifying_completed INTEGER,
  coverage_paid       REAL,
  coverage_target     REAL,
  rate                REAL,
  expected            REAL,
  vested_date         TEXT,
  state               TEXT,           -- pending|paid|vested|excluded_renewal|
                                      -- clawed_back|not_started
  flags               TEXT,           -- ; separated anomaly flags
  updated_at          TEXT
);

-- Manual attribution overrides (RULES §11): splits and D2D rescues.
-- When active rows exist for a subscription they REPLACE the soldBy
-- attribution. Soft-deleted via removed_at (full audit trail; removing a
-- share after payment correctly claws it back on the next run).
CREATE TABLE IF NOT EXISTS assignments (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_id TEXT NOT NULL,
  rep             TEXT NOT NULL,      -- canonical roster name
  share           REAL NOT NULL,      -- fraction of the commission (0..1]
  type            TEXT NOT NULL,      -- split | rescue
  note            TEXT,
  created_at      TEXT DEFAULT (datetime('now')),
  removed_at      TEXT                -- NULL = active
);
CREATE INDEX IF NOT EXISTS idx_assign_sub ON assignments(subscription_id);

-- One-time upsell credits (RULES §12). The upselling rep is paid `amount`
-- once in the period containing `date`; `acv_increase` is excluded from the
-- account's own commission math while it is inside the 3-service window so
-- the original rep is never trued up on value credited to someone else.
-- `d2d_rep` marks upsells on D2D-sold accounts for the year-end finance
-- report (D2D reps are paid only on the ACV they originally sold).
CREATE TABLE IF NOT EXISTS upsells (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_id TEXT NOT NULL,
  rep             TEXT NOT NULL,
  amount          REAL NOT NULL,
  acv_increase    REAL,
  date            TEXT NOT NULL,
  d2d_rep         TEXT,   -- free text from the form era: D2D name or tech name
  d2d_host        TEXT,   -- auto: the host sub's year-tagged D2D seller label
                          -- (e.g. 'R- Pat Example 2026') when soldBy is a D2D
                          -- rep; drives the year-end D2D finance report
  note            TEXT,
  created_at      TEXT DEFAULT (datetime('now')),
  removed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_upsells_sub ON upsells(subscription_id);

-- Re-signed detection (RULES §5): a cancelled ISA sub whose customer got an
-- ACTIVE replacement subscription (any seller — retention, D2D, ISA) within
-- -14d..+90d of the cancel. Filled by ingest.refresh_replacements.
CREATE TABLE IF NOT EXISTS replacements (
  subscription_id  TEXT PRIMARY KEY,
  replacement_sub_id TEXT,
  replacement_added  TEXT,
  replacement_soldby TEXT,
  checked_at TEXT
);

-- Every OTHER subscription on customers we track (non-ISA sellers included),
-- so a cancellation fee is matched to the right cancellation (RULES §5):
-- a fee billed for a D2D/retention sub's cancel must not be credited to the
-- ISA sub on the same account.
CREATE TABLE IF NOT EXISTS customer_subs (
  subscription_id TEXT PRIMARY KEY,
  customer_id     TEXT,
  date_added      TEXT,
  date_cancelled  TEXT,
  active          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_custsubs_cust ON customer_subs(customer_id);

-- Vesting is permanent (RULES §4): the first vest date the engine ever saw.
-- The coverage test is judged on the CURRENT plan, so a later price or plan
-- change would otherwise un-vest a locked account (900014); the statement
-- re-simulation wipes the ledger's 'vest' freezes, so the lock lives here.
CREATE TABLE IF NOT EXISTS vest_lock (
  subscription_id TEXT PRIMARY KEY,
  vested_date     TEXT NOT NULL,
  source          TEXT,               -- computed | ledger
  locked_at       TEXT DEFAULT (datetime('now'))
);

-- Go-live cutover (RULES §8c). Per sale: its state and contract value at the
-- cutover and how it is handled from go-live (stand = manual result stands,
-- moves only on post-cutover events; app = normal rules; settle = normal
-- rules, correcting the past difference). Per sale+rep: what the manual books
-- show the rep holding (paid, adjustments, CV true-downs, deductions).
-- Manual rows that could not be pinned to a sale sit in cutover_legacy.
CREATE TABLE IF NOT EXISTS cutover_sale (
  subscription_id TEXT PRIMARY KEY,
  customer_id     TEXT,
  state_cut       TEXT,             -- active|pending|claw|half|excluded|vested
  cv_cut          REAL,
  shares_cut      TEXT,             -- JSON {rep: share}
  manual_paid     INTEGER,
  disposition     TEXT,             -- stand|app|settle
  note            TEXT,
  built_at        TEXT
);
CREATE TABLE IF NOT EXISTS cutover_baseline (
  subscription_id TEXT,
  rep             TEXT,
  g REAL, a REAL, t REAL, d REAL,   -- paid, adjustments, CV true-downs, deductions
  held            REAL,
  rate            REAL,             -- rate the rep was paid (CV true-ups use it)
  PRIMARY KEY (subscription_id, rep)
);
CREATE TABLE IF NOT EXISTS cutover_legacy (rep TEXT, kind TEXT, amount REAL, rows INTEGER);
-- each stand sale's state after each live close (the next close compares to it)
CREATE TABLE IF NOT EXISTS live_state (
  period_end      TEXT,
  subscription_id TEXT,
  state           TEXT,
  cv              REAL,
  shares          TEXT,
  PRIMARY KEY (period_end, subscription_id)
);
CREATE INDEX IF NOT EXISTS idx_live_state_sub ON live_state(subscription_id, period_end);
-- things a live close did NOT do automatically and the owner should look at
CREATE TABLE IF NOT EXISTS live_flags (
  period_end      TEXT,
  subscription_id TEXT,
  note            TEXT
);
-- one row per live pay period run (draft until its payroll Monday passes)
CREATE TABLE IF NOT EXISTS payroll_runs (
  period_end  TEXT PRIMARY KEY,
  status      TEXT,
  started_at  TEXT,
  finished_at TEXT,
  data_as_of  TEXT,
  totals      TEXT
);

-- RULES §6: the contract value each live sale's commission rests on after
-- each close (set at first payment, lowered by cuts, never raised: a raise
-- pays only through a submitted upsell)
CREATE TABLE IF NOT EXISTS cv_basis (
  subscription_id TEXT NOT NULL,
  period_end      TEXT NOT NULL,
  cv              REAL NOT NULL,
  PRIMARY KEY (subscription_id, period_end)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# columns added after first deploy: (table, column, type)
MIGRATIONS = [
    ("subscriptions", "initial_quote", "REAL"),
    ("subscriptions", "initial_discount", "REAL"),
    ("subscriptions", "soldby_rep_now", "TEXT"),
    ("tickets", "sub_total", "REAL"),
    ("subscriptions", "cancel_note_id", "TEXT"),     # note the reason came from
    ("subscriptions", "cancel_note_added", "TEXT"),  # its dateAdded (latest wins)
    ("upsells", "d2d_host", "TEXT"),
    ("cutover_baseline", "rate", "REAL"),
    ("customers", "commercial", "INTEGER"),          # RULES §3 priority 0
    ("subscriptions", "rep_prior", "TEXT"),          # RULES §1 soldBy follows FR
    ("subscriptions", "rep_off_roster", "TEXT"),     # RULES §1: the rep tag cleared when
                                                     # that rep left the roster (who sold it)
    ("ledger", "event_date", "TEXT"),                # RULES §7 weekly split: the
                                                     # FR-dated event the line settles
]


def active_shares(conn: sqlite3.Connection) -> dict[str, list[tuple[str, float, str]]]:
    """{subscription_id: [(rep, share, type), ...]} for active assignments."""
    out: dict[str, list[tuple[str, float, str]]] = {}
    for sid, rep, share, typ in conn.execute(
            "SELECT subscription_id, rep, share, type FROM assignments "
            "WHERE removed_at IS NULL"):
        out.setdefault(sid, []).append((rep, share, typ))
    return out


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None
