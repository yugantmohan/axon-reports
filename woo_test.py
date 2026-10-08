"""
WooCommerce first look: pull recent orders and show their real shape, so the
offline ingestion is built against actual data rather than guesses.

Read-only. Needs in .env:
    WOO_URL=https://b2b.axonlifestyle.com
    WOO_KEY=ck_...
    WOO_SECRET=cs_...

Run:
    python3 woo_test.py

Saves raw/woo_orders_sample.json and prints:
  - how many orders exist in each status (decides which ones count as a sale)
  - one full sample line item (decides which fields hold SKU, qty, price)
  - line items with no SKU (these would be invisible in SKU reporting)
"""

import collections
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("WOO_URL", "").rstrip("/")
AUTH = (os.environ.get("WOO_KEY", ""), os.environ.get("WOO_SECRET", ""))

if not (BASE and AUTH[0] and AUTH[1]):
    sys.exit("Set WOO_URL, WOO_KEY and WOO_SECRET in .env first")


def get(path, **params):
    r = requests.get(f"{BASE}/wp-json/wc/v3/{path}", auth=AUTH, params=params, timeout=60)
    if r.status_code != 200:
        sys.exit(f"{path} -> {r.status_code}: {r.text[:400]}")
    return r


# 1. Order counts by status. Your portal has a custom order workflow, so the
#    statuses are not WooCommerce's defaults - we need the real list.
print("Order statuses")
totals = get("reports/orders/totals").json()
for t in totals:
    if t.get("total"):
        print(f"  {t['slug']:<28} {t['total']:>6}  ({t['name']})")

# 2. The most recent 20 orders, all statuses.
r = get("orders", per_page=20, orderby="date", order="desc", status="any")
orders = r.json()
print(f"\nTotal orders in the portal: {r.headers.get('X-WP-Total', '?')}")

os.makedirs("raw", exist_ok=True)
with open("raw/woo_orders_sample.json", "w") as f:
    json.dump(orders, f, indent=2)
print("Saved raw/woo_orders_sample.json")

if not orders:
    sys.exit("No orders returned.")

# 3. Shape of one order and one line item.
o = orders[0]
print("\nSample order fields:")
for key in ("id", "number", "status", "date_created", "date_completed", "customer_id",
            "total", "total_tax", "discount_total", "currency"):
    print(f"  {key:<16} {o.get(key)!r}")
print(f"  billing company  {o.get('billing', {}).get('company')!r}")
print(f"  billing name     {o.get('billing', {}).get('first_name')!r} {o.get('billing', {}).get('last_name')!r}")
print(f"  meta_data keys   {[m.get('key') for m in o.get('meta_data', [])][:15]}")

if o.get("line_items"):
    li = o["line_items"][0]
    print("\nSample line item:")
    for key in ("id", "name", "product_id", "variation_id", "sku", "quantity",
                "subtotal", "total", "total_tax", "price"):
        print(f"  {key:<14} {li.get(key)!r}")

# 4. Line items without a SKU - these cannot be mapped to a product.
no_sku = collections.Counter()
lines = 0
for order in orders:
    for li in order.get("line_items", []):
        lines += 1
        if not li.get("sku"):
            no_sku[li.get("name")] += 1
print(f"\nLine items in sample: {lines}, without a SKU: {sum(no_sku.values())}")
for name, n in no_sku.most_common(10):
    print(f"  {n} x {name}")
