"""section reported skill + B2/C1/C2 content (levels, key word transformation)

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-18 12:00:00.000000

Contracts: `contracts.content.Section.reported_skill` (B2 First / C1 Advanced / C2
Proficiency report the Reading and Use of English paper as TWO skills: Parts 2-4
keep `skill="reading"` and set `reported_skill="use_of_english"`). Nullable — every
existing section reports under its `skill`, nothing to backfill.

The rest of this contract change needs no DDL: the new levels (`B2_FIRST`,
`C1_ADVANCED`, `C2_PROFICIENCY`) live in the free-string `tests.level`, the new
item type `key_word_transformation` and the gap_fill `stem_word` / `max_words`
fields in `items.item_type` / `items.data` (JSON), the matching-pool
`each_option_once` / `audio` in `sections.stimulus` (JSON), and the new grade
method in the `attempt_reports.grading` JSON.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sections') as batch:
        batch.add_column(sa.Column('reported_skill', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sections') as batch:
        batch.drop_column('reported_skill')
