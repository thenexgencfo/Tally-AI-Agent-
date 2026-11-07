---
name: tally-domain
description: Tally and Indian accounting knowledge needed to work on the Tally AI Agent: debit/credit sign convention, group hierarchy, vouchers, bills and credit days, opening bills, closing stock, financial years, GST, and the quirks of tally-database-loader's export. Use when writing or debugging report SQL, ingest, or when answers disagree with Tally.
---

# Tally and accounting domain knowledge

## Sign convention (used everywhere in this repo)

In Tally exports, **debit amounts are negative and credit amounts are positive** (`trn_accounting.amount`, `mst_ledger.opening_balance`). So:

- ledger closing = `opening_balance + SUM(amount of its non-cancelled vouchers up to the date)`;
- display: closing < 0 → **Dr**, closing > 0 → **Cr**;
- a voucher's lines sum to zero; a correct trial balance sums to zero.
- Assets and expenses normally end up Dr (negative), liabilities, capital and income Cr (positive).

## Groups

- `mst_group.parent` is `''` for primary groups. `mst_group.primary_group` (loader field `_PrimaryGroup`) is the **top-level** group. For "Sundry Debtors" it is `Current Assets`, NOT `Sundry Debtors`. So never filter on `primary_group` to find debtors. Use `v_ledger_group.group_path` (the lower-cased chain of group names from the ledger's group up to the primary group) and test `'sundry debtors' = ANY(group_path)`. The view walks the hierarchy recursively and survives cycles.
- Groups the reports use by name: `sundry debtors`, `sundry creditors`, `bank accounts`, `bank od a/c`, `bank occ a/c`, `cash-in-hand`, `duties & taxes`, `stock-in-hand`. Party masking also uses `capital account`, `loans (liability)`, `secured loans`, `unsecured loans`, `loans & advances (asset)`, `deposits (asset)`.
- `is_revenue = 1` marks Profit & Loss groups and ledgers (income and expenses); `0` is balance sheet.

## Vouchers, bills, credit days

- `trn_voucher` (header) → `trn_accounting` (ledger lines) → `trn_bill` (bill-wise allocations on party ledgers). The loader's export already **excludes cancelled and optional vouchers**; the mirror keeps the flags at 0.
- Bill types: `New Ref` (an invoice), `Agst Ref` (settles an earlier bill), `Advance` (paid before any invoice), `On Account` (unallocated). A bill's outstanding = sum of its allocations. A **credit** balance on a debtor's bill is an advance and **reduces** what is due; it is never "overdue".
- **Opening bills** (`mst_opening_bill`, from the loader's `mst_opening_bill_allocation`) are dues carried from before the books started; receipts against them would otherwise look like advances.
- **Overdue** = bill age (as-of date minus bill date) greater than the party's credit days (`mst_ledger.credit_days`, loader field `bill_credit_period`). With 0 credit days every unpaid bill is overdue from its date, which is how Tally behaves.
- Ageing buckets: 0-30, 31-60, 61-90, 91-180, 180+, plus `Advance`.

## Financial year and stock

- Indian FY: 1 Apr to 31 Mar. At year end Tally moves P&L into "Profit & Loss A/c", so a trial balance as on a later year-end shows only **that year's** P&L ledgers, with earlier profit carried in "Profit & Loss A/c". `fn_balances` does this, plus stock valuation, so the books balance in every year.
- **Closing stock** is only in the books if the company keeps a value on a Stock-in-Hand ledger (loader table `trn_closingstock_ledger`). Companies that value stock through inventory won't have it: reports then exclude stock and the answer says so (`fn_stock_gap`).
- Opening stock for a period = the closing stock value at the day before it.

## GST

GST ledgers sit under `Duties & Taxes`. `fn_gst_summary` shows the movement per ledger (Output CGST/SGST/IGST, Input credits). It is a ledger summary, **not** a GSTR-1/3B return. Don't present it as filing-ready.

## Identifiers (for masking and tests)

- PAN: 5 letters + 4 digits + 1 letter (`AABCA1234K`). GSTIN: 2 state digits + PAN + entity digit + `Z` + checksum char (`27AABCA1234K1Z5`). IFSC: 4 letters + `0` + 6 alphanumerics. Aadhaar: 12 digits starting 2-9 with a Verhoeff checksum. Mobile: 10 digits starting 6-9, optionally with `+91`/`91`/`0`.
- Amounts display in Indian grouping: `₹1,23,45,678.90`. "Lakh" = 1,00,000 and "crore" = 1,00,00,000; model text using them is rejected by the grounding check.

## tally-database-loader quirks (external project: github.com/dhananjay1405/tally-database-loader)

- Reads Tally through the XML server (default port 9000) with TDL report specs; **Tally Prime only** (not ERP 9); the `batchsize` setting must stay ≤ 10000 or the export hangs; `loadmethod: insert` is required because `file` loading can't reach a database inside Docker.
- Its `schema` config setting is a **database name**, and it creates tables **without a schema prefix**. This repo therefore gives each company its own login whose `search_path` is its own schema (`sql/admin/add_loader_company.sql`).
- The loader's DDL (`database-structure.sql`, mirrored in `sql/loader_schema_pg.sql` for tests) has no company column and no line numbers. `fn_ingest` adds `company_id` and synthesises `line_no` per voucher.
- The incremental export configuration adds `alterid` and `_`-prefixed GUID columns that are not in the published DDL. `fn_ingest` selects columns by name, so extras are harmless, but they must all be built-in types or ingest refuses the schema.
- `party_name` is `varchar(256)`: very long names arrive truncated.
- **Unverified against a real company**: opening bill allocations, closing stock values, and the exact group hierarchy of a real chart of accounts. Always compare a real company's trial balance with Tally's own before trusting reports.
