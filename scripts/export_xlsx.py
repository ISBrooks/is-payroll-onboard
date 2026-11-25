"""Export a pay period's committed ledger to a comparison workbook.

  python scripts/export_xlsx.py --period-end 2026-08-29

Tabs: Summary (per-rep, formula-driven) / Commissions / Deductions / Flags.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from commissions.config import load_settings
from commissions.periods import week_of, week_ranges
from commissions.statement import statement_lines
from commissions.store import connect

ARIAL = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
MONEY = '$#,##0.00;($#,##0.00);"-"'
PCT = "0%"
THIN = Border(bottom=Side(style="thin", color="D9D9D9"))
# periods ending before settings.payroll_live_from were paid by the manual sheet
PRE_GO_LIVE_BANNER = ("PRE-GO-LIVE — parallel run, not the basis of pay. "
                      "The manual payroll sheet paid this period.")


def style_header(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = HDR_FONT
        cell.fill = HDR_FILL
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def autosize(ws, widths: dict[int, int]) -> None:
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period-end", required=True)
    ap.add_argument("--db", default=None, help="alternate DB (testing)")
    ap.add_argument("--out", default=None, help="alternate output path")
    args = ap.parse_args()
    end = args.period_end

    s = load_settings()
    conn = connect(Path(args.db) if args.db else s.db_path)

    # net lines per (rep, subscription), shared with the run card's totals
    detail = statement_lines(conn, end)

    wb = Workbook()

    # ---------- detail tabs ----------
    cols = ["Rep", "Subscription ID", "Customer ID", "Customer", "Amount",
            "Type", "Rate", "Contract Value", "Source", "Service Type",
            "Sold Date", "First Paid Date", "Qualifying Services",
            "Coverage Paid", "Coverage Target", "Coverage Short", "Status",
            "Cancel Date", "Cancel Reason", "Flags", "Note",
            "Event Date", "Week"]      # RULES §7 weekly split
    tabs = {"Commissions": [r for r in detail if r[4] >= 0],
            "Deductions": [r for r in detail if r[4] < 0]}
    for name, rows in tabs.items():
        ws = wb.create_sheet(name)
        ws.append(cols)
        style_header(ws, len(cols))
        for r in rows:
            short = 0.0
            if r[15] not in ("vested", "excluded_renewal"):
                short = max(0.0, round((r[14] or 0) - (r[13] or 0), 2))
            ws.append([r[0], r[1], r[2], r[3], round(r[4], 2), r[5], r[6],
                       r[7], r[8], r[9],
                       (r[10] or "")[:10], (r[11] or "")[:10], r[12],
                       r[13], r[14], short, r[15], (r[16] or "")[:10],
                       r[17] or "", r[18], r[19],
                       (r[20] or "")[:10], week_of(r[20], end)])
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.font = ARIAL
                cell.border = THIN
        for row in ws.iter_rows(min_row=2, min_col=5, max_col=5):
            row[0].number_format = MONEY
        for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
            row[0].number_format = PCT
        for col in (8, 14, 15, 16):
            for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                row[0].number_format = MONEY
        autosize(ws, {1: 20, 2: 14, 3: 12, 4: 24, 5: 12, 6: 10, 7: 7, 8: 14,
                      9: 22, 10: 22, 11: 11, 12: 13, 13: 10, 14: 13, 15: 14,
                      16: 14, 17: 15, 18: 11, 19: 20, 20: 34, 21: 44, 22: 11, 23: 6})

    # ---------- summary (formula-driven off the detail tabs) ----------
    ws = wb.active
    ws.title = "Summary"
    # §14: managers get their own block under the inside-sales total
    mgrs = sorted(set(s.managers))
    reps = sorted(({r[0] for r in detail} | set(s.isa_roster)) - set(mgrs))
    pre = end < s.payroll_live_from              # ISO dates compare as text
    ws.append([f"ISA Commission Statement — pay period ending {end}"
               + (" — PRE-GO-LIVE" if pre else " — LIVE (basis of pay)")])
    ws["A1"].font = Font(name="Arial", size=13, bold=True)
    if pre:                                      # row 2 is the banner row
        ws.append([PRE_GO_LIVE_BANNER])
        ws["A2"].font = Font(name="Arial", size=11, bold=True, color="9C0006")
        for c in range(1, 13):
            ws.cell(row=2, column=c).fill = PatternFill("solid", fgColor="FFC7CE")
    else:
        ws.append([])
    # RULES §7 weekly split: each week's commissions / deductions / total per
    # rep, then the period — all driven by the Week column on the detail tabs
    (w1s, w1e), (w2s, w2e) = week_ranges(end)
    md = lambda d: f"{d.month}/{d.day}"  # noqa: E731
    WK = get_column_letter(len(cols))            # the Week column on the detail tabs
    ws.append(["", f"Week 1 · Sun {md(w1s)} – Sat {md(w1e)}", "", "",
               f"Week 2 · Sun {md(w2s)} – Sat {md(w2e)}", "", "",
               f"Pay period · {md(w1s)} – {md(w2e)}", "", "", "Lines", ""])
    for a, b in (("B3", "D3"), ("E3", "G3"), ("H3", "J3"), ("K3", "L3")):
        ws.merge_cells(f"{a}:{b}")
    ws.append(["Rep", "Commissions", "Deductions", "Total",
               "Commissions", "Deductions", "Total",
               "Commissions", "Deductions", "Net",
               "Commission Lines", "Deduction Lines"])
    for hrow in (3, 4):
        for c in range(1, 13):
            cell = ws.cell(row=hrow, column=c)
            cell.font = HDR_FONT
            cell.fill = HDR_FILL
            if hrow == 3:
                cell.alignment = Alignment(horizontal="center")
    r0 = 5

    def wk(tab, row, w):
        return f'=SUMIFS({tab}!$E:$E,{tab}!$A:$A,$A{row},{tab}!${WK}:${WK},{w})'

    def person_row(rep, row):
        return [
            rep,
            wk("Commissions", row, 1), wk("Deductions", row, 1), f'=B{row}+C{row}',
            wk("Commissions", row, 2), wk("Deductions", row, 2), f'=E{row}+F{row}',
            f'=SUMIFS(Commissions!$E:$E,Commissions!$A:$A,$A{row})',
            f'=SUMIFS(Deductions!$E:$E,Deductions!$A:$A,$A{row})',
            f'=H{row}+I{row}',
            f'=COUNTIFS(Commissions!$A:$A,$A{row})',
            f'=COUNTIFS(Deductions!$A:$A,$A{row})',
        ]

    def block(names, first, total_label):
        """Rows for names from row first, then their total row; -> total row."""
        for i, rep in enumerate(names):
            ws.append(person_row(rep, first + i))
        last = first + len(names) - 1
        ws.append([total_label] + [
            f"=SUM({get_column_letter(c)}{first}:{get_column_letter(c)}{last})"
            if names else 0 for c in range(2, 13)])
        for row in ws.iter_rows(min_row=first, max_row=last + 1):
            for cell in row:
                cell.font = ARIAL
                cell.border = THIN
            for cell in row[1:10]:
                cell.number_format = MONEY
        for cell in ws[last + 1]:
            cell.font = BOLD
        return last + 1

    isa_total = block(reps, r0, "TOTAL")
    if mgrs:
        # §14 managers: flat 5% once the first service is paid, never deducted
        ws.append([])
        ws.append(["Managers — flat 5% once the first service is done and paid, "
                   "never deducted"])
        hrow = ws.max_row
        for c in range(1, 13):
            ws.cell(row=hrow, column=c).fill = HDR_FILL
            ws.cell(row=hrow, column=c).font = HDR_FONT
        mgr_total = block(mgrs, hrow + 1, "MANAGERS TOTAL")
        ws.append([])
        ws.append(["PAYROLL TOTAL (inside sales + managers)"] + [
            f"={get_column_letter(c)}{isa_total}+{get_column_letter(c)}{mgr_total}"
            for c in range(2, 13)])
        for cell in ws[ws.max_row]:
            cell.font = BOLD
        for cell in list(ws[ws.max_row])[1:10]:
            cell.number_format = MONEY
    autosize(ws, {1: 22, 2: 13, 3: 13, 4: 13, 5: 13, 6: 13, 7: 13, 8: 13, 9: 13,
                  10: 13, 11: 16, 12: 15})
    ws.freeze_panes = "B5"

    # ---------- flags tab ----------
    ws = wb.create_sheet("Flags")
    ws.append(["Rep", "Subscription ID", "Customer", "Status", "Flags",
               "Contract Value", "First Paid Date"])
    style_header(ws, 7)
    for r in conn.execute("""
            SELECT st.rep, st.subscription_id, cu.name, st.state, st.flags,
                   sub.contract_value, st.first_paid_date
            FROM sub_status st
            JOIN subscriptions sub ON sub.subscription_id = st.subscription_id
            LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
            WHERE st.flags IS NOT NULL ORDER BY st.rep, st.subscription_id"""):
        ws.append([r[0], r[1], r[2], r[3], r[4], r[5], (r[6] or "")[:10]])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = ARIAL
        row[5].number_format = MONEY
    autosize(ws, {1: 22, 2: 14, 3: 24, 4: 16, 5: 40, 6: 14, 7: 13})

    # ---------- review tab: what a live close did NOT do automatically ----------
    try:
        review = conn.execute("""
            SELECT lf.subscription_id, sub.customer_id, cu.name, lf.note
            FROM live_flags lf
            LEFT JOIN subscriptions sub ON sub.subscription_id = lf.subscription_id
            LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
            WHERE lf.period_end = ? ORDER BY lf.subscription_id""", (end,)).fetchall()
    except Exception:                     # pre-cutover DB: no live_flags table
        review = []
    if review:
        ws = wb.create_sheet("Review", 1)
        ws.append(["Subscription ID", "Customer ID", "Customer", "Needs a decision"])
        style_header(ws, 4)
        for r in review:
            ws.append(list(r))
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.font = ARIAL
        autosize(ws, {1: 14, 2: 12, 3: 26, 4: 110})

    # ---------- D2D upsells tab (year-end finance ledger, RULES §12) ----------
    ws = wb.create_sheet("D2D Upsells")
    ws.append(["Original D2D Rep", "Subscription ID", "Customer",
               "ISA Rep (upseller)", "Upsell Credit", "ACV Increase",
               "Upsell Date", "Note"])
    style_header(ws, 8)
    for r in conn.execute("""
            SELECT u.d2d_rep, u.subscription_id, cu.name, u.rep, u.amount,
                   u.acv_increase, u.date, u.note
            FROM upsells u
            LEFT JOIN subscriptions sub ON sub.subscription_id = u.subscription_id
            LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
            WHERE u.removed_at IS NULL AND u.d2d_rep IS NOT NULL
            ORDER BY u.d2d_rep, u.date"""):
        ws.append(list(r))
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = ARIAL
        row[4].number_format = MONEY
        row[5].number_format = MONEY
    autosize(ws, {1: 26, 2: 14, 3: 24, 4: 20, 5: 13, 6: 13, 7: 11, 8: 44})

    if pre:      # red tabs + title, so a forwarded copy can't pass as paid
        for sh in wb.worksheets:
            sh.sheet_properties.tabColor = "C00000"
        wb.properties.title = PRE_GO_LIVE_BANNER

    out = Path(args.out) if args.out else (
        s.db_path.parent / "reports" / f"commission_statement_{end}.xlsx")
    out.parent.mkdir(exist_ok=True)
    wb.save(out)
    print(str(out))


if __name__ == "__main__":
    main()
