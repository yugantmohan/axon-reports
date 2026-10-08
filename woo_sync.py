"""
WooCommerce (B2B portal) orders -> offline_order_lines.

The portal has under a hundred orders, so every run pulls the whole order
history and replaces the WooCommerce rows in one transaction. That is simpler
and safer than incremental sync: edited orders, status changes (on-hold ->
completed), cancellations and refunds are all picked up automatically, and a
missed night costs nothing. When the portal passes a few thousand orders,
switch to the modified_after parameter.

Every status is stored. Which statuses count as a sale is decided in the
v_offline_sku_daily view (currently completed + processing).

Run on its own:   python3 woo_sync.py
Or with Amazon:   python3 sync.py            (runs this at the end)
                  python3 sync.py --only woo
"""

import json
import os
import re
import sys

import psycopg
import requests
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///axon_amazon")
SOURCE = "woocommerce"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _base():
    base = os.environ.get("WOO_URL", "").rstrip("/")
    auth = (os.environ.get("WOO_KEY", ""), os.environ.get("WOO_SECRET", ""))
    if not (base and auth[0] and auth[1]):
        raise RuntimeError("Set WOO_URL, WOO_KEY and WOO_SECRET in .env")
    return base, auth


def _get(path, **params):
    base, auth = _base()
    r = requests.get(f"{base}/wp-json/wc/v3/{path}", auth=auth, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"{path} -> {r.status_code}: {r.text[:300]}")
    return r


def fetch_all_orders():
    orders, page = [], 1
    while True:
        r = _get("orders", per_page=100, page=page, status="any", orderby="id", order="asc")
        orders.extend(r.json())
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            return orders
        page += 1


def fetch_refunds(order_id):
    """{original line_item id: (qty refunded, amount refunded ex tax)}.
    Refund lines carry negative numbers and point back to the original line
    through the _refunded_item_id meta field."""
    out = {}
    for refund in _get(f"orders/{order_id}/refunds").json():
        for li in refund.get("line_items", []):
            orig = next((m.get("value") for m in li.get("meta_data", [])
                         if m.get("key") == "_refunded_item_id"), None)
            if orig is None:
                continue
            q, a = out.get(str(orig), (0, 0.0))
            out[str(orig)] = (q + abs(int(li.get("quantity") or 0)),
                              a + abs(float(li.get("total") or 0)))
    return out


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------

def sku_resolver(cur):
    """Portal SKUs carry suffixes ('5429-FBM', '5311-1-FBA', '5318-01'). Try
    the code as-is, then with trailing segments stripped one at a time. Anything still unmatched stays NULL and
    shows up in v_unmapped_offline_skus."""
    cur.execute("SELECT internal_sku FROM products")
    known = {row[0].upper(): row[0] for row in cur.fetchall()}

    def resolve(raw):
        if not raw:
            return None
        code = raw.strip().upper()
        while True:
            if code in known:
                return known[code]
            # Strip one trailing segment: -FBM, -FBA, -01, -1 ... but never
            # -COMBO, which would turn a combo into its first size.
            if code.endswith("-COMBO"):
                return None
            stripped = re.sub(r"-[A-Z0-9]+$", "", code)
            if stripped == code:
                return None
            code = stripped
    return resolve


def distributor_name(order):
    """Billing company if set, else the billing name. The portal stores the
    business name in first_name and fills last_name with junk: '.', 's', or
    the business name again ('Hanuman Tyre Hanuman Tyre'). Drop last_name
    when it is one of those."""
    b = order.get("billing") or {}
    if (b.get("company") or "").strip():
        return b["company"].strip()
    first = (b.get("first_name") or "").strip(" .")
    last = (b.get("last_name") or "").strip(" .")
    if len(last) <= 2 or last.lower() in first.lower():
        last = ""
    return f"{first} {last}".strip() or None


