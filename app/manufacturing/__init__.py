"""Manufacturing (§2.4) — the module's own tables, all company-scoped.

Phase 4 builds the four components §2.4 names: the multi-level bill of materials and
its routing (``bom``), work centres and their capacity (``work_centers``), shop-floor
control (``work_orders``, ``job_cards``, the material issues and the finished-goods
receipts) and the MRP engine (``mrp``).

Every table here carries the company dimension, so T-0.CORE.03's row-level security
isolates it and T-0.AUDIT.02's trail records every change without a line of code for
either: a work order's status transitions are auditable because the table is audited,
not because a module remembered to write a trail row.
"""
