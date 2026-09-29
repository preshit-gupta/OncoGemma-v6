"""Specimen type on cases (SPEC-04 §3.2).

Existing cases become 'unknown': the specimen type was never recorded. Preprocess refuses an
unknown specimen (SpecimenTypeRequired) until a pathologist sets it.

``cases`` is the root of the schema's foreign keys. SQLite cannot add a CHECK constraint without
rebuilding the table (batch mode), and with foreign keys enforced the rebuild's DROP TABLE
cascade-deletes every stage execution, grading and slide. So SQLite (tests and local
development) gets the column without the CHECK, which the models still declare; PostgreSQL
gets the constraint.

Revision ID: 0008_case_specimen_type
Revises: 0007_stain_profiles
Create Date: 2026-09-29 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0008_case_specimen_type'
down_revision: Union[str, None] = '0007_stain_profiles'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == 'sqlite'


def upgrade() -> None:
    op.add_column('cases', sa.Column('specimen_type', sa.String(), server_default='unknown', nullable=False))
    if not _is_sqlite():
        op.create_check_constraint(
            'ck_cases_specimen_type', 'cases', "specimen_type IN ('resection', 'core_biopsy', 'unknown')"
        )


def downgrade() -> None:
    if not _is_sqlite():
        op.drop_constraint('ck_cases_specimen_type', 'cases', type_='check')
    op.drop_column('cases', 'specimen_type')
