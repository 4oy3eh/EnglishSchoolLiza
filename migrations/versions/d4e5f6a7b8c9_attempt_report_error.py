"""attempt reports: persist why finalize failed

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `attempt_reports.error` (nullable text).

    A crashed background finalize now writes a `failed` report carrying the
    exception (`<Type>: message`, truncated) instead of leaving no row, so the
    dashboard can show the cause and offer a re-grade (contract: AttemptReport.error).
    """
    with op.batch_alter_table('attempt_reports') as batch:
        batch.add_column(sa.Column('error', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('attempt_reports') as batch:
        batch.drop_column('error')
