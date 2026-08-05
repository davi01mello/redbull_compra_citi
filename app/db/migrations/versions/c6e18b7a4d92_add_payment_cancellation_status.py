"""add payment cancellation status

Revision ID: c6e18b7a4d92
Revises: a13f5e7c9b20
Create Date: 2026-08-05
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c6e18b7a4d92"
down_revision: Union[str, Sequence[str], None] = "a13f5e7c9b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'CANCELLED'")
    op.execute("ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'CANCELLED'")


def downgrade() -> None:
    pass
