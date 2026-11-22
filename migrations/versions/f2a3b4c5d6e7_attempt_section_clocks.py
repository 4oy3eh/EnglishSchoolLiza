"""timed sections: per attempt + section server clock (IQ family, Phase 17.2)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-11-21 14:00:00.000000

Contract: `contracts.runtime.AttemptSection`. One row per (attempt, section) of an
attempt whose served sections all have `time_limit_seconds`: when the section
became current (intro), its begin (student / intro cap), the study window, the
answering start, its deadline and its close (finished / expired). New table only —
nothing to backfill (existing attempts never ran timed sections). The IQ report is
recomputed on read (no column; see app/admin/CLAUDE.md "IQ report").
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'attempt_sections',
        sa.Column('attempt_id', sa.String(), nullable=False),
        sa.Column('section_id', sa.String(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('intro_started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_by', sa.String(), nullable=True),
        sa.Column('study_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('study_ends_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('answering_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deadline', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('close_reason', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('attempt_id', 'section_id'),
    )


def downgrade() -> None:
    op.drop_table('attempt_sections')
