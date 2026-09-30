-- Review before running. This is intentionally not executed by this change.
ALTER TABLE sales_orders_cache ADD COLUMN IF NOT EXISTS shipping_city TEXT;
UPDATE sales_orders_cache
SET shipping_city = NULLIF(BTRIM(COALESCE(shipping_address->>'city', raw_json->'shipping_address'->>'city')), '');
CREATE INDEX IF NOT EXISTS ix_sales_orders_cache_shipping_city ON sales_orders_cache (shipping_city);