def canonical_names(rows):
    """One name per portal customer: the same distributor's orders can carry
    differently-typed names. Use the most frequent cleaned name for each
    customer_id (shortest on a tie). Guest orders keep their own name."""
    from collections import Counter, defaultdict
    seen = defaultdict(Counter)
    for r in rows:
        if r["distributor_id"] and r["distributor_id"] != "0" and r["distributor_name"]:
            seen[r["distributor_id"]][r["distributor_name"]] += 1
    best = {cid: sorted(c.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
            for cid, c in seen.items()}
    for r in rows:
        r["distributor_name"] = best.get(r["distributor_id"], r["distributor_name"])
    return rows


def meta(order, key):
    return next((m.get("value") for m in order.get("meta_data", []) if m.get("key") == key), None)


def order_rows(order, resolve):
    refunds = fetch_refunds(order["id"]) if order.get("refunds") else {}
    common = dict(
        order_id=str(order["id"]),
        # date_created is in the portal's own timezone (IST), which is the
        # business day we want. date_created_gmt would shift late orders.
        order_date=order["date_created"][:10],
        order_status=order.get("status"),
        distributor_id=str(order.get("customer_id") or "") or None,
        distributor_name=distributor_name(order),
        distributor_gstin=meta(order, "_billing_gstin"),
    )
    for li in order.get("line_items", []):
        rq, ra = refunds.get(str(li["id"]), (0, 0.0))
        yield dict(
            common,
            line_id=str(li["id"]),
            raw_sku=li.get("sku") or None,
            internal_sku=resolve(li.get("sku")),
            product_name=li.get("name"),
            quantity=int(li.get("quantity") or 0),
            line_total=li.get("total"),          # after discount, before tax
            line_tax=li.get("total_tax"),
            quantity_refunded=rq,
            amount_refunded=round(ra, 2),
        )


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

COLUMNS = ["order_id", "line_id", "order_date", "order_status", "distributor_id",
           "distributor_name", "distributor_gstin", "raw_sku", "internal_sku",
           "product_name", "quantity", "line_total", "line_tax",
           "quantity_refunded", "amount_refunded"]


def load(cur, orders):
    resolve = sku_resolver(cur)
    rows = canonical_names([r for o in orders for r in order_rows(o, resolve)])

    # Safety: never replace a populated table with an empty pull.
    cur.execute("SELECT count(*) FROM offline_order_lines WHERE source_system = %s", (SOURCE,))
    existing = cur.fetchone()[0]
    if existing and not rows:
        raise RuntimeError(f"API returned no order lines but {existing} are stored - not replacing")

    cur.execute("DELETE FROM offline_order_lines WHERE source_system = %s AND ingest_method = 'api'",
                (SOURCE,))
    cur.executemany(
        f"""INSERT INTO offline_order_lines (source_system, ingest_method, {', '.join(COLUMNS)})
            VALUES ('{SOURCE}', 'api', {', '.join(['%s'] * len(COLUMNS))})""",
        [[r[c] for c in COLUMNS] for r in rows],
    )
    return len(rows)


def run_checks(cur):
    print("WooCommerce")
    cur.execute("""SELECT order_status, count(DISTINCT order_id), sum(quantity), sum(line_total)
                   FROM offline_order_lines WHERE source_system = %s
                   GROUP BY 1 ORDER BY 2 DESC""", (SOURCE,))
    for status, n, units, value in cur.fetchall():
        print(f"  {status:<12} {n:>4} orders {units:>7} units  Rs {value:>12,.2f} ex tax")
    cur.execute("""SELECT min(order_date), max(order_date), sum(sku_units), sum(net_value_ex_tax)
                   FROM v_offline_sku_daily WHERE channel = %s""", (SOURCE,))
    lo, hi, units, value = cur.fetchone()
    print(f"  counted as sales: {units} units, Rs {value or 0:,.2f} ex tax ({lo} to {hi})")
    cur.execute("SELECT raw_sku, product_name, units FROM v_unmapped_offline_skus ORDER BY units DESC")
    unmapped = cur.fetchall()
    print(f"  unmapped portal SKUs: {len(unmapped)}")
    for raw, name, units in unmapped[:15]:
        print(f"    {raw or '(no sku)':<20} {units:>6}  {name}")


def sync(conn):
    """Called from sync.py. Logs to sync_runs like the Amazon reports."""
    print("\nWooCommerce: full order history")
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO sync_runs (report_type, account, status)
                       VALUES ('woo_orders', %s, 'running') RETURNING id""", (SOURCE,))
        run_id = cur.fetchone()[0]
    conn.commit()
    try:
        orders = fetch_all_orders()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO raw_report_documents (report_type, account, payload)
                           VALUES ('woo_orders', %s, %s)""", (SOURCE, json.dumps(orders)))
            rows = load(cur, orders)
        conn.commit()
        status, error = "success", None
        print(f"  {len(orders)} orders, {rows} lines loaded")
    except Exception as exc:
        conn.rollback()
        rows, status, error = None, "failed", str(exc)[:2000]
        print(f"  FAILED: {exc}")
    with conn.cursor() as cur:
        cur.execute("""UPDATE sync_runs SET status = %s, rows_written = %s, error_message = %s,
                              finished_at = now() WHERE id = %s""", (status, rows, error, run_id))
    conn.commit()
    return "ok" if status == "success" else "failed"


if __name__ == "__main__":
    with psycopg.connect(DATABASE_URL) as conn:
        result = sync(conn)
        with conn.cursor() as cur:
            print()
            run_checks(cur)
    sys.exit(0 if result == "ok" else 1)
