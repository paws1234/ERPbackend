"""Sales — the order-to-cash half of the ERP (§2.5), added by Phase 3.

`customers` is the first module here: the customer role on the shared party
(T-0.PARTY.01), plus the selling-side data a customer owns.

`pipeline` (T-3.SALES.02) is the opportunity board — configurable stages, recorded
movement, and the one way a won deal becomes a quotation.

`quotations` (T-3.SALES.03) is the priced document: its header, its priced lines with
the rule that produced each price, its validity window, and the re-pricing an expired
offer needs. `orders` is what an accepted quotation becomes — the order and its lines,
carried across without re-keying.

The rest of the phase — the order's credit check and lifecycle, fulfilment, and the
pricing engine — lands beside these, and every one of them reads the customer and the
quotation rather than a second copy of either.
"""
