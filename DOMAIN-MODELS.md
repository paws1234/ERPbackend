# Phase 1 Domain Models & API Contracts — Decision Record

- **Task**: `T-0.MODELS.01` (Phase 0) · Plan §5 (core entities), §7.2
- **Decided**: 2026-09-17
- **Repository**: backend (`ERPbackend`), per the ledger's `## Repositories` map
- **Consumers**: `T-1.ACCT.01`–`T-1.ACCT.07`, `T-1.INV.01`–`T-1.INV.09`. Those tasks **implement this document**; they do not re-decide the shapes in it. `T-0.MODELS.01`'s evidence is that this document is used unmodified by them.
- **Bound by**: plan §1 principles, §2.1, §2.2, §3, §5, §8 defaults, and the ledger's Variables table.

## 1. What this fixes, and what it does not

**In** — the Phase 1 entities of §5: `Account`, `Journal Entry / GL Transaction`, `Item`, `Item Variant`, `UOM Conversion`, `Warehouse / Location`, `Stock Ledger Entry`; their fields, relationships, cardinality, invariants and input/output contracts; and the semantics of the two hierarchies and of UOM conversion.

**Owned elsewhere — not decided here:**

| Not here | Owner |
|---|---|
| URL versioning, one error shape, pagination/filtering, auth claims, idempotency keys, how a request names its company | `T-0.API.01` |
| The `Party` master — a journal line *links* to a party; that table is not defined here | `T-0.PARTY.01` |
| Append-only enforcement machinery and the exact soft-delete column | `T-0.AUDIT.01`, `T-0.AUDIT.02` |
| Period locking, FX sync service, gain/loss, statements | `T-1.ACCT.04`–`T-1.ACCT.07` |
| Valuation maths, count/adjustment workflow, GL posting wiring | `T-1.INV.04`, `T-1.INV.06`, `T-1.INV.07` |
| Procurement, sales, POS, manufacturing, HR entities | Phases 2–6 — **not defined here at all** |

## 2. Rules every Phase 1 entity obeys

1. **Company dimension.** Every Phase 1 table carries `company_id uuid NOT NULL → company.id`. None is global and none is a child of another company-scoped table, so all of them are row-level-secured by `app/db.py`, which also fails the build for a table that lacks the dimension.
2. **Masters are soft-deleted; ledgers are append-only.** `journal_entry`, `journal_line` and `stock_ledger_entry` are never updated or deleted — a correction is a new entry. Masters (`account`, `item`, `item_variant`, `item_barcode`, `uom_conversion`, `location`) carry the soft-delete column fixed by `T-0.AUDIT.01`, and reads exclude deleted rows unless explicitly asked.
3. **Exact decimals, never float.** Money, quantities and rates are `numeric` (`MONEY` = `numeric(20,6)` in `app/ledger/posting.py`), on the wire as well as in the database: a JSON **string**, not a JSON number.
4. **Identifiers and time.** `uuid` primary keys, server-generated. `date` is ISO `YYYY-MM-DD`; `timestamptz` is ISO 8601 UTC.
5. **Enumerations** are lower-case snake_case strings validated at the boundary; an unknown value is refused, never coerced to a default.
6. **A document and its postings commit together.** A movement and the journal entry it produces are one transaction (`T-1.INV.07`); a refused document leaves neither behind.

## 3. Account — chart of accounts (tree)

**Table** `account`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `company.id` |
| `code` | varchar(32) | no | unique per company — what a posting line states |
| `name` | varchar(128) | no | |
| `class` | varchar(16) | no | `asset` \| `liability` \| `equity` \| `income` \| `expense` |
| `parent_id` | uuid | yes | FK `account.id`, same company |
| `deleted_at` | timestamptz | yes | convention of `T-0.AUDIT.01` |

**Cardinality**: `account 1 — 0..* account` (self-referencing, arbitrary depth).

**Invariants**
- A child's `class` equals its parent's. Reparenting across classes is refused.
- Unique `(company_id, code)`; no cycles in the tree.
- An account referenced by a journal line cannot be reparented into another class.
- A parent with active children cannot be soft-deleted.
- The five classes are the plan's (§2.1). Which accounts a locale seeds is the pack's business (`T-0.LOC.01` / `T-1.ACCT.01`), not this document's.

