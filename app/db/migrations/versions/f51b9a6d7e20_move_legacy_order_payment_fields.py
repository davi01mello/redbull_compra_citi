"""move legacy order payment fields

Revision ID: f51b9a6d7e20
Revises: d4296c5f08c1
Create Date: 2026-08-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f51b9a6d7e20"
down_revision: Union[str, Sequence[str], None] = "d4296c5f08c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    missing_payments = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM orders AS o
            LEFT JOIN payments AS p ON p.order_id = o.id
            WHERE p.id IS NULL
            """
        )
    ).scalar_one()
    if missing_payments:
        raise RuntimeError(
            f"Cannot remove legacy fields: {missing_payments} order(s) "
            "do not have a backfilled payment"
        )

    op.add_column("payments", sa.Column("qr_code", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE payments AS p
            SET provider_order_id = COALESCE(p.provider_order_id, o.txid),
                copy_paste = COALESCE(p.copy_paste, o.copy_paste),
                qr_code = o.qr_code
            FROM orders AS o
            WHERE p.order_id = o.id
            """
        )
    )

    incomplete_payments = bind.execute(
        sa.text("SELECT COUNT(*) FROM payments WHERE qr_code IS NULL")
    ).scalar_one()
    if incomplete_payments:
        raise RuntimeError(
            f"Cannot remove legacy fields: {incomplete_payments} payment(s) "
            "do not have a preserved QR code"
        )

    op.drop_constraint("orders_txid_key", "orders", type_="unique")
    op.drop_column("orders", "copy_paste")
    op.drop_column("orders", "qr_code")
    op.drop_column("orders", "txid")


def downgrade() -> None:
    op.add_column("orders", sa.Column("txid", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("qr_code", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("copy_paste", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE orders AS o
            SET txid = LEFT(
                    COALESCE(
                        p.provider_payment_id,
                        p.provider_order_id || '-' || o.id::text,
                        'ORDER-' || o.id::text
                    ),
                    100
                ),
                qr_code = p.qr_code,
                copy_paste = COALESCE(p.copy_paste, '')
            FROM payments AS p
            WHERE p.order_id = o.id
            """
        )
    )
    op.alter_column("orders", "txid", nullable=False)
    op.alter_column("orders", "qr_code", nullable=False)
    op.alter_column("orders", "copy_paste", nullable=False)
    op.create_unique_constraint("orders_txid_key", "orders", ["txid"])
    op.drop_column("payments", "qr_code")
