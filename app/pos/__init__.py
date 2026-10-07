"""Phase 3 §2.5 — point of sale.

:mod:`app.pos.sales` is where a sale happens: the till scans a barcode, the
T-3.SALES.06 pricing engine says what it costs, the pack says what tax is due, and
the completed sale posts its revenue and issues its stock. It is the one place in
this platform that sells retail, so it reuses the documents the rest of the platform
already has rather than keeping a second copy of any of them.

The rest of the phase — the drawer and its tenders (T-3.POS.02), the shift
(T-3.POS.03), the Z-Report (T-3.POS.04) and the reconciliation against the GL and
the stock ledger (T-3.POS.05) — reads what a sale records.
"""
