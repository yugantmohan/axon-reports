#!/usr/bin/env python3
"""
Compare live Amazon Seller Central listing prices against an internal selling-price list.

Usage:
    python compare_prices.py --report <All Listings Report> --pricelist <sheet> [--out <xlsx>]

The report is Amazon's All Listings Report (tab-delimited .txt from
Seller Central > Reports > Inventory Reports). The price list is any
xlsx/csv/tsv/txt with a SKU column and a price column, headers optional.
"""

import argparse
import os
import re
import sys
from datetime import date
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(name=FONT, bold=True, color="FFFFFF", size=10)
RED = PatternFill("solid", fgColor="FCE4E4")
AMBER = PatternFill("solid", fgColor="FFF2CC")

CHANNEL_MAP = {"AMAZON_IN": "FBA", "DEFAULT": "MFN"}

STALE_AFTER_DAYS = 3


def report_date(path):
    """Establish when the report data is from, and flag when the evidence disagrees.

    Two signals, neither trustworthy alone. The filename carries the date Amazon
    GENERATED the report -- but Seller Central keeps old reports in the download
    queue, so a file fetched today can hold weeks-old data under its original
    name. The file's mtime says when it arrived here, which is usually the
    download, not the export.

    When the two disagree the difference is exactly the trap: data older than it
    looks. Return both and let the caller say so rather than silently picking one.
    """
    fname_date = None
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", os.path.basename(str(path)))
    if m:
        mm, dd, yyyy = (int(x) for x in m.groups())
        try:
            fname_date = date(yyyy, mm, dd)
        except ValueError:
            pass
    try:
        file_date = date.fromtimestamp(os.path.getmtime(path))
    except OSError:
        file_date = None
    return fname_date, file_date


# --------------------------------------------------------------------------
# SKU normalisation
# --------------------------------------------------------------------------
def base_sku(raw):
    """Reduce an Amazon seller-sku to the base SKU used in the price list.

    Amazon SKUs carry channel and variant suffixes that the internal price
    list does not: 5312-FBA, 5322-FBM, 5320-1, 5311-1-FBA all trace back to
    a single catalogue SKU. Strip the channel suffix first, then collapse a
    trailing numeric variant only when what precedes it is purely numeric --
    that guard stops REARWIPER-AU-12 or 5605-5612-COMBO from being mangled.
    """
    s = str(raw).strip().upper()
    s = re.sub(r"-(FBA|FBM|FBM1|AMAZON)$", "", s)
    m = re.match(r"^(\d+)-\d+$", s)
    if m:
        s = m.group(1)
    return s


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_report(path):
    """Load the All Listings Report. It is tab-delimited with a UTF-8 BOM."""
    df = pd.read_csv(path, sep="\t", dtype=str, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]

    required = {"seller-sku", "price", "status"}
    missing = required - set(df.columns)
    if missing:
        sys.exit(
            f"ERROR: report is missing {sorted(missing)}.\n"
            "This looks like the wrong export. Use Seller Central > Reports >\n"
            "Inventory Reports > All Listings Report -- the Open Listings and\n"
            "Active Listings variants drop columns this comparison needs."
        )

    df["live_price"] = pd.to_numeric(df["price"], errors="coerce")
    df["mrp"] = pd.to_numeric(df.get("maximum-retail-price"), errors="coerce")
    df["sku_raw"] = df["seller-sku"].astype(str).str.strip()
    df["base_sku"] = df["sku_raw"].apply(base_sku)
    df["channel"] = df.get("fulfillment-channel", pd.Series(dtype=str)).map(CHANNEL_MAP)
    if "asin1" not in df.columns:
        df["asin1"] = ""
    if "item-name" not in df.columns:
        df["item-name"] = ""
    return df


