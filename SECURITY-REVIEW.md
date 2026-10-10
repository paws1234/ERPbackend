# Security review — T-6.HARD.02

The platform's own account of who may do what, written after reading the boundary rather than
after reading the documentation of it. Every claim below is either read out of the tree or
driven by a request, and `tests/check_security_review.py` re-runs all eight claims against a
scratch PostgreSQL 16 — a claim this document makes and the check cannot reproduce is a claim
that does not belong here.

**What was reviewed.** The request boundary: `app/api.py`'s identity dependency and error
shape, `app/security.py` (capabilities, refusals, field restrictions), the row-level scoping in
`app/db.py`, the credential handling in `app/integrations.py`, and every route the app serves —
59 method/route pairs, listed in full below with the capability guarding each. Read as a whole,
the review covers the boundary, authorisation, isolation, the credential path and the two
placements of a permission check; it does not cover the network, the browser or the dependencies
(see *What this review does not cover*).

**How it was done.** The access matrix was generated from the running app rather than written by
hand: the sweep walks `app.routes`, reads each endpoint's own source for its `require(...)`, and
follows one level into a shared helper where an endpoint asks through one (the portal's
`_portal_scope`). A route whose guard is in neither place and which is not named public is a
failure of the check, not a footnote in this document. Isolation, the field levels, the
credential path and the idempotency key were then exercised by request against two companies and
two suppliers.

## The access matrix

Every method/route pair the app serves, with the capability its handler asks for and the function
that answers it. An **empty capability cell** means the route carries no permission: the only such
route is the liveness probe, and the routes that are public besides it (the published contract and
the generated reference) are FastAPI's own, not `APIRoute`s, so they are named with their reason in
:data:`PUBLIC` in the check.

| Route | Capability | Answered by |
|-------|------------|-------------|
| `GET /api/v1/account-mappings` | account.read | `list_account_mappings` |
| `PUT /api/v1/account-mappings/{key}` | account.write | `put_account_mapping` |
| `POST /api/v1/accounts` | account.write | `new_account` |
| `POST /api/v1/accounts/import-coa` | account.write | `import_coa` |
| `GET /api/v1/accounts/tree` | account.read | `accounts_tree` |
| `GET /api/v1/companies/current` | company.read | `current_company` |
| `POST /api/v1/companies/current/credit-check-mode` | company.configure | `set_company_credit_check_mode` |
| `POST /api/v1/coupons` | pricing.configure | `create_coupon` |
| `POST /api/v1/coupons/{code}/redeem` | order.write | `redeem` |
| `GET /api/v1/currencies` | currency.read | `list_currencies` |
| `POST /api/v1/currencies` | currency.write | `new_currency` |
| `GET /api/v1/fx-rates` | fx.read | `get_fx_rate` |
| `PUT /api/v1/fx-rates` | fx.write | `put_fx_rate` |
| `GET /api/v1/health` |  | `health` |
| `GET /api/v1/journal-entries` | journal.read | `list_entries` |
| `POST /api/v1/journal-entries` | journal.post | `post_entry` |
| `POST /api/v1/opportunities` | opportunity.write | `new_opportunity` |
| `POST /api/v1/opportunities/{opportunity_id}/loss` | opportunity.write | `lose_opportunity_card` |
| `POST /api/v1/opportunities/{opportunity_id}/moves` | opportunity.write | `move_opportunity_card` |
| `POST /api/v1/opportunities/{opportunity_id}/quotation` | opportunity.write, quotation.write | `convert_opportunity_card` |
| `GET /api/v1/org-chart` | employee.read | `org_chart_view` |
| `GET /api/v1/pipeline/board` | pipeline.read | `pipeline_board_view` |
| `POST /api/v1/pipeline/stages` | pipeline.configure | `new_pipeline_stage` |
| `GET /api/v1/portal/documents` | portal.supplier (via _portal_scope) | `portal_view` |
| `POST /api/v1/portal/invoices` | portal.supplier (via _portal_scope) | `portal_invoice` |
| `POST /api/v1/portal/orders/{number}/acknowledge` | portal.supplier (via _portal_scope) | `portal_acknowledge_order` |
| `POST /api/v1/portal/rfqs/{number}/responses` | portal.supplier (via _portal_scope) | `portal_rfq_response` |
| `POST /api/v1/pos/cash-drawer-policy` | company.configure | `set_pos_cash_drawer_policy` |
| `POST /api/v1/pos/drawer-movements` | pos.drawer | `record_drawer_movement` |
| `GET /api/v1/pos/reconciliation` | pos.read | `pos_reconciliation` |
| `POST /api/v1/pos/sales` | pos.sell | `open_pos_sale` |
| `POST /api/v1/pos/sales/{number}/complete` | pos.sell | `complete_pos_sale` |
| `GET /api/v1/pos/sales/{number}/receipt` | pos.read | `pos_receipt` |
| `POST /api/v1/pos/sales/{number}/scan` | pos.sell | `scan_pos_sale` |
| `POST /api/v1/pos/sales/{number}/tender` | pos.sell | `tender_pos_sale` |
| `POST /api/v1/pos/sales/{number}/void` | pos.sell | `void_pos_sale` |
| `POST /api/v1/pos/shifts` | pos.shift | `open_pos_shift` |
| `GET /api/v1/pos/shifts/current` | pos.read | `current_pos_shift` |
| `POST /api/v1/pos/shifts/{shift_id}/close` | pos.shift | `close_pos_shift` |
| `POST /api/v1/pos/sync` | pos.sell | `sync_pos_sales` |
| `GET /api/v1/pos/sync/reports` | pos.read | `list_pos_sync_reports` |
| `GET /api/v1/pos/z-reports/day` | pos.read | `pos_day_report` |
| `GET /api/v1/pos/z-reports/shift/{shift_id}` | pos.read | `pos_shift_report` |
| `POST /api/v1/price-rules` | pricing.configure | `create_price_rule` |
| `POST /api/v1/price-rules/resolve` | order.read | `resolve_price_rule` |
| `POST /api/v1/quotations` | quotation.write | `new_quotation` |
| `GET /api/v1/quotations/{number}` | quotation.read | `read_quotation` |
| `POST /api/v1/quotations/{number}/order` | quotation.write, order.write | `order_from_quotation` |
| `POST /api/v1/quotations/{number}/reprice` | quotation.write | `reprice` |
| `GET /api/v1/dashboard` |  | `read_dashboard` |
| `GET /api/v1/reports/catalogue` | report.read | `read_report_catalogue` |
| `POST /api/v1/reports` | report.configure | `register_report_definition` |
| `POST /api/v1/reports/{code}/run` |  | `run_report_now` |
| `POST /api/v1/rfqs` | rfq.post | `new_rfq` |
| `GET /api/v1/rfqs/{number}` | rfq.read | `read_rfq` |
| `POST /api/v1/rfqs/{number}/responses` | rfq.post | `record_rfq_answer` |
| `GET /api/v1/sales-orders/{number}` | order.read | `sales_order` |
| `POST /api/v1/sales-orders/{number}/confirm` | order.write | `confirm_sales_order` |
| `POST /api/v1/sales-orders/{number}/pick-list` | order.write | `create_order_pick_list` |
| `POST /api/v1/sales-orders/{number}/pick-list/lines/{line_no}` | order.write | `record_order_pick` |
| `POST /api/v1/sales-orders/{number}/shipments` | order.write | `ship_sales_order` |

