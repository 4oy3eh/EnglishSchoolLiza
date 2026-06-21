"""attempt unique per roster entry

Revision ID: 25c1f9debd77
Revises: 495ca9c7b0ec
Create Date: 2026-06-21 06:18:14.490786

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '25c1f9debd77'
down_revision: Union[str, Sequence[str], None] = '495ca9c7b0ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """One attempt per roster entry (named for Postgres-portable downgrade)."""
    with op.batch_alter_table('attempts', schema=None) as batch_op:
        batch_op.create_unique_constraint('uq_attempts_roster_entry_id', ['roster_entry_id'])


def downgrade() -> None:
    """Drop the per-roster-entry uniqueness."""
    with op.batch_alter_table('attempts', schema=None) as batch_op:
        batch_op.drop_constraint('uq_attempts_roster_entry_id', type_='unique')