**Input** (create) `{ code, name, class, parent_id? }` · (update) `{ name?, parent_id? }` — `code` is immutable once a posting references it.
**Output**: the columns above; `deleted_at` is not returned on a normal read.

## 4. Journal Entry / GL Transaction

Two tables, already implemented in `app/ledger/posting.py` by `T-0.CORE.01`. The shape below is the **Phase 1 target**; `T-1.ACCT.01` converts the two link columns into foreign keys and `T-1.ACCT.02`/`T-1.ACCT.05` add source linking and the rate.

**Table** `journal_entry`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `company.id` |
| `posting_date` | date | no | §2.1's **posting date**; decides the period (`T-1.ACCT.04`) and which FX rate applies (`T-1.ACCT.05`) |
| `currency` | char(3) | no | the **transaction currency** (`transaction_currency`); equals the company's `base_currency` unless a foreign document states otherwise |
| `exchange_rate` | numeric(20,10) | no | 1 for a base-currency entry; added by `T-1.ACCT.05` |
| `memo` | text | yes | |
| `source_type` | varchar(32) | yes | the document type that produced the entry |
| `source_id` | uuid | yes | that document's id — together, `T-1.ACCT.02`'s drill-down |
| `created_at` | timestamptz | no | |

**Table** `journal_line`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `entry_id` | uuid | no | FK `journal_entry.id` — no `company_id` of its own (`CHILD_TABLES` in `app/db.py` isolates it through its parent) |
| `line_no` | integer | no | 1-based, the order the caller stated |
| `account_id` | uuid | no | FK `account.id` — §2.1's **account** link. Today the column holds the account *code* as a string; `T-1.ACCT.01` owns the conversion |
| `debit` | numeric(20,6) | no | ≥ 0, and never non-zero together with `credit` |
| `credit` | numeric(20,6) | no | ≥ 0, and never non-zero together with `debit` |
| `party_id` | uuid | yes | FK `party.id` — §2.1's **party link**, where the posting is attributable to one. The `Party` table is `T-0.PARTY.01`'s; today the column holds a string reference |

**Cardinality**: `journal_entry 1 — 2..* journal_line`. Fewer than two lines is not a posting, and the deferred constraint triggers refuse it at COMMIT even for a writer that bypasses the primitive.

**§2.1's five fields land exactly so**: *posting date* on the entry; *account, debit, credit* and the *party link* on the line. Amounts are stated in the entry's transaction currency. The base-currency amount is `amount × exchange_rate`, computed exactly and rounded only when displayed, so rounding cannot unbalance a posting that balances in its transaction currency.

