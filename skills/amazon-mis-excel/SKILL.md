---
name: "amazon-mis-excel"
description: "Build \"Amazon Sale MIS.xlsx\" in the exact layout of the MIS \"Latest\" tab from the axon-reports amazon_mis tool. Use when asked for the MIS in Excel, the MIS workbook or an MIS export."
---

# Amazon Sale MIS - Excel

Builds "Amazon Sale MIS.xlsx" laid out exactly like the "Latest" tab of Axon's MIS workbook, from the axon-reports connector's `amazon_mis` tool.

## Months
- Default: July 2026 to the current month.
- If the user names months or a range, use those instead.
- Call `amazon_mis(month='YYYY-MM')` once per month and keep each response (`days`, `total`, `coverage`, `ads_status`).

## Build it with code, never by hand
Write the workbook with code (openpyxl) that reads the values straight from the tool responses. Never retype a number, and never write a number that is not in the tool output.

### Layout
- One sheet, named "Latest". One block per month, side by side, oldest first.
- Row 1: month name merged across its block's 6 columns, bold, centred.
- Row 2: "Date" in column A, then per block: SC Sale | VC Sale | Total Sale | Ad spend | TACOS | W/o GST (bold, centred).
- Rows 3-33: days 1-31 in column A.
  - SC Sale and VC Sale: values from the tool (`sc_sale`, `vc_sale`).
  - Blank where the day is pending (null in the tool) or doesn't exist in that month (e.g. 31 Sep). Never write 0 for a pending day.
  - For days that don't exist in the month, leave the whole row of that block empty, formulas included.
- Formulas, not typed numbers (blank-safe, so future days stay empty):
  - Total Sale = `=IF(AND(SC="",VC=""),"",N(SC)+N(VC))`
  - W/o GST = `=IF(Total="","",Total/1.18)`
  - TACOS = `=IF(OR(AdSpend="",N(WoGST)=0),"",AdSpend/WoGST)`
- Ad spend: leave blank while `ads_status` says the Ads API is pending. Once it says ad spend comes from the Ads API, write each day's `ad_spend` value from the tool.
- Row 34 (totals, bold): SUM over rows 3-33 for SC, VC, Total and Ad spend; W/o GST = Total/1.18; TACOS = `=IF(OR(N(AdSpend)=0,N(WoGST)=0),"",AdSpend/WoGST)` on the totals.
- Number format `#,##0` for money, `0%` for TACOS. Freeze panes below row 2 (cell B3).
- Row 36, column A, italic grey: "Updated <date>. VC data to <coverage vendor data_until>, SC data to <coverage seller data_until>; later days are pending, not zero. Ad spend blank until the Ads API is approved." (Drop the last sentence once ad spend is live.)

## Check before handing it over
1. For every month, the sum of the SC cells written must equal the tool's `total.sc_sale`, and the VC cells `total.vc_sale`, to the rupee. Fix any mismatch; never deliver a file that doesn't tie out.
2. Recalculate the formulas if a recalculation tool is available and confirm there are no errors (#DIV/0!, #VALUE!).

## Reply
Give the file plus two or three lines: which months it covers, how far each channel's data goes (Vendor usually runs 3-4 days behind, Seller 1 day), and that the totals tie to the tool. VC Sale = Vendor shipped units x customer price incl GST - it is not Axon's revenue.

If a needed value is missing from the tool, say so. Do not propose code or schema changes.