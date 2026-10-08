"""
Look up Amazon titles for ASINs, to map them to SKUs.

    python3 lookup_asins.py                      # every unmapped ASIN with sales
    python3 lookup_asins.py B0GCMRL2KV B07JQW6HBL  # specific ASINs
    python3 lookup_asins.py --compare              # unmapped + the mapped inflators/gauges
                                                   #   they might duplicate, to compare models

Read-only: Catalog Items API + a SELECT on v_unmapped_asins.
"""

import os
import sys
import time

import psycopg
import requests
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"
MARKETPLACE_ID = "A21TJRUUN4KGV"
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///axon_amazon")


def token():
    r = requests.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": os.environ["SPAPI_REFRESH_TOKEN_VENDOR"],
        "client_id": os.environ["SPAPI_CLIENT_ID"],
        "client_secret": os.environ["SPAPI_CLIENT_SECRET"],
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def title(tok, asin):
    for _ in range(3):
        r = requests.get(f"{ENDPOINT}/catalog/2022-04-01/items/{asin}",
                         headers={"x-amz-access-token": tok},
                         params={"marketplaceIds": MARKETPLACE_ID, "includedData": "summaries"},
                         timeout=30)
        if r.status_code == 429:
            time.sleep(5)
            continue
        if r.status_code != 200:
            return f"(lookup failed: {r.status_code})"
        s = (r.json().get("summaries") or [{}])[0]
        extra = " | ".join(f"{k}: {s[k]}" for k in
                           ("modelNumber", "partNumber", "packageQuantity", "size", "manufacturer")
                           if s.get(k))
        return (s.get("itemName") or "(no title)") + (f"\n{'':19}{extra}" if extra else "")
    return "(throttled)"


if __name__ == "__main__":
    # Mapped ASINs the unmapped ones might be second listings of.
    COMPARE = ["B0G53FL3ZS", "B0G2SKZG33", "B07THV6XJ6", "B07T46ZHL7", "B07T6FLRQM",
               "B00KMZZJWC", "B074MX2168", "B005PW0OUE", "B004YF1Z4M", "B07QHWLQ7L"]
    args = [a for a in sys.argv[1:] if a != "--compare"]
    if args:
        asins = [(a, None) for a in args]
    else:
        with psycopg.connect(DATABASE_URL) as conn:
            asins = conn.execute(
                "SELECT asin, units FROM v_unmapped_asins WHERE units > 0 ORDER BY units DESC"
            ).fetchall()
    if "--compare" in sys.argv:
        asins += [(a, "mapped") for a in COMPARE]
    tok = token()
    for asin, units in asins:
        print(f"{asin}  {units if units is not None else '':>5}  {title(tok, asin)}")
        time.sleep(0.6)
