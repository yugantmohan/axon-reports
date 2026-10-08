-- Row counts for the migration check. Run on the Mac right after the dump and
-- on the PC right after the restore; the numbers must match.
--   Mac:     psql axon_amazon -f row_counts.sql
--   Windows: psql -U postgres -d axon_amazon -f row_counts.sql
SELECT 'vendor_sales_daily' AS table_name, count(*) AS rows FROM vendor_sales_daily
UNION ALL SELECT 'vendor_inventory_daily', count(*) FROM vendor_inventory_daily
UNION ALL SELECT 'vendor_traffic_daily', count(*) FROM vendor_traffic_daily
UNION ALL SELECT 'seller_sales_traffic_by_date', count(*) FROM seller_sales_traffic_by_date
UNION ALL SELECT 'seller_sales_traffic_daily', count(*) FROM seller_sales_traffic_daily
UNION ALL SELECT 'offline_order_lines', count(*) FROM offline_order_lines
UNION ALL SELECT 'buy_box_snapshots', count(*) FROM buy_box_snapshots
UNION ALL SELECT 'product_asins', count(*) FROM product_asins
UNION ALL SELECT 'asin_prices', count(*) FROM asin_prices
UNION ALL SELECT 'raw_report_documents', count(*) FROM raw_report_documents
ORDER BY 1;
