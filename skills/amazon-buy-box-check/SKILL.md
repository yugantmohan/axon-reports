---
name: "amazon-buy-box-check"
description: "Full Amazon Buy Box check for Axon's own ASINs from the axon-reports connector: outside sellers, suppressed Buy Boxes, prices below CP or sheet, and an action list."
---

# Amazon Buy Box check (Axon Lifestyle)

Read-only. Uses the **axon-reports** `buy_box` tool, which reads the latest scheduled pull (10:00, 13:00, 16:00 and 19:00). All prices are incl GST. CP is ex GST, so compare CP × 1.18 when quoting a floor. Never estimate anything the tool doesn't return.

Holders: **Etrade** (`vendor_channel`, A2AL6IVND0I91F) is our Vendor Central stock, and **Axon** (A1QTZ8OBV6L6UD) is our Seller Central account. Both are normal. Any other seller ID is an outside seller.

## Steps

1. Call `buy_box` with no filters. State the latest pull time. If it's more than 4 hours old, say so at the top and suggest running `python3 sync.py --only pricing`.
2. **Alerts.** Give one table per alert and skip any that are empty:
   a. **Outside seller holds the Buy Box:** SKU, product, ASIN, Buy Box ₹, seller ID, Etrade ₹, Axon ₹, sheet ₹.
   b. **Buy Box suppressed** (offers exist, nobody wins): SKU, product, ASIN, offers, Etrade/Axon ₹.
   c. **Below CP:** SKU, product, ASIN, holder, Buy Box ₹, CP ₹, CP floor incl GST (CP × 1.18), sheet ₹. Mark rows where the holder is **Axon (we set this price)**.
   d. **Below sheet price:** the same columns plus % vs sheet, biggest gap first. Mark Axon rows. Treat gaps under 5% as noise and list them in one line.
   e. **Holder changed in the last day.**
3. **Info**, one line each with no tables: the count and SKUs of ASINs with no offers at all (a stock question, not pricing), Axon undercutting Etrade, outside sellers cheaper than our lowest offer, and live ASINs with no sheet price or CP (so they couldn't be checked).
4. **Anything odd:** ASINs whose Buy Box is far above or below the sheet price, because these often mean the ASIN is mapped to the wrong SKU. Name them and ask what actually ships against them. Don't assume.
5. **Action list, at most 6 lines, in this order:**
   1. Prices we control: Axon holding the Buy Box below CP or sheet. Say what to raise it to.
   2. Outside sellers on our listings: the seller ID and ASIN, and check whether it's one of our distributors.
   3. Etrade prices to raise with the Amazon vendor manager, with only gaps of 5% or more.
   4. Listing issues: suppressed Buy Box, and any price above MRP. Note that old ASINs may carry a different pack MRP, so verify before calling it a legal issue.

## Rules

- The reference price is Axon's approved price sheet (via `import_prices.py`). If a price was changed on purpose, the sheet needs updating, so say so rather than calling it an error.
- Report exactly what the tool returns, and never invent seller names.