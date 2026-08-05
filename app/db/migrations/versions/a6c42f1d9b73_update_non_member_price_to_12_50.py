"""update non-member price to 12.50

Revision ID: a6c42f1d9b73
Revises: e91a7c4d2f30
Create Date: 2026-08-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6c42f1d9b73"
down_revision: Union[str, Sequence[str], None] = "e91a7c4d2f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE price_tiers
               SET unit_price = 12.50
             WHERE pricing_audience = 'NON_MEMBER'
               AND active = true
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE price_tiers
               SET unit_price = 12.00
             WHERE pricing_audience = 'NON_MEMBER'
               AND active = true
            """
        )
    )
