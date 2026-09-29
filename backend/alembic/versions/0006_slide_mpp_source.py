"""Where a slide's resolution came from, and its native resolution (SPEC-04 §3.1, §6).

``mpp_source`` (the SPEC-02 vocabulary) is 'file' when ingest read the resolution from the
slide's metadata, 'dataset_doc' when a dataset's documentation gave it and 'manual' when a
pathologist supplied it. It is NULL for slides ingested before v6, which cannot be told apart.
``native_mpp`` is the finest resolution the slide holds (level 0, the coarser axis); it is
backfilled from the recorded resolution.

Revision ID: 0006_slide_mpp_source
Revises: 0005_grading_histotype_nullable
Create Date: 2026-09-29 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0006_slide_mpp_source'
down_revision: Union[str, None] = '0005_grading_histotype_nullable'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('slides') as batch_op:
        batch_op.add_column(sa.Column('mpp_source', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('native_mpp', sa.Float(), nullable=True))
        batch_op.create_check_constraint(
            'ck_slides_mpp_source', "mpp_source IS NULL OR mpp_source IN ('file', 'dataset_doc', 'manual')"
        )
    op.execute(
        "UPDATE slides SET native_mpp = CASE WHEN mpp_x >= mpp_y THEN mpp_x ELSE mpp_y END "
        "WHERE mpp_x IS NOT NULL AND mpp_y IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table('slides') as batch_op:
        batch_op.drop_constraint('ck_slides_mpp_source', type_='check')
        batch_op.drop_column('native_mpp')
        batch_op.drop_column('mpp_source')
