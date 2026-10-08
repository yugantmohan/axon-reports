"""
Nightly Amazon sync: pull reports from SP-API and load them straight into
Postgres. Reuses the loaders in load_local.py, so data goes through exactly
the path you already verified against Vendor Central.

Usage (from the sp_api folder):
    python3 sync.py                          # rolling window: last 10 available days
    python3 sync.py --days 30                # wider rolling window
    python3 sync.py --start 2026-01-01 --end 2026-09-20     # backfill a range
    python3 sync.py --only woo               # WooCommerce portal orders + Buy Box prices
    python3 sync.py --only pricing           # Buy Box prices only

WooCommerce runs after the Amazon reports on every plain run. It always
re-pulls the full order history, so --days/--start do not affect it.

Why a rolling window instead of "just yesterday": Amazon restates recent
figures, and a missed night (laptop asleep) would otherwise leave a permanent
hole. Every run re-pulls the whole window and upserts, so both fix themselves.

Data availability lag:
    vendor traffic:           ~4 days
    vendor sales / inventory: ~6 days (Sep 2026), longer than Amazon documents
    seller sales & traffic:   ~1 day
Requesting a day Amazon has not published yet makes the whole report FATAL.
Rather than hard-code the lag, the vendor loop steps the end date back one day
and retries until Amazon accepts it. Those attempts are logged as
'unavailable', not 'failed'.

Every report attempt is recorded in sync_runs, and every payload is kept in
raw_report_documents so it can be reprocessed without calling Amazon again.
"""

import argparse
import datetime as dt
import gzip
import io
import json
import os
import sys
import time

import psycopg
import requests
from dotenv import load_dotenv

import load_local
import woo_sync
import pricing_sync

load_dotenv()

ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"
MARKETPLACE_ID = "A21TJRUUN4KGV"
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///axon_amazon")

VENDOR_LAG_DAYS = 3        # first guess; trimmed further if Amazon says not yet available
MAX_LAG_RETRIES = 7
SELLER_LAG_DAYS = 1
VENDOR_MAX_SPAN = 14      # Amazon allows at most 15 days per DAY-period vendor report
SELLER_SPACING_S = 100    # seller report: ~3 requests per 5 minutes
REPORT_TIMEOUT_S = 60 * 60  # Amazon sometimes takes >20 min to build a vendor report (30 Sep)

VENDOR_REPORTS = [
    # (name, reportType, reportOptions, loader)
    ("vendor_sales", "GET_VENDOR_SALES_REPORT",
     {"reportPeriod": "DAY", "distributorView": "SOURCING", "sellingProgram": "RETAIL"},
     load_local.load_vendor_sales),
    ("vendor_inventory", "GET_VENDOR_INVENTORY_REPORT",
     {"reportPeriod": "DAY", "distributorView": "SOURCING", "sellingProgram": "RETAIL"},
     load_local.load_vendor_inventory),
    # Traffic takes reportPeriod only - sending the other two options FATALs it.
    ("vendor_traffic", "GET_VENDOR_TRAFFIC_REPORT",
     {"reportPeriod": "DAY"},
     load_local.load_vendor_traffic),
]


# ---------------------------------------------------------------------------
# SP-API
# ---------------------------------------------------------------------------

class NotYetAvailable(Exception):
    """Amazon has not published data for the end of the requested range."""

_tokens = {}


NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                  requests.exceptions.ChunkedEncodingError)
NETWORK_WAITS = [15, 30, 60, 120, 240]   # ~8 min in total: rides out Wi-Fi drops and wake-from-sleep


def with_network_retry(label, fn):
    """Run fn(); on a dropped connection or DNS failure, wait and try again.
    Oct 2026: runs failed on 'Connection reset', 'RemoteDisconnected' and DNS
    errors when the Mac's network dropped mid-run (sleep, Wi-Fi change)."""
    for wait in NETWORK_WAITS + [None]:
        try:
            return fn()
        except NETWORK_ERRORS as exc:
            if wait is None:
                raise
            print(f"    network error on {label} ({type(exc).__name__}), retrying in {wait}s")
            time.sleep(wait)


def access_token(account):
    """Access tokens last an hour; a long backfill refreshes as it goes."""
    cached = _tokens.get(account)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    r = with_network_retry("token", lambda: requests.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": os.environ[f"SPAPI_REFRESH_TOKEN_{account.upper()}"],
        "client_id": os.environ["SPAPI_CLIENT_ID"],
        "client_secret": os.environ["SPAPI_CLIENT_SECRET"],
    }, timeout=30))
    r.raise_for_status()
    body = r.json()
    _tokens[account] = (body["access_token"], time.time() + body.get("expires_in", 3600))
    return body["access_token"]


