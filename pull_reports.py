"""
Pull every report type we care about and dump the raw JSON, so the Postgres
schema can be designed against real payloads rather than guesses.

Run:
    python pull_reports.py 2026-09-01 2026-09-07

Writes raw/<report_name>.json for each, then prints the top-level keys and one
sample row per report so you can see the field names at a glance.

Needs: pip install requests python-dotenv
.env must contain SPAPI_CLIENT_ID, SPAPI_CLIENT_SECRET,
SPAPI_REFRESH_TOKEN_VENDOR, SPAPI_REFRESH_TOKEN_SELLER
"""

import gzip
import io
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"
MARKETPLACE_ID = "A21TJRUUN4KGV"

CLIENT_ID = os.environ["SPAPI_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPAPI_CLIENT_SECRET"]

TOKENS = {
    "vendor": os.environ["SPAPI_REFRESH_TOKEN_VENDOR"],
    "seller": os.environ["SPAPI_REFRESH_TOKEN_SELLER"],
}

# reportOptions differ per report type. Sales and inventory require all three;
# traffic requires reportPeriod ONLY - sending the other two can FATAL it.
REPORTS = [
    {
        "name": "vendor_sales",
        "account": "vendor",
        "reportType": "GET_VENDOR_SALES_REPORT",
        "options": {
            "reportPeriod": "DAY",
            "distributorView": "SOURCING",
            "sellingProgram": "RETAIL",
        },
    },
    {
        "name": "vendor_inventory",
        "account": "vendor",
        "reportType": "GET_VENDOR_INVENTORY_REPORT",
        "options": {
            "reportPeriod": "DAY",
            "distributorView": "SOURCING",
            "sellingProgram": "RETAIL",
        },
    },
    {
        "name": "vendor_traffic",
        "account": "vendor",
        "reportType": "GET_VENDOR_TRAFFIC_REPORT",
        "options": {
            "reportPeriod": "DAY",
        },
    },
    {
        "name": "seller_sales_traffic",
        "account": "seller",
        "reportType": "GET_SALES_AND_TRAFFIC_REPORT",
        "options": {
            "dateGranularity": "DAY",
            "asinGranularity": "CHILD",
        },
    },
]


def get_access_token(account):
    r = requests.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": TOKENS[account],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def headers(token):
    return {"x-amz-access-token": token, "content-type": "application/json"}


def create_report(token, report_type, options, start, end):
    body = {
        "reportType": report_type,
        "marketplaceIds": [MARKETPLACE_ID],
        "dataStartTime": f"{start}T00:00:00Z",
        "dataEndTime": f"{end}T23:59:59Z",
        "reportOptions": options,
    }
    r = requests.post(f"{ENDPOINT}/reports/2021-06-30/reports",
                      headers=headers(token), json=body, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"create failed {r.status_code}: {r.text[:400]}")
    return r.json()["reportId"]


def wait_for_report(token, report_id, timeout_s=900):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(f"{ENDPOINT}/reports/2021-06-30/reports/{report_id}",
                         headers=headers(token), timeout=30)
        r.raise_for_status()
        info = r.json()
        status = info["processingStatus"]
        if status == "DONE":
            return info["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            # FATAL: fetch the document anyway - it carries Amazon's actual reason.
            doc_id = info.get("reportDocumentId")
            reason = fetch_document(token, doc_id) if doc_id else info
            raise RuntimeError(f"{status}: {json.dumps(reason)[:600]}")
        print(f"    {status}")
        time.sleep(30)
    raise TimeoutError("report did not finish in time")


def fetch_document(token, document_id):
    r = requests.get(f"{ENDPOINT}/reports/2021-06-30/documents/{document_id}",
                     headers=headers(token), timeout=30)
    r.raise_for_status()
    doc = r.json()

    payload = requests.get(doc["url"], timeout=180).content   # pre-signed: no auth header
    if doc.get("compressionAlgorithm") == "GZIP":
        payload = gzip.GzipFile(fileobj=io.BytesIO(payload)).read()
    return json.loads(payload.decode("utf-8"))


def describe(name, data):
    """Print the shape so we can design tables from it."""
    print(f"\n=== {name} ===")
    print("top-level keys:", list(data.keys()))
    for key, value in data.items():
        if isinstance(value, list) and value:
            print(f"\n  {key}: {len(value)} rows. First row:")
            print("  " + json.dumps(value[0], indent=2).replace("\n", "\n  "))
        elif isinstance(value, list):
            print(f"\n  {key}: empty")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python pull_reports.py <start YYYY-MM-DD> <end YYYY-MM-DD>")
        sys.exit(1)

    start, end = sys.argv[1], sys.argv[2]
    os.makedirs("raw", exist_ok=True)

    tokens = {}
    results = {}

    for spec in REPORTS:
        name = spec["name"]
        print(f"\n[{name}] {spec['reportType']}")
        try:
            account = spec["account"]
            if account not in tokens:
                tokens[account] = get_access_token(account)
                print(f"  {account} token ok")

            token = tokens[account]
            report_id = create_report(token, spec["reportType"], spec["options"], start, end)
            print(f"  reportId {report_id}")

            document_id = wait_for_report(token, report_id)
            data = fetch_document(token, document_id)

            path = f"raw/{name}.json"
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"  saved {path}")
            results[name] = data

        except Exception as exc:
            # Keep going: one failing report should not block the others.
            print(f"  FAILED: {exc}")

    for name, data in results.items():
        describe(name, data)
