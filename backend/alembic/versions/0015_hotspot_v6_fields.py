"""hotspot v6 fields: rank, rank_score, score_kind, tumor_fraction, prescan_expected, window_um (SPEC-05 §5.3).

Revision ID: 0015_hotspot_v6_fields
Revises: 0014_qa_items_dataset_protocol
Create Date: 2026-10-02 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0015_hotspot_v6_fields'
down_revision: Union[str, None] = '0014_qa_items_dataset_protocol'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('hotspots') as batch:
        batch.add_column(sa.Column('rank', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('rank_score', sa.Float(), nullable=True))
        batch.add_column(sa.Column('score_kind', sa.Text(), nullable=True))
        batch.add_column(sa.Column('tumor_fraction', sa.Float(), nullable=True))
        batch.add_column(sa.Column('prescan_expected', sa.Float(), nullable=True))
        batch.add_column(sa.Column('window_um', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('hotspots') as batch:
        batch.drop_column('window_um')
        batch.drop_column('prescan_expected')
        batch.drop_column('tumor_fraction')
        batch.drop_column('score_kind')
        batch.drop_column('rank_score')
        batch.drop_column('rank')
