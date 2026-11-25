# Inside Sales Commission Rules

## 1. When a commission is EARNED (initial payment trigger)

A rep is paid for a subscription only when **both** are true:

1. The subscription's **first qualifying service is Completed**, and
2. the **invoice (ticket) for that service is fully paid**. Coupon payments
   count here: a first-service invoice closed by coupon, in part or in
   full, is paid for trigger purposes.

**Inside-sales start date:** a rep earns inside-sales commission only on
sales made on or after the date they joined the inside sales team (set per
rep in Payroll → Commission Roster). Sales made earlier under the same
login — for example retention work before moving to inside sales — are
not inside-sales sales: no commission and no deduction. (Indigo Vale:
started inside sales 7/10/26.)

**Who gets the credit:** whoever PestRoutes shows as the sales rep at each
sync. If the sales rep on a subscription is changed to another ISA, the
credit moves with it. If it is changed to someone who is not on the ISA
roster, the ISA's commission is taken back. Sales paid before go-live are
not moved automatically; they are listed for review.

**Who is paid at all:** only the people on the Commission Roster
(Payroll → Commission Roster). Remove someone and they are off the next
statement — no commissions and no deductions.

## 2. Commission rate

Evaluated per subscription, re-evaluated at every true-up:

| Priority | Condition | Rate |
|---|---|---|
| 1 | **Commercial accounts:** the customer is set to **Commercial Property** in PestRoutes, or the subscription is **COMMERCIAL MONTHLY PEST** or **Monthly Business Only** | 5% |
| 2 | Customer has **no form of payment saved** on the account (note: AutoPay is not required) | 3% |
| 3 | Subscription source is **Mosquito upsell Campaign**, or **any renewal source** (Inside Sales Renewal, Bad Debt Renewal, Renewal - In/Out of Contract, and any other renewal) | 5% |
| 4 | **One-time jobs** (Initial Service only, no recurring subscription) | 5% |
| 5 | **Upsell-sourced 8- or 12-month mosquito** subscriptions | 5% |
| 6 | Everything else | 9% |

"Source" means the subscription's source in PestRoutes (the Source column
of the PestRoutes Sales Report). If a sale was entered under the wrong
source, correct it in PestRoutes.

Commercial accounts are always 5%, even with no card on file. Commission
already paid keeps its rate: if a commercial account paid at 9% before this
rule cancels, the full 9% is deducted.

## 3. Qualifying services (the 3-paid-services retention rule)

The rep **keeps** the commission permanently only if the subscription
receives **3 qualifying completed services** AND the value of those
services is fully paid.

A **qualifying service** is an appointment that:

- belongs to the subscription the rep sold,
- is status **Completed**, and
- is of the right service type: the **Initial Service** or a recurring
  plan service (e.g. MONTHLY PEST, QUARTERLY PEST). If the customer's plan
  changes partway through (e.g. Bimonthly → Quarterly), services done
  under the earlier plan still count.

**Re-sold subscriptions start over:** when an old subscription is sold
again, only services and payments from the new sold date count. It is not
vested when it is sold; it needs 3 new paid services.

