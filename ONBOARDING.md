# Payroll onboarding

Seven phases, in order. Each ends with something checked. Record every decision and
every finished phase in [onboarding/DECISIONS.md](onboarding/DECISIONS.md).

The order matters: the engine reads its roster from the dashboard, so the dashboard
comes first; it pays only from history it has synced, so the backfill comes before
any statement; claw-backs on old sales use what the manual payroll paid, so that
history is loaded before the parallel run; and the switch-over is built from it.

```
1 Dashboard + roster  ->  2 Install + settings  ->  3 Backfill  ->  4 Manual history
   ->  5 Parallel run  ->  6 Switch-over (once)  ->  7 First live payroll
```

---

## Phase 1: the dashboard and its roster

**Goal:** the engine has someone to pay.

1. The IS Dashboard is installed (its own onboarding) and syncing FieldRoutes.
2. On the dashboard, **Payroll → Commission Roster** lists:
   - every inside-sales rep the engine pays, as **inbound**;
   - any manager who earns the flat manager rate (rules §14), as **manager**;
   - any outbound agent who shares sales with inside-sales reps, as **outbound**
     (not paid by this engine; form intake needs their names to recognise an
     outbound split).
3. Each name is spelled the way FieldRoutes or the agent roster spells it (the
   roster offers the right spellings to pick from). A start date is set for anyone
   who moved into inside sales from another role (rules §1a).

**Check:** the roster shows at least one inbound rep. The engine refuses to run
without one.

## Phase 2: install and settings

**Goal:** the engine runs on the same server as the dashboard, with this company's
settings.

1. Put this folder at `/home/isdash/is-commissions` (the dashboard's status checks
   expect that path), owned by the same user as the dashboard:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
   cp .env.example .env
   ```
   The dashboard runs the engine with `.venv/bin/python3` from this folder.
2. Fill `.env` (each setting is explained in `.env.example`). The person fills the
   FieldRoutes key and token themselves; `PESTROUTES_BASE_URL` and
   `PESTROUTES_OFFICE_IDS` are the same as the dashboard's. Walk through the rest one
   question at a time and record each answer:
   - **Time zone:** `OFFICE_TIMEZONE`, the zone FieldRoutes shows their office times
     in (the same as the dashboard's reporting time zone).
   - **Go-live dates** (rules §8): `GO_LIVE_PERIOD_START`, the Sunday that starts
     the first pay period the app calculates (the parallel run starts there; it is
     also the day the Moved / re-signed / fee rules start applying to cancels).
     Choose a period a few periods back that the manual payroll already paid, so
     there is something to compare. `PAYROLL_ANCHOR_MONDAY` is that period's payroll
     Monday (15 days later). `PAYROLL_LIVE_FROM` is the first period paid from the
     app, a few periods out: phases 4 to 6 take time. `BACKFILL_SINCE` is at least
     12 months before `GO_LIVE_PERIOD_START`. The engine refuses to start if these
     don't fit the pay calendar, and the dashboard must have the same
     `PAYROLL_ANCHOR_MONDAY` and `PAYROLL_LIVE_FROM` (its built-in fallbacks are the
     original company's dates).
   - **Rates:** the defaults are the rules as written (9% standard, 5% from
     alternate-rate sources, 3% with no payment method on file, 5% commercial, 5%
     managers). They said their rules are the same; confirm each and leave the
     defaults unless they say otherwise.
   - **Which FieldRoutes sources pay the alternate rate** (`ALT_RATE_SOURCES`) and
     **which service types are business-only plans** (`COMMERCIAL_SERVICE_TYPES`):
     their own FieldRoutes labels. Fill them after the backfill, from
     `scripts/check_labels.py` (phase 3), before phase 5.
   - Leave `CUTOVER_LIST_AT_UTC` empty until phase 6.
3. On the dashboard's server settings (its `.env`), point it at the engine:
   `COMMISSIONS_DB=/home/isdash/is-commissions/data/commissions.sqlite`,
   `COMMISSIONS_APP_DIR=/home/isdash/is-commissions`,
   `COMMISSION_RULES_PATH=/home/isdash/is-commissions/docs/COMMISSION_RULES.md`, and
   the same `PAYROLL_LIVE_FROM` and `PAYROLL_ANCHOR_MONDAY`. The dashboard's web
   service must be allowed to write the engine's `data/` and `logs/` folders (add
   them to `ReadWritePaths` in its systemd unit), because **Lock & run payroll** and
   **Submit Adjustment** start engine scripts from it.

**Check:** `python -m pytest` passes in this folder, and
`.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from commissions.config import load_settings; s=load_settings(); print(len(s.isa_roster), 'inbound reps')"`
prints the roster's count.

## Phase 3: backfill FieldRoutes history

**Goal:** every sale since `BACKFILL_SINCE`, with its payments, services and
cancellations, in `data/commissions.sqlite`.