def _read_any(path):
    if str(path).lower().endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(path, dtype=str, header=None)
    for sep in ["\t", ","]:
        try:
            df = pd.read_csv(path, sep=sep, dtype=str, header=None,
                             encoding="utf-8-sig", engine="python")
            if df.shape[1] > 1:
                return df
        except Exception:
            continue
    sys.exit(f"ERROR: could not parse the price list at {path}.")


def load_pricelist(path, report_skus):
    """Load the price list and work out which columns hold SKU and price.

    Headers are unreliable -- these sheets are often pasted fragments with no
    header row at all, or headers like 'Item Code' / 'MRP' / 'Selling Price'.
    Rather than trusting names, pick the SKU column by how well its values
    actually overlap the SKUs in the Amazon report, and the price column as
    the most numeric of the remaining columns. Overlap is self-validating:
    if the guess is wrong, almost nothing matches and the script says so.
    """
    raw = _read_any(path)
    raw = raw.dropna(how="all").reset_index(drop=True)

    # Drop a header row if the first row overlaps the report far worse than the rest.
    def overlap(series):
        vals = series.dropna().astype(str).str.strip().str.upper()
        vals = vals[vals != ""]
        if len(vals) == 0:
            return 0.0
        return vals.isin(report_skus).mean()

    best_col, best_score = None, 0.0
    for c in raw.columns:
        s = overlap(raw[c])
        if s > best_score:
            best_col, best_score = c, s

    if best_col is None or best_score < 0.02:
        sys.exit(
            "ERROR: no column in the price list matches the SKUs in the Amazon report.\n"
            "Check that the sheet has an internal SKU / item-code column, and that\n"
            "it uses the same SKU scheme as Seller Central."
        )

    # Price column: most numeric column that is not the SKU column.
    def numeric_share(series):
        vals = pd.to_numeric(
            series.astype(str).str.replace(r"[^\d.\-]", "", regex=True),
            errors="coerce",
        )
        return vals.notna().mean()

    price_col, price_score = None, 0.0
    for c in raw.columns:
        if c == best_col:
            continue
        s = numeric_share(raw[c])
        if s > price_score:
            price_col, price_score = c, s
    if price_col is None:
        sys.exit("ERROR: could not find a price column in the price list.")

    # Product name: the widest remaining text column, if any.
    name_col = None
    widest = 0
    for c in raw.columns:
        if c in (best_col, price_col):
            continue
        w = raw[c].astype(str).str.len().mean()
        if w > widest:
            name_col, widest = c, w

    out = pd.DataFrame({
        "sku": raw[best_col].astype(str).str.strip().str.upper(),
        "list_price": pd.to_numeric(
            raw[price_col].astype(str).str.replace(r"[^\d.\-]", "", regex=True),
            errors="coerce",
        ),
    })
    out["product"] = raw[name_col].astype(str).str.strip() if name_col is not None else ""
    out = out[(out["sku"] != "") & (out["sku"].str.lower() != "nan")]
    out = out.drop_duplicates(subset="sku", keep="first")

    print(f"  price list: matched SKU column {best_col} "
          f"({best_score:.0%} overlap with report), price column {price_col}")
    return out


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------
def compare(report, pricelist, tolerance=0.0):
    active = report[report["status"].str.strip().str.lower() == "active"].copy()

    m = active.merge(pricelist, left_on="base_sku", right_on="sku", how="left")
    m["variance"] = m["live_price"] - m["list_price"]
    m["variance_pct"] = (m["variance"] / m["list_price"] * 100).round(1)

    def verdict(r):
        if pd.isna(r["list_price"]):
            return "NOT IN PRICE LIST"
        if pd.isna(r["live_price"]):
            return "NO LIVE PRICE"
        if abs(r["variance"]) <= tolerance:
            return "OK"
        return "UNDERPRICED" if r["variance"] < 0 else "OVERPRICED"

    m["result"] = m.apply(verdict, axis=1)

    # Same ASIN carrying two different prices across FBA and MFN SKUs.
    counts = active.groupby("asin1")["live_price"].nunique()
    conflict = set(counts[counts > 1].index)
    m["conflict"] = m["asin1"].isin(conflict).map({True: "YES", False: ""})

    return m, active, conflict


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def write_sheet(wb, name, df, headers, note=None, highlight=None):
    ws = wb.create_sheet(name[:31])
    r = 1
    if note:
        ws.cell(1, 1, note).font = Font(name=FONT, italic=True, size=9, color="555555")
        r = 3
    for j, h in enumerate(headers, 1):
        c = ws.cell(r, j, h)
        c.fill, c.font = HDR_FILL, HDR_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = ws.cell(r + 1, 1)

    money = {"List price", "Price at export", "Variance", "MRP on listing", "Price on listing"}
    for i, (_, row) in enumerate(df.iterrows(), r + 1):
        for j, v in enumerate(row.tolist(), 1):
            c = ws.cell(i, j, None if pd.isna(v) else v)
            c.font = Font(name=FONT, size=10)
            if headers[j - 1] in money:
                c.number_format = "#,##0"
            elif headers[j - 1] == "Variance %":
                c.number_format = "0.0"
        if highlight:
            fill = highlight(row)
            if fill:
                for j in range(1, len(headers) + 1):
                    ws.cell(i, j).fill = fill

    widths = {"Amazon title": 60, "Product (price list)": 38, "Seller SKU": 16,
              "ASIN": 14, "Base SKU": 12, "Channel": 9, "List price": 11,
              "Price at export": 13, "Variance": 10, "Variance %": 11,
              "MRP on listing": 14, "Same-ASIN conflict": 14, "Status": 12,
              "Qty": 8, "Price on listing": 14, "SKU": 16}
    for j, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(j)].width = widths.get(h, 18)
    return ws