**Explicitly NOT qualifying:** Reservices (free re-treatments) and General
Services (e.g. tech couldn't access the yard, courtesy visits) — these are
distinct service types and never count toward the 3, even though they hang
off the same subscription.

### 3a. Miscategorized initial services still count

Schedulers sometimes book the first visit as the subscription's regular
recurring type (e.g. BIMONTHLY PEST) instead of Initial Service. That
appointment still counts — as the first qualifying service and toward the
3-service count — as long as it is completed and its value is paid.

### 3b. Payment coverage by billing frequency

"Paid" is measured in **dollars of paid subscription invoices**, because
billing frequency decouples invoices from services:

- **Bill-at-service accounts:** each service generates its own invoice at
  the correct per-service amount. Coverage = the first service's invoice
  paid + the 2 additional services' invoices paid. (3 paid invoices.)
- **Monthly-billed accounts:** the service might be $99 every 2 months
  while the customer is invoiced $49.50 monthly. Coverage = the initial
  service's invoice paid + enough fully-paid monthly invoices to add up to
  the value of the 2 additional services (2 × per-service recurring
  value). Example: a $99 bimonthly service billed $49.50/month → initial
  invoice + 4 paid monthly invoices ($198) → vested.

**Couponed payments can only count toward the initial service payment —
no future coupons are valid.**

## 4. Vesting and deduction (the second decision point)

- **Vested:** once 3 qualifying paid services exist, the commission is
  locked permanently at the final trued-up amount. No later event can claw
  it back — including a later price or plan change.
- **One-time jobs** (Initial Service) are vested as soon as the service is
  done and paid. Cancelling the account afterwards never deducts.
- **Deduction (claw-back):** if the subscription is **cancelled before
  vesting**, the full commission previously paid is deducted from the
  rep's next commission run — EXCEPT:
  - **Paid-cancellation-fee exception:** if the customer pays the
    cancellation fee **for this cancellation** with real money, the rep
    keeps **HALF** the commission instead of losing all of it. The
    cancellation fee is the recapture of the initial-service discount, so a
    customer who bought the initial at **full price** owes no fee — that
    counts as paid, and the rep keeps half. It does **not** count when:
    the fee was paid by **coupon** (a waived fee), or the only fee on the
    account is an **old one** from an earlier cancellation, subscription,
    or customer. A voided or refunded charge is never a payment. If the
    fee is paid after the cancellation was deducted,
    half is given back in the pay period it is paid.
  - **Moved exclusion:** if the cancellation reason begins with "Moved"
    (including "Moved OOC"), the rep keeps the commission. When an account
    has more than one cancellation on record, the most recent one's reason
    governs.
  - **Renewal exclusion:** if the subscription's cancellation reason is
    "Renewal" (company renewed the customer onto a new/higher contract,
    possibly by another department), the rep keeps the commission,
    regardless of service count, for as long as that Renewal cancellation
    stands. If the subscription is later reinstated and cancelled again
    for a different reason before vesting, the latest cancellation governs.
  - **Re-signed exclusion:** if the customer is re-signed onto a new
    ACTIVE subscription (by anyone — retention, D2D, or ISA) within 90
    days of the cancel, the original rep keeps the commission — same
    spirit as the Renewal exclusion (e.g. a bad-debt customer pays their
    balance and the retention team re-sets them up).
  - **Bad-Debt-Renewal re-sell override:** if a DIFFERENT agent is paid on
    the customer's new "Bad Debt Renewal" subscription, the Renewal /
    Re-signed exclusions do not apply — the original agent's commission is
    deducted, and the new agent earns 5%. Same-agent renewals keep the
    exclusion. A Bad Debt Renewal cancelled before its first payment paid
    no one, so it does not trigger the override.

## 5. True-ups before vesting

Until a subscription vests (or is excluded via Renewal-cancel), the engine
re-evaluates every pay run:

- **Contract value lowered** (e.g. $1,000 → $800 save-desk offer): deduct
  the difference (9% × $200 = $18).
- **Contract value raised:** nothing is paid on its own. An increase pays
  only through a submitted upsell adjustment. Raises with no upsell are
  listed for review.
- **Card removed** (9% → 3%) or **added** (3% → 9%, or 3% → 5% for alt
  sources): pay/deduct the rate difference on the current contract value.
- **Vesting true-up:** when a subscription has 3 qualifying completed
  services AND their value fully paid, a final true-up runs on that
  period's numbers, then the commission vests and is frozen.

## 6. Pay periods and payroll calendar

- Pay periods are **14 days, Sunday through Saturday** (e.g. 8/2–8/15,
  8/16–8/29).
- Payroll is the **Monday two days after** the period ends (period ending
  Sat 8/29 → paid Mon 8/31).
- Payroll is still submitted every two weeks, but the statement shows each
  rep's commissions, deductions and total **for each week separately**
  (week 1 = the first Sunday–Saturday, week 2 = the second). A line counts in
  the week of the event that caused it: the first paid service, the
  cancellation, the paid cancellation fee, the change to the account, or the
  upsell date. Something that happened before the period counts in week 1;
  a line with no date of its own counts in week 2.
- The payroll report also shows each hourly rep's **regular hours, overtime
  hours and PTO hours for each week**, straight from the dashboard time
  clock (overtime = hours over 40 in a week; regular = hours worked minus
  overtime, so regular + overtime = hours worked). Salaried people show no
  hours.

## 7. Go-live

- **The first pay period paid from the app starts on `PAYROLL_LIVE_FROM`**
  (a setting). The manual payroll paid everything before it. Earlier
  statements are a parallel run and are labelled PRE-GO-LIVE.
- **Everything before go-live stays exactly as the manual payroll left
  it.**
  - Pre-go-live sales the manual never paid are not paid.
  - Past cancels the manual didn't deduct are not deducted.
  - Past contract-value reductions are left alone.
  - Past over-deductions stand.
- **Nothing is paid or deducted twice.**
  - A sale the manual paid is never paid again.
  - Upsells and splits on the manual's last adjustments list count as paid.
  - Entries on the manual "not paid" list are paid once the customer pays.
- **A sale the manual payroll settled changes only when something new
  happens after go-live.** Every change is based on what the rep actually
  holds:
  - **Cancelled before the qualifying paid services** (3 unless changed):
    the rep loses what they hold.
  - **Cancelled with the cancellation fee paid:** the rep keeps half.
  - **Reinstated:** the deduction is given back.
  - **Contract value lowered:** trued down at the rate the rep was paid. A
    raise pays nothing on its own.
  - **Split changes:** what the reps hold is re-divided.
- **Locking:** a pay period can be re-run until it is final: its payroll
  Monday has passed, the next period has been run, or it was marked paid.
  After that it is locked, and later changes land in the next payroll.

## 8. Manual attribution overrides: splits and D2D rescues

Sometimes commission ownership is not what soldBy says. A split-commission
assignment **replaces** the soldBy attribution for that subscription:

- **Split** — two (or more) ISA reps share one account's commission. Each
  rep is paid share × rate × CV, and each rep is deducted **their share**
  if the account fails the 3-service test.
- **Rescue** — a D2D-sold account cancels / never gets its initial
  service; an ISA rep revives it. The account stays in the D2D rep's name
  in PestRoutes, but the assignment credits the ISA rep. Rescue
  commissions are subject to the same vesting/deduction rules as anything
  else.
- **30-day rescue boundary:** rescue treatment applies only when the
  account is revived within **30 days** of its cancellation. If the account
  has been cancelled for more than 30 days, the revival is the reviving
  rep's own **new sale** — their name, their normal rate, no D2D
  attribution.

## 9. Upsells

- When a rep upsells an existing customer, they earn their standard **9%
  on the ACV increase** (pre-upsell → post-upsell), paid once, in the
  period containing the upsell.
- **If the account is inside another rep's 3-service window (unvested):**
  the ACV increase is excluded from the account's own commission math —
  the original rep's true-ups and vesting freeze are computed on the
  pre-upsell value, since the increase was already credited to the
  upselling rep. No double-pay.
- **If the account is outside the window (vested/frozen):** nothing else
  moves — the original rep is frozen anyway; the upselling rep still gets
  their one-time credit.
- **D2D accounts:** upsells on accounts originally sold by a door-to-door
  rep are tagged with the D2D rep's name **automatically** — the engine
  checks who sold the host subscription, and D2D sellers are clearly
  labeled in PestRoutes with a year tag (e.g. "R- Pat Example 2026"). If
  the seller carries a year tag, D2D rules apply; nobody has to enter the
  D2D rep on the form. Finance gets a year-end report of all D2D accounts
  upsold by inside sales, so each D2D rep's final annual commission
  payment is computed only on the ACV they originally sold.
- **Upsell credits are one-time and never clawed back.**

## 10. Managers

Managers are on the Commission Roster as **Manager**. They earn a **flat
5%** of the contract value on their own sales once the first service is
completed and paid — the same earning test as inside sales. That is the
whole rule: no 3-service requirement and **never a deduction**, even if the
account cancels or the contract value drops. Their start date is the first
day they are paid from; sales whose first service was paid before it are
not paid here. Managers appear in their own section at the bottom of the
payroll Summary and do not get their own sheet.
