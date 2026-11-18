"""attempt audio progress per listening track (per-part audio)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-17 12:00:00.000000

Contract: `contracts.runtime.AudioTrackProgress`. Each listening section may now
carry its own recording (Cambridge YLE ships one mp3 per part), so the single
`attempts.audio_progress_seconds` is no longer enough: one row per (attempt,
track asset id) holds the furthest point reached in that track. The old column
stays (an old runner still writes it during the transition; the new runner
applies it to the first listening track) — nothing to backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'attempt_audio_progress',
        sa.Column('attempt_id', sa.String(), nullable=False),
        sa.Column('asset_id', sa.String(), nullable=False),
        sa.Column('seconds', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('attempt_id', 'asset_id'),
    )


def downgrade() -> None:
    op.drop_table('attempt_audio_progress')
