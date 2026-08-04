"""add pending items and cancelled order status

Revision ID: 3289fb2850b3
Revises: 07d635433c38
Create Date: 2026-08-04 18:34:45.308523
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3289fb2850b3'
down_revision: Union[str, Sequence[str], None] = '07d635433c38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'CANCELLED'")
    op.add_column(
        "orders",
        sa.Column(
            "pending_items",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.alter_column("orders", "pending_items", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("orders", "pending_items")
    op.execute("UPDATE orders SET status = 'PENDING' WHERE status = 'CANCELLED'")
    op.execute("ALTER TYPE order_status RENAME TO order_status_old")
    op.execute("CREATE TYPE order_status AS ENUM ('PENDING', 'PAID')")
    op.execute(
        "ALTER TABLE orders ALTER COLUMN status TYPE order_status "
        "USING status::text::order_status"
    )
    op.execute("DROP TYPE order_status_old")
