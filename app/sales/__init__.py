"""Sales — the order-to-cash half of the ERP (§2.5), added by Phase 3.

`customers` is the first module here: the customer role on the shared party
(T-0.PARTY.01), plus the selling-side data a customer owns. The rest of the
phase — the pipeline, quotations, orders, fulfilment and the pricing engine —
lands beside it, and every one of them reads the customer rather than a second
copy of it.
"""
