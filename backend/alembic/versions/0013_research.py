"""Research view models: issues, gt_annotations, run_metrics, annotation_tasks, qa_items (SPEC-08 §5–7).

Revision ID: 0013_research
Revises: 0012_safety_hardening
Create Date: 2026-09-30 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import GUID

# revision identifiers, used by Alembic.
revision: str = '0013_research'
down_revision: Union[str, None] = '0012_safety_hardening'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    # 1. issues (SPEC-08 §5)
    op.create_table(
        'issues',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('category', sa.Text(), nullable=False),
        sa.Column('severity', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), server_default='open', nullable=False),
        sa.Column('metric_impact', JSONType, nullable=True),
        sa.Column('evidence', JSONType, server_default='[]', nullable=False),
        sa.Column('spec_ref', sa.Text(), nullable=True),
        sa.Column('owner', GUID(), nullable=True),
        sa.Column('created_by', GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('resolved_in', sa.Text(), nullable=True),
        sa.Column('resolution_note', sa.Text(), nullable=True),
        sa.CheckConstraint(
            "category IN ('biological', 'model', 'staging', 'technical')",
            name='ck_issues_category'
        ),
        sa.CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low')",
            name='ck_issues_severity'
        ),
        sa.CheckConstraint(
            "status IN ('open', 'triaged', 'in_progress', 'resolved', 'wont_fix')",
            name='ck_issues_status'
        ),
        sa.ForeignKeyConstraint(['owner'], ['users.id'], name='fk_issues_owner'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name='fk_issues_created_by'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_issues_status', 'issues', ['status'])
    op.create_index('ix_issues_category', 'issues', ['category'])

    # 2. gt_annotations (SPEC-08 §6)
    op.create_table(
        'gt_annotations',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('dataset', sa.Text(), nullable=False),
        sa.Column('slide_id', sa.Text(), nullable=False),
        sa.Column('task', sa.Text(), nullable=False),
        sa.Column('region_geojson', JSONType, nullable=True),
        sa.Column('payload', JSONType, nullable=False),
        sa.Column('annotator_id', GUID(), nullable=False),
        sa.Column('protocol_version', sa.Text(), nullable=False),
        sa.Column('blind', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('status', sa.Text(), server_default='draft', nullable=False),
        sa.Column('adjudicates', JSONType, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "task IN ('mitosis_points', 'component_scores', 'grade', 'tumor_region', 'label_qa')",
            name='ck_gt_annotations_task'
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'adjudicated')",
            name='ck_gt_annotations_status'
        ),
        sa.ForeignKeyConstraint(['annotator_id'], ['users.id'], name='fk_gt_annotations_annotator_id'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_gt_annotations_slide', 'gt_annotations', ['dataset', 'slide_id', 'task'])
    op.create_index('ix_gt_annotations_annotator', 'gt_annotations', ['annotator_id'])

    # 3. run_metrics (SPEC-08 §7)
    op.create_table(
        'run_metrics',
        sa.Column('run_id', GUID(), nullable=False),
        sa.Column('metric_id', sa.Text(), nullable=False),
        sa.Column('slice_key', sa.Text(), server_default='', nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('ci_low', sa.Float(), nullable=True),
        sa.Column('ci_high', sa.Float(), nullable=True),
        sa.Column('n', sa.Integer(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['validation_runs.id'], ondelete='CASCADE', name='fk_run_metrics_run_id'),
        sa.PrimaryKeyConstraint('run_id', 'metric_id', 'slice_key')
    )
    op.create_index('ix_run_metrics_lookup', 'run_metrics', ['run_id', 'metric_id'])

    # 4. annotation_tasks (review tasks queue)
    op.create_table(
        'annotation_tasks',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('dataset', sa.Text(), nullable=False),
        sa.Column('slide_id', sa.Text(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('regions_um', JSONType, server_default='[]', nullable=False),
        sa.Column('blind', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('definition_md', sa.Text(), server_default='', nullable=False),
        sa.Column('protocol_version', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('mitosis_points', 'component_scores', 'grade', 'tumor_region')",
            name='ck_annotation_tasks_kind'
        ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ann_tasks_dataset_slide', 'annotation_tasks', ['dataset', 'slide_id'])

    # 5. qa_items (label QA queue)
    op.create_table(
        'qa_items',
        sa.Column('patient_id', sa.Text(), nullable=False),
        sa.Column('dataset', sa.Text(), nullable=False),
        sa.Column('protocol_version', sa.Text(), nullable=False),
        sa.Column('report_text_url', sa.Text(), nullable=False),
        sa.Column('regex_data', JSONType, server_default='{}', nullable=False),
        sa.Column('llm_data', JSONType, server_default='{}', nullable=False),
        sa.Column('status', sa.Text(), server_default='pending', nullable=False),
        sa.Column('reviewed_by', GUID(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'edited', 'excluded')",
            name='ck_qa_items_status'
        ),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], name='fk_qa_items_reviewed_by'),
        sa.PrimaryKeyConstraint('patient_id')
    )
    op.create_index('ix_qa_items_status', 'qa_items', ['status'])


def downgrade() -> None:
    op.drop_table('qa_items')
    op.drop_table('annotation_tasks')
    op.drop_table('run_metrics')
    op.drop_table('gt_annotations')
    op.drop_table('issues')
