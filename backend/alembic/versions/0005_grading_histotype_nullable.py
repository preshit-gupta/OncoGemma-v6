"""Grading.histologic_type: nullable, no default (SPEC-01 §3.9).

v5 defaulted the column to IDC-NST, so a failed classification read as a
diagnosis. Existing values are kept; downgrade refuses to guess a type for
rows that are NULL by then.

Revision ID: 0005_grading_histotype_nullable
Revises: 0004_stage_run_mode
Create Date: 2026-09-29 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0005_grading_histotype_nullable'
down_revision: Union[str, None] = '0004_stage_run_mode'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('gradings') as batch_op:
        batch_op.alter_column('histologic_type', existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    unassessed = bind.execute(sa.text("SELECT COUNT(*) FROM gradings WHERE histologic_type IS NULL")).scalar_one()
    if unassessed:
        raise RuntimeError(
            f"{unassessed} gradings have no histologic type; downgrading would need an invented value. "
            "Set them explicitly first."
        )
    with op.batch_alter_table('gradings') as batch_op:
        batch_op.alter_column('histologic_type', existing_type=sa.String(), nullable=False)
