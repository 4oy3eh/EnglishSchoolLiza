"""papers: per-paper minutes on tests, assigned papers on roster entries + attempts

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-11-20 10:00:00.000000

Contracts: `contracts.content.Test.paper_minutes` (explicit minutes per Cambridge
paper; NULL = proportional split of `duration_minutes` by the official per-paper
times), `contracts.runtime.RosterEntry.papers` (the papers a teacher assigned to a
student; NULL = the whole test) and `contracts.runtime.Attempt.papers` (the papers
served, snapshotted at start). All three are nullable JSON: every existing test,
roster entry and attempt means "the whole test" — nothing to backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tests') as batch:
        batch.add_column(sa.Column('paper_minutes', sa.JSON(), nullable=True))
    with op.batch_alter_table('roster_entries') as batch:
        batch.add_column(sa.Column('papers', sa.JSON(), nullable=True))
    with op.batch_alter_table('attempts') as batch:
        batch.add_column(sa.Column('papers', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('attempts') as batch:
        batch.drop_column('papers')
    with op.batch_alter_table('roster_entries') as batch:
        batch.drop_column('papers')
    with op.batch_alter_table('tests') as batch:
        batch.drop_column('paper_minutes')
