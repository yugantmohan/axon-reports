"""
Axon reports MCP server - lets Claude Desktop answer questions from the local
axon_amazon database.

Every tool runs fixed SQL through a read-only login (see readonly_user.sql).
Claude chooses the tool and the parameters; it never writes SQL, so answers
come from the same views that were checked against Vendor Central and the
monthly actuals.

Claude Desktop starts this file itself (see the setup notes). To check it by
hand:   python3 mcp_server.py --check

Needs the 1.x MCP library:  python3 -m pip install "mcp<2"
(mcp 2.x renamed FastMCP and changed other APIs; this file is written for 1.x.)
"""

import datetime as dt
import decimal
import os
import sys

import psycopg
from psycopg.rows import dict_row
from mcp.server.fastmcp import FastMCP

try:   # the project's .env (Windows keeps the read-only password there)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# Only MCP_DATABASE_URL is used - the read-only login. Mac default: local
# passwordless login. Windows: postgresql://mcp_reader:PASSWORD@localhost:5432/axon_amazon
DATABASE_URL = os.environ.get("MCP_DATABASE_URL", "postgresql://mcp_reader@/axon_amazon")

mcp = FastMCP(
    "axon-reports",
    instructions=(
        "Sales, inventory and distributor data for Axon Lifestyle (Michelin licensee, India). "
        "Channels: 'vendor' = Amazon Vendor Central, 'seller' = Amazon Seller Central, "
        "'woocommerce' = B2B distributor portal = offline sales. "
        "Units are comparable across channels. Rupee values have a different basis per "
        "channel (see Money below), so label the basis whenever values from different "
        "channels appear together. Amazon vendor data lags about 6 days - every answer should "
        "state the latest date each channel covers, which the tools return under 'coverage'. "
        "Combos: WIPER combos (e.g. 5605-5612-COMBO) are always split into their sizes, so a "
        "wiper combo counts as one unit of each size - never hedge about this. CAR-CARE combos "
        "(5473, 5463, 5433-5436-COMBO, INFLATOR-GAUGE-COMBO) are deliberately their own SKUs and "
        "are NOT split into single cans. "
        "Money: use the sales_value tool. Value bases differ by channel and must always be "
        "labelled. Vendor has two bases: sell_through (default) = Amazon's customer sales x "
        "ETrade CP ex GST = Amazon's demand; sell_in = what Amazon received from Axon at "
        "Amazon's cost ex GST. Axon's monthly actuals are INVOICED sell-in, so compare them "
        "with vendor_basis='sell_in' (gap = stock in transit at month-end), never with "
        "sell-through. ETrade CP comes from the Michelin Amazon mastersheet, today's CP "
        "applied to all history; Seller = Amazon's "
        "shipped product sales incl GST (customer price); portal = billed ex GST. Never present "
        "a combined rupee total without stating that Seller includes GST. Amazon's own vendor "
        "revenue/COGS fields are 0 on amazon.in and are not used. Some new ASINs have no CP yet - "
        "sales_value reports their units as unpriced; never estimate a price for them. "
        "The Amazon Sale MIS (SC Sale, VC Sale, Total, W/o GST, orders/AOV by day) is the "
        "amazon_mis tool; its VC Sale is customer-price value, not Axon's revenue. Ad spend "
        "and TACOS are blank until the Amazon Ads API is approved - say so, never estimate "
        "them or take them from anywhere else. "
        "Buy Box: use the buy_box tool (scheduled pulls every ~3 hours). Reference price is "
        "Axon's approved price sheet. Alerts: Buy Box below the sheet price or below CP, an "
        "outside seller holding it, or it is suppressed. Axon or Etrade (holder "
        "'vendor_channel', our Vendor Central stock) holding it is normal. "
        "Offline data starts 13 Jul 2026 (portal go-live); there is no offline data before that. "
        "Vendor daily sales start 31 Dec 2025. "
        "If a question needs data the tools do not return, say plainly what is missing. Do not "
        "propose code or schema changes and do not assume columns exist - the server is "
        "maintained separately."
    ),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def query(sql, params=None):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return [{k: _clean(v) for k, v in row.items()} for row in cur.fetchall()]


def date_range(start_date, end_date, default_days=30):
    end = dt.date.fromisoformat(end_date) if end_date else dt.date.today()
    start = dt.date.fromisoformat(start_date) if start_date else end - dt.timedelta(days=default_days - 1)
    if start > end:
        raise ValueError("start_date is after end_date")
    return start, end


def coverage(end):
    """Latest day each channel has data for, and a warning when the requested
    period runs past it - otherwise a lagging channel looks like a sales drop."""
    rows = query("""
        SELECT 'vendor' AS channel, max(report_date) AS data_until FROM vendor_sales_daily
        UNION ALL SELECT 'seller', max(report_date) FROM seller_sales_traffic_daily
        UNION ALL SELECT 'woocommerce', max(order_date) FROM offline_order_lines""")
    out = {}
    for r in rows:
        until = r["data_until"]
        note = None
        if until is None:
            note = "no data loaded"
        elif dt.date.fromisoformat(until) < end:
            note = f"period ends {end}, data only to {until} - later days are missing, not zero"
        out[r["channel"]] = {"data_until": until, "warning": note}
    return out


CHANNEL_FILTERS = {
    "all": "TRUE",
    "online": "channel_type = 'online'",
    "offline": "channel_type = 'offline'",
    "vendor": "channel = 'vendor'",
    "seller": "channel = 'seller'",
    "woocommerce": "channel = 'woocommerce'",
}


def channel_filter(channel):
    if channel not in CHANNEL_FILTERS:
        raise ValueError(f"channel must be one of {sorted(CHANNEL_FILTERS)}")
    return CHANNEL_FILTERS[channel]


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@mcp.tool()
def data_freshness() -> dict:
    """How current each data source is, when each last synced successfully,
    and how many Amazon ASINs / portal SKUs are not mapped to a product.
    Call this first when the user asks about recent days, or if numbers look off."""
    latest = query("""
        SELECT 'vendor_sales' AS source, max(report_date) AS data_until FROM vendor_sales_daily
        UNION ALL SELECT 'vendor_inventory', max(report_date) FROM vendor_inventory_daily
        UNION ALL SELECT 'vendor_traffic', max(report_date) FROM vendor_traffic_daily
        UNION ALL SELECT 'seller_sales_traffic', max(report_date) FROM seller_sales_traffic_daily
        UNION ALL SELECT 'woocommerce_orders', max(order_date) FROM offline_order_lines""")
    syncs = query("""
        SELECT report_type, max(finished_at) FILTER (WHERE status = 'success') AS last_success,
               count(*) FILTER (WHERE status = 'failed' AND started_at > now() - interval '3 days')
                   AS failures_last_3_days
        FROM sync_runs GROUP BY report_type ORDER BY report_type""")
    unmapped = query("""
        SELECT (SELECT count(*) FROM v_unmapped_asins WHERE units > 0) AS unmapped_asins_with_sales,
               (SELECT count(*) FROM v_unmapped_offline_skus) AS unmapped_portal_skus""")
    return {"today": dt.date.today().isoformat(), "latest_data": latest,
            "sync_history": syncs, "unmapped": unmapped[0]}


@mcp.tool()
def sales_summary(start_date: str | None = None, end_date: str | None = None,
                  group_by: str = "month", split_by: str = "channel",
                  channel: str = "all") -> dict:
    """Units sold over a period, grouped by day/week/month and split by
    channel, channel_type (online vs offline), report_group (Wipers / Car Care)
    or category. Dates are YYYY-MM-DD; default is the last 30 days.
    channel filters first: all | online | offline | vendor | seller | woocommerce."""
    if group_by not in ("day", "week", "month", "total"):
        raise ValueError("group_by must be day, week, month or total")
    if split_by not in ("channel", "channel_type", "report_group", "category"):
        raise ValueError("split_by must be channel, channel_type, report_group or category")
    start, end = date_range(start_date, end_date)
    period = "'total'" if group_by == "total" else f"date_trunc('{group_by}', sale_date)::date::text"
    rows = query(f"""
        SELECT {period} AS period, {split_by} AS split, sum(sku_units) AS units
        FROM v_sales_all_daily
        WHERE sale_date BETWEEN %(s)s AND %(e)s AND {channel_filter(channel)}
        GROUP BY 1, 2 ORDER BY 1, 3 DESC""", {"s": start, "e": end})
    return {"period": [start.isoformat(), end.isoformat()], "rows": rows,
            "coverage": coverage(end)}


@mcp.tool()
def top_products(start_date: str | None = None, end_date: str | None = None,
                 channel: str = "all", limit: int = 15, bottom: bool = False,
                 report_group: str | None = None) -> dict:
    """Best (or, with bottom=True, worst) selling SKUs by units, with the
    vendor / seller / offline split. bottom=True includes SKUs that sold zero.
    report_group optionally limits to 'Wipers' or 'Car Care'. Default period
    is the last 30 days."""
    start, end = date_range(start_date, end_date)
    limit = max(1, min(int(limit), 100))
    rows = query(f"""
        WITH s AS (
            SELECT internal_sku,
                   sum(sku_units) FILTER (WHERE channel = 'vendor')      AS vendor,
                   sum(sku_units) FILTER (WHERE channel = 'seller')      AS seller,
                   sum(sku_units) FILTER (WHERE channel = 'woocommerce') AS offline,
                   sum(sku_units)                                        AS total
            FROM v_sales_all_daily
            WHERE sale_date BETWEEN %(s)s AND %(e)s AND {channel_filter(channel)}
              AND internal_sku IS NOT NULL
            GROUP BY 1)
        SELECT p.internal_sku AS sku, p.product_name, p.report_group,
               COALESCE(s.vendor, 0) AS vendor, COALESCE(s.seller, 0) AS seller,
               COALESCE(s.offline, 0) AS offline, COALESCE(s.total, 0) AS total
        FROM products p LEFT JOIN s USING (internal_sku)
        WHERE NOT p.is_bundle
          AND (%(g)s::text IS NULL OR p.report_group = %(g)s)
          AND ({'TRUE' if bottom else 's.total > 0'})
        ORDER BY total {'ASC' if bottom else 'DESC'}, p.internal_sku
        LIMIT %(n)s""", {"s": start, "e": end, "g": report_group, "n": limit})
    return {"period": [start.isoformat(), end.isoformat()], "rows": rows,
            "coverage": coverage(end)}


@mcp.tool()
def product_detail(search: str, start_date: str | None = None, end_date: str | None = None,
                   group_by: str = "month") -> dict:
    """Sales of one product across all channels over time, plus its latest
    Amazon vendor inventory. search is a SKU ('5311') or part of a product
    name ('shampoo'); up to 10 matching SKUs are returned. Default period is
    the last 90 days."""
    if group_by not in ("day", "week", "month"):
        raise ValueError("group_by must be day, week or month")
    start, end = date_range(start_date, end_date, default_days=90)
    matches = query("""
        SELECT internal_sku, product_name, report_group, pack_size, mrp, selling_price,
               (SELECT string_agg(asin, ', ') FROM product_asins a
                 WHERE a.internal_sku = p.internal_sku) AS asins
        FROM products p
        WHERE internal_sku ILIKE %(q)s OR product_name ILIKE '%%' || %(q)s || '%%'
        ORDER BY internal_sku = %(q)s DESC, internal_sku LIMIT 10""", {"q": search.strip()})
    if not matches:
        return {"matches": [], "note": f"no product matches '{search}'"}
    skus = [m["internal_sku"] for m in matches]
    sales = query(f"""
        SELECT internal_sku AS sku, date_trunc('{group_by}', sale_date)::date AS period,
               channel, sum(sku_units) AS units
        FROM v_sales_all_daily
        WHERE internal_sku = ANY(%(k)s) AND sale_date BETWEEN %(s)s AND %(e)s
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", {"k": skus, "s": start, "e": end})
    inventory = query("""
        SELECT pa.internal_sku AS sku, i.asin, i.report_date,
               i.sellable_on_hand_units, i.open_purchase_order_units, i.aged_90plus_sellable_units
        FROM vendor_inventory_daily i JOIN product_asins pa USING (asin)
        WHERE pa.internal_sku = ANY(%(k)s)
          AND i.report_date = (SELECT max(report_date) FROM vendor_inventory_daily)""",
        {"k": skus})
    return {"period": [start.isoformat(), end.isoformat()], "matches": matches,
            "sales": sales, "amazon_vendor_inventory": inventory, "coverage": coverage(end)}


@mcp.tool()
def distributor_sales(start_date: str | None = None, end_date: str | None = None,
                      distributor: str | None = None) -> dict:
    """Offline (B2B portal) sales by distributor: orders, units and value
    ex GST for counted orders (completed + processing), plus cancelled and
    on-hold orders shown separately. distributor optionally filters by part
    of the name. Default period is the last 90 days."""
    start, end = date_range(start_date, end_date, default_days=90)
    rows = query("""
        SELECT distributor_name, distributor_gstin,
               count(DISTINCT order_id) FILTER (WHERE order_status IN ('completed','processing')) AS orders,
               sum(quantity - quantity_refunded) FILTER (WHERE order_status IN ('completed','processing')) AS units,
               sum(line_total - amount_refunded) FILTER (WHERE order_status IN ('completed','processing')) AS value_ex_gst,
               count(DISTINCT order_id) FILTER (WHERE order_status = 'cancelled') AS cancelled_orders,
               sum(line_total) FILTER (WHERE order_status = 'cancelled') AS cancelled_value_ex_gst,
               count(DISTINCT order_id) FILTER (WHERE order_status = 'on-hold') AS on_hold_orders,
               sum(line_total) FILTER (WHERE order_status = 'on-hold') AS on_hold_value_ex_gst,
               max(order_date) AS last_order
        FROM offline_order_lines
        WHERE order_date BETWEEN %(s)s AND %(e)s
          AND (%(d)s::text IS NULL OR distributor_name ILIKE '%%' || %(d)s || '%%')
        GROUP BY 1, 2
        ORDER BY value_ex_gst DESC NULLS LAST""", {"s": start, "e": end, "d": distributor})
    return {"period": [start.isoformat(), end.isoformat()], "rows": rows,
            "note": "on-hold = awaiting processing; cancelled = genuine cancellations. "
                    "Neither is counted in sales.",
            "coverage": coverage(end)}


@mcp.tool()
def amazon_inventory(search: str | None = None, max_days_of_cover: float | None = None,
                     as_of_date: str | None = None) -> dict:
    """Amazon Vendor Central inventory per ASIN: sellable units on hand, open
    PO units (null = no open PO), aged 90+ days, and days of cover at the
    average daily VENDOR sales of the 30 days up to the inventory date
    (Seller Central sales do not draw on Vendor stock, so they are excluded).
    Default is the latest inventory date. as_of_date (YYYY-MM-DD) returns the
    snapshot on or before that date - use it to compare with last week.
    search filters by SKU, ASIN or product name; max_days_of_cover (e.g. 22)
    returns only items running low. Sorted by lowest cover first."""
    rows = _inventory_snapshot(as_of_date, search)
    if max_days_of_cover is not None:
        rows = [r for r in rows if r["days_of_cover"] is not None
                and r["days_of_cover"] <= max_days_of_cover]
    return {"rows": rows,
            "note": "Amazon's own warehouse stock only, not H-188. Out-of-stock dates count "
                    "from the inventory date, which lags today by about 6 days - stock may "
                    "already be lower. Days of cover is null when the ASIN had no vendor "
                    "sales in the 30 days before the inventory date."}


def _inventory_snapshot(as_of_date=None, search=None):
    as_of = dt.date.fromisoformat(as_of_date) if as_of_date else dt.date(9999, 12, 31)
    return query("""
        WITH snap AS (SELECT max(report_date) AS d FROM vendor_inventory_daily
                      WHERE report_date <= %(asof)s),
             sales_end AS (SELECT least(max(v.report_date), (SELECT d FROM snap)) AS d
                           FROM vendor_sales_daily v),
             rate AS (
                SELECT asin, sum(shipped_units) / 30.0 AS avg_daily_units
                FROM vendor_sales_daily, sales_end
                WHERE report_date > sales_end.d - 30 AND report_date <= sales_end.d
                GROUP BY asin)
        SELECT i.asin, pa.internal_sku AS sku, COALESCE(p.product_name, '(unmapped)') AS product_name,
               i.report_date, i.sellable_on_hand_units, i.open_purchase_order_units,
               i.aged_90plus_sellable_units, round(r.avg_daily_units, 1) AS avg_daily_units_30d,
               CASE WHEN r.avg_daily_units > 0
                    THEN round(i.sellable_on_hand_units / r.avg_daily_units, 1) END AS days_of_cover,
               CASE WHEN r.avg_daily_units > 0
                    THEN i.report_date + floor(i.sellable_on_hand_units / r.avg_daily_units)::int
                    END AS est_out_of_stock_date
        FROM vendor_inventory_daily i
        JOIN snap ON i.report_date = snap.d
        LEFT JOIN product_asins pa USING (asin)
        LEFT JOIN products p ON p.internal_sku = pa.internal_sku
        LEFT JOIN rate r USING (asin)
        WHERE (%(q)s::text IS NULL OR pa.internal_sku ILIKE %(q)s
               OR p.product_name ILIKE '%%' || %(q)s || '%%' OR i.asin = %(q)s)
        ORDER BY days_of_cover ASC NULLS LAST, i.sellable_on_hand_units DESC""",
        {"q": search, "asof": as_of})


@mcp.tool()
def distributor_product_sales(start_date: str | None = None, end_date: str | None = None,
                              by: str = "distributor", product_level: str = "category",
                              group_by: str = "total", distributor: str | None = None,
                              asm: str | None = None, category: str | None = None,
                              limit: int = 1000) -> dict:
    """Offline (B2B portal) sales: WHO bought WHAT. Units and billed value
    ex GST (after refunds) for counted orders (completed + processing).
      by:            distributor | asm | asm_distributor | none  (who)
      product_level: category | category_pack | sku | none       (what)
      group_by:      total | month
    Filters: distributor (part of name), asm (e.g. 'Sami'), category
    (e.g. 'Car Care'). Distributors with no ASM show as 'Unassigned'.
    Totals match sales_summary / distributor_sales for the same period.
    Default period is the last 90 days."""
    who = {"distributor": ["distributor_name"], "asm": ["asm"],
           "asm_distributor": ["asm", "distributor_name"], "none": []}
    what = {"category": ["category"], "category_pack": ["category", "pack_size"],
            "sku": ["category", "internal_sku", "product_name", "pack_size"], "none": []}
    if by not in who:
        raise ValueError(f"by must be one of {list(who)}")
    if product_level not in what:
        raise ValueError(f"product_level must be one of {list(what)}")
    if group_by not in ("total", "month"):
        raise ValueError("group_by must be total or month")
    start, end = date_range(start_date, end_date, default_days=90)
    limit = max(1, min(int(limit), 5000))
    cols = (["date_trunc('month', order_date)::date AS month"] if group_by == "month" else []) \
        + who[by] + what[product_level]
    keys = ", ".join(str(i + 1) for i in range(len(cols)))
    select_cols = ", ".join(cols) + ", " if cols else ""
    rows = query(f"""
        SELECT {select_cols}sum(sku_units) AS units, round(sum(net_value_ex_tax), 2) AS value_ex_gst
        FROM v_offline_sales_detail
        WHERE order_date BETWEEN %(s)s AND %(e)s
          AND (%(d)s::text IS NULL OR distributor_name ILIKE '%%' || %(d)s || '%%')
          AND (%(a)s::text IS NULL OR asm ILIKE %(a)s)
          AND (%(c)s::text IS NULL OR category ILIKE %(c)s)
        {"GROUP BY " + keys if cols else ""}
        ORDER BY {keys + ", " if group_by == "month" else ""}units DESC
        LIMIT %(n)s""",
        {"s": start, "e": end, "d": distributor, "a": asm, "c": category, "n": limit + 1})
    truncated = len(rows) > limit
    total = query("""
        SELECT sum(sku_units) AS units, round(sum(net_value_ex_tax), 2) AS value_ex_gst
        FROM v_offline_sales_detail
        WHERE order_date BETWEEN %(s)s AND %(e)s
          AND (%(d)s::text IS NULL OR distributor_name ILIKE '%%' || %(d)s || '%%')
          AND (%(a)s::text IS NULL OR asm ILIKE %(a)s)
          AND (%(c)s::text IS NULL OR category ILIKE %(c)s)""",
        {"s": start, "e": end, "d": distributor, "a": asm, "c": category})[0]
    unassigned = query("""
        SELECT DISTINCT distributor_name FROM v_offline_sales_detail
        WHERE asm = 'Unassigned' AND order_date BETWEEN %(s)s AND %(e)s ORDER BY 1""",
        {"s": start, "e": end})
    return {"period": [start.isoformat(), end.isoformat()],
            "rows": rows[:limit], "truncated": truncated, "total": total,
            "unassigned_distributors": [r["distributor_name"] for r in unassigned],
            "note": "Offline data starts 13 Jul 2026. Car-care combos are their own SKUs. "
                    "pack_size comes from the product master ('no size' where blank).",
            "coverage": coverage(end)}


@mcp.tool()
def sales_value(start_date: str | None = None, end_date: str | None = None,
                group_by: str = "month", split_by: str = "channel",
                channel: str = "all", report_group: str | None = None,
                vendor_basis: str = "sell_through", limit: int = 500) -> dict:
    """Sales in RUPEES (and units) per channel.
      Vendor      = vendor_basis 'sell_through' (default): Amazon's sales to
                    customers x ETrade CP ex GST - Amazon's DEMAND.
                    vendor_basis 'sell_in': what Amazon RECEIVED from Axon, at
                    Amazon's own cost ex GST - compare THIS with Axon's invoiced
                    Vendor actuals (differs only by stock in transit at month-end).
      Seller      = Amazon's shipped product sales, INCL GST
      woocommerce = portal billed value ex GST (offline)
    group_by: day | week | month | total.
    split_by: channel | channel_type | report_group | category | sku.
    channel filter: all | online | offline | vendor | seller | woocommerce.
    Units are listing units (a wiper combo counts once, at its combo price).
    'unpriced_units' are units with no price yet - report them, never estimate.
    Default period is the last 30 days."""
    if group_by not in ("day", "week", "month", "total"):
        raise ValueError("group_by must be day, week, month or total")
    splits = {"channel": "channel, value_basis", "channel_type": "channel_type",
              "report_group": "report_group", "category": "category",
              "sku": "channel, internal_sku, product_name"}
    if split_by not in splits:
        raise ValueError(f"split_by must be one of {list(splits)}")
    if vendor_basis not in ("sell_through", "sell_in"):
        raise ValueError("vendor_basis must be sell_through or sell_in")
    view = "v_sales_value_sellin_daily" if vendor_basis == "sell_in" else "v_sales_value_daily"
    start, end = date_range(start_date, end_date)
    limit = max(1, min(int(limit), 5000))
    period = "'total'" if group_by == "total" else f"date_trunc('{group_by}', sale_date)::date::text"
    cols = splits[split_by]
    n_keys = 1 + len(cols.split(","))
    rows = query(f"""
        SELECT {period} AS period, {cols},
               sum(units) AS units,
               round(sum(value), 2) AS value,
               sum(units) FILTER (WHERE NOT priced) AS unpriced_units
        FROM {view}
        WHERE sale_date BETWEEN %(s)s AND %(e)s AND {channel_filter(channel)}
          AND (%(g)s::text IS NULL OR report_group = %(g)s)
        GROUP BY {", ".join(str(i + 1) for i in range(n_keys))}
        ORDER BY 1, value DESC NULLS LAST
        LIMIT %(n)s""", {"s": start, "e": end, "g": report_group, "n": limit + 1})
    unpriced = query(f"""
        SELECT internal_sku, product_name, asin, sum(units) AS units
        FROM {view}
        WHERE NOT priced AND sale_date BETWEEN %(s)s AND %(e)s AND {channel_filter(channel)}
        GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 25""", {"s": start, "e": end})
    return {"period": [start.isoformat(), end.isoformat()],
            "rows": rows[:limit], "truncated": len(rows) > limit,
            "unpriced_items": unpriced,
            "value_basis": {"vendor": ("Amazon received (sell-in), Amazon cost ex GST - compare with "
                                       "invoiced actuals" if vendor_basis == "sell_in" else
                                       "sell-through: Amazon's customer sales x ETrade CP, ex GST - "
                                       "NOT the invoiced figure"),
                            "seller": "Amazon shipped product sales, incl GST",
                            "woocommerce": "portal billed, ex GST"},
            "coverage": coverage(end)}


@mcp.tool()
def buy_box(search: str | None = None, issues_only: bool = False,
            history_days: int = 0) -> dict:
    """Amazon Buy Box for our own ASINs, from the latest scheduled pull
    (every ~3 hours). Reference price = Axon's approved price sheet
    (selling price) and ETrade CP. Holder 'vendor_channel' = Etrade (our
    Vendor Central stock), 'axon' = our Seller Central; both are normal.
    ALERTS (issues_only=True returns only these):
      below_sheet_price     - Buy Box below the sheet selling price
      below_etrade_cp       - Buy Box /1.18 below ETrade CP (assumes 18% GST)
      buy_box_lost_to_other - an outside seller holds the Buy Box
      buy_box_suppressed    - offers exist but nobody has the Buy Box
      holder_changed_1d     - the holder changed vs ~1 day earlier
    INFO only: undercut_by_other (outside seller cheaper than our lowest
    offer), axon_undercuts_vendor, no_offers (out of stock / inactive - a
    stock question for amazon_inventory/stock_health), pct_vs_sheet.
    search: SKU, ASIN or product name. history_days > 0 (with search):
    price/holder/rank history per pull."""
    rows = query("""
        SELECT * FROM v_buy_box_latest
        WHERE (%(q)s::text IS NULL OR sku ILIKE %(q)s OR asin = upper(%(q)s)
               OR product_name ILIKE '%%' || %(q)s || '%%')
          AND (NOT %(i)s OR below_sheet_price OR below_etrade_cp OR buy_box_lost_to_other
               OR buy_box_suppressed OR holder_changed_1d)
        ORDER BY below_etrade_cp DESC NULLS LAST, buy_box_lost_to_other DESC NULLS LAST,
                 buy_box_suppressed DESC NULLS LAST, pct_vs_sheet ASC NULLS LAST, sku""",
        {"q": search, "i": issues_only})
    counts = query("""
        SELECT max(fetched_at) AS latest_pull, count(*) AS asins,
               count(*) FILTER (WHERE below_sheet_price)     AS alert_below_sheet_price,
               count(*) FILTER (WHERE below_etrade_cp)       AS alert_below_etrade_cp,
               count(*) FILTER (WHERE buy_box_lost_to_other) AS alert_lost_to_other_seller,
               count(*) FILTER (WHERE buy_box_suppressed)    AS alert_buy_box_suppressed,
               count(*) FILTER (WHERE holder_changed_1d)     AS alert_holder_changed_1d,
               count(*) FILTER (WHERE undercut_by_other)     AS info_undercut_by_other_seller,
               count(*) FILTER (WHERE no_offers)             AS info_no_offers,
               count(*) FILTER (WHERE axon_undercuts_vendor) AS info_axon_undercuts_etrade,
               count(*) FILTER (WHERE error IS NOT NULL)     AS fetch_errors
        FROM v_buy_box_latest""")[0]
    for r in rows:
        r.pop("offers", None)
    out = {"summary": counts, "rows": rows,
           "note": "Prices are customer prices incl GST. Reference = the approved price "
                   "sheet. Axon or Etrade (vendor_channel) holding the Buy Box is normal."}
    if search and history_days > 0 and rows:
        out["history"] = query("""
            SELECT asin, fetched_at, buy_box_price, buy_box_holder, axon_price,
                   vendor_channel_price, other_seller_count, sales_rank
            FROM buy_box_snapshots
            WHERE asin = ANY(%(a)s) AND fetched_at > now() - make_interval(days => %(d)s)
            ORDER BY asin, fetched_at""",
            {"a": [r["asin"] for r in rows][:20], "d": int(history_days)})
    return out


@mcp.tool()
def amazon_mis(month: str | None = None, months_back: int = 0) -> dict:
    """The Amazon Sale MIS, rebuilt from data. Same columns as the Excel MIS:
    SC Sale (Seller Central ordered sales, incl GST), VC Sale (Vendor shipped
    units x that day's customer price incl GST - Buy Box price where pulled,
    else sheet selling price), Total Sale, W/o GST (= Total / 1.18), plus
    units, Seller orders, Seller AOV (sales / orders) and Seller sales per unit
    (what the Nov/Dec MIS tabs called "AOV").
    Ad spend and TACOS are BLANK (null) until the Amazon Ads API is approved;
    'ads_status' says so. Never estimate them.
      month='YYYY-MM' (default: current month): one row per day + month total.
      months_back=N (>0): instead, monthly totals for the last N months up to
      'month', for month-on-month comparison.
    VC Sale is customer-price value (Amazon's sales), NOT Axon's revenue -
    for revenue use sales_value. Vendor data lags ~3-4 days, Seller ~1 day;
    'coverage' says which recent days are still missing."""
    today = dt.date.today()
    if month:
        y, m = (int(x) for x in month.split("-")[:2])
    else:
        y, m = today.year, today.month
    first = dt.date(y, m, 1)
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)

    def totals(rows):
        sc = sum(r["sc_sale"] or 0 for r in rows)
        vc = sum(r["vc_sale"] or 0 for r in rows)
        tot = sc + vc
        sc_units = sum(r["sc_units"] or 0 for r in rows)
        # Orders/AOV only over days that have an order count (older raw
        # Seller reports may lack it).
        od = [r for r in rows if r["sc_orders"] is not None]
        sc_orders = sum(r["sc_orders"] for r in od)
        od_sale = sum(r["sc_sale"] or 0 for r in od)
        out = {"sc_sale": round(sc, 2), "vc_sale": round(vc, 2), "total_sale": round(tot, 2),
               "total_ex_gst": round(tot / 1.18, 2),
               "sc_units": sc_units,
               "sc_orders": sc_orders if od else None,
               "sc_aov": round(od_sale / sc_orders, 2) if sc_orders else None,
               "sc_sale_per_unit": round(sc / sc_units, 2) if sc_units else None,
               "vc_units": sum(r["vc_units"] or 0 for r in rows),
               "total_units": sum(r["total_units"] or 0 for r in rows),
               "vc_units_buy_box_priced": sum(r["vc_units_buy_box_priced"] or 0 for r in rows),
               "vc_units_unpriced": sum(r["vc_units_unpriced"] or 0 for r in rows),
               "days_with_sc": sum(1 for r in rows if r["has_sc"]),
               "days_with_vc": sum(1 for r in rows if r["has_vc"]),
               "ad_spend": None, "tacos": None}
        # Ads API rows only. TACOS only over days that have Vendor sales loaded:
        # Vendor data lags 3-4 days, so recent days would inflate it.
        if any(r["has_ad"] for r in rows):
            cov = [r for r in rows if r["has_vc"] and r["has_ad"]]
            cov_tot = sum((r["sc_sale"] or 0) + (r["vc_sale"] or 0) for r in cov)
            out["ad_spend"] = round(sum(r["ad_spend"] or 0 for r in rows), 2)
            out["tacos"] = (round(sum(r["ad_spend"] for r in cov) / (cov_tot / 1.18), 4)
                            if cov_tot else None)
            out["tacos_basis"] = f"{len(cov)} days with both Vendor and ad data"
        return out

    ads_status = ("Ad spend and TACOS: blank - waiting for Amazon Ads API approval."
                  if not query("SELECT 1 FROM v_amazon_mis_daily WHERE has_ad LIMIT 1")
                  else "Ad spend from the Amazon Ads API.")

    if months_back and months_back > 0:
        start = dt.date(y, m, 1)
        for _ in range(int(months_back) - 1):
            start = dt.date(start.year - (start.month == 1), (start.month - 2) % 12 + 1, 1)
        rows = query("""SELECT * FROM v_amazon_mis_daily
                        WHERE sale_date >= %(s)s AND sale_date < %(e)s ORDER BY sale_date""",
                     {"s": start, "e": nxt})
        by = {}
        for r in rows:
            by.setdefault(r["sale_date"][:7], []).append(r)
        out_rows = [dict(month=k, **totals(v)) for k, v in sorted(by.items())]
        return {"months": out_rows, "ads_status": ads_status,
                "coverage": coverage(min(nxt - dt.timedelta(days=1), today)),
                "note": "Months before Seller data starts show VC only."}

    rows = query("""SELECT * FROM v_amazon_mis_daily
                    WHERE sale_date >= %(s)s AND sale_date < %(e)s ORDER BY sale_date""",
                 {"s": first, "e": nxt})
    return {"month": f"{y}-{m:02d}", "days": rows, "total": totals(rows),
            "ads_status": ads_status,
            "coverage": coverage(min(nxt - dt.timedelta(days=1), today)),
            "note": "SC = ordered sales incl GST; VC = shipped units x customer price incl GST; "
                    "W/o GST = total / 1.18. Days after the coverage date are missing, "
                    "not zero."}


def _bucket(r, cover_days, overstock_days, min_daily):
    rate = r["avg_daily_units_30d"] or 0
    cover = r["days_of_cover"]
    has_po = (r["open_purchase_order_units"] or 0) > 0
    if rate >= min_daily and cover is not None and cover <= cover_days:
        return "B_watch" if has_po else "A_urgent"
    if (cover is not None and cover > overstock_days) or (r["aged_90plus_sellable_units"] or 0) > 0:
        return "C_overstocked"
    return None


@mcp.tool()
def stock_health(cover_days: float = 22, overstock_days: float = 90, min_daily_units: float = 1,
                 target_cover_days: float = 45, compare_days: int = 7) -> dict:
    """Weekly Amazon Vendor Central PO tracker. Buckets every ASIN:
      A_urgent      - >= min_daily_units/day, <= cover_days of cover, NO open PO:
                      PO request needed now (includes out-of-stock ASINs)
      B_watch       - same, but WITH an open PO: confirm it arrives in time
      C_overstocked - > overstock_days of cover, or any 90+ day aged units
                      (any sales rate): deal / coupon / ad candidate
    Returns the vendor-manager list for bucket A with
    suggested_po_qty = ceil(target_cover_days x units/day - on hand - open PO),
    and the ASINs that entered bucket A since the snapshot compare_days earlier.
    All numbers are computed here - report them as returned."""
    import math
    now = _inventory_snapshot()
    if not now:
        return {"error": "no vendor inventory loaded"}
    snap_date = dt.date.fromisoformat(now[0]["report_date"])
    prev_date = (snap_date - dt.timedelta(days=compare_days)).isoformat()
    prev = _inventory_snapshot(prev_date)
    args = (cover_days, overstock_days, min_daily_units)
    prev_bucket = {r["asin"]: _bucket(r, *args) for r in prev}

    buckets = {"A_urgent": [], "B_watch": [], "C_overstocked": []}
    for r in now:
        b = _bucket(r, *args)
        if b:
            r = dict(r, out_of_stock_now=(r["sellable_on_hand_units"] or 0) == 0
                     and (r["avg_daily_units_30d"] or 0) > 0)
            buckets[b].append(r)
    buckets["C_overstocked"].sort(key=lambda r: -(r["days_of_cover"] or 1e9))

    vendor_list = []
    for r in buckets["A_urgent"]:
        qty = math.ceil(target_cover_days * (r["avg_daily_units_30d"] or 0)
                        - (r["sellable_on_hand_units"] or 0)
                        - (r["open_purchase_order_units"] or 0))
        vendor_list.append({"sku": r["sku"], "product_name": r["product_name"], "asin": r["asin"],
                            "units_per_day": r["avg_daily_units_30d"],
                            "on_hand": r["sellable_on_hand_units"],
                            "suggested_po_qty": max(qty, 0)})

    new_in_a = [{"asin": r["asin"], "sku": r["sku"], "product_name": r["product_name"],
                 "was": prev_bucket.get(r["asin"]) or ("not listed" if r["asin"] not in prev_bucket
                                                       else "no bucket")}
                for r in buckets["A_urgent"] if prev_bucket.get(r["asin"]) != "A_urgent"]

    return {"inventory_date": snap_date.isoformat(),
            "compared_with": prev[0]["report_date"] if prev else None,
            "rules": {"cover_days": cover_days, "overstock_days": overstock_days,
                      "min_daily_units": min_daily_units, "target_cover_days": target_cover_days},
            "buckets": buckets,
            "vendor_manager_list": vendor_list,
            "new_in_A_since_last_week": new_in_a,
            "note": "Units/day = Vendor Central shipped units, 30 days to the inventory date. "
                    "Inventory lags today by ~6 days. H-188 warehouse stock is NOT included - "
                    "confirm H-188 can fill each PO request before sending."}


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--check" in sys.argv:
        # Quick self-test from the terminal: runs every tool once.
        import json
        for name, fn, kwargs in [
            ("data_freshness", data_freshness, {}),
            ("sales_summary", sales_summary, {"split_by": "channel_type", "group_by": "total"}),
            ("top_products", top_products, {"limit": 5}),
            ("product_detail", product_detail, {"search": "5311"}),
            ("distributor_sales", distributor_sales, {}),
            ("amazon_inventory", amazon_inventory, {"max_days_of_cover": 15}),
            ("stock_health", stock_health, {}),
            ("sales_value", sales_value, {"group_by": "month", "split_by": "channel"}),
            ("buy_box", buy_box, {"issues_only": True}),
            ("amazon_mis", amazon_mis, {"months_back": 3}),
            ("sales_value sell-in", sales_value,
             {"group_by": "month", "split_by": "report_group", "channel": "vendor",
              "vendor_basis": "sell_in"}),
            ("distributor_product_sales", distributor_product_sales,
             {"by": "asm", "product_level": "category", "group_by": "month"}),
        ]:
            out = fn(**kwargs)
            print(f"\n== {name} ==")
            print(json.dumps(out, indent=1)[:1200])
        print("\nAll tools ran.")
    else:
        mcp.run()
