"""Shared SQLAlchemy declarative base — one metadata for every model module.

Every model in this repository inherits from ``Base`` so that a single
``create_all`` / migration covers the whole schema.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base every ERP model inherits from."""
