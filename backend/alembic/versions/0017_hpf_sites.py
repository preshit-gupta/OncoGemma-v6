"""HPF sites: centre, circle diameter and tissue fraction of a hotspot (SPEC-05 §5, D22).

A hotspot is now a circle (``center_um``, ``hpf_diameter_um``) in a padded frame (``polygon_um``).
The columns are NULL on rows from before; such a row has no HPF circle, so Stage 4 refuses it and
triage must run again for the case.

Revision ID: 0017_hpf_sites
Revises: 0016_detections_v6
Create Date: 2026-10-05 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0017_hpf_sites'
down_revision: Union[str, None] = '0016_detections_v6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('hotspots') as batch:
        batch.add_column(sa.Column('center_um', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('hpf_diameter_um', sa.Float(), nullable=True))
        batch.add_column(sa.Column('tissue_fraction', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('hotspots') as batch:
        batch.drop_column('tissue_fraction')
        batch.drop_column('hpf_diameter_um')
        batch.drop_column('center_um')
