---
name: amazon-price-variance
description: "Compare live Amazon Seller Central listing prices against an internal selling-price sheet and produce a variance workbook. Use this skill whenever the user wants to price-check, audit, reconcile, or find variations between Amazon listing prices and their own price list, MRP sheet, or selling-price Excel — including phrasings like 'check my Amazon prices', 'are my listings priced right', 'compare my price list to Amazon', 'price audit', or when they upload an All Listings Report or a SKU-and-price sheet. Also use it when they ask which SKUs are not listed on Amazon, or whether FBA and MFN prices have drifted apart. Do not use it for competitor price scraping — this skill only checks the user's own listings."
---

# Amazon price variance check

Compares the price on every active Amazon Seller Central listing against the
user's internal selling-price sheet, and writes a multi-tab Excel workbook
showing where they disagree.

## Get the right inputs first

Two files are needed. If either is missing, ask before running anything.

**1. The Amazon All Listings Report.** Seller Central → Reports → Inventory
Reports → **All Listings Report**. It downloads as a tab-delimited `.txt`.

This specific report matters. The Open Listings and Active Listings variants
drop columns the comparison depends on — `status`, `fulfillment-channel`, and
`maximum-retail-price`. If the user brings one of those instead, send them back
for the All Listings Report rather than working around it.

**2. The internal selling-price sheet.** Any `.xlsx`, `.csv`, `.tsv`, or pasted
table with an internal SKU column and a price column. Headers are optional —
the script identifies the SKU column by testing which column's values actually
overlap the SKUs in the Amazon report, so it works on raw pasted fragments.

Never bundle or cache a copy of the price list. A stale reference sheet in a
variance checker produces confident wrong answers, which is worse than no
checker at all. Ask for it fresh every run.

## Run it

```bash
python scripts/compare_prices.py \
  --report <All_Listings_Report.txt> \
  --pricelist <price sheet> \
  --out /mnt/user-data/outputs/Amazon_Price_Variance_<DDMonYYYY>.xlsx
```

Optional: `--tolerance 5` ignores gaps at or below ₹5, useful when rounding
noise is drowning out real problems.

The script prints a summary to stdout — counts, the largest gaps, and any
warnings. Read that output; it is what the written answer should be built from.
Then present the workbook with `present_files`.

## Why the SKU matching is not a plain lookup

Joining on ASIN does not work here even when the sheet has ASINs, and joining
on raw SKU misses most rows. Two reasons, both worth understanding before
trusting any output:

**Amazon SKUs carry suffixes the internal sheet does not.** `5312-FBA`,
`5322-FBM`, `5320-1` and `5311-1-FBA` all trace back to one catalogue SKU. The
script strips channel suffixes, then collapses a trailing numeric variant only
when everything before it is numeric — so `REARWIPER-AU-12` and
`5605-5612-COMBO` survive intact.

**One ASIN often carries two listings.** Sellers running both FBA and MFN have
two SKUs on the same ASIN, and those two can drift to different prices. An
ASIN-keyed lookup silently picks one and hides the drift. The script keys on
SKU and flags every ASIN carrying more than one distinct price in a
`Same-ASIN conflict` column — these are worth surfacing on their own, because
the customer sees whichever SKU wins the buy box, so the price is effectively
non-deterministic until one is corrected.

## The report is a snapshot, not a live feed

Every price in the All Listings Report is what Amazon had on file at the moment
of export. It is not the current price, and prices move. A report even a few
days old will show variances that have since been corrected, and will miss ones
introduced since.

This is the single easiest way to give a confidently wrong answer with this
skill, so guard against it explicitly:

- The script prints both the filename date and the date the file arrived, and
  warns when they disagree. Seller Central serves previously-generated reports
  from its download queue, so a file fetched today can carry weeks-old data
  under its original name — the user may believe it is fresh when it is not.
  When the two dates conflict, ask rather than assuming either one.
- Workbook columns say **Price at export**, never "live price", and the
  mismatch tab carries the export date in its header note.
- In the written answer, date the findings — "as exported on 19 Aug" — rather
  than saying a listing "is" priced at something. If the report is stale, say
  so before listing any gaps, and suggest a fresh export before anyone acts.

## The report is the seller's offer price, not the page price

This distinction causes the most confusing disagreements, so raise it before
the user finds it themselves on a product page.

The report gives what the seller has set on their own offer. The product page
shows whichever offer wins the buy box. Those differ whenever anyone else sells
the same ASIN — including, commonly, the seller's own Vendor Central business,
where Amazon Retail sets its own price on stock the same company supplied. A
listing can match the price list exactly while every customer pays something
else.

So "matches the price list" means the offers match, not that customers pay
those prices. Say it that way.

When a user reports that a page price disagrees with the audit, do not reach
straight for staleness. Both explanations are live, and they are distinguished
by a single check: **Manage Inventory in Seller Central shows the current price
for that SKU.** If it agrees with the report, the report is current and the
difference is the buy box. If it disagrees, the export was stale. Offer that
check rather than guessing between the two.

If the user needs what a customer actually sees right now — buy box price,
coupons, competitor undercutting — this skill cannot answer that at all, and
saying so is better than handing over offer-price figures dressed as page
prices. That needs live retrieval of the detail pages, which is a separate
tool.

## Only active listings are compared

Inactive rows typically carry a placeholder price at zero quantity, and
Incomplete rows have no price at all. Comparing them generates a wall of fake
mismatches. They are written to their own tab instead, so nothing is lost.

## Reading the result honestly

The mechanical output is a list of gaps. The useful answer is usually about the
shape of those gaps, so check the distribution before calling anything an error:

- **Gaps overwhelmingly in one direction, at round increments** (+₹10, +₹20
  across a whole product family) almost always mean a deliberate channel markup
  — pricing set to absorb referral fees or shipping — not drift. Say so, and
  ask which price the sheet is actually meant to represent before presenting a
  fix list. The script prints a warning when it detects this pattern.
- **Isolated large gaps** against an otherwise consistent family are the real
  finds. Lead with those.
- **A SKU on an unexpected ASIN** shows up as a gap whose live price exactly
  matches some *other* product in the sheet. That is a catalogue mislabel, not
  a pricing error, and it needs saying explicitly — the fix is different.
- **SKUs in the sheet with no active listing** are often the bigger story than
  the price gaps. Report the count.

## Answering

Lead with the headline count and the directional skew if there is one. Name the
handful of genuine outliers with their numbers. Flag catalogue problems —
mislabels, duplicate listings on separate ASINs, legacy SKUs that block
matching — separately from price problems, because they have different owners
and different fixes. Keep the workbook as the detail; do not re-list its
contents in prose.
