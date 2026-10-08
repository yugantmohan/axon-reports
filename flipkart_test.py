"""
One-off check of the Flipkart Seller API once the self-access app is Active.
Read-only: gets a token, lists a few of our listings, reads their price /
stock / status, and looks at last week's shipped orders. Writes nothing to
the database. Never prints the App Secret or the token.

    python3 flipkart_test.py

Needs in .env (from Seller Hub > Manage Profile > Developer Access > Self Access):
    FLIPKART_APP_ID=...
    FLIPKART_APP_SECRET=...
Paste the output back - it shows the exact shapes Amazon-style code will parse.
"""

import datetime as dt
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api.flipkart.net"
APP_ID = os.environ.get("FLIPKART_APP_ID")
APP_SECRET = os.environ.get("FLIPKART_APP_SECRET")
if not APP_ID or not APP_SECRET:
    sys.exit("Add FLIPKART_APP_ID and FLIPKART_APP_SECRET to .env first.")


def token():
    r = requests.get(f"{BASE}/oauth-service/oauth/token",
                     params={"grant_type": "client_credentials", "scope": "Seller_Api"},
                     auth=(APP_ID, APP_SECRET), timeout=30)
    if r.status_code != 200:
        sys.exit(f"Token request failed: HTTP {r.status_code} {r.text[:300]}\n"
                 "If the app still shows Pending in Seller Hub, wait for it to turn Active.")
    body = r.json()
    print(f"Token OK (valid ~{int(body.get('expires_in', 0)) // 86400} days, scope {body.get('scope')})")
    return body["access_token"]


def post(tok, paths, body):
    """Try each candidate path; Flipkart's docs show listing paths with and without /sellers."""
    for path in paths:
        r = requests.post(BASE + path, json=body, timeout=60,
                          headers={"Authorization": f"Bearer {tok}",
                                   "Content-Type": "application/json"})
        if r.status_code != 404:
            print(f"  {path} -> HTTP {r.status_code}")
            return r
        print(f"  {path} -> 404, trying next")
    return r


def show(label, obj, limit=1500):
    print(f"\n--- {label}\n" + json.dumps(obj, indent=1, ensure_ascii=False)[:limit])


tok = token()

print("\n== Listings (first page, ACTIVE)")
r = post(tok, ["/sellers/listings/v3/search", "/listings/v3/search"],
         {"filters": {"listing_status": "ACTIVE"}, "page_id": None})
listings = []
if r.ok:
    data = r.json()
    listings = data.get("listings") or []
    print(f"  {len(listings)} listings on page 1, has_more={data.get('has_more')}")
    show("first 3 listings", listings[:3])
else:
    print("  ", r.text[:500])

skus = [l.get("sku_id") for l in listings if l.get("sku_id")][:10]
if skus:
    print("\n== Details for up to 10 SKUs (price, status, stock)")
    r = post(tok, ["/sellers/listings/v3/details", "/listings/v3/details"], {"sku_ids": skus})
    if r.ok:
        show("details (first SKU)", dict(list((r.json().get("available") or {}).items())[:1]), 2500)
        print("  unavailable:", r.json().get("unavailable"), " invalid:", r.json().get("invalid"))
    else:
        print("  ", r.text[:500])

print("\n== Shipped / delivered orders, last 7 days (first page)")
to = dt.datetime.now().replace(microsecond=0)
frm = to - dt.timedelta(days=7)
r = post(tok, ["/sellers/v3/shipments/filter/"], {
    "filter": {"type": "postDispatch", "states": ["SHIPPED", "DELIVERED"],
               "orderDate": {"from": frm.isoformat(), "to": to.isoformat()}},
    "pagination": {"pageSize": 20}})
if r.ok:
    data = r.json()
    ships = data.get("shipments") or []
    items = [i for s in ships for i in (s.get("orderItems") or [])]
    print(f"  {len(ships)} shipments, {len(items)} order items on page 1, hasMore={data.get('hasMore')}")
    show("first order item", items[:1])
else:
    print("  ", r.text[:500])

print("\nDone. Nothing was changed on Flipkart.")
