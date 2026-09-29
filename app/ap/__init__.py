"""Phase 2 §2.1 — accounts payable.

One module here so far: :mod:`app.ap.invoices`, which owns the supplier invoice, its
posting to the payables control account, and the settlement record every later AP
document moves (a debit note in T-2.AP.03, a payment in T-2.AP.04). Aging
(T-2.AP.02), payment batches (T-2.AP.04) and the control-account reconciliation
(T-2.AP.05) read what it records rather than keeping figures of their own.
"""
