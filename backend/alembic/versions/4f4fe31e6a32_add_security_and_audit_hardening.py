"""add security and audit hardening

Revision ID: 4f4fe31e6a32
Revises: b8a2bcd5965a
Create Date: 2026-03-31 16:35:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4f4fe31e6a32'
down_revision = 'b8a2bcd5965a'
branch_labels = None
depends_on = None


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col['name'] for col in inspector.get_columns(table_name)}
    if column.name not in existing:
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing(
        'users',
        sa.Column('failed_login_attempts', sa.Integer(), nullable=False, server_default='0'),
    )
    _add_column_if_missing('users', sa.Column('lockout_until', sa.DateTime(), nullable=True))
    _add_column_if_missing('users', sa.Column('last_login_at', sa.DateTime(), nullable=True))

    _add_column_if_missing(
        'audit_logs',
        sa.Column('previous_hash', sa.String(length=64), nullable=False, server_default=''),
    )
    _add_column_if_missing(
        'audit_logs',
        sa.Column('entry_hash', sa.String(length=64), nullable=False, server_default=''),
    )

    _add_column_if_missing(
        'users',
        sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
    )

    _add_column_if_missing(
        'clinical_summaries',
        sa.Column('source_notes_hash', sa.String(length=64), nullable=False, server_default=''),
    )
    _add_column_if_missing(
        'clinical_summaries',
        sa.Column('is_reviewed', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    _add_column_if_missing(
        'clinical_summaries',
        sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True),
    )
    _add_column_if_missing(
        'clinical_summaries',
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    )
    _add_column_if_missing(
        'clinical_summaries',
        sa.Column('review_notes', sa.Text(), nullable=False, server_default=''),
    )

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'auth_sessions' not in existing_tables:
        op.create_table(
            'auth_sessions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('refresh_token_hash', sa.String(length=64), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('last_seen_at', sa.DateTime(), nullable=True),
            sa.Column('ip_address', sa.String(length=64), nullable=True),
            sa.Column('user_agent', sa.String(length=255), nullable=True),
        )

    if 'security_events' not in existing_tables:
        op.create_table(
            'security_events',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('timestamp', sa.DateTime(), nullable=True),
            sa.Column('event_type', sa.String(length=64), nullable=False),
            sa.Column('severity', sa.String(length=16), nullable=True),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('source_ip', sa.String(length=64), nullable=True),
            sa.Column('request_path', sa.String(length=255), nullable=True),
            sa.Column('details_json', sa.Text(), nullable=True),
        )

    if 'clinical_summary_revisions' not in existing_tables:
        op.create_table(
            'clinical_summary_revisions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('summary_id', sa.Integer(), sa.ForeignKey('clinical_summaries.id'), nullable=False),
            sa.Column('edited_by_user_id', sa.Integer(), nullable=True),
            sa.Column('previous_summary_text', sa.Text(), nullable=True),
            sa.Column('new_summary_text', sa.Text(), nullable=True),
            sa.Column('edit_note', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )

    if 'forecast_history' not in existing_tables:
        op.create_table(
            'forecast_history',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('scope', sa.String(length=16), nullable=False),
            sa.Column('scope_id', sa.Integer(), nullable=True),
            sa.Column('selected_model', sa.String(length=32), nullable=True),
            sa.Column('effective_model', sa.String(length=64), nullable=True),
            sa.Column('horizon_minutes', sa.Integer(), nullable=True),
            sa.Column('peak_predicted', sa.Float(), nullable=True),
            sa.Column('avg_predicted', sa.Float(), nullable=True),
            sa.Column('overload_expected', sa.Boolean(), nullable=True),
            sa.Column('mae', sa.Float(), nullable=True),
            sa.Column('rmse', sa.Float(), nullable=True),
            sa.Column('payload_json', sa.Text(), nullable=True),
            sa.Column('generated_at', sa.DateTime(), nullable=True),
        )

    if 'async_task_events' not in existing_tables:
        op.create_table(
            'async_task_events',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('timestamp', sa.DateTime(), nullable=True),
            sa.Column('task_name', sa.String(length=128), nullable=False),
            sa.Column('status', sa.String(length=32), nullable=False),
            sa.Column('retry_count', sa.Integer(), nullable=True),
            sa.Column('details_json', sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_columns = {col['name'] for col in inspector.get_columns('users')}
    if 'last_login_at' in user_columns:
        op.drop_column('users', 'last_login_at')
    if 'lockout_until' in user_columns:
        op.drop_column('users', 'lockout_until')
    if 'failed_login_attempts' in user_columns:
        op.drop_column('users', 'failed_login_attempts')

    audit_columns = {col['name'] for col in inspector.get_columns('audit_logs')}
    if 'entry_hash' in audit_columns:
        op.drop_column('audit_logs', 'entry_hash')
    if 'previous_hash' in audit_columns:
        op.drop_column('audit_logs', 'previous_hash')

    if 'token_version' in user_columns:
        op.drop_column('users', 'token_version')

    summary_columns = {col['name'] for col in inspector.get_columns('clinical_summaries')}
    if 'review_notes' in summary_columns:
        op.drop_column('clinical_summaries', 'review_notes')
    if 'reviewed_at' in summary_columns:
        op.drop_column('clinical_summaries', 'reviewed_at')
    if 'reviewed_by_user_id' in summary_columns:
        op.drop_column('clinical_summaries', 'reviewed_by_user_id')
    if 'is_reviewed' in summary_columns:
        op.drop_column('clinical_summaries', 'is_reviewed')
    if 'source_notes_hash' in summary_columns:
        op.drop_column('clinical_summaries', 'source_notes_hash')

    tables = set(inspector.get_table_names())
    if 'forecast_history' in tables:
        op.drop_table('forecast_history')
    if 'clinical_summary_revisions' in tables:
        op.drop_table('clinical_summary_revisions')
    if 'security_events' in tables:
        op.drop_table('security_events')
    if 'auth_sessions' in tables:
        op.drop_table('auth_sessions')
    if 'async_task_events' in tables:
        op.drop_table('async_task_events')
