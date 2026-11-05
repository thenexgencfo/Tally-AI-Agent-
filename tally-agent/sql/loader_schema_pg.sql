-- PostgreSQL rendering of tally-database-loader's database-structure.sql
-- (nvarchar -> varchar, tinyint -> smallint). The loader creates these itself;
-- this copy exists so the test-suite can simulate a loader run.
-- Usage: psql -v schema=loader_c1 -f loader_schema_pg.sql
CREATE SCHEMA IF NOT EXISTS :"schema";
SET search_path = :"schema";
CREATE TABLE IF NOT EXISTS config (name varchar(64) PRIMARY KEY, value varchar(1024));
CREATE TABLE IF NOT EXISTS mst_group (guid varchar(64) PRIMARY KEY, name varchar(1024), parent varchar(1024),
  primary_group varchar(1024), is_revenue smallint, is_deemedpositive smallint, is_reserved smallint,
  affects_gross_profit smallint, sort_position int);
CREATE TABLE IF NOT EXISTS mst_ledger (guid varchar(64) PRIMARY KEY, name varchar(1024), parent varchar(1024),
  alias varchar(256), description varchar(64), notes varchar(64), is_revenue smallint, is_deemedpositive smallint,
  opening_balance decimal(17,2), closing_balance decimal(17,2), mailing_name varchar(256),
  mailing_address varchar(1024), mailing_state varchar(256), mailing_country varchar(256),
  mailing_pincode varchar(64), email varchar(256), mobile varchar(32), it_pan varchar(64), gstn varchar(64),
  gst_registration_type varchar(64), gst_supply_type varchar(64), gst_duty_head varchar(16),
  bank_account_holder varchar(256), bank_account_number varchar(64), bank_ifsc varchar(64),
  bank_swift varchar(64), bank_name varchar(64), bank_branch varchar(64), bill_credit_period int);
CREATE TABLE IF NOT EXISTS mst_vouchertype (guid varchar(64) PRIMARY KEY, name varchar(1024), parent varchar(1024),
  numbering_method varchar(64), is_deemedpositive smallint, affects_stock smallint);
CREATE TABLE IF NOT EXISTS mst_cost_centre (guid varchar(64) PRIMARY KEY, name varchar(1024), parent varchar(1024),
  category varchar(1024));
CREATE TABLE IF NOT EXISTS mst_stock_item (guid varchar(64) PRIMARY KEY, name varchar(1024), parent varchar(1024),
  category varchar(1024), alias varchar(256), description varchar(64), notes varchar(64), part_number varchar(256),
  uom varchar(32), alternate_uom varchar(32), conversion decimal(15,4), opening_balance decimal(15,4),
  opening_rate decimal(15,4), opening_value decimal(17,2), closing_balance decimal(15,4), closing_rate decimal(15,4),
  closing_value decimal(17,2), costing_method varchar(32), gst_type_of_supply varchar(32), gst_hsn_code varchar(64),
  gst_hsn_description varchar(256), gst_rate decimal(9,4), gst_taxability varchar(32));
CREATE TABLE IF NOT EXISTS trn_voucher (guid varchar(64) PRIMARY KEY, date date, voucher_type varchar(1024),
  voucher_number varchar(64), reference_number varchar(64), reference_date date, narration varchar(4000),
  party_name varchar(256), place_of_supply varchar(256), is_invoice smallint, is_accounting_voucher smallint,
  is_inventory_voucher smallint, is_order_voucher smallint);
CREATE TABLE IF NOT EXISTS trn_accounting (guid varchar(64), ledger varchar(1024), amount decimal(17,2),
  amount_forex decimal(17,2), currency varchar(16));
CREATE TABLE IF NOT EXISTS trn_bill (guid varchar(64), ledger varchar(1024), name varchar(1024),
  amount decimal(17,2), billtype varchar(256), bill_credit_period int);
CREATE TABLE IF NOT EXISTS trn_cost_centre (guid varchar(64), ledger varchar(1024), costcentre varchar(1024),
  amount decimal(17,2));
CREATE TABLE IF NOT EXISTS trn_inventory (guid varchar(64), item varchar(1024), quantity decimal(15,4),
  rate decimal(15,4), amount decimal(17,2), additional_amount decimal(17,2), discount_amount decimal(17,2),
  godown varchar(1024), tracking_number varchar(256), order_number varchar(256), order_duedate date);
CREATE TABLE IF NOT EXISTS mst_opening_bill_allocation (ledger varchar(1024), opening_balance decimal(17,4),
  bill_date date, name varchar(1024), bill_credit_period int, is_advance smallint);
CREATE TABLE IF NOT EXISTS trn_closingstock_ledger (ledger varchar(1024), stock_date date, stock_value decimal(17,2));
