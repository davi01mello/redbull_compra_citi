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
    op.execute("UPDATE orders SET status = 'PENDING' WHERE status = 'CANCELLED'")
    op.execute("UPDATE payments SET status = 'FAILED' WHERE status = 'CANCELLED'")

    op.execute("ALTER TYPE order_status RENAME TO order_status_with_cancelled")
    op.execute("CREATE TYPE order_status AS ENUM ('PENDING', 'PAID')")
    op.execute(
        "ALTER TABLE orders ALTER COLUMN status TYPE order_status "
        "USING status::text::order_status"
    )
    op.execute("DROP TYPE order_status_with_cancelled")

    op.execute("ALTER TYPE payment_status RENAME TO payment_status_with_cancelled")
    op.execute(
        "CREATE TYPE payment_status AS ENUM "
        "('CREATING', 'WAITING_PAYMENT', 'APPROVED', 'EXPIRED', 'FAILED')"
    )
    op.execute(
        "ALTER TABLE payments ALTER COLUMN status TYPE payment_status "
        "USING status::text::payment_status"
    )
    op.execute("DROP TYPE payment_status_with_cancelled")
