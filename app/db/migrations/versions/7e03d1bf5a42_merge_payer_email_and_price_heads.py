"""merge payer email and price heads

Revision ID: 7e03d1bf5a42
Revises: 4c7b91e2a6d0, a6c42f1d9b73
Create Date: 2026-08-11
"""

from typing import Sequence, Union


revision: str = "7e03d1bf5a42"
down_revision: Union[str, Sequence[str], None] = (
    "4c7b91e2a6d0",
    "a6c42f1d9b73",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
