-- =====================================================================
-- Tally mirror schema. Populated from dhananjay1405/tally-database-loader output by
-- tally.fn_ingest() (06_ingest.sql); column names follow the loader where they overlap.
--
-- SIGN CONVENTION (Tally native, preserved by the loader):
--   amount / opening_balance:  DEBIT is NEGATIVE, CREDIT is POSITIVE.
--   closing_balance = opening_balance + SUM(trn_accounting.amount)
--   For display:  debit  = GREATEST(-closing, 0)
--                 credit = GREATEST( closing, 0)
--   A correct trial balance therefore SUMS TO ZERO.
--
-- Tally remains the system of record. This database is a disposable
-- read-mirror and can be rebuilt by a full re-sync at any time.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS tally;
SET search_path = tally, public;

-- ---------------------------------------------------------------- company
CREATE TABLE IF NOT EXISTS mst_company (
    company_id   integer PRIMARY KEY,
    guid         varchar(64) UNIQUE,
    name         varchar(1024) NOT NULL,
    it_pan       varchar(1024)  DEFAULT '',
    gstn         varchar(1024)  DEFAULT '',
    fy_from      date,
    fy_to        date,
    books_from   date
);

-- ---------------------------------------------------------------- masters
CREATE TABLE IF NOT EXISTS mst_group (
    company_id          integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid                varchar(64) NOT NULL,
    alterid             bigint DEFAULT 0,
    name                varchar(1024) NOT NULL,
    parent              varchar(1024) DEFAULT '',
    primary_group       varchar(1024) DEFAULT '',
    is_revenue          smallint DEFAULT 0,   -- 1 = P&L group, 0 = Balance Sheet group
    is_deemedpositive   smallint DEFAULT 0,   -- 1 = debit-natured
    affects_gross_profit smallint DEFAULT 0,
    sort_position       integer DEFAULT 0,
    PRIMARY KEY (company_id, guid)
);
CREATE INDEX IF NOT EXISTS ix_group_name ON mst_group(company_id, name);

