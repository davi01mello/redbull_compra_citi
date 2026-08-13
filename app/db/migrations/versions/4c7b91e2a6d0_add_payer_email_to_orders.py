"""align product category and add payer email to orders

Revision ID: 4c7b91e2a6d0
Revises: e91a7c4d2f30
Create Date: 2026-08-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4c7b91e2a6d0"
down_revision: Union[str, Sequence[str], None] = "e91a7c4d2f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    product_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("products")
    }
    if "category" not in product_columns:
        op.add_column(
            "products",
            sa.Column(
                "category",
                sa.String(),
                server_default="Regular",
                nullable=False,
            ),
        )
    op.add_column(
        "orders",
        sa.Column("payer_email", sa.String(length=254), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "payer_email")
    # category já existia no banco institucional antes desta revisão e é
    # preservada deliberadamente para que um downgrade não remova dados reais.
