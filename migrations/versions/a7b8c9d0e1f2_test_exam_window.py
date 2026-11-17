"""test exam window (opens_at / closes_at / late-start grace on tests)

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-11-17 10:00:00.000000

The scheduled exam window becomes teacher-editable per test. Dedicated columns
(not the section JSON): the dashboard list reads them with the row, and changing
the window is a one-row UPDATE that never rewrites a locked test's content. All
nullable / defaulted, so existing tests stay "always open" (no bound).

`level` needs nothing for the new PRE_A1_STARTERS / A2_FLYERS codes: it is a plain
String column without a CHECK constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD / DROP COLUMN (SQLite >= 3.35), never a batch table rebuild: `tests`
    # is the FK parent of sections / roster / attempts (ON DELETE CASCADE), and a
    # rebuild's DROP TABLE on a connection with foreign_keys=ON would wipe them.
    op.add_column('tests', sa.Column('opens_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tests', sa.Column('closes_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'tests',
        sa.Column('late_start_grace_minutes', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('tests', 'late_start_grace_minutes')
    op.drop_column('tests', 'closes_at')
    op.drop_column('tests', 'opens_at')
