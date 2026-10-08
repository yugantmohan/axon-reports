"""
Load the product map and the raw report JSON you already downloaded into
Postgres, then check the result against Vendor Central.

Run from the sp_api folder:
    pip install "psycopg[binary]"
    psql axon_amazon -f schema.sql
    python load_local.py

Safe to re-run: every insert is an upsert, so fixing a row in product_map.csv
and running again updates it instead of duplicating it.

Expects in the same folder:
    product_map.csv, bundle_components.csv
    raw/vendor_sales.json, raw/vendor_inventory.json,
    raw/vendor_traffic.json, raw/seller_sales_traffic.json
Any raw file that is missing is skipped with a message.
"""

import csv
import json
import os
import sys

import psycopg

try:   # .env holds DATABASE_URL on Windows (password login)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///axon_amazon")


def num(v):
    """Numbers from CSV cells and money objects. '' and None become None."""
    if v is None or v == "":
        return None
    if isinstance(v, dict):                 # {"amount": 12.5, "currencyCode": "INR"}
        v = v.get("amount")
    return float(v) if v is not None else None


def load_json(path):
    if not os.path.exists(path):
        print(f"  skip: {path} not found")
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# product map
# ---------------------------------------------------------------------------

def load_products(cur):
    with open("product_map.csv", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    products, asins = {}, {}
    duplicates, unmapped = [], []

    for r in rows:
        sku = r["internal_sku"].strip()
        asin = r["asin"].strip()
        if not sku:
            if asin:
                unmapped.append(asin)
            continue
        products[sku] = r
        if not asin:
            continue
        if asin in asins and asins[asin] != sku:
            # One ASIN can only belong to one SKU. First row wins; the clash
            # is reported so you can fix it in the CSV.
            duplicates.append((asin, asins[asin], sku))
            continue
        asins[asin] = sku

    bundles = set()
    if os.path.exists("bundle_components.csv"):
        with open("bundle_components.csv", newline="", encoding="utf-8-sig") as f:
            components = list(csv.DictReader(f))
        bundles = {c["bundle_sku"] for c in components}
    else:
        components = []

    cur.executemany(
        """
        INSERT INTO products (internal_sku, product_name, category, report_group,
                              pack_size, is_bundle, mrp, selling_price, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (internal_sku) DO UPDATE SET
            product_name = EXCLUDED.product_name,
            category     = EXCLUDED.category,
            report_group = EXCLUDED.report_group,
            pack_size    = EXCLUDED.pack_size,
            is_bundle    = EXCLUDED.is_bundle,
            mrp          = EXCLUDED.mrp,
            selling_price= EXCLUDED.selling_price,
            updated_at   = now()
        """,
        [
            (sku, r["product_name"], r["category"] or None, r["report_group"] or None,
             r["pack_size"] or None, sku in bundles, num(r["mrp"]), num(r["selling_price"]))
            for sku, r in products.items()
        ],
    )

    # Rebuild the ASIN map from the CSV so a correction removes the old link.
    cur.execute("DELETE FROM product_asins")
    cur.executemany(
        "INSERT INTO product_asins (asin, internal_sku) VALUES (%s, %s)",
        list(asins.items()),
    )

    cur.execute("DELETE FROM bundle_components")
    missing = [c for c in components
               if c["bundle_sku"] not in products or c["component_sku"] not in products]
    cur.executemany(
        "INSERT INTO bundle_components (bundle_sku, component_sku, qty) VALUES (%s, %s, %s)",
        [(c["bundle_sku"], c["component_sku"], int(c["qty"] or 1))
         for c in components if c not in missing],
    )

    print(f"  products: {len(products)}   asins: {len(asins)}   bundles: {len(bundles)}")
    for asin, kept, dropped in duplicates:
        print(f"  DUPLICATE {asin}: kept on SKU {kept}, ignored on SKU {dropped} - fix product_map.csv")
    for c in missing:
        print(f"  bundle row skipped, unknown SKU: {c}")
    if unmapped:
        print(f"  {len(unmapped)} ASINs in the CSV have no SKU yet: {', '.join(unmapped)}")


# ---------------------------------------------------------------------------
# vendor reports
# ---------------------------------------------------------------------------

def load_distributor_asm(cur):
    """distributor_asm.csv -> distributor_asm. Optional file; full replace."""
    if not os.path.exists("distributor_asm.csv"):
        print("  skip: distributor_asm.csv not found")
        return 0
    with open("distributor_asm.csv", newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("distributor_name") or "").strip()]
    cur.execute("DELETE FROM distributor_asm")
    cur.executemany(
        """INSERT INTO distributor_asm (name_key, distributor_name, asm)
           VALUES (lower(regexp_replace(%s, '[^a-zA-Z0-9]', '', 'g')), %s, %s)
           ON CONFLICT (name_key) DO UPDATE SET asm = EXCLUDED.asm""",
        [(r["distributor_name"].strip(), r["distributor_name"].strip(), r["asm"].strip()) for r in rows],
    )
    # Portal distributors with no ASM - fix the spelling in the CSV to match.
    cur.execute("""SELECT DISTINCT distributor_name FROM v_offline_sales_detail
                   WHERE asm = 'Unassigned' ORDER BY 1""")
    missing = [r[0] for r in cur.fetchall()]
    print(f"  distributor ASMs: {len(rows)}")
    if missing:
        print(f"  {len(missing)} portal distributors have no ASM: {', '.join(m or '(blank)' for m in missing)}")
    return len(rows)


