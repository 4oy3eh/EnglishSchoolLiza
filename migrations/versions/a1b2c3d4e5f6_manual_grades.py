"""manual grades (teacher-entered marks / overrides)

Revision ID: a1b2c3d4e5f6
Revises: 25c1f9debd77
Create Date: 2026-06-25 01:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '25c1f9debd77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Persist teacher-entered marks: writing scores + gap-fill ✓/✗ overrides."""
    op.create_table(
        'manual_grades',
        sa.Column('attempt_id', sa.String(), nullable=False),
        sa.Column('item_id', sa.String(), nullable=False),
        sa.Column('awarded', sa.Float(), nullable=False),
        sa.Column('graded_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('attempt_id', 'item_id'),
    )


def downgrade() -> None:
    op.drop_table('manual_grades')
