"""iq family: norm scoring on tests, timed/intro/study sections, roster birth date

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-11-21 10:00:00.000000

Contracts (Phase 17): `contracts.content.Test.norm_scoring` (authoring-only norm
tables, JSON), `contracts.content.Section.time_limit_seconds` (own subtest clock),
`Section.intro` (untimed instructions + samples, JSON) and `Section.study`
(show-then-hide stimulus, JSON), `contracts.runtime.RosterEntry.birth_date`
(teacher-only, for norms by age). All nullable: existing rows mean "not used" —
nothing to backfill. Level `IQ`, paper "iq" and the item types `mark_toggle` /
`drawing` live in free-string / JSON columns (no DDL).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tests') as batch:
        batch.add_column(sa.Column('norm_scoring', sa.JSON(), nullable=True))
    with op.batch_alter_table('sections') as batch:
        batch.add_column(sa.Column('time_limit_seconds', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('intro', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('study', sa.JSON(), nullable=True))
    with op.batch_alter_table('roster_entries') as batch:
        batch.add_column(sa.Column('birth_date', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('roster_entries') as batch:
        batch.drop_column('birth_date')
    with op.batch_alter_table('sections') as batch:
        batch.drop_column('study')
        batch.drop_column('intro')
        batch.drop_column('time_limit_seconds')
    with op.batch_alter_table('tests') as batch:
        batch.drop_column('norm_scoring')
