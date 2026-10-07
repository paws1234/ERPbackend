"""Phase 3 §2.1 — accounts receivable.

:mod:`app.ar.invoices` is the first module here: the customer invoice, its posting
to the receivables control account, and the settlement record every later AR
document moves (a gateway settlement in T-3.AR.05, a receipt at the counter). Aging
(T-3.AR.02), dunning (T-3.AR.04), the control-account reconciliation (T-3.AR.07) and
the credit exposure (T-3.AR.06) read what it records rather than keeping figures of
their own.
"""