def api(method, path, account, **kwargs):
    """Call SP-API, backing off on 429 (throttled) and on dropped connections."""
    for attempt in range(6):
        r = with_network_retry(path, lambda: requests.request(
            method, f"{ENDPOINT}{path}",
            headers={"x-amz-access-token": access_token(account),
                     "content-type": "application/json"},
            timeout=60, **kwargs,
        ))
        if r.status_code != 429:
            if r.status_code >= 300:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
            return r.json()
        wait = 30 * (attempt + 1)
        print(f"    throttled, waiting {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"{method} {path}: still throttled after retries")


def fetch_document(account, document_id):
    """Download a report document. If the download comes back as something
    other than the report (Oct 2026: an XML error page instead of gzip, i.e. an
    expired or failed pre-signed URL), ask Amazon for a fresh URL and retry."""
    for attempt in range(3):
        doc = api("GET", f"/reports/2021-06-30/documents/{document_id}", account)
        payload = with_network_retry("document download",
                                     lambda: requests.get(doc["url"], timeout=180).content)
        try:
            if doc.get("compressionAlgorithm") == "GZIP":
                payload = gzip.GzipFile(fileobj=io.BytesIO(payload)).read()
            return json.loads(payload.decode("utf-8"))
        except (OSError, ValueError) as exc:     # bad gzip / not JSON
            if attempt == 2:
                raise RuntimeError(f"document {document_id} unreadable after 3 tries: "
                                   f"{exc}; starts {payload[:80]!r}")
            print(f"    document download was not the report ({exc}), fetching a fresh URL")
            time.sleep(10)


def run_report(account, report_type, options, start, end):
    """Create, wait for, and download one report. Returns (report_id, data)."""
    created = api("POST", "/reports/2021-06-30/reports", account, json={
        "reportType": report_type,
        "marketplaceIds": [MARKETPLACE_ID],
        "dataStartTime": f"{start.isoformat()}T00:00:00Z",
        "dataEndTime": f"{end.isoformat()}T23:59:59Z",
        "reportOptions": options,
    })
    report_id = created["reportId"]

    deadline = time.time() + REPORT_TIMEOUT_S
    while time.time() < deadline:
        info = api("GET", f"/reports/2021-06-30/reports/{report_id}", account)
        status = info["processingStatus"]
        if status == "DONE":
            return report_id, fetch_document(account, info["reportDocumentId"])
        if status in ("FATAL", "CANCELLED"):
            # The FATAL document usually carries Amazon's actual reason.
            reason = info
            if info.get("reportDocumentId"):
                try:
                    reason = fetch_document(account, info["reportDocumentId"])
                except Exception:
                    pass
            text = json.dumps(reason)
            if "not yet available" in text:
                raise NotYetAvailable(text[:300])
            raise RuntimeError(f"report {report_id} {status}: {text[:500]}")
        time.sleep(20)
    raise TimeoutError(f"report {report_id} not done after {REPORT_TIMEOUT_S // 60} minutes")


# ---------------------------------------------------------------------------
# bookkeeping
# ---------------------------------------------------------------------------

def start_run(conn, name, account, start, end):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sync_runs (report_type, account, period_start, period_end, status)
               VALUES (%s, %s, %s, %s, 'running') RETURNING id""",
            (name, account, start, end),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn, run_id, status, rows=None, report_id=None, error=None):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE sync_runs SET status = %s, rows_written = %s, amazon_report_id = %s,
                      error_message = %s, finished_at = now() WHERE id = %s""",
            (status, rows, report_id, error, run_id),
        )
    conn.commit()


def sync_one(conn, name, account, report_type, options, loader, start, end):
    """One report for one period, in its own transaction: a failure here does
    not undo reports that already loaded."""
    print(f"  {name} {start} to {end}")
    run_id = start_run(conn, name, account, start, end)
    try:
        report_id, data = run_report(account, report_type, options, start, end)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO raw_report_documents
                       (report_type, account, period_start, period_end, amazon_report_id, payload)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (name, account, start, end, report_id, json.dumps(data)),
            )
            rows = loader(cur, data)
        conn.commit()
        finish_run(conn, run_id, "success", rows, report_id)
        return "ok"
    except NotYetAvailable as exc:
        conn.rollback()
        finish_run(conn, run_id, "unavailable", error=str(exc)[:2000])
        return "unavailable"
    except Exception as exc:
        conn.rollback()
        print(f"    FAILED: {exc}")
        finish_run(conn, run_id, "failed", error=str(exc)[:2000])
        return "failed"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def chunks(start, end, span_days):
    """Split [start, end] into pieces of at most span_days days."""
    cur = start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=span_days - 1), end)
        yield cur, stop
        cur = stop + dt.timedelta(days=1)


