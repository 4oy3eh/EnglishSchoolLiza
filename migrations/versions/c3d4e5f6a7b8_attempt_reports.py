"""attempt reports (frozen grade + per-skill pedagogical feedback)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Persist the report computed once when an attempt is finalized (on submit).

    Holds the frozen auto-grade (GradingResult) + the per-skill strengths/weaknesses
    feedback (list[SkillReport]) as JSON, so the dashboard reads it instead of
    re-running the LLM grader on every load.
    """
    op.create_table(
        'attempt_reports',
        sa.Column('attempt_id', sa.String(), nullable=False),
        sa.Column('graded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='complete'),
        sa.Column('grading', sa.JSON(), nullable=False),
        sa.Column('skills', sa.JSON(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False, server_default=''),
        sa.Column('model_id', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('attempt_id'),
    )


def downgrade() -> None:
    op.drop_table('attempt_reports')
