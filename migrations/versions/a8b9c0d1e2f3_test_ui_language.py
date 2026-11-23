"""test ui language: the runner's chrome language per test (Phase 20)

Revision ID: a8b9c0d1e2f3
Revises: f2a3b4c5d6e7
Create Date: 2026-11-23 10:00:00.000000

Contract: `contracts.content.Test.ui_language` ("en" | "ru" | "uk", default "en") —
a Russian-language IQ test runs its intro / ready / timer / study / finish screens
in Russian. Existing rows get "en" (the historical behaviour) via the server
default. Phase 20's other contract changes live in JSON columns and need no DDL:
`GapFillItem.partial` / `partial_points` / `review_unmatched` (items.data) and the
optional norm tables (`NormScoring.by_age` None, `scaled_tables_by_age`,
`NormComposite.basis`, `final="composite"`) in tests.norm_scoring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tests') as batch:
        batch.add_column(
            sa.Column('ui_language', sa.String(), nullable=False, server_default='en')
        )


def downgrade() -> None:
    with op.batch_alter_table('tests') as batch:
        batch.drop_column('ui_language')
