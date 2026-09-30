"""qa_items: the dataset and protocol version of each label-QA item (SPEC-08 §4.5).

Revision ID: 0014_qa_items_dataset_protocol
Revises: 0013_research
Create Date: 2026-10-01 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0014_qa_items_dataset_protocol'
down_revision: Union[str, None] = '0013_research'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # An existing row has no truthful dataset or protocol to backfill, so refuse rather than invent one.
    existing = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM qa_items")).scalar()
    if existing:
        raise RuntimeError(
            f"qa_items holds {existing} rows without a dataset and protocol_version; "
            "set both for each row (add the columns as nullable, backfill, then make them NOT NULL) "
            "before applying 0014"
        )
    with op.batch_alter_table('qa_items') as batch:
        batch.add_column(sa.Column('dataset', sa.Text(), nullable=False))
        batch.add_column(sa.Column('protocol_version', sa.Text(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('qa_items') as batch:
        batch.drop_column('protocol_version')
        batch.drop_column('dataset')
