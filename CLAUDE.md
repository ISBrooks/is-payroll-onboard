# Instructions for Claude Code

This repository is an **onboarding shell** for a commissions and payroll engine. It
is working code from a sister company's production payroll with every
company-specific value and every record removed. The person you are working with
wants it running for their own company: their own FieldRoutes data, their own
roster, their own manual payroll history, switched over on their own date.

When they say "let's start the payroll onboarding" (or anything like it), drive
[ONBOARDING.md](ONBOARDING.md) from wherever it was left off. You do the technical
work; they make the decisions and do the steps that need a login only they have.

## Who you are working with

Assume a business owner, not a developer, unless they show you otherwise.

- Plain language. Say what a step does for them before saying how.
- When they must run or click something, give the exact command or clicks, one step
  at a time, and say what they should see when it worked.
- Ask one question at a time.
- **This is people's pay.** Say plainly what a number means and where it came from.
  When something does not tie, say so and stop: never round it away.

## How to run the onboarding

1. **Read first:** `README.md`, `ONBOARDING.md`, `onboarding/DECISIONS.md`, then the
   rules in `docs/COMMISSION_RULES.md` (at least §1–§8 before phase 2).
2. **Resume, don't restart.** `onboarding/DECISIONS.md` is the memory between
   sessions. Find the first unfinished phase, say in a sentence or two where things
   stand, then continue.
3. **Write every decision down as it is made**, in `onboarding/DECISIONS.md`, dated,
   in their words.
4. **One phase at a time.** Stop at the end of each with what is done, what you
   checked and how, and what is open.
5. **Keep the tests green:** `python -m pytest` before and after every code change.

## Hard rules

- **Never print, paste, log or commit a secret.** Keys live only in `.env`
  (gitignored). Show key NAMES and whether each is filled, never values. Never ask
  them to paste a key into the chat.
- **FieldRoutes responses echo the API key and token** in their `params` field.
  Never print a raw response; strip `params` first.
- **The engine only READS FieldRoutes.** Never add anything that writes to it.
- **Their payroll history is private.** It lives in `data/` (gitignored) and never
  goes into git, a chat summary of names and amounts beyond what a check needs, or
  any outside service.
- **Ask before every step that writes the ledger or can't be redone:**
  `load_history.py --write`, `build_cutover.py --apply`, `report_payroll.py
  --finalize`, the first live **Lock & run payroll**. Show the dry run first.
- **`build_cutover.py --apply` is run once.** It refuses unless every rep ties to
  the manual history to the cent. Never work around that refusal: find the
  difference and explain it.
- **A period that has been paid is final.** Never re-run or edit a final period;
  corrections land in the next one (rules §8c).
- **The rules are the original company's, and this company said it uses the same
  rules.** Change a rule only after they decide it, and record the decision. When you
  change one, change `docs/COMMISSION_RULES.md` in the same commit: the code follows
  that file.
- **Nothing from the original company comes back in.** Every name and account number
  in the docs and comments is made up (accounts are 900xxx). If you find a value that
  looks real and belongs to someone else, stop and tell them.
- **One writer at a time.** SQLite allows one writer: never run a backfill or sync
  while a payroll run is going, and don't run the engine's FieldRoutes pulls while
  the dashboard's own sync is running (they may share the API's rate limit).
