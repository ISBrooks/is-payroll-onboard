# Payroll onboarding record

The memory of this onboarding. Claude reads it at the start of every session and
updates it as things are decided and done. Short, dated, in the company's own words.
**Never put a secret, a customer's details or anyone's pay amounts here**: this file
is in git.

## Company

- Company name:
- Person making the payroll decisions:
- Who enters payroll from the workbook:

## Progress

| Phase | Status | Date | Notes |
|---|---|---|---|
| 1. Dashboard + Commission Roster | not started | | |
| 2. Install + settings | not started | | |
| 3. Backfill FieldRoutes | not started | | |
| 4. Manual payroll history | not started | | |
| 5. Parallel run | not started | | |
| 6. Switch-over | not started | | |
| 7. First live payroll | not started | | |

## Settings decided (names and dates only, never keys)

| Setting | Value | Decided | Why |
|---|---|---|---|
| PAYROLL_ANCHOR_MONDAY | | | |
| BACKFILL_SINCE | | | |
| GO_LIVE_PERIOD_START | | | |
| PAYROLL_LIVE_FROM | | | |
| CUTOVER_LIST_AT_UTC | | | |
| ALT_RATE_SOURCES | | | |
| COMMERCIAL_SERVICE_TYPES | | | |
| Rates (if not the defaults) | | | |

## Rule decisions

Any place this company's rules differ from `docs/COMMISSION_RULES.md`, and what was
changed in the rulebook and the code to match.

| Date | Rule (§) | Decision | Changed |
|---|---|---|---|

## Parallel-run findings (phase 5)

| Period | Difference | Sorted as | Done |
|---|---|---|---|
