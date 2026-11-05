-- Sync watermark: every answer must be stamped with data freshness.
SET search_path = tally, public;

CREATE TABLE IF NOT EXISTS sync_state (
    company_id     integer PRIMARY KEY REFERENCES mst_company(company_id) ON DELETE CASCADE,
    last_sync_at   timestamptz NOT NULL DEFAULT now(),
    last_alterid   bigint DEFAULT 0,
    last_status    varchar(32) DEFAULT 'ok',   -- ok | failed | running
    last_error     text DEFAULT '',
    rows_synced    bigint DEFAULT 0
);

CREATE OR REPLACE VIEW v_sync_freshness WITH (security_invoker = true) AS
SELECT c.company_id,
       c.name AS company_name,
       s.last_sync_at,
       s.last_status,
       ROUND(EXTRACT(EPOCH FROM (now() - s.last_sync_at)) / 60.0, 1) AS lag_minutes
FROM mst_company c
LEFT JOIN sync_state s ON s.company_id = c.company_id;

-- Monitoring view for /healthz and n8n alerts. SECURITY DEFINER (owned by the
-- sync role) so it can see every company, but it returns ONLY aggregates:
-- no names, no figures, nothing a monitor needs to be trusted with.
CREATE OR REPLACE FUNCTION fn_sync_health()
RETURNS TABLE (companies integer, failed integer, max_lag_minutes numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = tally, pg_temp AS $$
    SELECT COUNT(*)::integer,
           COUNT(*) FILTER (WHERE s.last_status = 'failed')::integer,
           ROUND(MAX(EXTRACT(EPOCH FROM (now() - s.last_sync_at)) / 60.0), 1)
    FROM mst_company c
    LEFT JOIN sync_state s ON s.company_id = c.company_id;
$$;
