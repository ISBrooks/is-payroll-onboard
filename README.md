# Commissions engine: onboarding shell

The commissions and payroll engine behind the IS Dashboard's **Payroll** tab, with
every trace of the company it came from removed. It calculates inside-sales
commissions every two weeks from FieldRoutes (PestRoutes) data: what each rep earned,
what is taken back when a sale cancels early, and the payroll workbook the dashboard
hands out, split into week 1 and week 2 with each rep's hours.

It is a **shell**: the code and the rules are complete, but it holds no data. Your
company fills it with its own FieldRoutes history and its own manual payroll history,
then switches over from the manual payroll on a date you choose.

**Start here:** open this folder in Claude Code and say *"let's start the payroll
onboarding"*. Claude follows [ONBOARDING.md](ONBOARDING.md) and records every
decision in [onboarding/DECISIONS.md](onboarding/DECISIONS.md).

## What is in it

| Part | Where |
|---|---|
| The rules, the canonical reference the code follows | [docs/COMMISSION_RULES.md](docs/COMMISSION_RULES.md) |
| Payroll calendar and process | [docs/PAYROLL_RULES.md](docs/PAYROLL_RULES.md) |
| What FieldRoutes data the engine reads, and its quirks | [docs/DISCOVERY.md](docs/DISCOVERY.md) |
| The engine | `src/commissions/` |
| Sync, payroll run, statements, adjustments, switch-over | `scripts/` (each script says what it does at the top) |
| Your manual payroll history: fill-in files | `history_templates/`, loaded by `scripts/load_history.py` |
| Tests on made-up data | `tests/` (`python -m pytest`) |

## How it fits with the dashboard

- The dashboard must be installed first. Its **Payroll → Commission Roster** decides
  who the engine pays (inbound reps, managers) and who the outbound agents are. The
  engine reads it, read-only, from the dashboard's database.
- The dashboard's **Lock & run payroll** button starts `scripts/run_payroll.py`, and
  its **Submit Adjustment** form queues splits, rescues and upsell credits for
  `scripts/form_intake.py` to book.
- The engine's database (`data/commissions.sqlite`) is read by the dashboard's
  Commission Lookup and Downloads.

## Never in git

`.env` (keys), `secrets/`, `data/` (the ledger, statements, your payroll history),
`logs/`. The `.gitignore` already keeps them out.
