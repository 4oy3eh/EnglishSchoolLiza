"""roster PINs + attempt session tokens

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-17 00:00:00.000000

"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of contracts.runtime.PIN_ALPHABET / PIN_LENGTH (migrations stay
# self-contained: no imports of live app code).
_PIN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_PIN_LENGTH = 4


def _new_pin(taken: set[str]) -> str:
    while True:
        pin = "".join(secrets.choice(_PIN_ALPHABET) for _ in range(_PIN_LENGTH))
        if pin not in taken:
            return pin


def upgrade() -> None:
    """Add `roster_entries.pin` (+ backfill, unique per test) and `attempt_sessions`.

    Contract: `RosterEntry.pin`. Every existing roster row gets a random PIN that
    is unique within its test, then a unique index (test_id, pin) is created.
    `attempt_sessions` holds the SHA-256 of the per-attempt session tokens issued
    on a successful PIN start (server-internal auth state, no contract).
    """
    with op.batch_alter_table('roster_entries') as batch:
        batch.add_column(sa.Column('pin', sa.String(length=8), nullable=True))

    bind = op.get_bind()
    roster = sa.table(
        'roster_entries',
        sa.column('id', sa.String),
        sa.column('test_id', sa.String),
        sa.column('pin', sa.String),
    )
    rows = bind.execute(sa.select(roster.c.id, roster.c.test_id, roster.c.pin)).all()
    taken: dict[str, set[str]] = {}
    for row in rows:
        if row.pin:
            taken.setdefault(row.test_id, set()).add(row.pin)
    for row in rows:
        if row.pin:
            continue
        used = taken.setdefault(row.test_id, set())
        pin = _new_pin(used)
        used.add(pin)
        bind.execute(roster.update().where(roster.c.id == row.id).values(pin=pin))

    op.create_index(
        'ix_roster_entries_test_pin', 'roster_entries', ['test_id', 'pin'], unique=True
    )

    op.create_table(
        'attempt_sessions',
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('attempt_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('token_hash'),
    )
    op.create_index(
        op.f('ix_attempt_sessions_attempt_id'), 'attempt_sessions', ['attempt_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_attempt_sessions_attempt_id'), table_name='attempt_sessions')
    op.drop_table('attempt_sessions')
    op.drop_index('ix_roster_entries_test_pin', table_name='roster_entries')
    with op.batch_alter_table('roster_entries') as batch:
        batch.drop_column('pin')
