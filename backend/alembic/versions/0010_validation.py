"""Validation runs and items; run_id on stage executions and decision records (SPEC-02 §5.3).

SPEC-02 numbers this migration 0005; that number went to the histotype change, so it is 0010.
``stages`` is a JSON list rather than TEXT[] so SQLite (tests) and PostgreSQL share one model.

SQLite cannot add a foreign key to an existing table without rebuilding it (batch mode). With
foreign keys enforced, rebuilding ``stage_executions`` would cascade-delete its decision records
and hotspots, so on SQLite ``stage_executions.run_id`` is added through ``ALTER TABLE ... ADD
COLUMN ... REFERENCES``, which SQLite accepts for a nullable column. ``decision_records`` is
referenced only by itself, so batch mode is safe there.

Revision ID: 0010_validation
Revises: 0009_auth
Create Date: 2026-09-30 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.models.base import GUID

# revision identifiers, used by Alembic.
revision: str = '0010_validation'
down_revision: Union[str, None] = '0009_auth'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == 'sqlite'


def upgrade() -> None:
    op.create_table('validation_runs',
    sa.Column('id', GUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('dataset', sa.Text(), nullable=False),
    sa.Column('split', sa.Text(), nullable=False),
    sa.Column('manifest_uri', sa.Text(), nullable=False),
    sa.Column('manifest_sha256', sa.CHAR(length=64), nullable=False),
    sa.Column('stages', sa.JSON().with_variant(sa.dialects.postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('mode', sa.Text(), nullable=False),
    sa.Column('concurrency', sa.Integer(), nullable=False),
    sa.Column('config_hash', sa.CHAR(length=64), nullable=False),
    sa.Column('registry_sha256', sa.CHAR(length=64), nullable=False),
    sa.Column('splits_lock_sha256', sa.CHAR(length=64), nullable=False),
    sa.Column('arm', sa.Text(), nullable=True),
    sa.Column('is_locked_test', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('metrics_uri', sa.Text(), nullable=True),
    sa.CheckConstraint("mode IN ('auto', 'manual')", name='ck_validation_runs_mode'),
    sa.CheckConstraint(
        "status IN ('created', 'running', 'completed', 'cancelled', 'failed')", name='ck_validation_runs_status'
    ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('validation_items',
    sa.Column('run_id', GUID(), nullable=False),
    sa.Column('slide_id', sa.Text(), nullable=False),
    sa.Column('patient_id', sa.Text(), nullable=False),
    sa.Column('case_id', GUID(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('failed_stage', sa.Text(), nullable=True),
    sa.Column('error_class', sa.Text(), nullable=True),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('prediction', sa.JSON().with_variant(sa.dialects.postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('runtime_s', sa.Float(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.CheckConstraint(
        "status IN ('pending', 'running', 'succeeded', 'failed', 'excluded_qc', 'cancelled')",
        name='ck_validation_items_status',
    ),
    sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['validation_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('run_id', 'slide_id')
    )

    if _is_sqlite():
        op.execute(
            'ALTER TABLE stage_executions ADD COLUMN run_id CHAR(36) '
            'CONSTRAINT fk_stage_executions_run REFERENCES validation_runs (id)'
        )
    else:
        op.add_column('stage_executions', sa.Column('run_id', GUID(), nullable=True))
        op.create_foreign_key(
            'fk_stage_executions_run', 'stage_executions', 'validation_runs', ['run_id'], ['id']
        )

    with op.batch_alter_table('decision_records') as batch_op:
        batch_op.create_foreign_key('fk_dr_run', 'validation_runs', ['run_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('decision_records') as batch_op:
        batch_op.drop_constraint('fk_dr_run', type_='foreignkey')
    if _is_sqlite():
        # SQLite 3.35+ drops a column that no index or constraint other than its own FK uses.
        op.execute('ALTER TABLE stage_executions DROP COLUMN run_id')
    else:
        op.drop_constraint('fk_stage_executions_run', 'stage_executions', type_='foreignkey')
        op.drop_column('stage_executions', 'run_id')
    op.drop_table('validation_items')
    op.drop_table('validation_runs')
