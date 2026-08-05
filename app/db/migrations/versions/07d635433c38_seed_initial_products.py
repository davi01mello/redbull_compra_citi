"""seed initial products

Revision ID: 07d635433c38
Revises: e0307dabc231
Create Date: 2026-08-04 14:19:50.449083
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '07d635433c38'
down_revision: Union[str, Sequence[str], None] = 'e0307dabc231'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    products = sa.table(
        "products",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("color", sa.String),
        sa.column("price", sa.Numeric(10, 2)),
        sa.column("stock", sa.Integer),
    )
    op.bulk_insert(
        products,
        [
            {
                "id": "original",
                "name": "Energy Original",
                "description": "O clássico que dá asas.",
                "color": "#164194",
                "price": 8.00,
                "stock": 24,
            },
            {
                "id": "tropical",
                "name": "Tropical",
                "description": "Sabor tropical e refrescante.",
                "color": "#f5c400",
                "price": 8.00,
                "stock": 18,
            },
            {
                "id": "melancia",
                "name": "Melancia",
                "description": "Leve, frutado e marcante.",
                "color": "#e7354f",
                "price": 8.00,
                "stock": 16,
            },
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        sa.text(
            "DELETE FROM products AS product "
            "WHERE product.id IN ('original', 'tropical', 'melancia') "
            "AND NOT EXISTS ("
            "SELECT 1 FROM order_items AS item "
            "WHERE item.product_id = product.id"
            ")"
        )
    )
