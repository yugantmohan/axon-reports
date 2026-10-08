"""
One-off check that the Seller Central token can read Buy Box data (Pricing
role). Read-only: one batch call for a handful of our own ASINs. Writes nothing.

    python3 pricing_test.py
    python3 pricing_test.py B0G2SKZG33 B00K5S2RVW     # specific ASINs
"""

import sys

import sync   # reuses the token, retry and throttle handling

DEFAULT = ["B00K5S2RVW",   # 5311 Car Wash Shampoo (vendor)
           "B07QHWYFV1",   # 5319 Washer Concentrate (vendor, #1 seller)
           "B0G2SKZG33",   # 215912 HP inflator - 3 sold in Aug with 143 in stock
           "B00IOQ1LR8",   # 2112206 Foot Pump
           "B0846KZNHY"]   # 5322 Microfibre 4-pack


def money(m):
    return f"Rs {m['Amount']:,.2f}" if m and "Amount" in m else "-"


asins = sys.argv[1:] or DEFAULT
body = {"requests": [{"uri": f"/products/pricing/v0/items/{a}/offers", "method": "GET",
                      "MarketplaceId": sync.MARKETPLACE_ID, "ItemCondition": "New",
                      "CustomerType": "Consumer"} for a in asins[:20]]}
try:
    result = sync.api("POST", "/batches/products/pricing/v0/itemOffers", "seller", json=body)
except RuntimeError as exc:
    if "403" in str(exc) or "Unauthorized" in str(exc):
        sys.exit("ACCESS DENIED - the Seller token does not carry the Pricing role yet.\n"
                 "Re-authorise the Seller account in the Solution Provider Portal and put the "
                 "new refresh token in .env.\n\n" + str(exc))
    raise

for resp in result.get("responses", []):
    asin = resp.get("request", {}).get("uri", "").split("/items/")[-1].split("/")[0]
    status = resp.get("status", {}).get("statusCode")
    payload = (resp.get("body") or {}).get("payload") or {}
    print(f"\n== {asin}  (HTTP {status})")
    if status != 200:
        print("  ", (resp.get("body") or {}).get("errors"))
        continue
    s = payload.get("Summary", {})
    bb = (s.get("BuyBoxPrices") or [{}])[0]
    print(f"  Buy Box price   : {money(bb.get('LandedPrice'))}")
    print(f"  Offers          : {s.get('TotalOfferCount')}")
    print(f"  Lowest price    : {money(((s.get('LowestPrices') or [{}])[0]).get('LandedPrice'))}")
    print(f"  Sales rank      : {[(r.get('ProductCategoryId'), r.get('Rank')) for r in s.get('SalesRankings') or []]}")
    for o in payload.get("Offers", [])[:5]:
        print(f"  offer: seller {o.get('SellerId') or '?':<15} {money(o.get('ListingPrice')):>14}"
              f"  buybox={o.get('IsBuyBoxWinner')}  mine={o.get('MyOffer')}  FBA={o.get('IsFulfilledByAmazon')}")
    if not s.get("BuyBoxPrices"):
        print("  ** NO BUY BOX **")