Two rows need their reason stated rather than left to the table:

* `GET /api/v1/health` carries no capability: it reports whether the process is up and nothing
  about the data, so it is the one route that may be reached without a role.
* `GET /api/v1/dashboard` carries no capability of its own either: each tile names the
  capability that guards it (`report.read`, `invoice.read`, `stock.read`) and the dashboard asks
  for it **tile by tile**, so there is no single capability a request to it could ask for. That
  is why it is in the exception table and driven like the rest: a caller holding nothing is shown
  **no tile** — not a zeroed figure, not a blanked tile — and every one of them is named in
  `withheld` with the refusal, which is on the trail. `tests/check_dashboard.py` proves the same
  thing from the other side: what a withheld tile does not leak is its figure, its aggregate and
  its rows.
* `POST /api/v1/reports/{code}/run` carries no capability of its own: the capability is a column
  on the report **definition** (T-0.REPORT.01) and the reporting framework asks for it before it
  builds anything. The check drives it — a subject holding nothing is refused `403` naming
  `journal.post` for a definition that carries it, and the permitted caller is served — because a
  guard that lives in another module is exactly where a hole would hide.

## What the review verified

Run 2026-09-17 with `DATABASE_URL=postgresql+psycopg://…@127.0.0.1:5432/erpv1` against scratch
PostgreSQL 16, `tests/check_security_review.py`, exit 0, green on all eight:

1. the sweep found **no unguarded route**: 59 method/route pairs, each naming a capability (21
   capabilities in use), with the public routes named and their reasons given;
2. the matrix above **is** the app's matrix — a route added, removed or re-guarded without this
   document fails the check;
3. field restrictions hold on read and write: a restricted reader's payload has the field
   **absent** (`{account, credit, debit, line_no}` remain, `party` is gone), a write naming it is
   refused `403` with `journal_line.party` named, and the refused write stored nothing;
4. isolation holds by request: another company's ledger list is empty of this company's postings,
   supplier B sees none of supplier A's RFQs and is refused answering one
   (`NotInvitedError`);
