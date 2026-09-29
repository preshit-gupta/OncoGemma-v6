"""Stain profiles (SPEC-04 §3.4).

One row per Stage-2 stain fit of a slide. v5 slides have none, so their preprocess stage must run
again before any later stage can normalise colour.

Revision ID: 0007_stain_profiles
Revises: 0006_slide_mpp_source
Create Date: 2026-09-29 19:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.models.base import GUID
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0007_stain_profiles'
down_revision: Union[str, None] = '0006_slide_mpp_source'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
    op.create_table('stain_profiles',
    sa.Column('id', GUID(), nullable=False),
    sa.Column('slide_id', GUID(), nullable=False),
    sa.Column('fitter_version', sa.Text(), nullable=False),
    sa.Column('reference_id', sa.Text(), nullable=False),
    sa.Column('w_src', json_type, nullable=False),
    sa.Column('maxc_src', json_type, nullable=False),
    sa.Column('w_tgt', json_type, nullable=False),
    sa.Column('maxc_tgt', json_type, nullable=False),
    sa.Column('fit_status', sa.Text(), nullable=False),
    sa.Column('n_patches', sa.Integer(), nullable=False),
    sa.Column('mosaic_sha256', sa.CHAR(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("fit_status IN ('fitted', 'sparse', 'degenerate')", name='ck_stain_profiles_fit_status'),
    sa.ForeignKeyConstraint(['slide_id'], ['slides.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_stain_profiles_slide', 'stain_profiles', ['slide_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_stain_profiles_slide', table_name='stain_profiles')
    op.drop_table('stain_profiles')
