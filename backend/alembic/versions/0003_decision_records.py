"""Decision records (SPEC-01 §3.3).

One row per model, heuristic, human or fallback decision, naming the component
that actually produced it. ``run_id`` gets its foreign key when SPEC-02 adds
``validation_runs``.

Revision ID: 0003_decision_records
Revises: 0002_drop_v5_reports
Create Date: 2026-09-28 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.models.base import GUID
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0003_decision_records'
down_revision: Union[str, None] = '0002_drop_v5_reports'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
    op.create_table('decision_records',
    sa.Column('id', GUID(), nullable=False),
    sa.Column('case_id', GUID(), nullable=False),
    sa.Column('stage_execution_id', GUID(), nullable=False),
    sa.Column('run_id', GUID(), nullable=True),
    sa.Column('stage', sa.Text(), nullable=False),
    sa.Column('task', sa.Text(), nullable=False),
    sa.Column('entity_type', sa.Text(), nullable=False),
    sa.Column('entity_id', sa.Text(), nullable=False),
    sa.Column('entity_ids_uri', sa.Text(), nullable=True),
    sa.Column('producer_kind', sa.Text(), nullable=False),
    sa.Column('producer_id', sa.Text(), nullable=False),
    sa.Column('producer_version', sa.Text(), nullable=False),
    sa.Column('endpoint', sa.Text(), nullable=True),
    sa.Column('prompt_id', sa.Text(), nullable=True),
    sa.Column('prompt_sha256', sa.CHAR(length=64), nullable=True),
    sa.Column('input_sha256', sa.CHAR(length=64), nullable=False),
    sa.Column('input_spec', json_type, nullable=False),
    sa.Column('params', json_type, nullable=False),
    sa.Column('output', json_type, nullable=True),
    sa.Column('raw_output_uri', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('error_class', sa.Text(), nullable=True),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('cache_hit', sa.Boolean(), nullable=False),
    sa.Column('run_mode', sa.Text(), nullable=False),
    sa.Column('config_hash', sa.CHAR(length=64), nullable=False),
    sa.Column('supersedes_id', GUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("producer_kind IN ('model', 'heuristic', 'human', 'fallback', 'shadow')", name='ck_decision_records_producer_kind'),
    sa.CheckConstraint("status IN ('ok', 'schema_invalid', 'timeout', 'unavailable', 'error', 'skipped')", name='ck_decision_records_status'),
    sa.CheckConstraint("run_mode IN ('clinical', 'eval', 'shadow')", name='ck_decision_records_run_mode'),
    sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stage_execution_id'], ['stage_executions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['supersedes_id'], ['decision_records.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_dr_case_stage', 'decision_records', ['case_id', 'stage'], unique=False)
    op.create_index('ix_dr_entity', 'decision_records', ['case_id', 'entity_type', 'entity_id'], unique=False)
    op.create_index('ix_dr_producer', 'decision_records', ['task', 'producer_id', 'producer_version'], unique=False)
    op.create_index('ix_dr_run', 'decision_records', ['run_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_dr_run', table_name='decision_records')
    op.drop_index('ix_dr_producer', table_name='decision_records')
    op.drop_index('ix_dr_entity', table_name='decision_records')
    op.drop_index('ix_dr_case_stage', table_name='decision_records')
    op.drop_table('decision_records')
