---
name: "amazon-stock-health"
description: "Weekly Amazon Vendor Central PO tracker from the axon-reports connector: urgent PO requests, watch list, overstock, and the list for the Amazon vendor manager."
---

# Weekly Amazon stock health / PO tracker (Axon Lifestyle)

Read-only. All numbers come from the **axon-reports** connector. The `stock_health` tool computes the buckets, cover, out-of-stock dates, PO quantities and week-on-week changes itself. Report its numbers exactly as returned. Don't recompute them and don't estimate missing ones.

## Steps

1. Call `data_freshness`. State the latest Vendor inventory and sales dates. Note that inventory lags today by about 6 days, so real stock may already be lower.
2. Call `stock_health` with its defaults (22 days cover, 90 days overstock, 1 unit/day minimum, 45-day PO target), unless the user gives different thresholds.
3. Present one table per bucket, and write "none" for an empty bucket:
   - **A. URGENT (PO request this week):** SKU, product, ASIN, units/day, on hand, open PO, days of cover, est. out-of-stock date. Put `out_of_stock_now` items first and mark them OUT OF STOCK.
   - **B. WATCH (open PO; confirm it arrives before the out-of-stock date):** same columns.
   - **C. OVERSTOCKED (deal / coupon / ad candidates):** same columns plus aged 90+ units.
4. **For the Amazon vendor manager:** the `vendor_manager_list` as a plain table (SKU, product, ASIN, units/day, on hand, suggested PO qty), ready to paste into an email.
5. **New in bucket A since last week:** `new_in_A_since_last_week`, with where each item was before. Write "none" if it's empty.
6. **H-188 reminder:** H-188 warehouse stock is not in this data. Confirm H-188 can fill each PO request before sending the list.
7. Finish with at most 3 lines on what to act on first.

## Rules

- Units only. Vendor revenue and COGS are not available on amazon.in.
- If a tool errors or returns nothing, say so and stop.
- Unmapped ASINs appear with no SKU. List them as they are and don't guess a SKU.