"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

The template states what every migration in this tree owes a reader: what changed, and
what an operator has to do besides running it. A revision that alters a table the
platform is serving says so here, because the deploy step (T-9.EDGE.01's pipeline) runs
`alembic upgrade head` and nothing else.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
