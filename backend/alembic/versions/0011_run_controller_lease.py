"""Controller lease on validation runs (SPEC-02 §6.1–6.2).

Workers drive active runs; the lease makes one worker at a time the run's controller. The
columns are nullable additions, so no table is rebuilt on SQLite.

Revision ID: 0011_run_controller_lease
Revises: 0010_validation
Create Date: 2026-09-30 21:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0011_run_controller_lease'
down_revision: Union[str, None] = '0010_validation'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('validation_runs', sa.Column('controller_lease_owner', sa.Text(), nullable=True))
    op.add_column('validation_runs', sa.Column('controller_lease_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('validation_runs', 'controller_lease_until')
    op.drop_column('validation_runs', 'controller_lease_owner')
