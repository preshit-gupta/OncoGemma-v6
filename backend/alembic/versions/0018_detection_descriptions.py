"""detections: morphology description and its status (SPEC-06 §5.6, D22).

``description`` is the strict MitosisDescription JSON, or NULL. ``description_status`` is
``ok`` | ``unavailable`` | ``not_requested``; rows from before are ``not_requested``.

Revision ID: 0018_detection_descriptions
Revises: 0017_hpf_sites
Create Date: 2026-10-05 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0018_detection_descriptions'
down_revision: Union[str, None] = '0017_hpf_sites'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMN, not batch_alter_table: SQLite's table rebuild cannot copy the generated ``counted`` column.
    op.add_column('detections', sa.Column('description', sa.JSON(), nullable=True))
    op.add_column('detections', sa.Column('description_status', sa.Text(), nullable=False, server_default='not_requested'))


def downgrade() -> None:
    op.drop_column('detections', 'description_status')
    op.drop_column('detections', 'description')
