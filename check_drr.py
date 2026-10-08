"""
First real SP-API call: pull a vendor sales report for amazon.in
and print per-ASIN shipped units so you can diff against the DRR sheet.

Run:  python vendor_sales_test.py
Needs: pip install requests python-dotenv
"""

import gzip
import io
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

# --- config -----------------------------------------------------------------

CLIENT_ID = os.environ["SPAPI_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPAPI_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPAPI_REFRESH_TOKEN_VENDOR"]

ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"   # India is served from EU
MARKETPLACE_ID = "A21TJRUUN4KGV"                        # amazon.in

# Pick a range you already know from the DRR sheet.
# reportPeriod DAY  -> boundaries must be whole days
# reportPeriod WEEK -> must run Sunday 00:00 to Saturday 23:59
START = "2026-09-01T00:00:00Z"
END   = "2026-09-07T23:59:59Z"

REPORT_OPTIONS = {
    "reportPeriod": "DAY",
    "distributorView": "SOURCING",   # try MANUFACTURING if numbers look wrong
    "sellingProgram": "RETAIL",
}

# --- auth -------------------------------------------------------------------

def get_access_token():
    r = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def headers(token):
    return {"x-amz-access-token": token, "content-type": "application/json"}

# --- report lifecycle: create -> poll -> download ---------------------------

def create_report(token):
    body = {
        "reportType": "GET_VENDOR_SALES_REPORT",
        "marketplaceIds": [MARKETPLACE_ID],
        "dataStartTime": START,
        "dataEndTime": END,
        "reportOptions": REPORT_OPTIONS,
    }
    r = requests.post(
        f"{ENDPOINT}/reports/2021-06-30/reports",
        headers=headers(token),
        json=body,
        timeout=30,
    )
    print("create:", r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()["reportId"]


def wait_for_report(token, report_id, timeout_s=900):
    """Poll until DONE. Reports usually take 1-10 minutes."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(
            f"{ENDPOINT}/reports/2021-06-30/reports/{report_id}",
            headers=headers(token),
            timeout=30,
        )
        r.raise_for_status()
        info = r.json()
        status = info["processingStatus"]
        print("  status:", status)

        if status == "DONE":
            return info["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            # FATAL almost always means reportOptions were wrong for this account
            raise RuntimeError(f"report ended as {status}: {json.dumps(info)}")

        time.sleep(30)
    raise TimeoutError("report did not finish in time")


def download_report(token, document_id):
    r = requests.get(
        f"{ENDPOINT}/reports/2021-06-30/documents/{document_id}",
        headers=headers(token),
        timeout=30,
    )
    r.raise_for_status()
    doc = r.json()

    payload = requests.get(doc["url"], timeout=120).content
    if doc.get("compressionAlgorithm") == "GZIP":
        payload = gzip.GzipFile(fileobj=io.BytesIO(payload)).read()
    return json.loads(payload.decode("utf-8"))

# --- main -------------------------------------------------------------------

def main():
    token = get_access_token()
    print("got access token")

    report_id = create_report(token)
    print("reportId:", report_id)

    document_id = wait_for_report(token, report_id)
    data = download_report(token, document_id)

    # Keep the raw file — you will want it when the shape surprises you.
    with open("vendor_sales_raw.json", "w") as f:
        json.dump(data, f, indent=2)
    print("\nraw saved to vendor_sales_raw.json")

    rows = data.get("salesByAsin", [])
    if not rows:
        print("no salesByAsin rows - inspect the raw file for the real shape")
        print(json.dumps(data, indent=2)[:2000])
        return

    # Sum shipped units per ASIN across the period
    totals = {}
    for row in rows:
        asin = row.get("asin")
        units = row.get("shippedUnits", 0)
        totals[asin] = totals.get(asin, 0) + units

    print(f"\n{'ASIN':<15} {'shipped units':>14}")
    print("-" * 30)
    for asin, units in sorted(totals.items(), key=lambda x: -x[1]):
        print(f"{asin:<15} {units:>14}")
    print("-" * 30)
    print(f"{'TOTAL':<15} {sum(totals.values()):>14}")


if __name__ == "__main__":
    main()