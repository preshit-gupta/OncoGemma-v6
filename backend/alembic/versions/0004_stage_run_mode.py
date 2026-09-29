"""Run mode on stage executions (SPEC-01 §3.2).

Existing rows were interactive app runs, so they become ``clinical``.

Revision ID: 0004_stage_run_mode
Revises: 0003_decision_records
Create Date: 2026-09-28 20:00:01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0004_stage_run_mode'
down_revision: Union[str, None] = '0003_decision_records'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('stage_executions') as batch_op:
        batch_op.add_column(sa.Column('run_mode', sa.String(), server_default='clinical', nullable=False))
        batch_op.create_check_constraint(
            'ck_stage_executions_run_mode', "run_mode IN ('clinical', 'eval', 'shadow')"
        )


def downgrade() -> None:
    with op.batch_alter_table('stage_executions') as batch_op:
        batch_op.drop_constraint('ck_stage_executions_run_mode', type_='check')
        batch_op.drop_column('run_mode')
