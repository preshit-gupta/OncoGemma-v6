"""Drop the v5 Stage-6 reports table.

WP-1.2 removed the CAP report stack and the Report model. The program owner
approved deleting the table and its data (2026-09-28). Downgrade recreates the
empty table as 0001 defined it; the data is not restored.

Revision ID: 0002_drop_v5_reports
Revises: 0001_v5_baseline
Create Date: 2026-09-28 18:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.models.base import GUID
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0002_drop_v5_reports'
down_revision: Union[str, None] = '0001_v5_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('reports')


def downgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
    op.create_table('reports',
    sa.Column('case_id', GUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('specimen_type', sa.String(), nullable=False),
    sa.Column('procedure', sa.String(), nullable=False),
    sa.Column('laterality', sa.String(), nullable=False),
    sa.Column('tumor_site', sa.String(), nullable=False),
    sa.Column('histologic_type', sa.String(), nullable=False),
    sa.Column('tumor_size_mm', sa.Float(), nullable=True),
    sa.Column('lvi_status', sa.String(), nullable=False),
    sa.Column('dcis_present', sa.Boolean(), nullable=False),
    sa.Column('margins', json_type, nullable=True),
    sa.Column('lymph_nodes', json_type, nullable=False),
    sa.Column('biomarkers', json_type, nullable=True),
    sa.Column('staging', json_type, nullable=False),
    sa.Column('narrative', json_type, nullable=False),
    sa.Column('visual_evidence', json_type, nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('pdf_path', sa.String(), nullable=True),
    sa.Column('pdf_sha256', sa.String(), nullable=True),
    sa.Column('signed_by', sa.String(), nullable=True),
    sa.Column('npi', sa.String(), nullable=True),
    sa.Column('attestation_statement', sa.String(), nullable=True),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('integrity_hash', sa.String(), nullable=True),
    sa.Column('narrative_edited', sa.Boolean(), nullable=False),
    sa.Column('amendments', json_type, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('case_id', 'version')
    )
