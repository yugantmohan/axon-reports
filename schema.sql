-- ============================================================================
-- Axon reporting database - schema v2
--
-- Changes from v1:
--   * products are keyed on your internal SKU (same code as WooCommerce),
--     not on ASIN. Amazon ASINs map to a SKU through product_asins.
--   * wiper combos: bundle_components splits a combo SKU into its sizes, so
--     one combo pack counts as one unit of each size.
--   * offline_order_lines added (WooCommerce now, FieldAssist later) with
--     source_system and ingest_method on every row.
--
-- Fact tables deliberately have NO foreign key to products. An ASIN that is
-- not in the product map must still load - otherwise the nightly sync fails
-- the day Amazon shows a new listing. Unmapped ASINs show up in
-- v_unmapped_asins instead.
--
-- Safe to re-run: every statement is CREATE ... IF NOT EXISTS / OR REPLACE.
-- Run:  psql axon_amazon -f schema.sql
-- ============================================================================


-- ---------------------------------------------------------------------------
-- PRODUCT DIMENSION
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS products (
    internal_sku        text PRIMARY KEY,           -- same code as WooCommerce SKU
    product_name        text NOT NULL,
    category            text,                       -- Wipers, Car Care, Microfibre, Aerosols, ...
    report_group        text,                       -- Wipers | Car Care (matches monthly actuals)
    pack_size           text,
    is_bundle           boolean NOT NULL DEFAULT false,
    mrp                 numeric(12,2),
    selling_price       numeric(12,2),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- One ASIN belongs to exactly one SKU (the PRIMARY KEY enforces it). A combo
-- ASIN points at the combo SKU; bundle_components does the splitting.
CREATE TABLE IF NOT EXISTS product_asins (
    asin                text PRIMARY KEY,
    internal_sku        text NOT NULL REFERENCES products(internal_sku),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_product_asins_sku ON product_asins (internal_sku);

CREATE TABLE IF NOT EXISTS bundle_components (
    bundle_sku          text NOT NULL REFERENCES products(internal_sku),
    component_sku       text NOT NULL REFERENCES products(internal_sku),
    qty                 integer NOT NULL DEFAULT 1 CHECK (qty > 0),
    PRIMARY KEY (bundle_sku, component_sku)
);

-- Dated unit cost. Kept out of the fact tables so a cost change never rewrites
-- history. Filled in later once the revenue formula is settled.
CREATE TABLE IF NOT EXISTS product_costs (
    internal_sku        text NOT NULL REFERENCES products(internal_sku),
    valid_from          date NOT NULL,
    valid_to            date,                       -- NULL = current
    cost_per_unit       numeric(12,2) NOT NULL,
    source              text,
    PRIMARY KEY (internal_sku, valid_from)
);


-- ---------------------------------------------------------------------------
-- VENDOR CENTRAL  (daily grain)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vendor_sales_daily (
    asin                text NOT NULL,
    report_date         date NOT NULL,
    distributor_view    text NOT NULL DEFAULT 'SOURCING',
    selling_program     text NOT NULL DEFAULT 'RETAIL',
    shipped_units       integer,
    shipped_revenue     numeric(14,2),              -- 0.00 on amazon.in today
    shipped_cogs        numeric(14,2),              -- 0.00 on amazon.in today
    customer_returns    integer,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asin, report_date, distributor_view, selling_program)
);
CREATE INDEX IF NOT EXISTS idx_vsd_date ON vendor_sales_daily (report_date);

-- Stored as Amazon reports it, not summed from ASIN rows, so drift between the
-- two is detectable.
CREATE TABLE IF NOT EXISTS vendor_sales_aggregate_daily (
    report_date         date NOT NULL,
    distributor_view    text NOT NULL DEFAULT 'SOURCING',
    selling_program     text NOT NULL DEFAULT 'RETAIL',
    shipped_units       integer,
    shipped_revenue     numeric(14,2),
    shipped_cogs        numeric(14,2),
    customer_returns    integer,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (report_date, distributor_view, selling_program)
);

CREATE TABLE IF NOT EXISTS vendor_inventory_daily (
    asin                            text NOT NULL,
    report_date                     date NOT NULL,
    distributor_view                text NOT NULL DEFAULT 'SOURCING',
    selling_program                 text NOT NULL DEFAULT 'RETAIL',
    -- populated on amazon.in:
    open_purchase_order_units       integer,
    net_received_inventory_units    integer,
    net_received_inventory_cost     numeric(14,2),
    sellable_on_hand_units          integer,
    sellable_on_hand_cost           numeric(14,2),
    unsellable_on_hand_units        integer,
    unsellable_on_hand_cost         numeric(14,2),
    aged_90plus_sellable_units      integer,
    aged_90plus_sellable_cost       numeric(14,2),
    -- NULL or 0.0 on amazon.in as of Sep 2026; stored in case that changes.
    -- Do not report off these without checking they are populated.
    sell_through_rate               numeric(8,4),
    receive_fill_rate               numeric(8,4),
    average_vendor_lead_time_days   numeric(8,2),
    vendor_confirmation_rate        numeric(8,4),
    ingested_at                     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asin, report_date, distributor_view, selling_program)
);
CREATE INDEX IF NOT EXISTS idx_vid_date ON vendor_inventory_daily (report_date);

