"""track order webhook resources

Revision ID: a13f5e7c9b20
Revises: f51b9a6d7e20
Create Date: 2026-08-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a13f5e7c9b20"
down_revision: Union[str, Sequence[str], None] = "f51b9a6d7e20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicate_order_ids = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM (
                SELECT provider_order_id
                FROM payments
                WHERE provider_order_id IS NOT NULL
                GROUP BY provider_order_id
                HAVING COUNT(*) > 1
            ) AS duplicates
            """
        )
    ).scalar_one()
    if duplicate_order_ids:
        raise RuntimeError(
            "Cannot enforce provider order uniqueness: duplicate IDs exist"
        )

    op.create_unique_constraint(
        "uq_payments_provider_order_id",
        "payments",
        ["provider_order_id"],
    )
    op.add_column(
        "webhook_events",
        sa.Column("provider_order_id", sa.String(length=120), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE webhook_events AS event
            SET provider_order_id = payment.provider_order_id
            FROM payments AS payment
            WHERE event.provider_payment_id = payment.provider_payment_id
              AND payment.provider_order_id IS NOT NULL
            """
        )
    )
    op.create_index(
        "ix_webhook_events_provider_order_id",
        "webhook_events",
        ["provider_order_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_events_provider_order_id",
        table_name="webhook_events",
    )
    op.drop_column("webhook_events", "provider_order_id")
    op.drop_constraint(
        "uq_payments_provider_order_id",
        "payments",
        type_="unique",
    )
