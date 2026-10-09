"""Payroll (T-5.PAY) — the run, what it pays and what it withholds."""

from __future__ import annotations

# The modules of this package, and what each owns:
#
# * :mod:`app.payroll.components` (T-5.PAY.01) — the structure: earnings, deductions and
#   contributions as dated, per-company rows, with the statutory ones loaded from the
#   localization pack.
#
# The rest of the phase lands beside it: the engine that computes a run (T-5.PAY.02), loans
# (T-5.PAY.03), statutory reports (T-5.PAY.04), payslips (T-5.PAY.05), bank files
# (T-5.PAY.06) and the posting to the ledger (T-5.PAY.07). Money is an exact decimal
# throughout, as everywhere else in the platform (DOMAIN-MODELS §2).