5. a configured integration token reaches the transport through `endpoint_for` and appears in
   **no** delivery-log payload, **no** destination and no response body;
6. an idempotency key is matched on its method, path and actor as well as its body (see finding
   1);
7. every route whose capability is asked for outside its own body was driven by request — four
   portal endpoints and the report run — each refusing a subject holding nothing and serving the
   permitted caller;
8. **7 findings**, each stating `remediated` or `accepted risk` with its reason.

## Findings

| Finding | Disposition | Why |
|---------|-------------|-----|
| The idempotency replay matched on the company and the key alone: `api_idempotency_key`'s `method`, `path` and new `actor` columns are written and were never read back, so a key presented at another endpoint, or by another caller, replayed the stored answer — including a document the presenter need never have been allowed to read (`pos.sell` does not imply `journal.read`) | remediated | the lookup now matches the method, the path and the actor that wrote the key, and the writer is recorded on the row (`app/api.py`); the check drives both cases — the writer's own retry still replays (`Idempotent-Replay: true`), another caller presenting the key gets `409 idempotency_key_reused`, and a key planted for `/pos/sync` with this body's own fingerprint is refused rather than answering the journal request |
| Actor identity arrives as `X-Actor` and the company as `X-Company-Id`, so any caller may claim any actor or company; a request with no `X-Actor` is the actor `unknown` | accepted risk | stated at the top of `app/api.py`: the headers are the boundary's identity until T-0.SEC.01's successor supplies authenticated claims, and the deployment puts the API behind the proxy that does so. Isolation underneath is the database's (row-level security on `company_id`), so a claimed company cannot read another's rows — but it can name which company it acts for. An install that assigns a role to the subject `unknown` would hand it to every anonymous caller: do not |
| Every integration credential for the install lives in one `INTEGRATION_ENDPOINTS` value: one leaked value is every channel | accepted risk | one setting for one install is the shape T-0.INT.01 asked for, and a single-tenant deployment that can read the process environment can read all of them anyway; splitting it would add a setting per channel without changing what a compromised process gets |
| The portal's payloads are not passed through `readable_fields`: a field restricted for staff is still visible to the supplier whose document it is | accepted risk | deliberate, and said so in `app/procurement/portal.py`: the portal builds its own explicit projection of the documents that belong to the account (its RFQs, its released orders, its invoices) instead of serialising rows, so a restriction added for staff neither leaks through it nor silently hides a supplier's own document from them. The restriction tool is a staff-facing control; a supplier seeing its own data is not the disclosure it guards against |
| No rate limiting or throttling anywhere in the API, and none in the deployment stack | accepted risk | the plan names none, nothing in the platform is billed per call, and a limit belongs at the edge: the stack's reverse proxy (T-0.DEPLOY.03) sees every request before the app does. Recorded here so that the absence is a decision rather than an oversight — a public-facing install is expected to add it there |
| A report's built payload is not filtered by field restrictions; the definition's capability is the whole guard | accepted risk | the reporting framework aggregates rows into a shape of its own (T-0.REPORT.01), so there is no one entity whose field list applies; the guard is the capability on the definition, which is what decides who may see the report. A definition's capability is free text written by a caller holding `report.configure`, which fails closed on a typo (the capability is then held by nobody) and cannot escalate: the framework asks the *caller* for it |
| The supplier portal is served from the same host and application as the staff endpoints | accepted risk | the portal is a route family, not a second deployment: the sweep (claim 1) shows every other route names a staff capability, and a supplier subject holds `portal.supplier` alone, so it is refused everywhere else — driven, not assumed (claim 7 refuses it `403` naming `portal.supplier` on all four portal endpoints and the report run). Splitting hosts would duplicate the boundary without changing what a subject may reach |

## What this review does not cover

* **The network and the platform.** TLS, the reverse proxy, host names and network policy are the
  deployment stack's (T-0.DEPLOY.03); this review reads the application boundary only.
* **Dependencies.** No dependency scanner is installed in the repository, so no third-party
  advisory list was consulted. `requirements.txt` pins by version; an install that wants advisory
  coverage needs the scanner, not this document.
* **Authentication.** No provider, session mechanism or token format is chosen here — the same
  stance `app/security.py` states. `X-Actor` is an assertion, and finding 2 records it as such.
* **The browser and the frontend.** The frontend calls the API from its server, so no CORS
  middleware is configured and no browser talks to the API directly; the frontend's own rendering
  of what a caller may see is not a control (the API refuses, it does not hide).
* **Load and resource exhaustion.** T-6.HARD.01 measures throughput and latency; this review
  found no resource-exhaustion control to measure, and finding 5 records that absence.
