"""Users and sessions for Google Workspace sign-in (SPEC-03 §3.3).

v5 had no users table: roles came from request headers, so no stored role needs mapping from
``technician`` to ``viewer``. Emails are stored lower-cased (a CHECK), which SQLite and
PostgreSQL both enforce, instead of CITEXT, which SQLite lacks.

Revision ID: 0009_auth
Revises: 0008_case_specimen_type
Create Date: 2026-09-30 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.models.base import GUID

# revision identifiers, used by Alembic.
revision: str = '0009_auth'
down_revision: Union[str, None] = '0008_case_specimen_type'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', GUID(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('google_sub', sa.String(), nullable=True),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('status', sa.String(), server_default='invited', nullable=False),
    sa.Column('allowlisted_external', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('created_by', GUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("role IN ('admin', 'researcher', 'pathologist', 'viewer')", name='ck_users_role'),
    sa.CheckConstraint("status IN ('invited', 'active', 'disabled')", name='ck_users_status'),
    sa.CheckConstraint('email = lower(email)', name='ck_users_email_lower'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('google_sub')
    )
    op.create_table('sessions',
    sa.Column('id', GUID(), nullable=False),
    sa.Column('user_id', GUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ip', sa.String(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_sessions_user_id', table_name='sessions')
    op.drop_table('sessions')
    op.drop_table('users')