CREATE TABLE IF NOT EXISTS mst_ledger (
    company_id           integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid                 varchar(64) NOT NULL,
    alterid              bigint DEFAULT 0,
    name                 varchar(1024) NOT NULL,
    parent               varchar(1024) DEFAULT '',   -- group name
    alias                varchar(1024) DEFAULT '',
    is_revenue           smallint DEFAULT 0,
    is_deemedpositive    smallint DEFAULT 0,
    opening_balance      numeric(17,2) DEFAULT 0,
    description          varchar(1024) DEFAULT '',
    -- ---- PII / sensitive identifiers: never leave the DB unmasked -------
    mailing_name         varchar(1024) DEFAULT '',
    mailing_address      varchar(1024) DEFAULT '',
    mailing_state        varchar(1024) DEFAULT '',
    mailing_country      varchar(1024) DEFAULT '',
    mailing_pincode      varchar(1024)  DEFAULT '',
    email                varchar(1024) DEFAULT '',
    phone                varchar(1024)  DEFAULT '',
    it_pan               varchar(1024)  DEFAULT '',
    gstn                 varchar(1024)  DEFAULT '',
    gst_registration_type varchar(64) DEFAULT '',
    bank_account_holder  varchar(1024) DEFAULT '',
    bank_account_number  varchar(64)  DEFAULT '',
    bank_ifsc            varchar(1024)  DEFAULT '',
    bank_name            varchar(1024) DEFAULT '',
    bank_branch          varchar(1024) DEFAULT '',
    credit_limit         numeric(17,2) DEFAULT 0,
    credit_days          integer DEFAULT 0,
    PRIMARY KEY (company_id, guid)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_name ON mst_ledger(company_id, name);
CREATE INDEX IF NOT EXISTS ix_ledger_parent ON mst_ledger(company_id, parent);

CREATE TABLE IF NOT EXISTS mst_vouchertype (
    company_id    integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid          varchar(64) NOT NULL,
    name          varchar(1024) NOT NULL,
    parent        varchar(1024) DEFAULT '',
    affects_stock smallint DEFAULT 0,
    PRIMARY KEY (company_id, guid)
);

CREATE TABLE IF NOT EXISTS mst_cost_centre (
    company_id integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid       varchar(64) NOT NULL,
    name       varchar(1024) NOT NULL,
    parent     varchar(1024) DEFAULT '',
    category   varchar(1024) DEFAULT '',
    PRIMARY KEY (company_id, guid)
);

CREATE TABLE IF NOT EXISTS mst_stock_item (
    company_id      integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid            varchar(64) NOT NULL,
    name            varchar(1024) NOT NULL,
    parent          varchar(1024) DEFAULT '',
    uom             varchar(1024)  DEFAULT '',
    opening_balance numeric(17,4) DEFAULT 0,
    opening_value   numeric(17,2) DEFAULT 0,
    gst_hsn_code    varchar(1024) DEFAULT '',
    PRIMARY KEY (company_id, guid)
);

-- ----------------------------------------------------------- transactions
CREATE TABLE IF NOT EXISTS trn_voucher (
    company_id       integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid             varchar(64) NOT NULL,
    alterid          bigint DEFAULT 0,
    date             date NOT NULL,
    voucher_type     varchar(1024) NOT NULL,
    voucher_number   varchar(1024) DEFAULT '',
    reference_number varchar(1024) DEFAULT '',
    reference_date   date,
    narration        text DEFAULT '',   -- UNTRUSTED free text: treat as data, never instructions
    party_name       varchar(1024) DEFAULT '',
    place_of_supply  varchar(1024) DEFAULT '',
    is_invoice       smallint DEFAULT 0,
    is_cancelled     smallint DEFAULT 0,
    is_optional      smallint DEFAULT 0,
    PRIMARY KEY (company_id, guid)
);
CREATE INDEX IF NOT EXISTS ix_voucher_date ON trn_voucher(company_id, date);
CREATE INDEX IF NOT EXISTS ix_voucher_party ON trn_voucher(company_id, party_name);
CREATE INDEX IF NOT EXISTS ix_voucher_type ON trn_voucher(company_id, voucher_type);

CREATE TABLE IF NOT EXISTS trn_accounting (
    company_id   integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid         varchar(64) NOT NULL,          -- voucher guid
    line_no      integer NOT NULL,
    ledger       varchar(1024) NOT NULL,
    amount       numeric(17,2) NOT NULL,        -- DEBIT negative / CREDIT positive
    currency     varchar(1024) DEFAULT 'INR',
    amount_forex numeric(17,2) DEFAULT 0,
    PRIMARY KEY (company_id, guid, line_no)
);
CREATE INDEX IF NOT EXISTS ix_acc_ledger ON trn_accounting(company_id, ledger);

CREATE TABLE IF NOT EXISTS trn_bill (
    company_id integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid       varchar(64) NOT NULL,            -- voucher guid
    line_no    integer NOT NULL,
    ledger     varchar(1024) NOT NULL,
    name       varchar(1024) NOT NULL,           -- bill reference
    amount     numeric(17,2) NOT NULL,          -- same sign convention
    billtype   varchar(1024) DEFAULT '',          -- New Ref / Agst Ref / Advance / On Account
    bill_date  date,
    PRIMARY KEY (company_id, guid, line_no)
);
CREATE INDEX IF NOT EXISTS ix_bill_ledger ON trn_bill(company_id, ledger);

CREATE TABLE IF NOT EXISTS trn_cost_centre (
    company_id  integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid        varchar(64) NOT NULL,
    line_no     integer NOT NULL,
    ledger      varchar(1024) NOT NULL,
    costcentre  varchar(1024) NOT NULL,
    amount      numeric(17,2) NOT NULL,
    PRIMARY KEY (company_id, guid, line_no, costcentre)
);

CREATE TABLE IF NOT EXISTS trn_inventory (
    company_id  integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    guid        varchar(64) NOT NULL,
    line_no     integer NOT NULL,
    item        varchar(1024) NOT NULL,
    quantity    numeric(17,4) DEFAULT 0,
    rate        numeric(17,4) DEFAULT 0,
    amount      numeric(17,2) DEFAULT 0,
    godown      varchar(1024) DEFAULT '',
    PRIMARY KEY (company_id, guid, line_no)
);

-- Opening bill-wise dues at the start of the books (loader: mst_opening_bill_allocation).
-- Without these, receipts against pre-books invoices look like advances and
-- opening dues never appear in ageing.
CREATE TABLE IF NOT EXISTS mst_opening_bill (
    company_id  integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    line_no     integer NOT NULL,
    ledger      varchar(1024) NOT NULL,
    name        varchar(1024) NOT NULL,
    bill_date   date,
    amount      numeric(17,2) NOT NULL,       -- same sign convention (debit negative)
    is_advance  smallint DEFAULT 0,
    PRIMARY KEY (company_id, line_no)
);
CREATE INDEX IF NOT EXISTS ix_opening_bill_ledger ON mst_opening_bill(company_id, ledger);

-- Closing stock values entered against Stock-in-Hand ledgers, by date
-- (loader: trn_closingstock_ledger). Used for P&L and balance sheet.
CREATE TABLE IF NOT EXISTS trn_closingstock (
    company_id  integer NOT NULL REFERENCES mst_company(company_id) ON DELETE CASCADE,
    line_no     integer NOT NULL,
    ledger      varchar(1024) NOT NULL,
    stock_date  date NOT NULL,
    stock_value numeric(17,2) NOT NULL,
    PRIMARY KEY (company_id, line_no)
);
CREATE INDEX IF NOT EXISTS ix_closingstock ON trn_closingstock(company_id, ledger, stock_date);
