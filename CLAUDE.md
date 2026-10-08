# Axon reporting (axon-reports)

Local reporting system for Axon Lifestyle (Michelin licensee, India). Amazon
SP-API data, WooCommerce B2B portal orders (offline distributor sales) and
Amazon Buy Box snapshots are pulled into a local PostgreSQL database
(`axon_amazon`). Claude Desktop answers questions through a read-only MCP
server (`mcp_server.py`, connector name `axon-reports`). Owner: Yugant.

Read this file before changing anything. Code changes are made in one place
(a Claude Code session on this machine), never from Claude Desktop - two
sessions editing the same files overwrite each other.

## Hard rules (compliance - do not relax)

- **Never print, log, commit or paste credentials.** They live only in `.env`
  (git-ignored). `.env.example` lists the names with no values.
- **The database user for Claude Desktop is read-only** (`mcp_reader`, see
  `readonly_user.sql`: SELECT only, read-only sessions, 30 s statement
  timeout). MCP tools run fixed SQL; Claude never writes SQL through them.
- **SP-API Pricing role:** only our own ASINs (`product_asins`), read-only,
  no repricing, never competitor ASINs. Competitor prices are out of scope
  here (they belong to the separately declared crawler project).
- **Ads API: reporting only.** No code that changes bids, budgets or
  campaigns, ever. Only the profiles and reporting endpoints.
- **WooCommerce key is Read-only.** Never call write endpoints.
- **Flipkart self-access credentials** are for Axon's own use only - never
  share with third parties. Code reads listings/orders; never calls the
  price or inventory update endpoints.
- **No scraping** of amazon.in or flipkart.com from this project.
- Amazon data stays on this machine: never in git, email or cloud drives.
  `raw/`, `logs/`, dumps and Excel files are git-ignored.

## Files

| File | What it does |
|---|---|
| `sync.py` | Scheduled sync: Vendor (sales, inventory, traffic) -> WooCommerce -> Buy Box -> Seller. `--only vendor/seller/woo/pricing`, `--start/--end` for backfills, rolling 10-day window otherwise (Amazon restates recent days). Retries network errors; logs every attempt in `sync_runs`, keeps payloads in `raw_report_documents`. |
| `load_local.py` | Loaders shared by sync + full reload of the CSV masters (`product_map.csv`, `bundle_components.csv`, `distributor_asm.csv`, `asin_prices.csv`). |
| `woo_sync.py` | Full WooCommerce order history each run (delete + insert in one transaction; refuses to replace data with an empty pull). Counts `processing` + `completed` only. |
| `pricing_sync.py` | Buy Box snapshots via Product Pricing `getItemOffersBatch` (20 ASINs/call, ~11 s apart). |
| `mcp_server.py` | The Desktop connector: 11 read-only tools. `python mcp_server.py --check` runs every tool. |
| `schema.sql` | All tables and views. Re-runnable (`psql -f schema.sql`). |
| `readonly_user.sql` | Creates/updates `mcp_reader`. |
| `import_prices.py` | Michelin Amazon mastersheet -> `asin_prices.csv` (CP / SP / MRP per ASIN). |
| `lookup_asins.py` | Catalog titles for unmapped ASINs (`--compare` for look-alike listings). |
| `flipkart_test.py`, `flipkart_fsn_map.csv` | Flipkart Seller API check (waiting for app approval) and the FSN -> ASIN -> SKU priority map (42 FSNs). |
| `vc_revenue_test.py`, `pricing_test.py`, `woo_test.py` | One-off API checks. |
| `com.axon.*.plist`, `install_schedule.sh` | Mac schedules (launchd). |
| `windows/install_schedule.ps1` | Windows schedules (Task Scheduler) + prints the Desktop config entry. |
| `skills/` | Copies of the Claude skills that sit on top of the tools. |

MCP tools: `data_freshness`, `sales_summary`, `top_products`, `product_detail`,
`distributor_sales`, `distributor_product_sales`, `amazon_inventory`,
`stock_health`, `sales_value`, `buy_box`, `amazon_mis`.