def build_workbook(m, report, active, pricelist, out_path, tolerance, rdate=None):
    cols = ["seller-sku", "base_sku", "asin1", "channel", "product", "list_price",
            "live_price", "variance", "variance_pct", "mrp", "conflict", "item-name"]
    hdr = ["Seller SKU", "Base SKU", "ASIN", "Channel", "Product (price list)",
           "List price", "Price at export", "Variance", "Variance %", "MRP on listing",
           "Same-ASIN conflict", "Amazon title"]

    mismatch = m[m["result"].isin(["OVERPRICED", "UNDERPRICED"])].sort_values(
        "variance_pct", ascending=False)
    ok = m[m["result"] == "OK"].sort_values("base_sku")
    unmatched = m[m["result"] == "NOT IN PRICE LIST"].sort_values("seller-sku")
    listed = set(active["base_sku"])
    not_listed = pricelist[~pricelist["sku"].isin(listed)][["product", "sku", "list_price"]]
    inactive = report[report["status"].str.strip().str.lower() != "active"][
        ["seller-sku", "asin1", "status", "price", "quantity", "item-name"]]

    wb = Workbook()
    wb.remove(wb.active)

    stamp = (f"Prices are as exported on {rdate:%d %b %Y} -- NOT live. Anything changed "
             f"since that date will not appear here. " if rdate else
             "Prices are as of the report export, not live. ")
    write_sheet(wb, "Price mismatches", mismatch[cols], hdr,
                stamp + f"Active listings differing from the price list by more than "
                f"{tolerance:g}. Sorted by variance %. Red = gap of 10% or more.",
                lambda r: RED if pd.notna(r["variance_pct"]) and abs(r["variance_pct"]) >= 10 else None)
    write_sheet(wb, "Matches OK", ok[cols], hdr,
                "Active listings priced in line with the price list.")
    write_sheet(wb, "SKU not in price list",
                unmatched[["seller-sku", "asin1", "channel", "live_price", "mrp", "item-name"]],
                ["Seller SKU", "ASIN", "Channel", "Price at export", "MRP on listing", "Amazon title"],
                "Active listings whose SKU has no match in the price list -- usually legacy "
                "or Amazon-generated SKUs. Price could not be checked; reconcile the SKU.")
    write_sheet(wb, "Not listed on Amazon", not_listed,
                ["Product (price list)", "SKU", "List price"],
                "Price-list SKUs with no ACTIVE listing. Some may exist as Inactive -- see next tab.")
    write_sheet(wb, "Inactive & incomplete", inactive,
                ["Seller SKU", "ASIN", "Status", "Price on listing", "Qty", "Amazon title"],
                "Non-active listings, excluded from the comparison. Inactive rows usually carry "
                "a placeholder price at zero quantity; Incomplete rows have no price at all.")

    wb.save(out_path)
    return mismatch, ok, unmatched, not_listed, inactive


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="Amazon All Listings Report (.txt)")
    ap.add_argument("--pricelist", required=True, help="Internal selling-price sheet")
    ap.add_argument("--out", default="price_variance.xlsx", help="Output .xlsx path")
    ap.add_argument("--tolerance", type=float, default=0.0,
                    help="Ignore gaps at or below this rupee amount (default 0)")
    args = ap.parse_args()

    fname_date, file_date = report_date(args.report)
    rdate = fname_date or file_date
    print("Loading...")
    if fname_date:
        print(f"  filename says generated {fname_date:%d %b %Y}", end="")
        print(f", file arrived {file_date:%d %b %Y}" if file_date else "")
    report = load_report(args.report)
    pricelist = load_pricelist(args.pricelist, set(report["base_sku"]))

    m, active, conflict = compare(report, pricelist, args.tolerance)
    mismatch, ok, unmatched, not_listed, inactive = build_workbook(
        m, report, active, pricelist, args.out, args.tolerance, rdate)

    over = (m["result"] == "OVERPRICED").sum()
    under = (m["result"] == "UNDERPRICED").sum()

    print()
    print("=" * 64)
    print(f"  Listings in report      {len(report):>5}")
    print(f"  Active                  {len(active):>5}   ({active['asin1'].nunique()} unique ASINs)")
    print(f"  Priced above list       {over:>5}")
    print(f"  Priced below list       {under:>5}")
    print(f"  Matching                {len(ok):>5}")
    print(f"  SKU not in price list   {len(unmatched):>5}")
    print(f"  In list, not listed     {len(not_listed):>5}   of {len(pricelist)} price-list SKUs")
    print(f"  Same-ASIN conflicts     {len(conflict):>5}")
    print("=" * 64)

    if fname_date and file_date and abs((file_date - fname_date).days) > STALE_AFTER_DAYS:
        gap = (file_date - fname_date).days
        print(f"\n  DATE CONFLICT: filename says {fname_date:%d %b %Y}, file arrived "
              f"{file_date:%d %b %Y} ({gap} days apart).")
        print("  Seller Central serves previously-generated reports from its download")
        print("  queue, so this may hold data older than it appears. Ask the user to")
        print("  confirm, or to hit Request Report for a genuinely fresh export.")
    elif rdate and (date.today() - rdate).days > STALE_AFTER_DAYS:
        age = (date.today() - rdate).days
        print(f"\n  STALE REPORT: data is {age} days old ({rdate:%d %b %Y}).")
        print("  Prices changed since then will show as variances that no longer exist.")

    if over >= 5 and over > len(ok) and over >= 5 * max(under, 1):
        print("\n  NOTE: every gap runs one direction (above list). That pattern usually")
        print("  means a deliberate channel markup rather than pricing errors -- confirm")
        print("  which price the sheet is meant to represent before treating these as fixes.")

    if len(mismatch):
        print("\nLargest gaps:")
        top = mismatch.head(8)
        for _, r in top.iterrows():
            print(f"  {str(r['seller-sku']):<16} {str(r['product'])[:34]:<34} "
                  f"{r['list_price']:>8,.0f} -> {r['live_price']:>8,.0f}  "
                  f"{r['variance_pct']:+.1f}%")

    print(f"\nSaved: {args.out}")


if __name__ == "__main__":
    main()
