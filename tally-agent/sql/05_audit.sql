-- =====================================================================
-- Append-only audit trail (DPDP Act 2023: auditable access to personal data).
--
-- The RAW question is never stored -- it may contain a client's PAN or name.
-- We store the MASKED question plus a salted hash, so repeat questions can be
-- correlated without retaining the personal data itself.
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS audit.query_log (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    user_id         varchar(128) NOT NULL,
    user_role       varchar(32)  NOT NULL,
    client_ip       inet,
    company_id      integer,
    question_masked text         NOT NULL,   -- masked, never raw
    question_hash   varchar(64)  NOT NULL,   -- salted SHA-256 of raw
    report_id       varchar(64),
    params          jsonb        NOT NULL DEFAULT '{}'::jsonb,
    row_count       integer      NOT NULL DEFAULT 0,
    unmasked_shown  boolean      NOT NULL DEFAULT false,
    llm_model       varchar(64),
    latency_ms      integer,
    outcome         varchar(24)  NOT NULL,   -- ok | denied | error | no_match
    error_detail    text,
    data_as_of      timestamptz
);
CREATE INDEX IF NOT EXISTS ix_audit_ts      ON audit.query_log(ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_user    ON audit.query_log(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_outcome ON audit.query_log(outcome, ts DESC);

-- Append-only: block UPDATE, DELETE and TRUNCATE for everyone, including the owner.
-- Statement-level, so it fires even when no row matches -- and TRUNCATE, which
-- row-level triggers never see, is covered too.
CREATE OR REPLACE FUNCTION audit.fn_block_mutation()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'audit.query_log is append-only (attempted %)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;

DROP TRIGGER IF EXISTS trg_audit_no_update ON audit.query_log;
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.query_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.fn_block_mutation();

-- Retention helper. DPDP requires a defined retention period; purging older
-- rows is a deliberate owner-only operation, not something the app can do.
CREATE OR REPLACE FUNCTION audit.fn_purge_older_than(p_days integer)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = audit, pg_temp AS $$
DECLARE n bigint;
BEGIN
    ALTER TABLE audit.query_log DISABLE TRIGGER trg_audit_no_update;
    WITH d AS (DELETE FROM audit.query_log WHERE ts < now() - make_interval(days => p_days) RETURNING 1)
    SELECT count(*) INTO n FROM d;
    ALTER TABLE audit.query_log ENABLE TRIGGER trg_audit_no_update;
    RETURN n;
END $$;
REVOKE ALL ON FUNCTION audit.fn_purge_older_than(integer) FROM PUBLIC;

-- The app may only append.
GRANT USAGE ON SCHEMA audit TO tally_ro;
GRANT INSERT ON audit.query_log TO tally_ro;
GRANT USAGE, SELECT ON SEQUENCE audit.query_log_id_seq TO tally_ro;
-- Deliberately NOT granted: SELECT on the log. Reading the audit trail is a
-- separate privileged role so a compromised app cannot mine past questions.