## Schedules

- 10:00 daily: full sync (`sync.py`).
- 13:00, 16:00, 19:00: `sync.py --only woo` (WooCommerce + Buy Box).
- Missed runs (machine asleep/off) run when it is back.

## Data facts

- Vendor Central started ~Jan 2026 (daily vendor data from 31 Dec 2025 is real,
  not a retention limit). Nov-Dec 2025 was Seller-only.
- Lags: vendor sales/inventory ~3-6 days, vendor traffic ~4, seller ~1.
  Missing recent days are pending, never zero - every tool returns `coverage`.
- amazon.in vendor reports return revenue = 0 in both SOURCING and
  MANUFACTURING views (tested Oct 2026); MANUFACTURING has orderedRevenue but
  covers only ~82% of units. So rupee values are computed (below).
- Offline (WooCommerce portal) data starts 13 Jul 2026.
- Seller IDs: `A1QTZ8OBV6L6UD` = Axon (Seller Central, FBM);
  `A2AL6IVND0I91F` = Etrade, the entity Axon bills through Vendor Central
  (holder `vendor_channel`). Anyone else holding the Buy Box = outside seller.
- Flipkart Buy Box holders seen: SURICYBCOMBazaar, BTPLD, MYTHANGLORYRetail
  (the Seller API cannot report Buy Box holders).

## Value bases (always label which one)

- Vendor sell-through = units x ETrade CP ex GST ("ETrade CP W/o GST" in the
  mastersheet; today's CP applied to all history). Default.
- Vendor sell-in = net received units at Amazon's cost ex GST. Compare
  invoiced actuals with sell-in (gap = stock in transit).
- Seller = Amazon shipped product sales incl GST (`sales_value`); the MIS uses
  ordered product sales incl GST.
- Portal = billed ex GST.
- MIS "VC Sale" = vendor shipped units x customer price incl GST (that day's
  Buy Box price where pulled, else sheet selling price). Within ~1% of the
  Excel MIS by month. It is NOT Axon's revenue.
- ASINs with no CP are reported as unpriced units, never estimated.
- Ad spend / TACOS: blank until the Ads API is approved (rows with source
  `ads_api` only; the old MIS-workbook import is ignored).

## Products

- `product_map.csv` maps every ASIN to an internal SKU (= WooCommerce SKU).
- Wiper combo ASINs split into their sizes via `bundle_components.csv`.
- Car-care combos (5473, 5463, 5433-5436-COMBO, INFLATOR-GAUGE-COMBO) are
  their own SKUs - not split.

## Conventions

- `schema.sql` must stay re-runnable. `CREATE OR REPLACE VIEW` can only add
  columns at the end; to reorder, `DROP VIEW IF EXISTS` + `CREATE VIEW` (and
  re-grant to `mcp_reader`).
- CSV loaders do a full replace and skip when the file is missing.
- The sync must never fail because of an optional step (wrap it, log, retry
  next run).
- Code must run on Mac and Windows: `encoding="utf-8"` on every file open,
  paths relative to the project folder, no shell-specific commands.
- After any change: `psql -f schema.sql` (if schema changed), then
  `python mcp_server.py --check` must end with "All tools ran."

## Open items (Oct 2026)

- Ads API approval pending -> ads_sync (reporting only), ad_performance tool,
  MIS ad spend. SP retention 95 days, SB/SD 60: pull on approval day.
- Flipkart self-access app pending approval -> run `flipkart_test.py`, then
  build the Flipkart listings pull (price/stock/status), Flipkart vs Amazon
  parity, and Flipkart sales per FSN. Target price = Amazon sheet price;
  floor = ETrade CP.
- Product map questions: B077G5CM66 (model 12290, likely 2212290 gauge),
  B09P8LV5JP (model 12314, likely 2112314). Rat repellent 5390/5392 and 11
  live ASINs have no CP / sheet price.