1. Ask before the first pull against their real data. Then:
   ```
   .venv/bin/python scripts/backfill.py
   .venv/bin/python scripts/backfill_commercial.py
   ```
   The backfill is slow (it is paced to stay inside FieldRoutes' rate limit) and
   safe to re-run. Don't run it while the dashboard's own sync is running.
2. Afterwards, `scripts/sync.py` brings it up to date; every payroll run also syncs
   first.

**Check:** read back the number of subscriptions and customers, the newest sale
date, and the inbound reps found as sellers. A rep on the roster with no sales at
all usually means a spelling difference: fix the roster name.

**Then check their labels:** `.venv/bin/python scripts/check_labels.py`. Some rules
match FieldRoutes text the way the original company's account spells it: a cancel
reason starting with "Moved" means no deduction, "Renewal" is not a real cancel, a
source containing "renewal" pays the alternate rate, an "Initial Service" plan is a
one-time job, a service type containing "ancellation" is a fee invoice. The script
lists every source, cancel reason and service type in their data and which rule
recognises each. Go through it with them: a label that means the same thing but is
spelled differently ("Customer Moved") is a decision to record and a code change
(the places are listed at the top of the script). Fill `ALT_RATE_SOURCES` and
`COMMERCIAL_SERVICE_TYPES` from it.

Also ask how their door-to-door (D2D) reps appear in FieldRoutes: the engine
recognises a D2D seller by an employee name tagged like `R- Pat Example 2026` (a
letter, a dash, the name, the year; `scripts/form_intake.py`, rules §11). If theirs
are named differently, that is a decision and a code change too.

## Phase 4: the manual payroll history

**Goal:** the engine knows everything the manual payroll paid and took back, so at
go-live nothing is paid twice and nothing is deducted that was never paid.

1. `mkdir -p data/history`, copy `history_templates/*.csv` into it (never in git),
   **delete the made-up example row in each**, and fill them from their payroll
   records, from `BACKFILL_SINCE` on: commissions paid,
   adjustments, deductions (cancel claw-backs and contract-value true-downs), and
   the manual's "not paid yet" list. The column meanings are at the top of
   `scripts/load_history.py`.
2. Check (writes nothing):
   ```
   .venv/bin/python scripts/load_history.py
   ```
   Fix every problem it lists. It prints each rep's lifetime totals: they must match
   the manual payroll's own totals per rep. Compare them with the person before
   going on. (The switch-over tie-out in phase 6 leaves out upsell credits, which
   are matched to the app's own upsell records instead, so its totals can be lower
   than these by exactly the reps' upsell credits.)
3. Load (ask first): `.venv/bin/python scripts/load_history.py --write`. It
   replaces the history each time, so a corrected file is simply loaded again.

## Phase 5: parallel run

**Goal:** they trust the numbers before any pay depends on them. With the history
loaded, claw-backs on old sales use what the manual payroll really paid, so the
comparison is like for like.

For two or three recent pay periods their manual payroll already paid, make the
app's statement and compare it, rep by rep, with what the manual payroll paid:

```
.venv/bin/python scripts/run_payroll.py --period-end YYYY-MM-DD --allow-pre-go-live
```

(The period end is a Saturday. Periods before `PAYROLL_LIVE_FROM` are refused
without `--allow-pre-go-live`, and the dashboard won't run them.) The workbook
appears on the dashboard under **Payroll → Downloads**, labelled PRE-GO-LIVE.

Differences are expected: the app follows the rules exactly, and a manual payroll
rarely does. Go through the biggest ones with them and sort each into "the app is
right", "the manual was right" (a rule question: record and decide it) or "missing
data" (a split or rescue missing from the history files: add it there and load
again, phase 4).
Nothing from this phase is ever paid: the switch-over (rules §8c) keeps the past as
the manual payroll left it.

## Phase 6: the switch-over (once)

**Goal:** the app's ledger starts from exactly what each rep holds in the manual
books.

Do this around the LAST manual payroll (the period that ends the day before
`PAYROLL_LIVE_FROM`):

1. **When the manual payroll pulls its data for that period**, take the snapshot
   (the app's view of FieldRoutes at the same moment):
   ```
   .venv/bin/python scripts/sync.py
   .venv/bin/python scripts/take_snapshot.py
   ```
   (It saves a consistent copy as `data/cutover/snapshot.sqlite`; a plain file copy
   can miss the newest writes.)
2. **When the manual payroll's last adjustments list is made**, write that moment,
   in UTC, into `.env` as `CUTOVER_LIST_AT_UTC`. Splits and upsells booked in the app
   after it are the app's to pay.
3. Once that manual payroll is final, add its rows to the history files and load
   them again (phase 4, `--write`).
4. Dry run:
   ```
   .venv/bin/python scripts/build_cutover.py
   ```
   It fills the cutover tables (the ledger is untouched) and prints every rep's
   manual-books total against the app's baseline. **Every rep must tie to the
   cent.** If one doesn't, find the rows behind the difference and
   fix the history; never work around it.
5. Seed (ask first; it runs once): `.venv/bin/python scripts/build_cutover.py --apply`.
   After this the history can no longer be reloaded.

## Phase 7: the first live payroll

1. After the first live period ends (a Saturday), on the dashboard: **Payroll →
   Downloads → Lock & run payroll** for that period. The run syncs, books queued
   adjustments, closes the period and makes the workbook: week 1, week 2 and the
   whole period per rep, with hours from the time clock.
2. Read it with them before payroll is entered. Anything the close could not decide
   by itself is on the **Review** tab; nothing moves on those until someone decides.
3. A period can be re-run until it is final (its payroll Monday passes, the next
   period is run, or `report_payroll.py --period-end YYYY-MM-DD --finalize`). Don't
   re-run once payroll has been entered from it.

From here on, every pay period is phase 7. Splits, rescues and upsell credits go in
through **Payroll → Submit Adjustment** on the dashboard.

## If something is stuck

- "commission roster not readable": `ISADASH_DB` doesn't point at the dashboard's
  database, or the dashboard has no Commission Roster yet (phase 1).
- "<SETTING> is not set in .env": a required date is missing (phase 2).
- "settings in .env don't fit together": the message lists which (phase 2).
- "the go-live switch-over needs ... CUTOVER_LIST_AT_UTC": phase 6, step 2.
- "no manual payroll history is loaded": phase 4 before phase 6.
