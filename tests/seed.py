"""Shared seeding for the checks — **not** a check itself.

The pipeline runs `tests/check_*.py`; this file is a helper they import.

Every check builds its own scratch schema and its own company. What several of
them also need, since T-1.ACCT.01, is a **chart of accounts**: a posting line
states an account *code* (DOMAIN-MODELS.md §3) and the posting primitive resolves
it to a real account row, so a check that posts must first have the accounts it
posts to. The codes below are the Philippines pack's own (T-0.LOC.01), with the
classes and parents a real installation has, so nothing here is invented for the
tests' convenience.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.ledger.accounts import Account, create_account
from app.ledger.mapping import set_mapping

# code -> (name, class, parent code) — the pack's own rows, keyed by the codes the
# checks post with.
USUAL_ACCOUNTS: dict[str, tuple[str, str, str | None]] = {
    "1000": ("Cash on Hand", "asset", None),
    "1010": ("Cash in Bank", "asset", None),
    "1020": ("Petty Cash", "asset", "1010"),
    "1100": ("Accounts Receivable", "asset", None),
    "1200": ("Inventory", "asset", None),
    "1210": ("Inventory — Raw Materials", "asset", "1200"),
    "2000": ("Accounts Payable", "liability", None),
    "3000": ("Capital Stock", "equity", None),
    "4000": ("Sales Revenue", "income", None),
    "4100": ("Service Revenue", "income", None),
    "4910": ("Foreign Exchange Gain", "income", None),
    "5000": ("Cost of Goods Sold", "expense", None),
    "5200": ("Rent Expense", "expense", None),
    "5900": ("Inventory Shrinkage and Adjustments", "expense", None),
    "5990": ("Foreign Exchange Loss", "expense", None),
}

# The account mappings a stock movement posts through (T-1.INV.07): the inventory
# account and one counterpart per kind of movement. A company points them at its
# own accounts; these are the pack's.
STOCK_MAPPINGS = {
    "inventory": "1200",
    "stock_receipt": "2000",
    "stock_issue": "5000",
    "stock_adjustment": "5900",
}


def seed_stock_accounts(session: Session, *, company_id: uuid.UUID) -> dict[str, Account]:
    """The usual chart plus the mappings a stock movement needs to post (T-1.INV.07)."""
    accounts = seed_accounts(session, company_id=company_id)
    for key, code in STOCK_MAPPINGS.items():
        set_mapping(session, company_id=company_id, key=key, account_code=code)
    return accounts


def seed_accounts(
    session: Session, *, company_id: uuid.UUID, codes: list[str] | None = None
) -> dict[str, Account]:
    """Create the named accounts (the usual ones by default) and return them by code.

    Parents are created before their children, which is the only order the tree
    rule allows.
    """
    wanted = list(codes or USUAL_ACCOUNTS)
    # parents first, so a child's parent already exists when it is created
    ordered = sorted(wanted, key=lambda code: (USUAL_ACCOUNTS[code][2] is not None, code))
    made: dict[str, Account] = {}
    for code in ordered:
        name, account_class, parent = USUAL_ACCOUNTS[code]
        made[code] = create_account(
            session,
            company_id=company_id,
            code=code,
            name=name,
            account_class=account_class,
            parent_id=made[parent].id if parent else None,
        )
    return made