CREATE TABLE IF NOT EXISTS vendor_traffic_daily (
    asin                text NOT NULL,
    report_date         date NOT NULL,
    glance_views        integer,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asin, report_date)
);
CREATE INDEX IF NOT EXISTS idx_vtd_date ON vendor_traffic_daily (report_date);


-- ---------------------------------------------------------------------------
-- SELLER CENTRAL
-- The ASIN section of GET_SALES_AND_TRAFFIC_REPORT carries no date, so the
-- ingest job requests one day at a time and stamps report_date itself.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS seller_sales_traffic_daily (
    child_asin                  text NOT NULL,
    report_date                 date NOT NULL,
    parent_asin                 text,
    units_ordered               integer,
    ordered_product_sales       numeric(14,2),
    units_shipped               integer,
    shipped_product_sales       numeric(14,2),
    units_refunded              integer,
    sessions                    integer,
    page_views                  integer,
    buy_box_percentage          numeric(8,2),
    unit_session_percentage     numeric(8,2),
    ingested_at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (child_asin, report_date)
);
CREATE INDEX IF NOT EXISTS idx_sstd_date ON seller_sales_traffic_daily (report_date);

CREATE TABLE IF NOT EXISTS seller_sales_traffic_by_date (
    report_date                 date PRIMARY KEY,
    units_ordered               integer,
    ordered_product_sales       numeric(14,2),
    units_shipped               integer,
    shipped_product_sales       numeric(14,2),
    units_refunded              integer,
    sessions                    integer,
    page_views                  integer,
    buy_box_percentage          numeric(8,2),
    unit_session_percentage     numeric(8,2),
    ingested_at                 timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- OFFLINE (distributor orders)  -- PROVISIONAL until the first Woo payload
-- One row per order line. WooCommerce today; FieldAssist later writes into the
-- same table with a different source_system.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS offline_order_lines (
    source_system       text NOT NULL,              -- 'woocommerce' | 'fieldassist' | 'tally'
    order_id            text NOT NULL,
    line_id             text NOT NULL,
    order_date          date NOT NULL,
    order_status        text,
    distributor_id      text,
    distributor_name    text,
    internal_sku        text,                       -- NULL if the line had no SKU
    product_name        text,
    quantity            integer NOT NULL,
    line_total          numeric(14,2),              -- after discount, before tax
    ingest_method       text NOT NULL,              -- 'api' | 'file'
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, order_id, line_id)
);
CREATE INDEX IF NOT EXISTS idx_ool_date ON offline_order_lines (order_date);
CREATE INDEX IF NOT EXISTS idx_ool_sku  ON offline_order_lines (internal_sku);


-- ---------------------------------------------------------------------------
-- AMAZON ADS  -- PROVISIONAL, column names not yet verified against a payload
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ads_advertised_product_daily (
    profile_id          bigint NOT NULL,
    campaign_id         bigint NOT NULL,
    ad_group_id         bigint NOT NULL,
    asin                text NOT NULL,
    report_date         date NOT NULL,
    ad_product          text NOT NULL,              -- SP | SB | SD
    campaign_name       text,
    impressions         bigint,
    clicks              bigint,
    cost                numeric(14,2),
    attributed_sales    numeric(14,2),              -- 14-day window for vendors
    attributed_units    integer,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, campaign_id, ad_group_id, asin, report_date, ad_product)
);
CREATE INDEX IF NOT EXISTS idx_aapd_asin_date ON ads_advertised_product_daily (asin, report_date);


