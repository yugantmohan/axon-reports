"""
Buy Box snapshots for every mapped ASIN -> buy_box_snapshots.

Runs with every WooCommerce refresh (10:00, 13:00, 16:00, 19:00 via sync.py),
or on its own:   python3 sync.py --only pricing

Read-only use of the Product Pricing API (getItemOffersBatch), limited to OUR
OWN ASINs from product_asins - matching the Pricing use declared to Amazon
(checking our listing prices against the approved selling-price sheet; no
repricing, no competitor ASINs).

About 120 ASINs = 7 batch calls of 20, one per ~10 s (Amazon's limit), so a
refresh takes a little over a minute.
"""

import json
import os
import time

# Seller IDs seen on our listings (confirmed Oct 2026).
AXON_SELLER_ID = os.environ.get("AXON_SELLER_ID", "A1QTZ8OBV6L6UD")            # our Seller Central
VENDOR_CHANNEL_SELLER_ID = os.environ.get("VENDOR_CHANNEL_SELLER_ID", "A2AL6IVND0I91F")
# ^ Etrade - the entity we bill through Vendor Central; it sells our Vendor stock (confirmed Oct 2026).

BATCH = 20
SPACING_S = 11


def holder_type(seller_id):
    if not seller_id:
        return None
    if seller_id == AXON_SELLER_ID:
        return "axon"
    if seller_id == VENDOR_CHANNEL_SELLER_ID:
        return "vendor_channel"
    return "other"


def amount(m):
    return (m or {}).get("Amount")


def parse(asin, resp):
    """One batch response item -> one snapshot row."""
    status = (resp.get("status") or {}).get("statusCode")
    body = resp.get("body") or {}
    payload = body.get("payload") or {}
    row = {"asin": asin, "status": status, "error": None, "offers": None,
           "buy_box_price": None, "buy_box_seller": None, "buy_box_holder": None,
           "buy_box_fba": None, "total_offers": None, "lowest_price": None,
           "axon_price": None, "vendor_channel_price": None,
           "other_seller_count": 0, "other_seller_min_price": None,
           "sales_rank": None, "sales_rank_sub": None, "sales_rank_category": None}
    if status != 200:
        row["error"] = json.dumps(body.get("errors") or body)[:500]
        return row

    s = payload.get("Summary") or {}
    offers = payload.get("Offers") or []
    bb = (s.get("BuyBoxPrices") or [{}])[0]
    row["buy_box_price"] = amount(bb.get("LandedPrice")) or amount(bb.get("ListingPrice"))
    row["total_offers"] = s.get("TotalOfferCount")
    row["lowest_price"] = amount(((s.get("LowestPrices") or [{}])[0]).get("LandedPrice"))
    ranks = s.get("SalesRankings") or []
    if ranks:
        row["sales_rank"] = ranks[0].get("Rank")
        if len(ranks) > 1:
            row["sales_rank_sub"] = ranks[1].get("Rank")
            row["sales_rank_category"] = ranks[1].get("ProductCategoryId")

    slim = []
    for o in offers:
        sid = o.get("SellerId")
        price = amount(o.get("ListingPrice"))
        kind = holder_type(sid)
        slim.append({"seller": sid, "type": kind, "price": price,
                     "buy_box": o.get("IsBuyBoxWinner"), "fba": o.get("IsFulfilledByAmazon")})
        if o.get("IsBuyBoxWinner"):
            row["buy_box_seller"], row["buy_box_holder"] = sid, kind
            row["buy_box_fba"] = o.get("IsFulfilledByAmazon")
        if price is None:
            continue
        if kind == "axon":
            row["axon_price"] = min(price, row["axon_price"] or price)
        elif kind == "vendor_channel":
            row["vendor_channel_price"] = min(price, row["vendor_channel_price"] or price)
        else:
            row["other_seller_count"] += 1
            row["other_seller_min_price"] = min(price, row["other_seller_min_price"] or price)
    row["offers"] = json.dumps(slim)
    return row


COLS = ["asin", "status", "buy_box_price", "buy_box_seller", "buy_box_holder", "buy_box_fba",
        "total_offers", "lowest_price", "axon_price", "vendor_channel_price",
        "other_seller_count", "other_seller_min_price", "sales_rank", "sales_rank_sub",
        "sales_rank_category", "offers", "error"]


def fetch_and_store(conn, api, marketplace_id):
    """api = sync.api (token, retries, throttling). Returns rows written."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT asin FROM product_asins ORDER BY asin")
        asins = [r[0] for r in cur.fetchall()]
    rows = []
    for i in range(0, len(asins), BATCH):
        chunk = asins[i:i + BATCH]
        if i:
            time.sleep(SPACING_S)
        result = api("POST", "/batches/products/pricing/v0/itemOffers", "seller", json={
            "requests": [{"uri": f"/products/pricing/v0/items/{a}/offers", "method": "GET",
                          "MarketplaceId": marketplace_id, "ItemCondition": "New",
                          "CustomerType": "Consumer"} for a in chunk]})
        responses = result.get("responses") or []
        for n, resp in enumerate(responses):
            payload = ((resp.get("body") or {}).get("payload") or {})
            req = resp.get("request") or {}
            # Responses carry the ASIN in different places; fall back to request order.
            asin = (payload.get("ASIN") or (payload.get("Identifier") or {}).get("ASIN")
                    or req.get("Asin") or (req.get("uri") or "").split("/items/")[-1].split("/")[0]
                    or (chunk[n] if n < len(chunk) else None))
            if asin:
                rows.append(parse(asin, resp))
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO buy_box_snapshots ({', '.join(COLS)}) VALUES ({', '.join(['%s'] * len(COLS))})"
            " ON CONFLICT (asin, fetched_at) DO NOTHING",
            [[r[c] for c in COLS] for r in rows])
    return len(rows)
