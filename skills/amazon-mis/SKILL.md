---
name: "amazon-mis"
description: "Show the Amazon Sale MIS from the axon-reports connector: daily SC/VC sale, total, W/o GST, orders, AOV for a month, plus month-on-month. Ad spend/TACOS once the Ads API is live."
---

# Amazon Sale MIS

Rebuilds the Excel "Amazon Sale MIS" from the axon-reports connector's `amazon_mis` tool.

## Which period
- No month named: the current month.
- A month named ("September", "Aug 2026"): that month as `YYYY-MM`; without a year, the most recent one.
- "Month on month" / "last N months" only: skip the daily table and show just the comparison with `months_back=N`.

## Steps
1. Call `amazon_mis(month='YYYY-MM')` for the daily rows and month total.
2. Call `amazon_mis(month='YYYY-MM', months_back=3)` for the comparison.
3. Present the three sections below, in this order.

### 1. Daily table (the Excel's column order)
Date | SC Sale | VC Sale | Total Sale | Ad spend | TACOS | W/o GST | SC Orders | SC AOV | Total Units
- Rupees in Indian format (3,57,882). TACOS as a %.
- Days after the `coverage` date for a channel: show "pending", never 0, and say which channel lags (Vendor runs ~3-4 days behind, Seller ~1 day).
- Month total row at the bottom, totals in lakhs (Rs 107.39L).

### 2. Month-on-month (last 3 months)
Month | SC Sale | VC Sale | Total | W/o GST | SC Orders | SC AOV | % change vs previous month
- For a part month, compare on a daily-average basis (SC / days_with_sc, VC / days_with_vc), not raw totals, and say so.

### 3. Three-line summary
- What moved most vs last month.
- Any day more than 30% above or below the month's daily average.
- `vc_units_unpriced` if above 0 (those Vendor units had no price and are not in VC Sale).

## Rules
- Ad spend and TACOS: if `ads_status` says they are pending (Ads API not approved yet), show "-" in both columns and one line saying so. Never estimate them or take them from anywhere else. Once live, show them and state `tacos_basis` (TACOS only counts days that have Vendor data).
- SC Sale = Seller Central ordered sales incl GST. VC Sale = Vendor shipped units x customer price incl GST (Buy Box price where pulled, else sheet selling price). VC Sale is NOT Axon's revenue; for revenue use the `sales_value` tool and say which basis.
- SC AOV = SC Sale / SC order items. `sc_sale_per_unit` is what the old Nov/Dec MIS tabs called "AOV"; label it as sales per unit if shown.
- Months before Seller data starts show VC only; say so rather than treating SC as zero.
- If the user asks for the MIS as an Excel file or workbook, use the amazon-mis-excel skill (the "Latest" tab layout) instead of exporting these tables.
- If a question needs data the tool does not return, say what is missing. Do not propose code or schema changes.