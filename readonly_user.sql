-- Read-only login for the MCP server. Run once:
--     psql axon_amazon -f readonly_user.sql
-- Safe to re-run.
--
-- Three layers, so a bad prompt can never change data:
--   1. the role only has SELECT
--   2. every session it opens is read-only by default
--   3. any single query is killed after 30 seconds

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_reader') THEN
        CREATE ROLE mcp_reader LOGIN;
    END IF;
END $$;

GRANT CONNECT ON DATABASE axon_amazon TO mcp_reader;
GRANT USAGE ON SCHEMA public TO mcp_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_reader;          -- includes views
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_reader;

ALTER ROLE mcp_reader SET default_transaction_read_only = on;
ALTER ROLE mcp_reader SET statement_timeout = '30s';
