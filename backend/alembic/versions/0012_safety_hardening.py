"""Safety hardening: soft delete, idempotency keys, and audit trigger (SPEC-03 §5).

Revision ID: 0012_safety_hardening
Revises: 0011_run_controller_lease
Create Date: 2026-09-30 14:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = '0012_safety_hardening'
down_revision: Union[str, None] = '0011_run_controller_lease'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(JSONB, "postgresql")


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == 'sqlite'


def upgrade() -> None:
    # 1. Soft delete on cases (SPEC-03 §5.3.1)
    op.add_column('cases', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_cases_deleted_at', 'cases', ['deleted_at'], unique=False)

    # 2. Idempotency keys (SPEC-03 §5.3.3)
    op.create_table(
        'idempotency_keys',
        sa.Column('user_id', sa.String(), primary_key=True, nullable=False),
        sa.Column('key', sa.String(255), primary_key=True, nullable=False),
        sa.Column('endpoint', sa.String(), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('response_body', JSONType, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('in_flight', 'completed')", name='ck_idempotency_status'),
    )
    op.create_index('ix_idempotency_keys_expires_at', 'idempotency_keys', ['expires_at'], unique=False)

    # 3. Append-only trigger on audit_events (SPEC-03 §5.5)
    if _is_sqlite():
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE is prohibited');
            END;
        """)
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only: DELETE is prohibited');
            END;
        """)
    else:
        op.execute("""
            CREATE OR REPLACE FUNCTION trg_audit_events_immutable()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events is append-only: UPDATE and DELETE are prohibited';
            END;
            $$ LANGUAGE plpgsql;
        """)
        op.execute("""
            CREATE TRIGGER trg_audit_events_immutable
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION trg_audit_events_immutable();
        """)
        # The API runs migrations as its own DB role, so CURRENT_USER is the application role.
        op.execute("REVOKE UPDATE, DELETE ON audit_events FROM CURRENT_USER;")
        op.execute("REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;")


def downgrade() -> None:
    if _is_sqlite():
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_delete;")
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_update;")
    else:
        op.execute("GRANT UPDATE, DELETE ON audit_events TO CURRENT_USER;")
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events;")
        op.execute("DROP FUNCTION IF EXISTS trg_audit_events_immutable();")

    op.drop_index('ix_idempotency_keys_expires_at', table_name='idempotency_keys')
    op.drop_table('idempotency_keys')

    op.drop_index('ix_cases_deleted_at', table_name='cases')
    op.drop_column('cases', 'deleted_at')