def load_asin_prices(cur):
    """asin_prices.csv -> asin_prices. Optional file; full replace."""
    if not os.path.exists("asin_prices.csv"):
        print("  skip: asin_prices.csv not found (run import_prices.py)")
        return 0
    with open("asin_prices.csv", newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("asin") or "").strip()]
    cur.execute("DELETE FROM asin_prices")
    cur.executemany(
        """INSERT INTO asin_prices (asin, valid_from, cost_price_ex_gst, selling_price, mrp,
                                    india_sku, source_tab)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        [(r["asin"].strip(), r["valid_from"], r["cost_price_ex_gst"], r["selling_price"] or None,
          r["mrp"] or None, r["india_sku"] or None, r["source_tab"] or None) for r in rows],
    )
    # Mapped ASINs that sold on Vendor Central but have no price at all.
    cur.execute("""SELECT internal_sku, asin, sum(units) FROM v_sales_value_daily
                   WHERE channel = 'vendor' AND NOT priced
                   GROUP BY 1, 2 ORDER BY 3 DESC""")
    missing = cur.fetchall()
    print(f"  ASIN prices: {len(rows)}")
    if missing:
        print(f"  {len(missing)} Vendor ASINs sold with no CP ({sum(m[2] for m in missing)} units unvalued): "
              + ", ".join(f"{m[0] or '?'}/{m[1]} ({m[2]})" for m in missing[:12])
              + (" ..." if len(missing) > 12 else ""))
    return len(rows)


def report_options(data):
    opts = data.get("reportSpecification", {}).get("reportOptions", {})
    return opts.get("distributorView", "SOURCING"), opts.get("sellingProgram", "RETAIL")


def daily_rows(rows, label):
    """Keep single-day rows only. A weekly or monthly row would be stored under
    its start date and silently inflate that day."""
    keep = [r for r in rows if r["startDate"] == r["endDate"]]
    if len(keep) != len(rows):
        print(f"  {label}: skipped {len(rows) - len(keep)} rows that are not single-day")
    return keep


def load_vendor_sales(cur, data=None):
    if data is None:
        data = load_json("raw/vendor_sales.json")
    if not data:
        return 0
    view, program = report_options(data)

    rows = daily_rows(data.get("salesByAsin", []), "vendor_sales")
    cur.executemany(
        """
        INSERT INTO vendor_sales_daily (asin, report_date, distributor_view, selling_program,
                                        shipped_units, shipped_revenue, shipped_cogs, customer_returns)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (asin, report_date, distributor_view, selling_program) DO UPDATE SET
            shipped_units    = EXCLUDED.shipped_units,
            shipped_revenue  = EXCLUDED.shipped_revenue,
            shipped_cogs     = EXCLUDED.shipped_cogs,
            customer_returns = EXCLUDED.customer_returns,
            ingested_at      = now()
        """,
        [(r["asin"], r["startDate"], view, program, r.get("shippedUnits"),
          num(r.get("shippedRevenue")), num(r.get("shippedCogs")), r.get("customerReturns"))
         for r in rows],
    )

    agg = daily_rows(data.get("salesAggregate", []), "vendor_sales aggregate")
    cur.executemany(
        """
        INSERT INTO vendor_sales_aggregate_daily (report_date, distributor_view, selling_program,
                                                  shipped_units, shipped_revenue, shipped_cogs, customer_returns)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (report_date, distributor_view, selling_program) DO UPDATE SET
            shipped_units    = EXCLUDED.shipped_units,
            shipped_revenue  = EXCLUDED.shipped_revenue,
            shipped_cogs     = EXCLUDED.shipped_cogs,
            customer_returns = EXCLUDED.customer_returns,
            ingested_at      = now()
        """,
        [(r["startDate"], view, program, r.get("shippedUnits"),
          num(r.get("shippedRevenue")), num(r.get("shippedCogs")), r.get("customerReturns"))
         for r in agg],
    )
    print(f"  vendor sales: {len(rows)} ASIN rows, {len(agg)} daily totals")
    return len(rows)


def load_vendor_inventory(cur, data=None):
    if data is None:
        data = load_json("raw/vendor_inventory.json")
    if not data:
        return 0
    view, program = report_options(data)
    rows = daily_rows(data.get("inventoryByAsin", []), "vendor_inventory")
    cur.executemany(
        """
        INSERT INTO vendor_inventory_daily (
            asin, report_date, distributor_view, selling_program,
            open_purchase_order_units, net_received_inventory_units, net_received_inventory_cost,
            sellable_on_hand_units, sellable_on_hand_cost,
            unsellable_on_hand_units, unsellable_on_hand_cost,
            aged_90plus_sellable_units, aged_90plus_sellable_cost,
            sell_through_rate, receive_fill_rate, average_vendor_lead_time_days, vendor_confirmation_rate)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (asin, report_date, distributor_view, selling_program) DO UPDATE SET
            open_purchase_order_units     = EXCLUDED.open_purchase_order_units,
            net_received_inventory_units  = EXCLUDED.net_received_inventory_units,
            net_received_inventory_cost   = EXCLUDED.net_received_inventory_cost,
            sellable_on_hand_units        = EXCLUDED.sellable_on_hand_units,
            sellable_on_hand_cost         = EXCLUDED.sellable_on_hand_cost,
            unsellable_on_hand_units      = EXCLUDED.unsellable_on_hand_units,
            unsellable_on_hand_cost       = EXCLUDED.unsellable_on_hand_cost,
            aged_90plus_sellable_units    = EXCLUDED.aged_90plus_sellable_units,
            aged_90plus_sellable_cost     = EXCLUDED.aged_90plus_sellable_cost,
            sell_through_rate             = EXCLUDED.sell_through_rate,
            receive_fill_rate             = EXCLUDED.receive_fill_rate,
            average_vendor_lead_time_days = EXCLUDED.average_vendor_lead_time_days,
            vendor_confirmation_rate      = EXCLUDED.vendor_confirmation_rate,
            ingested_at                   = now()
        """,
        [(r["asin"], r["startDate"], view, program,
          r.get("openPurchaseOrderUnits"), r.get("netReceivedInventoryUnits"),
          num(r.get("netReceivedInventoryCost")),
          r.get("sellableOnHandInventoryUnits"), num(r.get("sellableOnHandInventoryCost")),
          r.get("unsellableOnHandInventoryUnits"), num(r.get("unsellableOnHandInventoryCost")),
          r.get("aged90PlusDaysSellableInventoryUnits"), num(r.get("aged90PlusDaysSellableInventoryCost")),
          r.get("sellThroughRate"), r.get("receiveFillRate"),
          r.get("averageVendorLeadTimeDays"), r.get("vendorConfirmationRate"))
         for r in rows],
    )
    print(f"  vendor inventory: {len(rows)} ASIN rows")
    return len(rows)


def load_vendor_traffic(cur, data=None):
    if data is None:
        data = load_json("raw/vendor_traffic.json")
    if not data:
        return 0
    rows = daily_rows(data.get("trafficByAsin", []), "vendor_traffic")
    cur.executemany(
        """
        INSERT INTO vendor_traffic_daily (asin, report_date, glance_views)
        VALUES (%s, %s, %s)
        ON CONFLICT (asin, report_date) DO UPDATE SET
            glance_views = EXCLUDED.glance_views, ingested_at = now()
        """,
        [(r["asin"], r["startDate"], r.get("glanceViews")) for r in rows],
    )
    print(f"  vendor traffic: {len(rows)} ASIN rows")
    return len(rows)


# ---------------------------------------------------------------------------
# seller report
# ---------------------------------------------------------------------------

def load_seller(cur, data=None):
    if data is None:
        data = load_json("raw/seller_sales_traffic.json")
    if not data:
        return 0

    by_date = data.get("salesAndTrafficByDate", [])
    cur.executemany(
        """
        INSERT INTO seller_sales_traffic_by_date (
            report_date, units_ordered, ordered_product_sales, units_shipped,
            shipped_product_sales, units_refunded, sessions, page_views,
            buy_box_percentage, unit_session_percentage, total_order_items)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (report_date) DO UPDATE SET
            units_ordered           = EXCLUDED.units_ordered,
            total_order_items       = EXCLUDED.total_order_items,
            ordered_product_sales   = EXCLUDED.ordered_product_sales,
            units_shipped           = EXCLUDED.units_shipped,
            shipped_product_sales   = EXCLUDED.shipped_product_sales,
            units_refunded          = EXCLUDED.units_refunded,
            sessions                = EXCLUDED.sessions,
            page_views              = EXCLUDED.page_views,
            buy_box_percentage      = EXCLUDED.buy_box_percentage,
            unit_session_percentage = EXCLUDED.unit_session_percentage,
            ingested_at             = now()
        """,
        [(d["date"],
          d["salesByDate"].get("unitsOrdered"), num(d["salesByDate"].get("orderedProductSales")),
          d["salesByDate"].get("unitsShipped"), num(d["salesByDate"].get("shippedProductSales")),
          d["salesByDate"].get("unitsRefunded"),
          d["trafficByDate"].get("sessions"), d["trafficByDate"].get("pageViews"),
          d["trafficByDate"].get("buyBoxPercentage"), d["trafficByDate"].get("unitSessionPercentage"),
          d["salesByDate"].get("totalOrderItems"))
         for d in by_date],
    )
    print(f"  seller daily totals: {len(by_date)} days")

    # The ASIN section has no date. It can only go into the daily table when
    # the report covered exactly one day.
    spec = data.get("reportSpecification", {})
    start = (spec.get("dataStartTime") or "")[:10]
    end = (spec.get("dataEndTime") or "")[:10]
    if not start or start != end:
        print(f"  seller ASIN rows NOT loaded: report covers {start} to {end}, not a single day. "
              "The nightly sync pulls seller data one day at a time.")
        return len(by_date)

    rows = data.get("salesAndTrafficByAsin", [])
    cur.executemany(
        """
        INSERT INTO seller_sales_traffic_daily (
            child_asin, report_date, parent_asin, units_ordered, ordered_product_sales,
            units_shipped, shipped_product_sales, units_refunded, sessions, page_views,
            buy_box_percentage, unit_session_percentage)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (child_asin, report_date) DO UPDATE SET
            parent_asin             = EXCLUDED.parent_asin,
            units_ordered           = EXCLUDED.units_ordered,
            ordered_product_sales   = EXCLUDED.ordered_product_sales,
            units_shipped           = EXCLUDED.units_shipped,
            shipped_product_sales   = EXCLUDED.shipped_product_sales,
            units_refunded          = EXCLUDED.units_refunded,
            sessions                = EXCLUDED.sessions,
            page_views              = EXCLUDED.page_views,
            buy_box_percentage      = EXCLUDED.buy_box_percentage,
            unit_session_percentage = EXCLUDED.unit_session_percentage,
            ingested_at             = now()
        """,
        [(r["childAsin"], start, r.get("parentAsin"),
          r["salesByAsin"].get("unitsOrdered"), num(r["salesByAsin"].get("orderedProductSales")),
          r["salesByAsin"].get("unitsShipped"), num(r["salesByAsin"].get("shippedProductSales")),
          r["salesByAsin"].get("unitsRefunded"),
          r["trafficByAsin"].get("sessions"), r["trafficByAsin"].get("pageViews"),
          r["trafficByAsin"].get("buyBoxPercentage"), r["trafficByAsin"].get("unitSessionPercentage"))
         for r in rows],
    )
    print(f"  seller ASIN rows: {len(rows)}")
    return len(by_date) + len(rows)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def run_checks(cur):
    print("\nChecks")

    cur.execute("SELECT min(report_date), max(report_date), sum(shipped_units) FROM vendor_sales_daily")
    first, last, total = cur.fetchone()
    print(f"  vendor shipped units {first} to {last}: {total}")

    cur.execute("""
        SELECT a.report_date, a.shipped_units, sum(s.shipped_units)
        FROM vendor_sales_aggregate_daily a
        JOIN vendor_sales_daily s USING (report_date, distributor_view, selling_program)
        GROUP BY a.report_date, a.shipped_units
        HAVING a.shipped_units <> sum(s.shipped_units)
    """)
    drift = cur.fetchall()
    print("  ASIN rows match Amazon's daily totals" if not drift
          else f"  DRIFT between ASIN rows and daily totals: {drift}")

    cur.execute("SELECT count(*), coalesce(sum(units), 0) FROM v_unmapped_asins WHERE units > 0")
    n, units = cur.fetchone()
    print(f"  unmapped ASINs with sales: {n} ({units} units) - see: SELECT * FROM v_unmapped_asins")

    cur.execute("""
        SELECT coalesce(p.report_group, 'UNMAPPED') AS grp, sum(v.sku_units)
        FROM v_amazon_sku_daily v
        LEFT JOIN products p ON p.internal_sku = v.internal_sku
        WHERE v.channel = 'vendor'
        GROUP BY 1 ORDER BY 2 DESC
    """)
    print("  vendor units by report group (combos split into sizes):")
    for grp, units in cur.fetchall():
        print(f"    {grp:<10} {units}")


if __name__ == "__main__":
    try:
        conn = psycopg.connect(DATABASE_URL)
    except psycopg.OperationalError as exc:
        print(f"Could not connect to {DATABASE_URL}: {exc}")
        sys.exit(1)

    # One transaction: if anything fails, nothing is half-loaded.
    with conn, conn.cursor() as cur:
        print("Products")
        load_products(cur)
        load_distributor_asm(cur)
        load_asin_prices(cur)
        print("\nReports")
        load_vendor_sales(cur)
        load_vendor_inventory(cur)
        load_vendor_traffic(cur)
        load_seller(cur)
        run_checks(cur)
