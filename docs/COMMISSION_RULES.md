# Inside Sales Commission Rules — Canonical Reference

This is the single source of truth for how inside-sales commissions are
calculated. The engine implements exactly what is written here; if payroll
policy changes, change THIS FILE first, then the code. Last confirmed with
owner 2026-08-27.

---

## 1. The unit of commission

A commission attaches to a **subscription** sold by an inside sales agent
(`soldBy` = one of the agent's FieldRoutes employee IDs). The commissionable
amount is the subscription's **contract value** (`contractValue`) at the time
of evaluation — never the invoice or service price.

**The credited rep is whoever FieldRoutes' soldBy maps to at each sync**
(owner, 2026-11-03). A lead whose first appointment never happened and which
another rep later sells is credited to that rep once soldBy is updated. If
soldBy moves to another ISA, the credit moves with it (the first rep's
payment is taken back, the new rep is paid). If soldBy moves to someone
**off the roster** (outbound agent, D2D rep, Kai…), no ISA earns it: the
sub keeps its `rep_prior` so the payment is taken back at the next close
(statement flag `soldby-moved-off-roster`). A booked assignment (§11)
always wins over soldBy. Sales the manual books paid before go-live never
move on their own — a soldBy change on one goes on the Review tab and is
booked by hand (§8c).

**The commission roster is the dashboard's Payroll → Commission Roster**
(owner, 2026-11-23). The engine reads it at every run: role **inbound** =
ISA (this rulebook), **manager** = §14, **outbound** = not paid here (form
intake reads them to recognise an outbound split; since 2026-11-24 the only
list of outbound agents). Only
people on it get lines. **Removing someone takes them off the next
statement entirely — no commissions and no deductions** (Morgan Hale and
Reese Frost, removed before the 11/21 close, still showed deductions on
it because the engine was reading its own `.env` ISA_ROSTER, not this
list). Their subscriptions lose the rep tag at the next sync
(`rep_off_roster` remembers who sold them, so a rule that looks at another
sale's seller — the Bad-Debt-Renewal re-sell, §5 — does not change for
anyone else); a split partner keeps their own share. Taking someone off
and putting them back before the next sync loses nothing; after a sync,
their already-stored open sales are not re-tagged. `.env` ISA_ROSTER is
used only with `ROSTER_SOURCE=env` (dev laptops); if the dashboard roster
can't be read, or has no inbound agents, the run stops instead of
guessing.

### 1a. Inside-sales start date (owner, 2026-10-14)

(Inbound ISAs only — a manager's start date works differently, §14.)
A rep earns inside-sales commission only on subscriptions **sold on or after
the date they joined the inside sales team** — their ISA start date, set per
rep in Payroll → Commission Roster (blank = no limit; it is NOT the company
hire date). Some reps sold under the same FieldRoutes logins while on another
team: Indigo Vale was on the retention team until **2026-07-10**, and his
retention renewals carry his soldBy. A subscription whose sold date
(FieldRoutes dateAdded) is before the rep's start date is **not an ISA sale**:
no commission, no true-up, no deduction — exactly as if a non-ISA employee
had sold it — and it is never booked at cutover (state `excluded_prestart`).

Exceptions: (a) a pre-start sale the **manual process actually paid** (a
payment dated on/after that sale, and the rep has no later sale to the same
customer) stays a normal account — it can still be deducted until it vests;
(b) an assignment (§11) that credits someone OTHER than the pre-start rep
still applies. A subscription renewed in place after the start date (dateAdded
reset) counts as a sale on its new date. Start dates are validated; a
malformed date is rejected rather than silently excluding a rep's sales.

## 2. When a commission is EARNED (initial payment trigger)

A rep is paid for a subscription when **both** are true:

1. The subscription's **first qualifying service is Completed**, and
2. the **invoice (ticket) for that service is fully paid** (balance $0,
   not via write-off). **Coupon payments count here** (confirmed 2026-10-01):
   a first-service invoice closed by coupon, in part or in full, is paid
   for trigger purposes. The real-money rule (§4b) applies only to the
   invoices AFTER the first.

The first service normally carries its own invoice regardless of billing
frequency ("the initial will always be one invoice tied to the initial
service"). The **first qualifying service** is the earliest qualifying
completed appointment — whether or not it was correctly flagged as an
Initial (see §4a, miscategorized initials).

The commission lands in the pay period containing the **later** of the two
dates (completion date vs. final payment date). Example: serviced 8/14 but
paid 8/17 → it belongs to the 8/16–8/29 period (paid Mon 8/31), NOT the
8/2–8/15 period.

## 3. Commission rate

Evaluated per subscription, re-evaluated at every true-up (see §6):

| Priority | Condition | Rate |
|---|---|---|
| 0 (wins) | **Commercial account** (owner, 2026-10-20): the FieldRoutes customer card's property type is **Commercial Property**, OR the subscription is a business-only plan (**COMMERCIAL MONTHLY PEST**, **Monthly Business Only**, **CLARK Monthly Commercial Business**) | **5%** |
| 1 | Customer has **no form of payment saved** on the account | **3%** |
| 2 | Subscription source is **Mosquito upsell Campaign**, or **any renewal source** — any source whose name contains "Renewal" (Inside Sales Renewal, Bad Debt Renewal, Renewal - In/Out of/No Contract, Rate Raise Renewal, ZOLD - Renewal …). owner 2026-10-14: every renewal an ISA sells pays 5% | **5%** |
| 2b | **One-time job** (Initial Service subscription / no recurring charge) — CFO ruling 2026-10-05, supersedes the 2026-10-01 9% ruling | **5%** |
| 2c | Source **'Upsell'** on an **8- or 12-month mosquito** subscription — CFO ruling 2026-10-05 | **5%** |
| 3 | Everything else | **9%** |

- "Form of payment saved" means a payment profile or card exists on the
  customer account (`autoPayPaymentProfileID` set OR
  `mostRecentCreditCardLastFour` present). **Autopay enrollment is
  irrelevant** — only that a payment method is stored.
- **"Source" is always the FieldRoutes SUBSCRIPTION source** — the value
  FieldRoutes' Customers › Sales Report prints in its Source column (blank
  prints "N/A"); the customer-level source is never used. Row 2c applies
  only when that subscription source is "Upsell". If a sale was entered
  under the wrong source, fix it in FieldRoutes — the app picks the change up
  at the next sync and trues up unvested accounts. Hand-editing a payroll
  spreadsheet's Source cell is not a correction (acct 900001, 2026-10-14:
  FR source 'Source 54', the manual sheet's 'Upsell' was typed over).
- The 3% no-payment-method rate overrides source-based rates in all cases.
  Only the commercial 5% beats it.
- If a card is later added or removed before vesting, the rate changes and
  the difference is trued up (§6).
- **Commercial accounts are always 5%, no matter what**, even with no card
  on file. "Commercial Property" is the customer card's property-type
  toggle (FieldRoutes `commercialAccount`), re-read at every sync. Like the
  card, it is re-checked until the sale vests: a card switched to
  Commercial before vesting trues the commission to 5%.
- **Money already paid keeps its rate.** A sale the manual books paid
  before go-live (§8c) keeps what the rep holds. If it cancels, the rep is
  deducted exactly what they hold (the full 9% when they were paid 9%), and
  a contract-value cut comes off at the as-paid rate. Only new money from
  10/11 on is paid at 5%: first payments. (A contract-value raise pays
  nothing on its own, §6.)
- An upsell on a commercial account pays 5% of the ACV increase (§12).

## 4. Qualifying services (the 3-paid-services retention rule)

The rep **keeps** the commission permanently only if the subscription
receives **3 qualifying completed services** AND the **value of those
services is fully paid** (§4b — payment coverage depends on billing
frequency).

A **qualifying service** is an appointment that:
- belongs to the subscription the rep sold (`appointment.subscriptionID`),
- is status **Completed**, and
- is of the right service type: the **Initial Service** (counts as #1 of 3)
  or a **recurring plan service** (e.g. MONTHLY PEST, QUARTERLY PEST, 8
  MONTH MOSQUITO). **Plan changes (owner, 2026-10-14):** when the
  subscription's plan changes mid-life (e.g. Bimonthly → Quarterly), the
  services completed under the earlier plan still count — they are the
  same subscription's recurring services. Engine: `plan_type_ids` = every
  service type some subscription uses as its plan, minus reservices,
  General Service, Inspection and fees.

**Explicitly NOT qualifying:** Reservices (free re-treatments) and General
Services (e.g. tech couldn't access the yard, courtesy visits) — these are
distinct service types in FieldRoutes and never count toward the 3, even
though they hang off the same subscription.

**A re-sold subscription restarts its count** (CFO 2026-11-09). When
FieldRoutes re-uses an old subscription record for a new sale (sold date
reset; 900003, 900091), only services and payments from the new sold date
on count toward the trigger, the 3 services and the payment coverage — so
it is never vested the moment it is sold. A service up to 14 days before
the sold date still counts (data-entry lag; real re-sales are months or
years apart). Renewal-source subscriptions already worked this way (1
day). Sales the manual books settled before go-live keep the count they
were settled on. A re-sold subscription the app had already paid and
treated as vested under the earlier rule keeps its payment (flag
`paid-at-resale`) but is un-vested: it is deducted if it cancels before 3
new paid services.

### 4a. Miscategorized initial services still count

Schedulers sometimes book the first visit as the subscription's regular
recurring type (e.g. BIMONTHLY PEST) instead of Initial Service. That
appointment **still counts** — as the first qualifying service for the §2
trigger and toward the 3-service count — as long as it is completed and its
value is paid. Never lose a commission over a scheduling miscategorization.
(These are surfaced on the report so the data can be cleaned up.)

### 4b. Payment coverage by billing frequency

"Paid" is measured in **dollars of paid subscription invoices**, because
billing frequency decouples invoices from services:

- **Bill-at-service accounts:** each service generates its own invoice at
  the correct per-service amount. Coverage = the first service's invoice
  paid + the 2 additional services' invoices paid. (3 paid invoices.)
- **Monthly-billed accounts (the majority):** the service might be $99
  every 2 months while the customer is invoiced $49.50 monthly. Coverage =
  the initial service's invoice paid + **enough fully-paid monthly invoices
  to add up to the value of the 2 additional services** (2 × per-service
  recurring value). Example: $99 bimonthly service billed $49.50/month →
  initial invoice + 4 paid monthly invoices ($198) → vested.

Unified test the engine applies to every subscription regardless of billing
mode:

    vested ⇔ (≥ 3 qualifying completed services)
          AND (first service's invoice fully paid)
          AND (sum of OTHER fully-paid subscription invoices
               ≥ 2 × per-service recurring value)

Only fully-paid invoices count (balance $0, no write-offs); partial
payments on an open invoice contribute nothing until the invoice closes.
**Coupon payments are not real money** (added 2026-09-18, Padron case): the
couponed portion of an invoice contributes $0 to coverage — only the
cash/card/check/ACH-paid portion counts. Example: a $30 monthly bill paid
$20 by coupon + $10 by card contributes $10.

**Exception — the FIRST service's invoice (confirmed by owner 2026-10-01):
a couponed first service fully counts.** A first-service invoice closed
partly or entirely by coupon still fires the §2 trigger on its paid date,
and never reduces anything — the first service's invoice is excluded from
the coverage sum entirely (coverage is measured only on the OTHER
invoices). The real-money requirement applies to every invoice after the
first: recurring service invoices and monthly bills must be paid by
card/cash/check/ACH to count toward the 2× coverage test.

## 5. Vesting and deduction (the second decision point)

- **Vested:** once 3 qualifying paid services exist, the commission is locked
  permanently at the final trued-up amount. No later event can claw it back.
  That includes a later **price or plan change**: the coverage test is
  judged on the plan in effect when the account vested, and the engine
  records the first vest date it sees and never re-derives it (900014:
  vested 7/24 at $89 per service, re-planned 9/16 to $135 quarterly — still
  vested, no deduction when it later cancelled).
- **One-time jobs vest when they are paid** (owner, 2026-10-23). A one-time
  job (the §3 row 2b definition: an "Initial Service" subscription, or no
  recurring charge and no frequency) has no later services to wait for. Once
  its service is completed and its invoice paid, the commission is kept. A
  later cancel never deducts it; these are often cancelled only to close
  the account for reporting (900022: Morgan, paid 9/14, cancelled 10/12
  "Initial Appointment Canceled"). The manual payroll never deducted one
  either (0 of 27 paid-then-cancelled).
- **Deduction (claw-back):** if the subscription is **cancelled before
  vesting**, the full commission previously paid is deducted from the rep's
  next commission run — EXCEPT:
- **Paid-cancellation-fee exception** (added 2026-09-18; tightened
  2026-10-14): if the customer **pays THIS subscription's cancellation fee
  with real money**, the rep **keeps HALF** the commission instead of losing
  all of it. the company's cancellation fee is the **recapture of the
  initial-service discount** (fee = undiscounted initial price, FieldRoutes
  `initialQuote`, minus the discounted initial actually charged). The fee
  counts as paid by any of three routes:
  1. a **Cancellation Charge** invoice for THIS cancellation, paid in full;
  2. an **ad-hoc invoice** (untyped or plan-type, no appointment) billed
     within 14 days of the cancel for the recapture amount (the
     subscription's initial discount, ±$1), paid in full — CSRs sometimes
     bill the fee this way (e.g. 900032, $291 untyped);
  3. **no fee owed because the initial was bought at full price:** the
     first-service invoice was paid with real money for at least the
     undiscounted initial price (`initialQuote`, never less than $299), so
     the recapture is $0 and no fee is billed (900025: $350 initial, $0
     discount — "deduct half since full fee was paid", owner 2026-10-14).
     Recurring subscriptions only.
  Routes 2 and 3 apply to cancels on/after go-live.
  A fee invoice **belongs to a cancellation** only when it is on the same
  customer, invoiced **on or after the subscription's sold date**, between
  **30 days before and 60 days after** the cancel, and no OTHER subscription
  on the account (ISA-sold or not) was cancelled closer to the invoice date.
  A fee left over from an earlier cancellation — a previous subscription, a
  previous owner, a fee from years ago — **never counts** (900047: 2023 fee;
  900046: 9/12/2024 fee).
  **"Paid" means real money:** balance $0, nothing written off, and **no
  coupon dollars applied**. A fee closed by coupon — including a card
  payment refunded and replaced by a coupon — is a **waived** fee: full
  deduction (900030; 900010; 900005). A refund is never a payment date, and
  a **voided** charge (FieldRoutes note "Voided") never happened at all — it
  can never date any invoice (900042: two card charges voided on 9/12, paid
  by check 9/24 → paid 9/24).
  **Event-dated:** the half-keep counts from the fee's paid date. If the fee
  is still unpaid when the cancel is deducted, the full commission is
  deducted then, and half is repaid in the period the fee is paid.
- **Moved exclusion** (added 2026-09-18): if the cancellation reason
  **begins with "Moved"** (including "Moved OOC" — widened 2026-10-05), the
  rep keeps the commission — moving away is not the rep's fault. Applies to
  cancels dated after go-live (2026-08-16); older ones stay as the manual
  process settled them. When an account has cancelled more than once with
  different reasons, the reason of the LATEST cancellation governs (see
  "Which cancellation governs" below).
- **Re-signed exclusion** (added 2026-09-18): if the customer is re-signed
  onto a **new ACTIVE subscription** (by anyone — retention, D2D, or ISA)
  within **-14 to +90 days** of the cancel, the original rep keeps the
  commission — same spirit as the Renewal exclusion (e.g. bad-debt customer
  pays their balance and the retention team re-sets them up). Detected
  automatically each sync via a replacement-subscription scan. Post-go-live
  cancels only.
- **Renewal exclusion:** if the subscription's cancellation reason is
  **"Renewal"** (company renewed the customer onto a new/higher contract,
  possibly by another department), the rep **keeps the commission**,
  regardless of service count, **for as long as that Renewal cancellation
  stands**. If FieldRoutes later **reinstates** the subscription, the
  exclusion lapses and the account rejoins the normal pool (§6
  Reactivated); if it is then cancelled again before vesting, the new
  cancellation is judged on its own reason (900016: Renewal 6/03,
  reinstated 10/01, cancelled "Out of Contract" 10/05 → deductible).
- **Which cancellation governs** (2026-10-14): deductions and exclusions are
  judged on the subscription's CURRENT FieldRoutes state and on the reason
  of its LATEST "Subscription Cancel" note. Every sync re-reads every
  renewal-excluded subscription (cancelled or not) and sweeps cancel notes
  added or edited since the last sync, so a reinstatement, a re-cancel, or
  a reason corrected after the fact is picked up automatically. An edit to
  an OLDER cancellation's note never overrides a newer one.
  - Edge case: a Renewal-cancel on a subscription whose first service was
    never completed+paid (rep never received commission) is **flagged as an
    anomaly** on the report for manual review. It should never happen; the
    engine pays nothing automatically.
- **Bad-Debt-Renewal re-sell override (CFO ruling 2026-10-05):** the
  Renewal / Re-signed exclusions do NOT protect the old subscription when a
  **different agent** is paid on the customer's newer subscription sourced
  **"Bad Debt Renewal"**. In that case the original agent's commission is
  **deducted** (at the actual amount paid, §8) and the new agent earns 5%
  (§3). Same-agent renewals keep the exclusion. **"Is paid" is literal:** a
  Bad Debt Renewal that was cancelled before its first payment paid no one,
  so the old subscription's exclusion stands (900011: Devon Rivers' BD
  renewal never paid → Kendall keeps his commission). "Different agent" is judged
  against soldBy AND against who the engine's own ledger actually paid, so
  a later soldBy rewrite (name changed on the old sub, e.g. Marlow → Sage on
  cust 900008) cannot hide the switch once the app has paid the account.
  For MANUAL-era accounts (no ledger rows) a rewrite leaves only the manual
  books as witness — those are flagged `bd-renewal-check-original-payee`
  for manual handling: deduct the original payee on the manual sheet (the
  manual_paid history names them). After the cutover baseline (which seeds
  each account under its actual manual payee), the ledger check covers
  every account automatically.

## 6. True-ups before vesting

Until a subscription vests (or is excluded via Renewal-cancel), the engine
re-evaluates every pay run:

    expected = current_rate × current_contract_value   (0 if cancelled, non-renewal)
    adjustment_this_period = expected − total_paid_so_far

- **Contract value lowered** (e.g. $1,000 → $800 save-desk offer): deduct the
  difference (9% × $200 = $18).
- **Contract value raised: nothing is paid on its own** (CFO 2026-11-09). A
  raise with no submitted upsell was done by customer service or
  cancellations, not the ISA. Only an upsell adjustment (§12) pays on an
  increase. The sale keeps resting on the value it was last paid or trued
  down on (`cv_basis` for app-paid sales, `live_state.cv` for sales the
  manual books paid), so a later cut is measured from there and a cut
  followed by a raise back up is not restored. Each unpaid raise is listed
  on the Review tab so an upsell can be submitted if an ISA earned it. The
  8 raises paid on the 10/24 payroll ($111.89) were left as paid.
- **Card removed** (9% → 3%) or **added** (3% → 9%, or 3% → 5% for alt
  sources): pay/deduct the rate difference on current contract value.
- **Cancelled (non-renewal):** expected = 0 → full claw-back of whatever was
  paid.
- **Reactivated** (e.g. collections recovers a 60-day non-pay auto-cancel):
  expected returns to the computed amount → the commission is automatically
  re-paid, and the account rejoins the normal pool still subject to the
  3-paid-services rule.
- When the **§4b vesting test passes** (3 qualifying completed services AND
  their value fully paid), a final true-up runs on that period's numbers,
  then the commission **vests** and is frozen.

This single ledger rule generates every payment, deduction, and correction —
a rep's biweekly check line is simply the sum of their adjustments for the
period (e.g. +$3,000 new commissions, −$400 deductions → $2,600).

## 7. Pay periods and payroll calendar

- Pay periods are **14 days, Sunday through Saturday** (e.g. 8/2–8/15,
  8/16–8/29).
- Payroll is the **Monday two days after the period ends** (period ending
  Sat 8/29 → paid Mon 8/31). The calendar is fixed by `PAYROLL_ANCHOR_MONDAY`
  (§8).
- The statement is made by **Lock & run payroll** on the dashboard after the
  period ends, ready for that Monday.
- An event (first-payment, cancel, value change, vesting) counts in the
  period containing its FieldRoutes timestamp (office-local time, `OFFICE_TIMEZONE`).
- **Weekly split (owner, 2026-11-23):** payroll is still run every two weeks,
  but each rep's commissions, deductions and total are reported **separately
  for week 1 and week 2** (each Sun–Sat). Every ledger line carries the
  FieldRoutes date of the event it settles (`ledger.event_date`): the first
  paid service (initial), the cancellation (claw-back), the paid cancellation
  fee (half-keep / half back), the change to the subscription — contract
  value, rate, soldBy, booked split — (true-up, transfer, credit removed),
  the reinstatement (re-pay / restore), or the upsell date (upsell credit).
  Week 1 = event on/before the first Saturday (an event dated before the
  period counts in week 1: it was due from the period's first day); week 2 =
  later, and any line whose event cannot be dated (booked at the close).
  The amounts and the period totals do not depend on the split.
- **Hours on the payroll workbook (owner, 2026-11-23):** the dashboard's
  payroll workbook (Payroll → Downloads) adds each rep's hours worked,
  overtime hours and PTO hours per week and for the period, from the
  dashboard time clock (`timeclock.hours_report`: weekly overtime over 40,
  PTO taken on days in the week). A rep with hours but no commission lines
  is listed; someone the clock has nothing for (salaried) shows blanks. The
  engine's own statement carries no hours.
- ⚠️ AUDIT NOTE: the owner's concrete date examples define Sun–Sat periods
  (Sunday 8/30 falls in the NEXT period), but one Q&A answer said "through
  Sunday night." The engine follows the concrete dates — Sun–Sat, Saturday
  11:59:59pm office-time cutoff. Flag to confirm on the first run.

## 8. Backfill / go-live

This app takes over from a manual payroll (a spreadsheet, or whatever paid
commissions before). Four settings in `.env` place the switch; each is a
date, and all four are required:

| Setting | What it is |
|---|---|
| `BACKFILL_SINCE` | how far back FieldRoutes history is synced: at least 12 months before go-live, so old sales carry their real state |
| `PAYROLL_ANCHOR_MONDAY` | the payroll Monday of the first period the app calculates (`GO_LIVE_PERIOD_START` + 15 days): it fixes the 14-day Sun–Sat pay-period grid (§7), and the parallel run starts there |
| `GO_LIVE_PERIOD_START` | first day (a Sunday) of the first period the app calculates. Sales first paid before it count as paid by the manual payroll, and the Moved / re-signed / cancellation-fee rules (§5) apply to cancels from this day on |
| `PAYROLL_LIVE_FROM` | first day of the first period PAID from the app: a whole number of pay periods after `GO_LIVE_PERIOD_START`. The day before it is the last day the manual payroll paid |

The engine checks at every start that these fit together. Plus, for the
switch-over itself, `CUTOVER_LIST_AT_UTC`: the moment (UTC) the manual
payroll's last adjustments list was made.

- **Backfill.** The engine syncs full history so sales sold months ago carry
  their real state (paid-service counts, prior payments, card status).
  Commissions the manual payroll already paid are recorded as paid
  externally, so they are never paid twice, but stay subject to deduction
  until vested.
- **The manual payroll history is loaded** before the switch-over
  (`scripts/load_history.py`): every commission it paid, every adjustment,
  every deduction and contract-value true-down, and its "not paid yet" list.
- **Claw-backs on pre-go-live sales use the ACTUAL amount the manual payroll
  paid,** never a recomputation from today's card state. The history is
  keyed by customer + rep, so each payment or deduction is assigned to the
  rep's latest sale to that customer dated on or before it; a payment on an
  older sale never inflates a newer sale's claw. Two sales by the same rep
  to the same customer on the same day are told apart by the contract value
  the payment was made on (±$1); when that matches neither or both, the
  engine falls back to the card-state rate (flagged
  `claw-rate-uncertain-pre-golive-card-state`).
  For each sale: **G** = commission paid, **T** = contract-value true-downs
  taken, **D** = cancel deductions. The rep holds G − T − D, and the line is:
  - **claw-back** (cancelled, unvested, no fee): take back exactly what is
    held, never the original payment again when a true-down already took
    part of it;
  - **paid cancellation fee:** the rep keeps half of G − T; a late-paid fee
    repays exactly half, never 100%;
  - **re-pay** (reactivated): back to G − T.
  If the history shows the sale already fully deducted, no line appears:
  deducting again would double-charge the rep.
  - **Never paid:** the sale is on the manual "not paid" list, or the manual
    payroll was paying this rep in the 75 days after the sale's first
    payment but never paid this sale: there is nothing to take back, the
    claw is $0 ("never deduct more than was paid").
  - **Paid under a different rep** (reassignments): when the history shows
    exactly ONE other rep paid on that customer on or after the sale, the
    line uses that amount, noted "manual paid under <rep>".
  **All these lookups are period-aware:** closing pay period P, only manual
  payments dated on or before P and manual deductions from periods strictly
  BEFORE P count. A deduction in P's own manual history is the manual
  payroll agreeing with the claw, and never suppresses it.

### 8b. Cutover baseline policy

Every sale paid before the switch-over starts at its ACTUAL manual net
(paid − deducted, split transfers folded in per rep), and then:

- **Old underpayments are grandfathered.** A sale the manual payroll paid at
  a wrong rate is never back-paid or trued up. Correct rates apply to new
  sales.
- **Cancel → claw exactly what was paid.** Never a recomputation at today's
  rate or contract value.
- **Vest → lock as-is.** The only true-ups on pre-switch-over sales are
  contract-value changes, at the sale's as-paid rate (paid ÷ contract value
  at payment); the percentage itself is never revised.
- **Splits: per share from now on; the past stands.** The adjustments history
  (split transfers) is folded into what each rep holds on each sale, so a
  later cancel deducts each rep their part of what each actually kept. Sales already
  (even wrongly) deducted in full are left alone.
- **The baseline is built from the FINAL manual history.** If the last
  manual payroll is re-run, load the refreshed history first. Sign-off gate:
  every rep's total in the app equals their manual paid-minus-deducted
  total to the cent (`scripts/build_cutover.py` refuses to seed otherwise).

### 8c. Go-live

**The manual payroll paid everything through the day before
`PAYROLL_LIVE_FROM`; the app pays from that day on.** "We aren't here to
correct manual errors from the past, but get it right moving forward."

**Everything before `PAYROLL_LIVE_FROM` stays exactly as the manual payroll
left it.**
- **Pre-go-live sales the manual payroll never paid are not paid** (misses,
  sources it excluded, "not paid" entries that fell out of its own lookup).
- **Past cancels it never deducted are not deducted.**
- **Past contract-value reductions it never trued down are left alone.**
- **Past over-deductions and double deductions stand.**
- **Pre-go-live statements are labelled PRE-GO-LIVE** and can't be re-run
  from the dashboard.

**Nothing is paid twice, deducted twice, or lost to timing.**
- **What reps hold at go-live is what the manual history shows:** for each
  sale and rep, commission paid + adjustments − true-downs − deductions.
  Every rep's total ties to the manual history to the cent; manual rows that
  can't be pinned to a single sale are kept per rep and never move.
- **A sale the manual payroll paid is never paid again,** even if
  FieldRoutes marks the invoice paid only after the switch.
- **Upsells and splits on the manual's last adjustments list count as
  paid** (booked in the app on or before `CUTOVER_LIST_AT_UTC`); ones booked
  after it are paid by the app.
- **"Not paid" list entries are owed once the customer pays:** the app pays
  them when their first invoice is paid.
- **The world at go-live is the manual payroll's own view:** a copy of this
  app's database synced at the time the manual payroll pulled its data for
  its last period (`data/cutover/snapshot.sqlite`). Anything that changes
  after it is the app's to act on.

**From go-live, a sale the manual payroll settled moves money only when
something new happens,** based on what each rep actually holds:
- **Cancelled before the qualifying services:** deduct what the rep holds.
- **Cancelled with the fee paid:** the rep keeps half of what they hold.
- **A fee paid after a full deduction:** half of the deduction is given back.
- **Reinstated:** the deduction is given back.
- **Contract value lowered:** true down in proportion to what the rep holds.
  A raise pays nothing on its own (§6).
- **Split changed (booked after go-live):** what the reps hold is re-divided.
- **Vested:** locked; nothing moves.

A sale with nothing new produces no line, whatever the old differences.

**Further rulings:**
- **Never deduct what the manual history shows no payment for.**
- **An adjustment entered twice by accident counts once** (same account,
  rep, period, amount and note). A rep is paid twice on an account only when
  two subscriptions both earn it.
- **A cancel reason later changed to an excluded one** gives the deduction
  back, including one from before go-live, whether or not the account is
  also reinstated.
- **A live sale whose contract value shows $0** keeps its last value; it is
  not trued down and goes on the Review tab.
- **People the manual payroll paid who are not on the commission roster**
  are outside this app.
- **Negative statements are valid** and go out as they are.
Sales not yet earned at go-live, and every sale after, follow the normal
rules (§2–§6).

**Live periods are closed on the permanent ledger.**
- **Closes are protected:** each runs under a lock, after a backup, and
  periods close in order.
- **Re-runs:** a period can be re-run (its rows are replaced) until it is
  final: when its payroll Monday has passed, when the next period is run, or
  when marked paid (`report_payroll.py --finalize`). After that it is
  locked and later changes land in the next period. Don't re-run a period
  once payroll has been entered from it.
- **Failed runs never leave half a period:** the database is put back
  exactly as it was before the run.
- **Review tab:** anything the close can't decide on its own is listed on
  the statement's Review tab, and no money moves until someone decides.

## 9. Known edge cases and how they're handled

| Scenario | Handling |
|---|---|
| Serviced in period N, paid in period N+1 | Commission in period N+1 (§2) |
| Customer pays, then payment reversed/refunded before payroll run | Ticket balance reopens → not "paid"; if already paid to rep, next run deducts via §6 |
| Invoice "paid" only by write-off | Not a payment; does not trigger or count (§2, §4) |
| Free / $0 first-service invoice | Counts as paid once completed (balance is $0) — treated like any other account, no review flag (free initials are specially approved; confirmed 2026-10-02) |
| Cancel then reactivate (collections save) | Claw-back then automatic re-pay (§6) |
| Cancel reason = Renewal, rep already paid | Rep keeps commission while that Renewal cancellation stands (§5) |
| Renewal-cancelled sub reinstated, then cancelled for another reason before vesting | Latest cancellation governs — deducted unless that reason is excluded (§5) |
| Old cancellation fee on the account (invoiced before this sale) | Does not count — full deduction (§5) |
| Cancellation fee paid by coupon, or refunded and replaced by a coupon | Waived — full deduction (§5) |
| Cancellation fee paid after the cancel's pay period | Full deduction now, half repaid when the fee is paid (§5) |
| Initial bought at full price (no discount), then cancelled | No fee owed → counts as paid → rep keeps half (§5) |
| Vested, then the price or plan changes (coverage target rises) | Still vested — the first vest date stands (§5) |
| Renewal-cancelled sub; a different agent's Bad Debt Renewal never paid | Exclusion stands — no deduction (§5) |
| Card charge voided, invoice paid later another way | Paid when the real payment lands (§5) |
| Sale under a rep's login before their ISA start date | Not an ISA sale — no pay, no deduction (§1a) |
| Cancel reason = Renewal, rep never paid | Anomaly flag, no auto-pay (§5) |
| Reservice / General Service between real services | Ignored for the 3-service count (§4) |
| First visit booked as the recurring type instead of Initial | Counts anyway — trigger and 3-service count (§4a); flagged on report |
| Monthly billing: invoices smaller than the per-service value | Dollar-coverage test — paid monthly invoices must sum to the services' value (§4b) |
| Monthly invoice paid partially | Contributes nothing until that invoice's balance is $0 (§4b) |
| Card on/off file flips multiple times before vesting | Each run trues up to the CURRENT state; net effect is correct (§6) |
| Contract value changed after vesting | No effect — vested is frozen (§5) |
| Rep sells multiple subscriptions to one customer | Each subscription is its own commission |

## 10. Out of scope (assumed, flag if wrong)

- Only subscriptions sold by reps on the ISA roster, on/after their ISA
  start date (§1a), are processed.
- Commission is always on contract value, even when the initial invoice is
  discounted or $0.
- No cap, floor, or proration; no commission on add-ons billed outside the
  subscription's contract value.

## 11. Manual attribution overrides: splits and D2D rescues

Sometimes commission ownership is not what `soldBy` says. An **assignment**
(entered by the owner via `scripts/assign.py` or the web UI) replaces the
soldBy attribution for that subscription:

- **Split** — two (or more) ISA reps share one account's commission, e.g.
  60/40. Each rep is paid `share × rate × CV`, and — the fix over the old
  manual process — each rep is **deducted their share** if the account
  fails the 3-service test. Never one rep eating the full deduction.
- **Rescue** — a D2D-sold account cancels / never gets its initial service;
  an ISA rep revives it. The account stays in the D2D rep's name in
  PestRoutes, but the assignment credits the ISA rep. Rescue commissions
  are subject to the same vesting/deduction rules as anything else.
  **30-day boundary (owner, 2026-10-06):** rescue treatment applies only
  when the account is revived within 30 days of the cancellation. If it has
  been cancelled for MORE than 30 days, a revival is not a rescue — it is
  the reviving ISA rep's own new sale, in their name, at their normal rate,
  with no D2D attribution (e.g. acct 900023: cancelled over a month, Finley
  rescheduled it 9/22 → Finley's own sale; the 8/7 rescue form row is void).
- Removing an assignment before anything was paid is a clean no-op (the
  "reschedule fell through, account is free game again" flow). Removing it
  after payment claws back that rep's share on the next run — that is what
  removing the credit means.
- Assignments are soft-deleted — the full history of who was assigned what
  and when is kept for audit.

## 12. Upsells

When a rep upsells an existing customer, they earn their **standard 9% on
the ACV increase** (pre-upsell → post-upsell), paid once, in the period
containing the upsell. **On a commercial account (§3) it is 5%** (the owner
2026-10-20).

- **If the account is inside another rep's 3-service window** (unvested):
  the ACV increase is **excluded from the account's own commission math**
  — the original rep's true-ups and vesting freeze are computed on the
  pre-upsell value, since the increase was already credited to the
  upselling rep. No double-pay.
- **If the account is outside the window** (vested/frozen): nothing else
  moves — the original rep is frozen anyway; the upselling rep still gets
  their one-time credit.
- **D2D accounts:** upsells on accounts originally sold by a door-to-door
  rep are tagged with the D2D rep's name — **automatic since 2026-10-12**:
  `upsells.d2d_host` is set at booking from the host subscription's soldBy
  employee label when it carries a year tag (`\b20\d\d\b`, e.g. "R- Pat
  Example 2026", "V- Sam Example 2026"; names come from the dashboard DB's
  employees table, read-only). The free-text `d2d_rep` column remains for
  tech names on tech referrals and form-era entries; the year-end D2D
  report groups by `COALESCE(d2d_host, d2d_rep)`. Finance gets a year-end
  report of all D2D accounts upsold by inside sales, so each D2D rep's
  final annual commission payment is computed only on the ACV they
  originally sold.
- **Upsell credits are one-time and never clawed back**, even if the
  account later cancels inside the window (confirmed by the owner
  2026-09-02). Only manually removing the upsell entry reverses it.

## 13. Confirmed policy decisions (2026-09-02)

- **$0 first-service invoices** (mostly monthly-billed accounts): commission
  triggers at service **completion** — nothing to collect means the paid
  test is trivially met. Confirmed by the owner; on 2026-10-02 he confirmed
  these are specially-approved free initials and retired the review flag —
  they are treated exactly like any other account.
- **Upsell credits are one-time** — never deducted on later cancellation
  (§12). Confirmed by the owner.
- **Coupons (2026-10-01):** a couponed FIRST service fully counts (trigger
  fires, nothing reduced); every later recurring/monthly invoice must be
  paid with real money (card/cash/check/ACH) to count toward coverage.
  Confirmed by the owner — see §2 and §4b.
- **Rates (SUPERSEDED 2026-10-05 by CFO ruling, see §3):** one-time Initial
  Service jobs and upsell-sourced 8/12-month mosquito subs pay **5%** —
  matching the original manual payroll's practice.
- Pending the owner's review: write-off-paid first invoices (currently: no
  commission until real payment) and Renewal-cancels with no paid first
  service (currently: nothing paid; most look like same-day renewal-contract
  rewrites, source "Inside Sales Renewal").

## 14. Managers (owner, 2026-11-23)

Managers are on the commission roster with role **manager**. Their sales
(soldBy = the manager) earn a **flat 5% of the contract value**, paid once,
when the sale meets the same earning test as an ISA sale (§2: first
qualifying service completed and its invoice paid). Nothing else applies:

- **5% flat** — no card / source / commercial / one-time rates (§3).
- **Never deducted, for any reason** — no 3-service vesting, no claw-back
  or half-keep on a cancel, no contract-value true-down, no take-back if
  soldBy later moves to someone else. The payment is kept from the first
  paid service on (stored as vested at that moment; flag `manager-flat-5%`).
- A contract-value raise never pays on its own (§6), same as for ISAs.
- **Start date** = the first day the app pays the manager: a sale whose
  first service was paid **before** it is not paid here (it was the old
  process's, state `excluded_prestart`, flag
  `manager-paid-before-app-start-<date>`). Unlike an ISA start date (§1a)
  it is judged by the **paid date**, not the sold date, so a sale sold
  before the start and paid after it is paid. Making someone a manager on
  the roster sets it to the first day of the current pay period
  (editable); a manager with no date at all is paid from the start of the
  pay period they were added in.
- A booked assignment (§11) on the account wins: the assigned reps are paid
  under the normal rules.
- Statement: manager lines are in the Commissions tab like anyone's; the
  Summary lists managers in their own block under the inside-sales TOTAL,
  then a payroll total of both. No per-manager sheet in the payroll
  workbook.
