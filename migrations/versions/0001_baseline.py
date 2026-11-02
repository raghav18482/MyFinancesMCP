"""Baseline: the app's tables as production has them before Alembic.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-02

A frozen snapshot. It never imports the models, so later model changes cannot
alter what this revision builds.

Source: scripts/replay_schema_history.py (every schema commit through R6
replayed on an empty database), because the production dump (U1) is not in
hand yet. That is a stand-in. Before the production stamp, this revision is
compared with a restore of the real dump (scripts/db_rehearse.py) and
corrected wherever they differ; docs/db/baseline-drift.md records each
difference and its decision.

Production is never upgraded *through* this revision: it is stamped at it.
Only empty databases (development, test, rehearsal) run upgrade().
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migrations import helpers

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

#: True when the previous release's code can run against this revision.
EXPAND_ONLY = True


def upgrade() -> None:
    op.create_table('app_instances',
    sa.Column('instance_id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('last_seen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('instance_id', name='app_instances_pkey')
    )
    op.create_index('ix_app_instances_last_seen_at', 'app_instances', ['last_seen_at'], unique=False)
    op.create_table('feedback_submissions',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('feedback', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('channel', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.CheckConstraint("channel IN ('react', 'jinja', 'api')", name='ck_feedback_channel'),
    sa.CheckConstraint('length(feedback) BETWEEN 1 AND 1000', name='ck_feedback_length'),
    sa.PrimaryKeyConstraint('id', name='feedback_submissions_pkey')
    )
    op.create_index('ix_feedback_submissions_created_at', 'feedback_submissions', ['created_at'], unique=False)
    op.create_table('oauth_clients',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('client_id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('client_secret_hash', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('metadata_json', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('client_name', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='oauth_clients_pkey')
    )
    op.create_index('ix_oauth_clients_client_id', 'oauth_clients', ['client_id'], unique=True)
    op.create_table('users',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('whatsapp_number', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('angel_api_key', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('angel_client_id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('angel_password_encrypted', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('angel_totp_secret_encrypted', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('angel_access_token', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('is_active', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('app_password_hash', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('session_version', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('last_manual_briefing_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name='users_pkey'),
    postgresql_ignore_search_path=False
    )
    op.create_index('ix_users_angel_client_id', 'users', ['angel_client_id'], unique=True)
    op.create_index('ix_users_whatsapp_number', 'users', ['whatsapp_number'], unique=True)
    op.create_table('chat_threads',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('agent_type', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('adk_session_id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('title', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('archived', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='chat_threads_user_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='chat_threads_pkey')
    )
    op.create_index('ix_chat_threads_adk_session_id', 'chat_threads', ['adk_session_id'], unique=True)
    op.create_index('ix_chat_threads_agent_type', 'chat_threads', ['agent_type'], unique=False)
    op.create_index('ix_chat_threads_archived', 'chat_threads', ['archived'], unique=False)
    op.create_index('ix_chat_threads_updated_at', 'chat_threads', ['updated_at'], unique=False)
    op.create_index('ix_chat_threads_user_id', 'chat_threads', ['user_id'], unique=False)
    op.create_table('decision_records',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('session_ref', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('proposal_id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('symbol', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('side', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('quantity', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('price', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('outcome', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('approved_by', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('order_id', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('payload_hash', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('payload_json', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('model_version', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('feature_set_version', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('model_scores_json', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('ruleset_id', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('compliance_json', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('rationale', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='decision_records_user_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='decision_records_pkey')
    )
    op.create_index('ix_decision_records_created_at', 'decision_records', ['created_at'], unique=False)
    op.create_index('ix_decision_records_outcome', 'decision_records', ['outcome'], unique=False)
    op.create_index('ix_decision_records_proposal_id', 'decision_records', ['proposal_id'], unique=False)
    op.create_index('ix_decision_records_session_ref', 'decision_records', ['session_ref'], unique=False)
    op.create_index('ix_decision_records_symbol', 'decision_records', ['symbol'], unique=False)
    op.create_index('ix_decision_records_user_id', 'decision_records', ['user_id'], unique=False)
    op.create_table('mcp_tokens',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('name', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('token_prefix', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('token_hash', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('scopes', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('revoked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='mcp_tokens_user_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='mcp_tokens_pkey')
    )
    op.create_index('ix_mcp_tokens_token_hash', 'mcp_tokens', ['token_hash'], unique=True)
    op.create_index('ix_mcp_tokens_user_id', 'mcp_tokens', ['user_id'], unique=False)
    op.create_table('oauth_grants',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('client_id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('scopes', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('resource', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('revoked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='oauth_grants_user_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='oauth_grants_pkey')
    )
    op.create_index('ix_oauth_grants_client_id', 'oauth_grants', ['client_id'], unique=False)
    op.create_index('ix_oauth_grants_user_id', 'oauth_grants', ['user_id'], unique=False)
    op.create_table('risk_profiles',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('age', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('goal', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('horizon_years', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('risk_tolerance', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('tax_bracket', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('max_single_order_value', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('max_position_pct', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('allowed_products', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('max_daily_trades', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='risk_profiles_user_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='risk_profiles_pkey')
    )
    op.create_index('ix_risk_profiles_user_id', 'risk_profiles', ['user_id'], unique=True)
    op.create_table('schedules',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('interval_minutes', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('next_run', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('last_run', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('enabled', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(), server_default=sa.text("'pending'::character varying"), autoincrement=False, nullable=True),
    sa.Column('claimed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('lease_expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('claim_token', sa.VARCHAR(length=32), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='schedules_user_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='schedules_pkey'),
    postgresql_ignore_search_path=False
    )
    op.create_index('ix_schedules_enabled', 'schedules', ['enabled'], unique=False)
    op.create_index('ix_schedules_next_run', 'schedules', ['next_run'], unique=False)
    op.create_index('ix_schedules_user_id', 'schedules', ['user_id'], unique=False)
    op.create_table('logs',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('schedule_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('status', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('message', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('duration_ms', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], name='logs_schedule_id_fkey'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='logs_user_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='logs_pkey')
    )
    op.create_index('ix_logs_created_at', 'logs', ['created_at'], unique=False)
    op.create_index('ix_logs_user_id', 'logs', ['user_id'], unique=False)
    op.create_table('oauth_codes',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('code_hash', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('client_id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('scopes', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('resource', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('code_challenge', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('redirect_uri', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('redirect_uri_provided_explicitly', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('grant_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['grant_id'], ['oauth_grants.id'], name='oauth_codes_grant_id_fkey'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='oauth_codes_user_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='oauth_codes_pkey')
    )
    op.create_index('ix_oauth_codes_client_id', 'oauth_codes', ['client_id'], unique=False)
    op.create_index('ix_oauth_codes_code_hash', 'oauth_codes', ['code_hash'], unique=True)
    op.create_index('ix_oauth_codes_user_id', 'oauth_codes', ['user_id'], unique=False)
    op.create_table('oauth_tokens',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('grant_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('token_hash', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('revoked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['grant_id'], ['oauth_grants.id'], name='oauth_tokens_grant_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='oauth_tokens_pkey')
    )
    op.create_index('ix_oauth_tokens_grant_id', 'oauth_tokens', ['grant_id'], unique=False)
    op.create_index('ix_oauth_tokens_kind', 'oauth_tokens', ['kind'], unique=False)
    op.create_index('ix_oauth_tokens_token_hash', 'oauth_tokens', ['token_hash'], unique=True)


def downgrade() -> None:
    # Children first. Each drop refuses if the table holds rows.
    helpers.drop_table_if_empty("oauth_tokens")
    helpers.drop_table_if_empty("oauth_codes")
    helpers.drop_table_if_empty("logs")
    helpers.drop_table_if_empty("schedules")
    helpers.drop_table_if_empty("risk_profiles")
    helpers.drop_table_if_empty("oauth_grants")
    helpers.drop_table_if_empty("mcp_tokens")
    helpers.drop_table_if_empty("decision_records")
    helpers.drop_table_if_empty("chat_threads")
    helpers.drop_table_if_empty("users")
    helpers.drop_table_if_empty("oauth_clients")
    helpers.drop_table_if_empty("feedback_submissions")
    helpers.drop_table_if_empty("app_instances")