**Input** — one interface only, `post_journal_entry(session, company_id=…, posting_date=…, currency=…, exchange_rate=…, memo=…, lines=[{ account_id, debit, credit, party_id? }, …])`. Nothing else writes these two tables (`T-1.ACCT.03` publishes it and `T-0.CORE.02`'s ledger gate proves no second writer).
**Output**: the entry with its lines in `line_no` order.

**Refused**: a line with both sides non-zero, a negative amount, a zero-amount line (the table's check constraints); fewer than two lines or `debit ≠ credit` (`UnbalancedEntryError` from the primitive, and the database's own triggers behind it).
**Immutable**: no update, no delete. A correction is a new entry.

## 5. Item, Item Variant, UOM Conversion, barcode

### 5.1 Item — **table** `item`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `company.id` |
| `sku` | varchar(64) | no | unique per company |
| `name` | varchar(128) | no | |
| `base_uom` | varchar(16) | no | the UOM every stored quantity is expressed in |
| `traceability_mode` | varchar(16) | no | `none` \| `batch_lot` \| `serial`. **No default** — plan §8 leaves it "Not stated", so creating an item states it |
| `deleted_at` | timestamptz | yes | |

**Cardinality**: `item 1 — 0..* item_variant`; `item 1 — 0..* item_barcode`; `item 1 — 0..* uom_conversion`.
**Invariants**: `base_uom` is immutable once the item has movements; `sku` is unique per company.
**No `costing_method` column here**: the decided `valuation_scope` is **per company**, so the method is resolved on `company` (`costing_method`, default `moving_average`), added by `T-1.INV.04`. Because the stock ledger is append-only, changing the method never rewrites what was already valued.

**Input** (create) `{ sku, name, base_uom, traceability_mode, barcodes?: [{ value, symbology }], uom_conversions?: [{ from_uom, to_uom, factor }] }` · (update) `{ name? }` — `sku`, `base_uom` and `traceability_mode` are immutable once the item has movements.
**Output**: the columns above, with its variants, barcodes and conversions when the caller asks for them; offer/stock figures are other tasks' reads, not this entity's.

### 5.2 Item Variant — **table** `item_variant`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `company.id` |
| `item_id` | uuid | no | FK `item.id` |
| `sku` | varchar(64) | no | unique per company — the variant is what is stocked and sold |
| `attributes` | jsonb | no | the attribute combination that distinguishes it (`item_variant_attributes`) |
| `deleted_at` | timestamptz | yes | |

**Invariants**: unique `(item_id, attributes)` — one row per combination, so a wider matrix never means duplicate SKUs. An item whose attribute set is empty (the default) has no variant rows at all.
**Cardinality**: `item_variant 1 — 0..* stock_ledger_entry`.
**Input**: `{ item_id, sku, attributes }` — `attributes` must be the attribute set the item declares. **Output**: the columns above.

### 5.3 Barcode — **table** `item_barcode` (part of the Item entity, §2.2 "barcode/QR")

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `company.id` |
| `item_id` | uuid | no | FK `item.id` |
| `variant_id` | uuid | yes | FK `item_variant.id`, when the code identifies a variant |
| `value` | varchar(64) | no | the encoded value |
| `symbology` | varchar(8) | no | `ean` \| `upc` \| `qr` — §8's decided symbologies |

**Invariant**: `value` is unique **across the installation**, not merely per company, so a scan resolves to exactly one item or variant. An item may carry several codes (EAN/UPC **and** QR).
**Input**: `{ item_id, variant_id?, value, symbology }` · **Output**: the columns above, and on a scan the one item (or variant) the value resolves to.

### 5.4 UOM Conversion — **table** `uom_conversion`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `company.id` |
| `item_id` | uuid | no | FK `item.id` |
| `from_uom` | varchar(16) | no | |
| `to_uom` | varchar(16) | no | |
| `factor` | numeric(20,6) | no | `CHECK (factor > 0)` |

**Semantics — the whole of it, so nothing is left to interpretation:**
- **Direction**: 1 `from_uom` = `factor` × `to_uom`. A row is stored in one direction only; the reverse is `1/factor`, and (from_uom, to_uom) is unique per item.
- **Scope**: per item, never global. Two items may convert the same pair differently.
- **Base UOM**: the item's `base_uom` needs no row — its factor is 1 by definition. `from_uom = to_uom` is refused.
- **Composition, not transitivity assumptions**: `base → box → pallet` is two rows; a conversion walks the chain and multiplies. A pair with no path to the base UOM is refused rather than treated as 1:1.
- **Exactness**: quantities are `numeric(20,6)`, so a round trip (`a→b→a`) must return the original quantity exactly at that scale. A factor that cannot round-trip at scale 6 is refused, not silently rounded.
- **Where it is applied**: at the edge of a stock movement. The transaction input states the UOM and quantity the user gave; the ledger stores the quantity converted to the item's `base_uom`, so every stored quantity is comparable without re-reading the factors.
- **Refused**: `factor <= 0` (database check), `from_uom = to_uom`, a duplicate direction.

**Input**: `{ item_id, from_uom, to_uom, factor }` · **Output**: the columns above, and on a conversion request the resulting quantity at the target UOM.

## 6. Warehouse / Location — hierarchy

**Table** `location`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `company.id` |
| `code` | varchar(32) | no | unique per company |
| `name` | varchar(128) | no | |
| `type` | varchar(16) | no | `warehouse` \| `zone` \| `aisle` \| `bin` |
| `parent_id` | uuid | yes | FK `location.id`, same company |
| `deleted_at` | timestamptz | yes | |

**Semantics** (`warehouse_hierarchy_levels`, §2.2's named four): a `warehouse` has no parent; a `zone`'s parent is a `warehouse`; an `aisle`'s parent is a `zone`; a `bin`'s parent is an `aisle`. A level cannot be skipped and a type cannot be nested under the wrong parent type.
**Cardinality**: `location 1 — 0..* location`; `location 1 — 0..* stock_ledger_entry`.
**Invariants**: parent and child are in the same company; no cycles; a stock movement references a **leaf** (`type = 'bin'`) and referencing a non-leaf is refused; a location holding stock cannot be removed; the tree is retrievable per company.
**Input** (create) `{ code, name, type, parent_id? }` · (update) `{ name?, parent_id? }` — a reparent must keep the level rule, and a location with stock cannot be moved or removed.
**Output**: a location row, or the whole tree for the company with each node's type and parent.

## 7. Stock Ledger Entry — quantity + value, append-only

**Table** `stock_ledger_entry`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `company.id` |
| `item_id` | uuid | no | FK `item.id` |
| `variant_id` | uuid | yes | FK `item_variant.id`; required when the item has variants |
| `location_id` | uuid | no | FK `location.id` — a leaf |
| `quantity` | numeric(20,6) | no | **signed**, in the item's `base_uom`: positive is received into the location, negative issued out of it |
| `value` | numeric(20,6) | no | **signed**, same sign as `quantity`, stated in `currency` |
| `currency` | char(3) | no | the transaction currency of `value` (`transaction_currency`) |
| `batch_id` | uuid | yes | FK to the `batch` table of `T-1.INV.08` |
| `serial_id` | uuid | yes | FK to the `serial` table of `T-1.INV.09` |
| `source_type` | varchar(32) | no | the document type that caused the movement |
| `source_id` | uuid | no | that document's id |
| `posting_date` | date | no | the date the movement counts from |
| `created_at` | timestamptz | no | |

**Cardinality**: `item 1 — 0..* stock_ledger_entry`; `item_variant 1 — 0..*`; `location 1 — 0..*`. **One row per movement** — no update to an existing row, ever.

**Invariants**
- A movement without a source document is refused: `source_type` and `source_id` are both required.
- On-hand quantity and value for an item at a location are the **sum** of its entries. Phase 1 keeps no balance table, so the ledger is the only source of truth.
- Rows are signed, but the **sum** may not go below zero: `allow_negative_stock` is `false`, so an issue that would drive a location negative is refused by the movement (`T-1.INV.05`), and the ledger never records a consequence of one.
- `batch_id` is required on every movement of a `batch_lot` item, `serial_id` on every movement of a `serial` item, and neither applies to a `none` item. Enforcing that per movement is `T-1.INV.08` / `T-1.INV.09`; **fixing the two columns here** is what let traceability move into Phase 1, because an entry's shape is cheap to fix before it has rows and a migration afterwards (§4 Phase 1, amended 2026-09-17).

**Input** (a stock transaction, e.g. `T-1.INV.05`): `{ item_id, variant_id?, location_id, uom, quantity, value, currency, source_type, source_id, posting_date, batch_id?, serial_id? }` — `uom` and `quantity` are converted to the item's base UOM before the entry is written; `quantity` is signed by the transaction type, not by the caller.
**Output**: the columns above, per entry, plus the derived sums an item/location read reports.

## 8. What Phase 1 does not define

- No purchase, sales, work order, BOM, operation, employee, attendance, leave or payroll entity. A document is referenced by the opaque `(source_type, source_id)` pair until its own phase defines it.
- The `batch` and `serial` **tables** are `T-1.INV.08` / `T-1.INV.09`'s; this document fixes only their columns on the stock ledger entry and when they are required.
- No valuation algorithm, no statements, no count/adjustment workflow, no GL reconciliation — `T-1.INV.04`, `T-1.INV.06`, `T-1.INV.07`, `T-1.ACCT.07`.

## 9. Values deliberately left to their owner

| Value | Owner | Why not fixed here |
|---|---|---|
| Which accounts the CoA template seeds | `T-0.LOC.01` / `T-1.ACCT.01` | The five classes are the plan's; the accounts are the Philippines pack's |
| `costing_method` column and its scope resolution | `T-1.INV.04` | §8 decides the value (Moving Average) and the scope (per company); the column is that task's |
| `fiscal_year_start_month` | confirmed with the pack in `T-0.LOC.01` | Already implemented on `company`; still undecided per plan §8 — not to be assumed here |
| How a request names its company | `T-0.API.01` | The company dimension is every table's; naming it on the wire is the API convention's |
