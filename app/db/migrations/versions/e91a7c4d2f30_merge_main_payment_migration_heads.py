"""merge main and Mercado Pago migration heads

Revision ID: e91a7c4d2f30
Revises: c6e18b7a4d92, 81330d38373b
Create Date: 2026-08-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e91a7c4d2f30"
down_revision: Union[str, Sequence[str], None] = (
    "c6e18b7a4d92",
    "81330d38373b",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    orphaned_pending_orders = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM orders AS legacy_order
            WHERE json_array_length(
                    COALESCE(legacy_order.pending_items, '[]'::json)
                  ) > 0
              AND NOT EXISTS (
                    SELECT 1
                    FROM order_items AS migrated_item
                    WHERE migrated_item.order_id = legacy_order.id
              )
            """
        )
    ).scalar_one()
    if orphaned_pending_orders:
        raise RuntimeError(
            "Cannot remove pending_items: legacy order items were not migrated"
        )

    op.execute(
        sa.text(
            """
            UPDATE payments AS payment
            SET status = 'CANCELLED'::payment_status,
                updated_at = CURRENT_TIMESTAMP
            FROM orders AS legacy_order
            WHERE payment.order_id = legacy_order.id
              AND legacy_order.status::text = 'CANCELLED'
              AND payment.status <> 'APPROVED'::payment_status
            """
        )
    )
    op.drop_column("orders", "pending_items")


def downgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "pending_items",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.execute(
        sa.text(
            """
            WITH serialized_items AS (
                SELECT
                    order_id,
                    json_agg(
                        json_build_object(
                            'product_id', product_id,
                            'product_name', product_name,
                            'quantity', quantity,
                            'unit_price', unit_price,
                            'subtotal', subtotal
                        )
                        ORDER BY id
                    ) AS items
                FROM order_items
                GROUP BY order_id
            )
            UPDATE orders AS legacy_order
            SET pending_items = serialized_items.items
            FROM serialized_items
            WHERE serialized_items.order_id = legacy_order.id
            """
        )
    )
    op.alter_column("orders", "pending_items", server_default=None)
