"""detections v6: decision columns and a generated `counted` (SPEC-06 §5.6, contract mitosis_v6); HPF coverage.

Existing rows are mapped deterministically:
- ``det_conf`` -> ``p_a``. A pathologist-added figure (``label_source`` starting with ``pathologist`` and
  either no ``det_conf`` or a v5 ``m_user_*`` id, whose ``det_conf`` of 1.0 was a forced value) gets
  ``p_a = NULL``, ``decision_path = 'human'`` and ``final_decision = 'mitosis'``.
- Any other ``label_source`` starting with ``pathologist``: ``review_label = label`` (NULL when the label
  is ``unreviewed``). The v5 review overwrote the model's own label, so ``final_decision`` is
  ``equivocal`` (unknown) and ``decision_path = 'A'``; the pathologist's label decides the count.
- Otherwise ``label`` ``mitosis`` / ``not_mitosis`` / ``unreviewed`` -> ``final_decision`` ``mitosis`` /
  ``not_mitosis`` / ``equivocal``, ``decision_path = 'A'``.
- ``vlm`` is NULL for every existing row: the contract has no "referee without B" path and a v5 verdict
  has none of the ``VlmVerdict`` criteria, so ``medgemma_*`` is not carried over. ``p_b`` and
  ``record_ids`` are NULL, ``rule_override`` false and ``in_tumor`` NULL (the tumour gate never ran).

Then ``det_conf``, ``ver_conf``, ``label``, ``label_source``, ``medgemma_*`` and the crop URIs are dropped
(crops are addressed by candidate id). ``detections`` is a child table, so batch mode is safe on SQLite;
``counted`` is added after the rebuild because a rebuild cannot copy into a generated column.

``hpf_sites`` gains ``tissue_coverage`` and ``tumor_fraction`` (NULL on existing rows).

Revision ID: 0016_detections_v6
Revises: 0015_hotspot_v6_fields
Create Date: 2026-10-04 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0016_detections_v6'
down_revision: Union[str, None] = '0015_hotspot_v6_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
COUNTED_SQL = "COALESCE(review_label = 'mitosis', final_decision = 'mitosis' AND COALESCE(in_tumor, TRUE))"

HUMAN_ADDED = "(label_source LIKE 'pathologist%' AND (det_conf IS NULL OR id LIKE 'm_user%'))"
REVIEWED = "label_source LIKE 'pathologist%'"


def upgrade() -> None:
    with op.batch_alter_table('detections') as batch:
        batch.add_column(sa.Column('p_a', sa.Float(), nullable=True))
        batch.add_column(sa.Column('p_b', sa.Float(), nullable=True))
        batch.add_column(sa.Column('vlm', JSONType, nullable=True))
        batch.add_column(sa.Column('rule_override', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('in_tumor', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('final_decision', sa.String(), nullable=True))
        batch.add_column(sa.Column('decision_path', sa.String(), nullable=True))
        batch.add_column(sa.Column('review_label', sa.String(), nullable=True))
        batch.add_column(sa.Column('record_ids', JSONType, nullable=True))

    op.execute(f"""
        UPDATE detections SET
            p_a = CASE WHEN {HUMAN_ADDED} THEN NULL ELSE det_conf END,
            review_label = CASE WHEN {REVIEWED} AND label IN ('mitosis', 'not_mitosis') THEN label END,
            decision_path = CASE WHEN {HUMAN_ADDED} THEN 'human' ELSE 'A' END,
            final_decision = CASE
                WHEN {HUMAN_ADDED} THEN 'mitosis'
                WHEN {REVIEWED} THEN 'equivocal'
                WHEN label = 'mitosis' THEN 'mitosis'
                WHEN label = 'not_mitosis' THEN 'not_mitosis'
                ELSE 'equivocal'
            END
    """)

    with op.batch_alter_table('detections') as batch:
        batch.alter_column('final_decision', existing_type=sa.String(), nullable=False)
        batch.alter_column('decision_path', existing_type=sa.String(), nullable=False)
        for column in ('det_conf', 'ver_conf', 'label', 'label_source', 'medgemma_verdict',
                       'medgemma_rationale', 'medgemma_confidence', 'crop_uri', 'crop_orig_uri'):
            batch.drop_column(column)

    op.add_column('detections', sa.Column('counted', sa.Boolean(), sa.Computed(COUNTED_SQL), nullable=True))

    with op.batch_alter_table('hpf_sites') as batch:
        batch.add_column(sa.Column('tissue_coverage', sa.Float(), nullable=True))
        batch.add_column(sa.Column('tumor_fraction', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('hpf_sites') as batch:
        batch.drop_column('tumor_fraction')
        batch.drop_column('tissue_coverage')

    op.drop_column('detections', 'counted')
    with op.batch_alter_table('detections') as batch:
        batch.add_column(sa.Column('det_conf', sa.Float(), nullable=True))
        batch.add_column(sa.Column('ver_conf', sa.Float(), nullable=True))
        batch.add_column(sa.Column('label', sa.String(), nullable=True))
        batch.add_column(sa.Column('label_source', sa.String(), nullable=True))
        batch.add_column(sa.Column('medgemma_verdict', sa.String(), nullable=True))
        batch.add_column(sa.Column('medgemma_rationale', sa.Text(), nullable=True))
        batch.add_column(sa.Column('medgemma_confidence', sa.String(), nullable=True))
        batch.add_column(sa.Column('crop_uri', sa.Text(), nullable=True))
        batch.add_column(sa.Column('crop_orig_uri', sa.Text(), nullable=True))

    op.execute("""
        UPDATE detections SET
            det_conf = p_a,
            label = COALESCE(review_label, CASE final_decision WHEN 'equivocal' THEN 'unreviewed' ELSE final_decision END),
            label_source = CASE WHEN review_label IS NOT NULL OR decision_path = 'human' THEN 'pathologist' ELSE 'model' END
    """)

    with op.batch_alter_table('detections') as batch:
        batch.alter_column('label', existing_type=sa.String(), nullable=False)
        batch.alter_column('label_source', existing_type=sa.String(), nullable=False)
        for column in ('record_ids', 'review_label', 'decision_path', 'final_decision', 'in_tumor',
                       'rule_override', 'vlm', 'p_b', 'p_a'):
            batch.drop_column(column)
