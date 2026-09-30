"""Phase 2 §2.3 — supply chain, procurement and payables.

Three streams live here, one package each:

* :mod:`app.procurement` — requisition → RFQ → award → PO → receipt, plus supplier
  data and the tax rules procurement documents apply (`T-2.PROC.*`);
* :mod:`app.ap` — supplier invoices, aging, debit notes, payment runs and the
  payables reconciliation (`T-2.AP.*`);
* :mod:`app.matching` — the 3-way match and its exception workflow (`T-2.MATCH.*`).

Nothing here posts on its own: every module calls
:func:`app.ledger.posting.post_journal_entry` through the account mapping of
T-1.ACCT.03, and stock effects go through :mod:`app.stock`.
"""
