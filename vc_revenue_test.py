"""
One-off test: does Amazon's MANUFACTURING view of the Vendor sales report carry
revenue? (Our nightly sync uses the SOURCING view, where amazon.in returns
revenue = 0.) Read-only, writes nothing.

Compares 1-3 July 2026 with the MIS "VC Sale" column:
    1 Jul 621,184   2 Jul 639,822   3 Jul 479,187

    python3 vc_revenue_test.py
"""

import json

import sync

MIS = {"2026-07-01": 621184, "2026-07-02": 639822, "2026-07-03": 479187}

for view in ("MANUFACTURING", "SOURCING"):
    print(f"\n== distributorView {view}")
    try:
        _, data = sync.run_report(
            "vendor", "GET_VENDOR_SALES_REPORT",
            {"reportPeriod": "DAY", "distributorView": view, "sellingProgram": "RETAIL"},
            sync.dt.date(2026, 7, 1), sync.dt.date(2026, 7, 3))
    except Exception as exc:
        print("  failed:", str(exc)[:300])
        continue
    for day in data.get("salesAggregate", []):
        d = day.get("startDate")
        fields = {k: (v.get("amount") if isinstance(v, dict) else v)
                  for k, v in day.items() if k not in ("startDate", "endDate")}
        print(f"  {d}  MIS VC Sale {MIS.get(d, 0):>9,}  |  {json.dumps(fields)}")
