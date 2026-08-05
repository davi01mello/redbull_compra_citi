"""update initial product prices to 8.50

Revision ID: 81330d38373b
Revises: 3289fb2850b3
Create Date: 2026-08-05 09:44:52.510683
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '81330d38373b'
down_revision: Union[str, Sequence[str], None] = '3289fb2850b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        sa.text(
            "UPDATE products SET price = 8.50 "
            "WHERE id IN ('original', 'tropical', 'melancia')"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        sa.text(
            "UPDATE products SET price = 8.00 "
            "WHERE id IN ('original', 'tropical', 'melancia')"
        )
    )
