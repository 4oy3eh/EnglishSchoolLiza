"""attempt reports: persist the integrity profile + advisory verdict

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `attempt_reports.integrity` + `attempt_reports.verdict` (nullable JSON).

    The finalize path now computes the deterministic IntegrityProfile and the
    advisory AnalysisVerdict ONCE (Gemini, or the deterministic-only fallback) and
    stores both next to the frozen grade, instead of the dashboard recomputing a
    verdict on every read (contract: AttemptReport.integrity / AttemptReport.verdict).
    Advisory only: neither column ever feeds a score (golden rule #2).
    """
    with op.batch_alter_table('attempt_reports') as batch:
        batch.add_column(sa.Column('integrity', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('verdict', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('attempt_reports') as batch:
        batch.drop_column('verdict')
        batch.drop_column('integrity')