def sync_buy_box(conn):
    """One Buy Box pull for all mapped ASINs, logged like any report."""
    print("\nBuy Box: all mapped ASINs")
    run_id = start_run(conn, "buy_box", "seller", None, None)
    try:
        rows = pricing_sync.fetch_and_store(conn, api, MARKETPLACE_ID)
        conn.commit()
        finish_run(conn, run_id, "success", rows)
        print(f"  {rows} ASINs priced")
        return "ok"
    except Exception as exc:
        conn.rollback()
        print(f"    FAILED: {exc}")
        finish_run(conn, run_id, "failed", error=str(exc)[:2000])
        return "failed"


def parse_args():
    p = argparse.ArgumentParser(description="Sync Amazon reports into Postgres")
    p.add_argument("--days", type=int, default=10, help="rolling window length (default 10)")
    p.add_argument("--start", type=dt.date.fromisoformat, help="backfill start date YYYY-MM-DD")
    p.add_argument("--end", type=dt.date.fromisoformat, help="backfill end date YYYY-MM-DD")
    p.add_argument("--only", choices=["vendor", "seller", "woo", "pricing"], help="sync one account only")
    return p.parse_args()


def keep_awake():
    """Windows: don't idle-sleep while this run lasts (the Mac uses caffeinate
    in its schedule instead). Released automatically when the run ends."""
    if sys.platform == "win32":
        try:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            pass


def main():
    args = parse_args()
    keep_awake()
    today = dt.date.today()

    vendor_end = today - dt.timedelta(days=VENDOR_LAG_DAYS)
    seller_end = today - dt.timedelta(days=SELLER_LAG_DAYS)
    if args.start:
        # Backfill: never ask for days Amazon has not published yet.
        vendor_start = seller_start = args.start
        vendor_end = min(args.end or vendor_end, vendor_end)
        seller_end = min(args.end or seller_end, seller_end)
    else:
        vendor_start = vendor_end - dt.timedelta(days=args.days - 1)
        seller_start = seller_end - dt.timedelta(days=args.days - 1)

    conn = psycopg.connect(DATABASE_URL)
    failures = 0

    if args.only in (None, "vendor"):
        print(f"Vendor: {vendor_start} to {vendor_end}")
        for piece_start, piece_end in chunks(vendor_start, vendor_end, VENDOR_MAX_SPAN):
            for name, report_type, options, loader in VENDOR_REPORTS:
                # Publication lag differs by report (sales ~6 days, traffic ~4)
                # and drifts, so find the latest available day instead of
                # hard-coding it.
                #   rolling mode: slide the whole window back (keep N days)
                #   backfill mode: keep the start you asked for, trim the end
                start, end = piece_start, piece_end
                for _ in range(MAX_LAG_RETRIES + 1):
                    result = sync_one(conn, name, "vendor", report_type, options, loader,
                                      start, end)
                    if result != "unavailable":
                        break
                    end -= dt.timedelta(days=1)
                    if not args.start:
                        start -= dt.timedelta(days=1)
                    if end < start:
                        print("    nothing in this range is published yet - skipped")
                        break
                    print(f"    not published yet, retrying {start} to {end}")
                failures += result == "failed"

    # WooCommerce before Seller: it takes seconds, and Seller's throttled
    # loop is the part most likely to be cut short (laptop closed mid-run).
    # Skipped during an Amazon backfill (--start); it has no date range.
    if args.only == "woo" or (args.only is None and not args.start):
        failures += woo_sync.sync(conn) == "failed"

    # Buy Box snapshot rides along with every Woo refresh (10/13/16/19h).
    if args.only in ("woo", "pricing") or (args.only is None and not args.start):
        failures += sync_buy_box(conn) == "failed"

    if args.only in (None, "seller"):
        # One report per day: the seller ASIN section has no date of its own.
        print(f"\nSeller: {seller_start} to {seller_end} (one report per day)")
        # Amazon allows this report about 3 times per 5 minutes, so space
        # them out. A 10-day nightly run takes ~17 min; a year's backfill ~10 h.
        day = seller_start
        last_request = 0.0
        while day <= seller_end:
            wait = SELLER_SPACING_S - (time.time() - last_request)
            if wait > 0:
                time.sleep(wait)
            last_request = time.time()
            ok = sync_one(conn, "seller_sales_traffic", "seller", "GET_SALES_AND_TRAFFIC_REPORT",
                          {"dateGranularity": "DAY", "asinGranularity": "CHILD"},
                          load_local.load_seller, day, day)
            failures += ok == "failed"   # a not-yet-published day is not a failure
            day += dt.timedelta(days=1)

    with conn.cursor() as cur:
        print()
        if args.only not in ("woo", "pricing"):
            load_local.run_checks(cur)
        if args.only in (None, "woo"):
            print()
            woo_sync.run_checks(cur)
    conn.close()

    print(f"\nDone with {failures} failed report(s). History: SELECT * FROM sync_runs ORDER BY id DESC;")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