-- ---------------------------------------------------------------------------
-- OPERATIONS
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sync_runs (
    id                  bigserial PRIMARY KEY,
    report_type         text NOT NULL,
    account             text NOT NULL,
    period_start        date,
    period_end          date,
    status              text NOT NULL,              -- running | success | failed
    rows_written        integer,
    amazon_report_id    text,
    error_message       text,
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_lookup ON sync_runs (report_type, status, started_at DESC);

CREATE TABLE IF NOT EXISTS raw_report_documents (
    id                  bigserial PRIMARY KEY,
    report_type         text NOT NULL,
    account             text NOT NULL,
    period_start        date,
    period_end          date,
    amazon_report_id    text,
    payload             jsonb NOT NULL,
    ingested_at         timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- VIEWS
-- ---------------------------------------------------------------------------

-- How many units of each SKU one unit of an ASIN represents.
-- Normal ASIN: (asin, its sku, 1). Combo ASIN: one row per component size.
CREATE OR REPLACE VIEW v_asin_to_sku AS
SELECT pa.asin,
       COALESCE(bc.component_sku, pa.internal_sku) AS internal_sku,
       COALESCE(bc.qty, 1)                         AS units_per_asin_unit,
       pa.internal_sku                             AS listed_sku
FROM product_asins pa
LEFT JOIN bundle_components bc ON bc.bundle_sku = pa.internal_sku;


-- Amazon sales at SKU level, combos split into sizes. Unmapped ASINs keep a
-- NULL sku rather than disappearing, so channel totals still reconcile.
-- NOTE: units here are SKU units. A combo pack counts as 2 here but as 1 in
-- Amazon's own reports - compare against Amazon using vendor_sales_daily.
CREATE OR REPLACE VIEW v_amazon_sku_daily AS
SELECT s.report_date,
       'vendor'::text                                 AS channel,
       s.asin,
       m.internal_sku,
       s.shipped_units * COALESCE(m.units_per_asin_unit, 1) AS sku_units,
       s.shipped_units                                AS asin_units,
       s.customer_returns
FROM vendor_sales_daily s
LEFT JOIN v_asin_to_sku m ON m.asin = s.asin
WHERE s.distributor_view = 'SOURCING' AND s.selling_program = 'RETAIL'
UNION ALL
SELECT t.report_date,
       'seller'::text,
       t.child_asin,
       m.internal_sku,
       t.units_shipped * COALESCE(m.units_per_asin_unit, 1),
       t.units_shipped,
       t.units_refunded
FROM seller_sales_traffic_daily t
LEFT JOIN v_asin_to_sku m ON m.asin = t.child_asin;


-- ASINs that have sold or had traffic but are not in the product map.
-- Anything here is missing from every SKU- and category-level number.
CREATE OR REPLACE VIEW v_unmapped_asins AS
SELECT asin,
       sum(units)        AS units,
       min(first_seen)   AS first_seen,
       max(last_seen)    AS last_seen,
       string_agg(DISTINCT source, ', ') AS seen_in
FROM (
    SELECT asin, shipped_units AS units, report_date AS first_seen, report_date AS last_seen, 'vendor_sales' AS source
    FROM vendor_sales_daily
    UNION ALL
    SELECT child_asin, units_shipped, report_date, report_date, 'seller_sales'
    FROM seller_sales_traffic_daily
    UNION ALL
    SELECT asin, 0, report_date, report_date, 'vendor_traffic'
    FROM vendor_traffic_daily
) x
WHERE NOT EXISTS (SELECT 1 FROM product_asins pa WHERE pa.asin = x.asin)
GROUP BY asin;


-- ---------------------------------------------------------------------------
-- WOOCOMMERCE ADDITIONS (v3) - columns confirmed against the live portal
-- ---------------------------------------------------------------------------
-- raw_sku keeps the portal's own code ('5429-FBM'); internal_sku is the
-- product-map code it resolved to ('5429'), or NULL if it did not resolve.
ALTER TABLE offline_order_lines ADD COLUMN IF NOT EXISTS raw_sku           text;
ALTER TABLE offline_order_lines ADD COLUMN IF NOT EXISTS distributor_gstin text;
ALTER TABLE offline_order_lines ADD COLUMN IF NOT EXISTS line_tax          numeric(14,2);
ALTER TABLE offline_order_lines ADD COLUMN IF NOT EXISTS quantity_refunded integer NOT NULL DEFAULT 0;
ALTER TABLE offline_order_lines ADD COLUMN IF NOT EXISTS amount_refunded   numeric(14,2) NOT NULL DEFAULT 0;

-- Which order statuses count as a sale is decided HERE, in one place.
-- Everything is stored; cancelled and on-hold (awaiting approval) are left
-- out of sales. Change the list and re-run this file to change every report.
CREATE OR REPLACE VIEW v_offline_sku_daily AS
SELECT l.order_date,
       l.source_system                          AS channel,
       l.distributor_id,
       l.distributor_name,
       l.internal_sku,
       l.raw_sku,
       l.quantity - l.quantity_refunded         AS sku_units,
       l.line_total - l.amount_refunded         AS net_value_ex_tax
FROM offline_order_lines l
WHERE l.order_status IN ('completed', 'processing');

-- Portal SKUs that did not resolve to the product map.
CREATE OR REPLACE VIEW v_unmapped_offline_skus AS
SELECT raw_sku, product_name, sum(quantity) AS units, count(*) AS lines,
       min(order_date) AS first_seen, max(order_date) AS last_seen
FROM offline_order_lines
WHERE internal_sku IS NULL
GROUP BY raw_sku, product_name;


-- ---------------------------------------------------------------------------
-- ALL CHANNELS (v4) - what the MCP tools read
-- ---------------------------------------------------------------------------
-- One row per day x channel x SKU, units only. Units are the one measure all
-- three channels share: vendor revenue is 0 on amazon.in, seller value is
-- customer price incl. GST, portal value is distributor price ex GST.
-- Values stay in their own tools until the revenue formula exists.
CREATE OR REPLACE VIEW v_sales_all_daily AS
WITH x AS (
    SELECT report_date AS sale_date, channel, internal_sku, sku_units
    FROM v_amazon_sku_daily
    UNION ALL
    SELECT order_date, channel, internal_sku, sku_units
    FROM v_offline_sku_daily
)
SELECT x.sale_date,
       x.channel,                                   -- vendor | seller | woocommerce
       CASE WHEN x.channel = 'woocommerce' THEN 'offline' ELSE 'online' END AS channel_type,
       x.internal_sku,
       COALESCE(p.product_name, '(unmapped)')        AS product_name,
       COALESCE(p.category, 'Unmapped')              AS category,
       COALESCE(p.report_group, 'Unmapped')          AS report_group,
       x.sku_units
FROM x
LEFT JOIN products p ON p.internal_sku = x.internal_sku;


-- ---------------------------------------------------------------------------
-- DISTRIBUTOR -> ASM (v5) - loaded from distributor_asm.csv by load_local.py
-- ---------------------------------------------------------------------------
-- Matched on a normalised name (lower case, letters and digits only), because
-- the portal's spelling varies ('velocity', 'K.S.S. CAR ACCESSORIES').
-- Distributors that do not match show as 'Unassigned' rather than vanish.
CREATE TABLE IF NOT EXISTS distributor_asm (
    name_key            text PRIMARY KEY,
    distributor_name    text NOT NULL,
    asm                 text NOT NULL
);

-- Offline sales lines with product attributes and ASM - what
-- distributor_product_sales reads. Same rows as v_offline_sku_daily.
CREATE OR REPLACE VIEW v_offline_sales_detail AS
SELECT o.order_date,
       o.distributor_id,
       o.distributor_name,
       COALESCE(a.asm, 'Unassigned')                 AS asm,
       o.internal_sku,
       COALESCE(p.product_name, '(unmapped)')        AS product_name,
       COALESCE(p.category, 'Unmapped')              AS category,
       COALESCE(NULLIF(p.pack_size, ''), 'no size')  AS pack_size,
       o.sku_units,
       o.net_value_ex_tax
FROM v_offline_sku_daily o
LEFT JOIN products p ON p.internal_sku = o.internal_sku
LEFT JOIN distributor_asm a
       ON a.name_key = lower(regexp_replace(o.distributor_name, '[^a-zA-Z0-9]', '', 'g'));


-- ---------------------------------------------------------------------------
-- SALES VALUE (v6) - prices from the Michelin Amazon mastersheet
-- ---------------------------------------------------------------------------
-- asin_prices is loaded from asin_prices.csv (import_prices.py builds it).
-- cost_price_ex_gst = "ETrade CP W/o GST" = what Amazon pays Axon per unit.
-- valid_from dates a price; the first import is 2000-01-01, i.e. today's
-- prices value all history (agreed Oct 2026).
CREATE TABLE IF NOT EXISTS asin_prices (
    asin                text NOT NULL,
    valid_from          date NOT NULL,
    cost_price_ex_gst   numeric(12,2) NOT NULL,
    selling_price       numeric(12,2),
    mrp                 numeric(12,2),
    india_sku           text,
    source_tab          text,
    PRIMARY KEY (asin, valid_from)
);

-- Rupee value per day, channel and ASIN/SKU. Value bases differ by channel
-- and are labelled in value_basis - never add them up without saying so:
--   vendor      : shipped units x ETrade CP (ex GST)  = Axon's revenue
--   seller      : Amazon's shipped product sales (customer price incl GST)
--   woocommerce : billed line value after discount/refunds (ex GST)
-- Units here are LISTING units (a wiper combo counts once, at its combo
-- price) - unlike v_sales_all_daily, which splits combos into sizes.
-- An ASIN with no CP falls back to its SKU's CP when all that SKU's priced
-- ASINs share one CP; otherwise value is NULL and priced = false.
CREATE OR REPLACE VIEW v_sales_value_daily AS
WITH sku_price AS (
    SELECT pa.internal_sku, min(ap.cost_price_ex_gst) AS cp
    FROM asin_prices ap JOIN product_asins pa USING (asin)
    GROUP BY 1
    HAVING min(ap.cost_price_ex_gst) = max(ap.cost_price_ex_gst)
),
x AS (
    SELECT s.report_date AS sale_date, 'vendor'::text AS channel, s.asin, pa.internal_sku,
           s.shipped_units AS units,
           COALESCE(ap.cost_price_ex_gst, sp.cp) AS unit_price,
           s.shipped_units * COALESCE(ap.cost_price_ex_gst, sp.cp) AS value,
           'ETrade CP ex GST'::text AS value_basis
    FROM vendor_sales_daily s
    LEFT JOIN product_asins pa USING (asin)
    LEFT JOIN LATERAL (SELECT a.cost_price_ex_gst FROM asin_prices a
                       WHERE a.asin = s.asin AND a.valid_from <= s.report_date
                       ORDER BY a.valid_from DESC LIMIT 1) ap ON true
    LEFT JOIN sku_price sp ON sp.internal_sku = pa.internal_sku
    WHERE s.distributor_view = 'SOURCING' AND s.selling_program = 'RETAIL'
    UNION ALL
    SELECT t.report_date, 'seller', t.child_asin, pa.internal_sku,
           t.units_shipped,
           CASE WHEN t.units_shipped > 0 THEN round(t.shipped_product_sales / t.units_shipped, 2) END,
           t.shipped_product_sales,
           'Amazon shipped sales incl GST'
    FROM seller_sales_traffic_daily t
    LEFT JOIN product_asins pa ON pa.asin = t.child_asin
    UNION ALL
    SELECT o.order_date, 'woocommerce', NULL, o.internal_sku,
           o.sku_units,
           CASE WHEN o.sku_units > 0 THEN round(o.net_value_ex_tax / o.sku_units, 2) END,
           o.net_value_ex_tax,
           'Portal billed ex GST'
    FROM v_offline_sku_daily o
)
SELECT x.sale_date, x.channel,
       CASE WHEN x.channel = 'woocommerce' THEN 'offline' ELSE 'online' END AS channel_type,
       x.asin, x.internal_sku,
       COALESCE(p.product_name, '(unmapped)')  AS product_name,
       COALESCE(p.category, 'Unmapped')        AS category,
       COALESCE(p.report_group, 'Unmapped')    AS report_group,
       x.units, x.unit_price, x.value,
       (x.value IS NOT NULL OR COALESCE(x.units, 0) = 0) AS priced,
       x.value_basis
FROM x
LEFT JOIN products p ON p.internal_sku = x.internal_sku;


-- ---------------------------------------------------------------------------
-- VENDOR SELL-IN (v7) - what Amazon received from Axon
-- ---------------------------------------------------------------------------
-- Same columns as v_sales_value_daily, but the vendor rows are Amazon's NET
-- RECEIVED inventory (units and Amazon's own cost, ex GST) per day, instead of
-- Amazon's sales to customers. This tracks Axon's invoices to Amazon except
-- for stock in transit at month-end (invoiced, not yet received). Received
-- cost of 0/NULL with units > 0 falls back to units x ETrade CP.
CREATE OR REPLACE VIEW v_sales_value_sellin_daily AS
WITH sku_price AS (
    SELECT pa.internal_sku, min(ap.cost_price_ex_gst) AS cp
    FROM asin_prices ap JOIN product_asins pa USING (asin)
    GROUP BY 1
    HAVING min(ap.cost_price_ex_gst) = max(ap.cost_price_ex_gst)
),
recv AS (
    SELECT i.report_date AS sale_date, 'vendor'::text AS channel, 'online'::text AS channel_type,
           i.asin, pa.internal_sku,
           i.net_received_inventory_units AS units,
           COALESCE(NULLIF(i.net_received_inventory_cost, 0),
                    i.net_received_inventory_units * COALESCE(ap.cost_price_ex_gst, sp.cp)) AS value
    FROM vendor_inventory_daily i
    LEFT JOIN product_asins pa USING (asin)
    LEFT JOIN LATERAL (SELECT a.cost_price_ex_gst FROM asin_prices a
                       WHERE a.asin = i.asin AND a.valid_from <= i.report_date
                       ORDER BY a.valid_from DESC LIMIT 1) ap ON true
    LEFT JOIN sku_price sp ON sp.internal_sku = pa.internal_sku
    WHERE i.distributor_view = 'SOURCING' AND i.selling_program = 'RETAIL'
      AND COALESCE(i.net_received_inventory_units, 0) <> 0
)
SELECT r.sale_date, r.channel, r.channel_type, r.asin, r.internal_sku,
       COALESCE(p.product_name, '(unmapped)') AS product_name,
       COALESCE(p.category, 'Unmapped')       AS category,
       COALESCE(p.report_group, 'Unmapped')   AS report_group,
       r.units,
       CASE WHEN r.units <> 0 THEN round(r.value / r.units, 2) END AS unit_price,
       r.value,
       (r.value IS NOT NULL) AS priced,
       'Amazon received (sell-in), Amazon cost ex GST'::text AS value_basis
FROM recv r
LEFT JOIN products p ON p.internal_sku = r.internal_sku
UNION ALL
SELECT * FROM v_sales_value_daily WHERE channel <> 'vendor';


-- ---------------------------------------------------------------------------
-- BUY BOX (v8) - pricing_sync.py, every ~3 hours with the Woo refresh
-- ---------------------------------------------------------------------------
-- One row per ASIN per pull. All rows of one pull share fetched_at.
-- buy_box_holder: 'axon' (our Seller Central, A1QTZ8OBV6L6UD),
-- 'vendor_channel' (Etrade, A2AL6IVND0I91F - the entity we bill via Vendor
-- Central, selling our Vendor stock; confirmed Oct 2026),
-- 'other' (anyone else), NULL (no Buy Box).
CREATE TABLE IF NOT EXISTS buy_box_snapshots (
    asin                    text NOT NULL,
    fetched_at              timestamptz NOT NULL DEFAULT now(),
    status                  integer,
    buy_box_price           numeric(12,2),
    buy_box_seller          text,
    buy_box_holder          text,
    buy_box_fba             boolean,
    total_offers            integer,
    lowest_price            numeric(12,2),
    axon_price              numeric(12,2),
    vendor_channel_price    numeric(12,2),
    other_seller_count      integer,
    other_seller_min_price  numeric(12,2),
    sales_rank              integer,
    sales_rank_sub          integer,
    sales_rank_category     text,
    offers                  jsonb,
    error                   text,
    PRIMARY KEY (asin, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_bbs_time ON buy_box_snapshots (fetched_at);

-- Latest pull per ASIN with the sheet prices and the issue flags.
CREATE OR REPLACE VIEW v_buy_box_latest AS
WITH latest AS (
    SELECT DISTINCT ON (asin) * FROM buy_box_snapshots ORDER BY asin, fetched_at DESC
),
prev AS (   -- the pull about a day earlier, for change detection
    SELECT DISTINCT ON (b.asin) b.asin, b.buy_box_price AS price_1d_ago,
           b.buy_box_holder AS holder_1d_ago, b.sales_rank AS rank_1d_ago
    FROM buy_box_snapshots b JOIN latest l USING (asin)
    WHERE b.fetched_at <= l.fetched_at - interval '20 hours'
    ORDER BY b.asin, b.fetched_at DESC
),
asin_price AS (
    SELECT DISTINCT ON (asin) asin, cost_price_ex_gst, selling_price, mrp
    FROM asin_prices ORDER BY asin, valid_from DESC
),
sku_price AS (   -- fallback for ASINs without their own sheet row: the SKU's
                 -- price, when all of that SKU's priced ASINs agree
    SELECT pa.internal_sku,
           CASE WHEN min(ap.cost_price_ex_gst) = max(ap.cost_price_ex_gst) THEN min(ap.cost_price_ex_gst) END AS cost_price_ex_gst,
           CASE WHEN min(ap.selling_price) = max(ap.selling_price) THEN min(ap.selling_price) END AS selling_price,
           CASE WHEN min(ap.mrp) = max(ap.mrp) THEN min(ap.mrp) END AS mrp
    FROM asin_prices ap JOIN product_asins pa USING (asin)
    GROUP BY 1
),
price AS (
    SELECT pa.asin,
           COALESCE(a.cost_price_ex_gst, sp.cost_price_ex_gst)::numeric(12,2) AS cost_price_ex_gst,
           COALESCE(a.selling_price, sp.selling_price)::numeric(12,2)         AS selling_price,
           COALESCE(a.mrp, sp.mrp)::numeric(12,2)                             AS mrp
    FROM product_asins pa
    LEFT JOIN asin_price a USING (asin)
    LEFT JOIN sku_price sp ON sp.internal_sku = pa.internal_sku
)
SELECT l.asin, pa.internal_sku AS sku,
       COALESCE(p.product_name, '(unmapped)') AS product_name,
       COALESCE(p.category, 'Unmapped') AS category,
       l.fetched_at, l.status, l.error,
       l.buy_box_price, l.buy_box_holder, l.buy_box_seller, l.buy_box_fba,
       pr.selling_price AS sheet_selling_price, pr.cost_price_ex_gst AS etrade_cp, pr.mrp,
       l.axon_price, l.vendor_channel_price,
       l.other_seller_count, l.other_seller_min_price,
       l.total_offers, l.lowest_price, l.sales_rank, l.sales_rank_sub,
       pv.price_1d_ago, pv.holder_1d_ago, pv.rank_1d_ago,
       -- flags
       (l.status = 200 AND l.buy_box_price IS NULL)                         AS no_buy_box,
       (l.buy_box_holder = 'other')                                         AS buy_box_lost_to_other,
       (l.buy_box_price < pr.selling_price)                                 AS below_sheet_price,
       -- Buy Box is a customer price incl GST; CP is ex GST. Compare like with like,
       -- assuming 18% GST (most of the range; microfibre may be lower).
       (l.buy_box_price / 1.18 < pr.cost_price_ex_gst)                      AS below_etrade_cp,
       (l.axon_price < l.vendor_channel_price)                              AS axon_undercuts_vendor,
       (COALESCE(l.other_seller_count, 0) > 0)                              AS other_sellers_present,
       (pv.holder_1d_ago IS DISTINCT FROM l.buy_box_holder AND pv.asin IS NOT NULL) AS holder_changed_1d,
       round(100.0 * (l.buy_box_price - pr.selling_price) / NULLIF(pr.selling_price, 0), 1) AS pct_vs_sheet,
       -- no_buy_box split in two (added Oct 2026):
       (l.status = 200 AND l.buy_box_price IS NULL AND COALESCE(l.total_offers, 0) > 0) AS buy_box_suppressed,
       (l.status = 200 AND COALESCE(l.total_offers, 0) = 0)                            AS no_offers,
       -- Core rule (Oct 2026): the Buy Box should be Axon's or Etrade's, and no
       -- outside seller should be cheaper than our lowest offer.
       (l.other_seller_min_price < LEAST(l.axon_price, l.vendor_channel_price))     AS undercut_by_other
FROM latest l
LEFT JOIN prev pv USING (asin)
LEFT JOIN price pr USING (asin)
LEFT JOIN product_asins pa USING (asin)
LEFT JOIN products p ON p.internal_sku = pa.internal_sku;


-- ---------------------------------------------------------------------------
-- AMAZON MIS (v9) - the daily "Amazon Sale MIS" sheet, rebuilt from data
-- ---------------------------------------------------------------------------
-- Ad spend: filled by the Ads API sync once access is approved (source
-- 'ads_api'). Until then the MIS shows Ad spend and TACOS blank. Rows with
-- any other source (the old MIS-workbook import, 'mis') are ignored by the view.
CREATE TABLE IF NOT EXISTS ad_spend_daily (
    spend_date      date PRIMARY KEY,
    ad_spend        numeric(14,2) NOT NULL,
    source          text NOT NULL DEFAULT 'mis'
);

-- Seller order count (for orders / AOV in the MIS). Existing days are filled
-- from the stored raw Seller reports; new pulls write it directly.
ALTER TABLE seller_sales_traffic_by_date ADD COLUMN IF NOT EXISTS total_order_items integer;
UPDATE seller_sales_traffic_by_date t
SET total_order_items = x.v
FROM (
    SELECT DISTINCT ON ((e->>'date')::date)
           (e->>'date')::date AS d, (e->'salesByDate'->>'totalOrderItems')::int AS v
    FROM raw_report_documents r,
         jsonb_array_elements(r.payload->'salesAndTrafficByDate') e
    WHERE r.account = 'seller' AND e->'salesByDate' ? 'totalOrderItems'
    ORDER BY (e->>'date')::date, r.ingested_at DESC
) x
WHERE t.report_date = x.d AND t.total_order_items IS NULL;

-- One row per day, matching the MIS columns:
--   sc_sale   = Seller Central ORDERED product sales (incl GST) - matches the
--               MIS "SC Sale" (July: Rs 14.46 L vs MIS 14.55 L)
--   sc_orders = Seller order items; sc_aov = sc_sale / sc_orders;
--               sc_sale_per_unit = sc_sale / sc_units (the old Nov/Dec MIS "AOV")
--   vc_sale   = Vendor shipped units x the customer price that day (incl GST):
--               the day's Buy Box price where we have a pull, else the sheet
--               selling price. Matches the MIS "VC Sale" basis (customer
--               price), NOT Axon's revenue - that is sales_value / CP.
--   ad_spend, tacos = ad_spend / (total / 1.18): Ads API only, blank until then
--   total_ex_gst = total / 1.18
-- DROP + CREATE because columns were inserted mid-list (v9.1).
DROP VIEW IF EXISTS v_amazon_mis_daily;
CREATE VIEW v_amazon_mis_daily AS
WITH bb_day AS (    -- last Buy Box price per ASIN per IST day
    SELECT DISTINCT ON (asin, (fetched_at AT TIME ZONE 'Asia/Kolkata')::date)
           asin, (fetched_at AT TIME ZONE 'Asia/Kolkata')::date AS d, buy_box_price
    FROM buy_box_snapshots
    WHERE buy_box_price IS NOT NULL
    ORDER BY asin, (fetched_at AT TIME ZONE 'Asia/Kolkata')::date, fetched_at DESC
),
sku_sp AS (
    SELECT pa.internal_sku, min(ap.selling_price) AS sp
    FROM asin_prices ap JOIN product_asins pa USING (asin)
    WHERE ap.selling_price IS NOT NULL
    GROUP BY 1 HAVING min(ap.selling_price) = max(ap.selling_price)
),
vc AS (
    SELECT s.report_date AS d,
           sum(s.shipped_units * COALESCE(bb.buy_box_price, ap.selling_price, sp.sp)) AS vc_sale,
           sum(s.shipped_units) AS vc_units,
           sum(s.shipped_units) FILTER (WHERE bb.buy_box_price IS NOT NULL) AS vc_units_buy_box_priced,
           sum(s.shipped_units) FILTER (WHERE COALESCE(bb.buy_box_price, ap.selling_price, sp.sp) IS NULL)
               AS vc_units_unpriced
    FROM vendor_sales_daily s
    LEFT JOIN product_asins pa USING (asin)
    LEFT JOIN bb_day bb ON bb.asin = s.asin AND bb.d = s.report_date
    LEFT JOIN LATERAL (SELECT a.selling_price FROM asin_prices a
                       WHERE a.asin = s.asin AND a.valid_from <= s.report_date
                       ORDER BY a.valid_from DESC LIMIT 1) ap ON true
    LEFT JOIN sku_sp sp ON sp.internal_sku = pa.internal_sku
    WHERE s.distributor_view = 'SOURCING' AND s.selling_program = 'RETAIL'
    GROUP BY 1
),
sc AS (
    SELECT report_date AS d, ordered_product_sales AS sc_sale, units_ordered AS sc_units,
           total_order_items AS sc_orders
    FROM seller_sales_traffic_by_date
),
ad AS (
    SELECT spend_date AS d, ad_spend FROM ad_spend_daily WHERE source = 'ads_api'
),
days AS (
    SELECT d FROM vc UNION SELECT d FROM sc UNION SELECT d FROM ad
)
SELECT days.d AS sale_date,
       sc.sc_sale, sc.sc_units, sc.sc_orders,
       round(sc.sc_sale / NULLIF(sc.sc_orders, 0), 2) AS sc_aov,
       round(sc.sc_sale / NULLIF(sc.sc_units, 0), 2) AS sc_sale_per_unit,
       round(vc.vc_sale, 2) AS vc_sale, vc.vc_units,
       vc.vc_units_buy_box_priced, vc.vc_units_unpriced,
       round(COALESCE(sc.sc_sale, 0) + COALESCE(vc.vc_sale, 0), 2) AS total_sale,
       COALESCE(sc.sc_units, 0) + COALESCE(vc.vc_units, 0) AS total_units,
       round((COALESCE(sc.sc_sale, 0) + COALESCE(vc.vc_sale, 0)) / 1.18, 2) AS total_ex_gst,
       ad.ad_spend,
       round(ad.ad_spend / NULLIF((COALESCE(sc.sc_sale, 0) + COALESCE(vc.vc_sale, 0)) / 1.18, 0), 4) AS tacos,
       (sc.d IS NOT NULL) AS has_sc, (vc.d IS NOT NULL) AS has_vc, (ad.d IS NOT NULL) AS has_ad
FROM days
LEFT JOIN sc ON sc.d = days.d
LEFT JOIN vc ON vc.d = days.d
LEFT JOIN ad ON ad.d = days.d;

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_reader') THEN
        GRANT SELECT ON v_amazon_mis_daily TO mcp_reader;
    END IF;
END $$;
