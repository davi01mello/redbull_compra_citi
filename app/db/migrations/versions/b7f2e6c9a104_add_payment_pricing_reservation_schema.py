"""add payment pricing reservation schema

Revision ID: b7f2e6c9a104
Revises: 07d635433c38
Create Date: 2026-08-04
"""

from datetime import UTC, datetime
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7f2e6c9a104"
down_revision: Union[str, Sequence[str], None] = "07d635433c38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


pricing_audience_enum = postgresql.ENUM(
    "MEMBER",
    "NON_MEMBER",
    name="pricing_audience",
    create_type=False,
)
reservation_status_enum = postgresql.ENUM(
    "ACTIVE",
    "RELEASED",
    "CONSUMED",
    name="reservation_status",
    create_type=False,
)
payment_status_enum = postgresql.ENUM(
    "CREATING",
    "WAITING_PAYMENT",
    "APPROVED",
    "EXPIRED",
    "FAILED",
    "CANCELLED",
    name="payment_status",
    create_type=False,
)
payment_provider_enum = postgresql.ENUM(
    "MOCK",
    "MERCADOPAGO",
    name="payment_provider",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    pricing_audience_enum.create(bind, checkfirst=True)
    reservation_status_enum.create(bind, checkfirst=True)
    payment_status_enum.create(bind, checkfirst=True)
    payment_provider_enum.create(bind, checkfirst=True)

    op.add_column(
        "products",
        sa.Column(
            "reserved_stock",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    op.create_table(
        "price_tiers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pricing_audience", pricing_audience_enum, nullable=False),
        sa.Column("min_quantity", sa.Integer(), nullable=False),
        sa.Column("max_quantity", sa.Integer(), nullable=True),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("min_quantity >= 1", name="ck_price_tiers_min_quantity"),
        sa.CheckConstraint(
            "max_quantity IS NULL OR max_quantity >= min_quantity",
            name="ck_price_tiers_quantity_range",
        ),
        sa.CheckConstraint("unit_price > 0", name="ck_price_tiers_unit_price"),
        sa.PrimaryKeyConstraint("id"),
    )

    price_tiers = sa.table(
        "price_tiers",
        sa.column("pricing_audience", pricing_audience_enum),
        sa.column("min_quantity", sa.Integer()),
        sa.column("max_quantity", sa.Integer()),
        sa.column("unit_price", sa.Numeric(10, 2)),
        sa.column("active", sa.Boolean()),
    )
    op.bulk_insert(
        price_tiers,
        [
            {
                "pricing_audience": "MEMBER",
                "min_quantity": 1,
                "max_quantity": 1,
                "unit_price": 8.50,
                "active": True,
            },
            {
                "pricing_audience": "MEMBER",
                "min_quantity": 2,
                "max_quantity": 3,
                "unit_price": 8.00,
                "active": True,
            },
            {
                "pricing_audience": "MEMBER",
                "min_quantity": 4,
                "max_quantity": None,
                "unit_price": 7.50,
                "active": True,
            },
            {
                "pricing_audience": "NON_MEMBER",
                "min_quantity": 1,
                "max_quantity": None,
                "unit_price": 12.00,
                "active": True,
            },
        ],
    )

    op.add_column(
        "orders",
        sa.Column(
            "pricing_audience",
            pricing_audience_enum,
            server_default=sa.text("'MEMBER'::pricing_audience"),
            nullable=True,
        ),
    )
    op.add_column("orders", sa.Column("price_tier_id", sa.Integer(), nullable=True))
    op.add_column(
        "orders",
        sa.Column(
            "reservation_status",
            reservation_status_enum,
            server_default=sa.text("'RELEASED'::reservation_status"),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_orders_price_tier_id_price_tiers",
        "orders",
        "price_tiers",
        ["price_tier_id"],
        ["id"],
    )

    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", payment_provider_enum, nullable=False),
        sa.Column("provider_order_id", sa.String(length=120), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=120), nullable=True),
        sa.Column("status", payment_status_enum, nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default=sa.text("'BRL'"),
            nullable=False,
        ),
        sa.Column("copy_paste", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_payments_order_id"),
        sa.UniqueConstraint(
            "provider_payment_id",
            name="uq_payments_provider_payment_id",
        ),
    )

    op.create_table(
        "webhook_events",
        sa.Column("provider", payment_provider_enum, nullable=False),
        sa.Column("event_id", sa.String(length=120), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=120), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "provider",
            "event_id",
            name="pk_webhook_events",
        ),
    )

    op.execute(
        sa.text(
            """
            UPDATE orders
            SET pricing_audience = 'MEMBER'::pricing_audience,
                reservation_status = CASE
                    WHEN status = 'PAID'::order_status
                        THEN 'CONSUMED'::reservation_status
                    ELSE 'RELEASED'::reservation_status
                END
            """
        )
    )

    order_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("orders")
    }
    if "pending_items" in order_columns:
        op.execute(
            sa.text(
                """
                INSERT INTO order_items (
                    order_id,
                    product_id,
                    product_name,
                    quantity,
                    unit_price,
                    subtotal
                )
                SELECT
                    legacy_order.id,
                    item->>'product_id',
                    item->>'product_name',
                    (item->>'quantity')::integer,
                    (item->>'unit_price')::numeric(10, 2),
                    (item->>'subtotal')::numeric(10, 2)
                FROM orders AS legacy_order
                CROSS JOIN LATERAL json_array_elements(
                    COALESCE(legacy_order.pending_items, '[]'::json)
                ) AS item
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM order_items AS existing_item
                    WHERE existing_item.order_id = legacy_order.id
                )
                """
            )
        )

    op.execute(
        sa.text(
            """
            WITH order_quantities AS (
                SELECT order_id, SUM(quantity) AS total_quantity
                FROM order_items
                GROUP BY order_id
            )
            UPDATE orders AS orders_to_update
            SET price_tier_id = tier.id
            FROM order_quantities AS quantity,
                 price_tiers AS tier
            WHERE quantity.order_id = orders_to_update.id
              AND tier.pricing_audience = 'MEMBER'::pricing_audience
              AND tier.active = true
              AND quantity.total_quantity >= tier.min_quantity
              AND (
                    tier.max_quantity IS NULL
                    OR quantity.total_quantity <= tier.max_quantity
              )
            """
        )
    )

    unlinked_orders = bind.execute(
        sa.text("SELECT COUNT(*) FROM orders WHERE price_tier_id IS NULL")
    ).scalar_one()
    if unlinked_orders:
        raise RuntimeError(
            f"Backfill could not associate {unlinked_orders} existing order(s) "
            "with a member price tier"
        )

    existing_orders = bind.execute(
        sa.text(
            """
            SELECT id, total, status, copy_paste, created_at, paid_at
            FROM orders
            """
        )
    ).mappings()
    payments = sa.table(
        "payments",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("order_id", postgresql.UUID(as_uuid=True)),
        sa.column("provider", payment_provider_enum),
        sa.column("status", payment_status_enum),
        sa.column("amount", sa.Numeric(10, 2)),
        sa.column("currency", sa.String(3)),
        sa.column("copy_paste", sa.Text()),
        sa.column("approved_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    payment_rows = []
    for order in existing_orders:
        approved = order["status"] == "PAID"
        cancelled = order["status"] == "CANCELLED"
        created_at = order["created_at"] or now
        payment_rows.append(
            {
                "id": uuid4(),
                "order_id": order["id"],
                "provider": "MOCK",
                "status": (
                    "APPROVED"
                    if approved
                    else "CANCELLED"
                    if cancelled
                    else "WAITING_PAYMENT"
                ),
                "amount": order["total"],
                "currency": "BRL",
                "copy_paste": order["copy_paste"],
                "approved_at": order["paid_at"] if approved else None,
                "created_at": created_at,
                "updated_at": order["paid_at"] or created_at,
            }
        )
    if payment_rows:
        op.bulk_insert(payments, payment_rows)

    op.alter_column("orders", "pricing_audience", nullable=False)
    op.alter_column("orders", "reservation_status", nullable=False)


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("payments")
    op.drop_constraint(
        "fk_orders_price_tier_id_price_tiers",
        "orders",
        type_="foreignkey",
    )
    op.drop_column("orders", "reservation_status")
    op.drop_column("orders", "price_tier_id")
    op.drop_column("orders", "pricing_audience")
    op.drop_table("price_tiers")
    op.drop_column("products", "reserved_stock")

    payment_provider_enum.drop(op.get_bind(), checkfirst=True)
    payment_status_enum.drop(op.get_bind(), checkfirst=True)
    reservation_status_enum.drop(op.get_bind(), checkfirst=True)
    pricing_audience_enum.drop(op.get_bind(), checkfirst=True)
