"""T-8.MIG.01 — the baseline: the schema as it stood when Alembic took it over.

Every table the platform declares, generated from `app/db.py`'s metadata against an
**empty database** (`alembic revision --autogenerate`), so the DDL below is what the
models say rather than a copy somebody typed. From here the schema changes by migration:
`alembic check` compares the metadata with the database and fails when a model arrives
without one, which is the rule §4b principle 2 states ("the schema evolves by migration,
never by `create_all`").

Three things this file deliberately does **not** contain, because they are not tables and
would otherwise be a second copy of something that already exists:

* the company-scoping rows (row-level security, one policy per company-scoped table),
* the trail's triggers and the ledger's constraint triggers,
* the queue's own tables (`pgqueuer`'s).

All three are installed by `migrations/env.py` after the versioned steps: the first two by
**replaying the metadata's own `after_create` listeners** — the same functions a
`create_all` schema gets them from — and the third from pgqueuer's own DDL.
`tests/check_migrations.py` compares a migrated schema with a `create_all` one, table by
table, so the two entry points cannot drift apart.

**Downgrade** drops everything, in reverse dependency order, which is what makes the drill
in `tests/check_migrations.py` (drop, migrate again, compare) possible on a scratch
database. Nothing in the deployment pipeline runs it: a production downgrade is a restore
(`OPERATIONS.md`), not a script.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001_baseline'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('app_user',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('login', sa.String(length=64), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=True),
    sa.Column('display_name', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=True),
    sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('must_change_password', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('failed_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'disabled')", name='ck_app_user_status'),
    sa.CheckConstraint('failed_attempts >= 0', name='ck_app_user_failed_attempts_not_negative'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('login')
    )
    op.create_table('company',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('base_currency', sa.String(length=3), nullable=False),
    sa.Column('fiscal_year_start_month', sa.SmallInteger(), nullable=False),
    sa.Column('costing_method', sa.String(length=16), nullable=False),
    sa.Column('credit_check_mode', sa.String(length=8), nullable=True),
    sa.Column('cash_drawer_required', sa.Boolean(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("costing_method IN ('fifo', 'moving_average', 'standard_cost')", name='ck_company_costing_method'),
    sa.CheckConstraint("credit_check_mode IS NULL OR credit_check_mode IN ('off', 'warn', 'block')", name='ck_company_credit_check_mode'),
    sa.CheckConstraint("status IN ('active', 'suspended')", name='ck_company_status'),
    sa.CheckConstraint('fiscal_year_start_month BETWEEN 1 AND 12', name='ck_company_fiscal_year_start_month'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('job_run',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job', sa.String(length=64), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('running', 'succeeded', 'failed', 'skipped')", name='ck_job_run_status'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_job_run_job_started', 'job_run', ['job', 'started_at'], unique=False)
    op.create_index('uq_job_run_once', 'job_run', ['job', 'key'], unique=True, postgresql_where=sa.text("status = 'succeeded'"))
    op.create_table('account',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('class', sa.String(length=16), nullable=False),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("class IN ('asset', 'liability', 'equity', 'income', 'expense')", name='ck_account_class'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['parent_id'], ['account.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_account_company_code')
    )
    op.create_index(op.f('ix_account_company_id'), 'account', ['company_id'], unique=False)
    op.create_index(op.f('ix_account_parent_id'), 'account', ['parent_id'], unique=False)
    op.create_table('accounting_period',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('year', sa.SmallInteger(), nullable=False),
    sa.Column('month', sa.SmallInteger(), nullable=False),
    sa.Column('state', sa.String(length=8), nullable=False),
    sa.Column('changed_by', sa.String(length=64), nullable=False),
    sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.CheckConstraint("state IN ('open', 'closed')", name='ck_accounting_period_state'),
    sa.CheckConstraint('month BETWEEN 1 AND 12', name='ck_accounting_period_month'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'year', 'month', name='uq_accounting_period_month')
    )
    op.create_index(op.f('ix_accounting_period_company_id'), 'accounting_period', ['company_id'], unique=False)
    op.create_table('api_idempotency_key',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('method', sa.String(length=8), nullable=False),
    sa.Column('path', sa.String(length=128), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'key', name='uq_api_idempotency_company_key')
    )
    op.create_index(op.f('ix_api_idempotency_key_company_id'), 'api_idempotency_key', ['company_id'], unique=False)
    op.create_table('approval_workflow',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('doc_type', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'doc_type', name='uq_approval_workflow_doc_type')
    )
    op.create_index(op.f('ix_approval_workflow_company_id'), 'approval_workflow', ['company_id'], unique=False)
    op.create_table('audit_log',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('entity', sa.String(length=64), nullable=False),
    sa.Column('entity_id', sa.String(length=64), nullable=True),
    sa.Column('before_values', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after_values', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('origin_type', sa.String(length=64), nullable=True),
    sa.Column('origin_id', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_log_company_id'), 'audit_log', ['company_id'], unique=False)
    op.create_index(op.f('ix_audit_log_entity_id'), 'audit_log', ['entity_id'], unique=False)
    op.create_index(op.f('ix_audit_log_occurred_at'), 'audit_log', ['occurred_at'], unique=False)
    op.create_table('biometric_device',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('site', sa.String(length=128), nullable=True),
    sa.Column('registered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_pull_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_window_to', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_biometric_device_code')
    )
    op.create_index(op.f('ix_biometric_device_company_id'), 'biometric_device', ['company_id'], unique=False)
    op.create_table('coupon',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('campaign', sa.String(length=40), nullable=False),
    sa.Column('discount_type', sa.String(length=16), nullable=False),
    sa.Column('discount_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('valid_from', sa.Date(), nullable=True),
    sa.Column('valid_until', sa.Date(), nullable=True),
    sa.Column('max_redemptions', sa.Integer(), nullable=True),
    sa.Column('stacking_allowance', sa.Integer(), nullable=False),
    sa.CheckConstraint("discount_type <> 'percent' OR discount_value <= 100", name='ck_coupon_percent_within_a_hundred'),
    sa.CheckConstraint("discount_type IN ('percent', 'amount')", name='ck_coupon_discount_type'),
    sa.CheckConstraint('discount_value >= 0', name='ck_coupon_value_not_negative'),
    sa.CheckConstraint('max_redemptions IS NULL OR max_redemptions >= 1', name='ck_coupon_redemption_limit'),
    sa.CheckConstraint('stacking_allowance >= 1', name='ck_coupon_allowance'),
    sa.CheckConstraint('valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from', name='ck_coupon_window_the_right_way'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_coupon_company_code')
    )
    op.create_index(op.f('ix_coupon_company_id'), 'coupon', ['company_id'], unique=False)
    op.create_table('currency',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=3), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('char_length(code) = 3', name='ck_currency_code_length'),
    sa.CheckConstraint('code = upper(code)', name='ck_currency_code_upper'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_currency_company_code')
    )
    op.create_index(op.f('ix_currency_company_id'), 'currency', ['company_id'], unique=False)
    op.create_table('dunning_level',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('from_days', sa.Integer(), nullable=False),
    sa.Column('to_days', sa.Integer(), nullable=True),
    sa.Column('channel', sa.String(length=8), nullable=False),
    sa.Column('template', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("channel IN ('email', 'sms')", name='ck_dunning_level_channel'),
    sa.CheckConstraint('from_days >= 0', name='ck_dunning_level_from_day'),
    sa.CheckConstraint('to_days IS NULL OR to_days >= from_days', name='ck_dunning_level_span'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_dunning_level_company_code')
    )
    op.create_index(op.f('ix_dunning_level_company_id'), 'dunning_level', ['company_id'], unique=False)
    op.create_table('fx_rate',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('base_currency', sa.String(length=3), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('rate_date', sa.Date(), nullable=False),
    sa.Column('rate', sa.Numeric(precision=20, scale=10), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.CheckConstraint('rate > 0', name='ck_fx_rate_positive'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'base_currency', 'currency', 'rate_date', name='uq_fx_rate_pair_date')
    )
    op.create_index(op.f('ix_fx_rate_company_id'), 'fx_rate', ['company_id'], unique=False)
    op.create_table('holiday',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('region', sa.String(length=16), nullable=True),
    sa.Column('holiday_date', sa.Date(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.CheckConstraint("kind IN ('regular', 'special')", name='ck_holiday_kind'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_holiday_company_id'), 'holiday', ['company_id'], unique=False)
    op.create_index('uq_holiday_company_region_day', 'holiday', ['company_id', 'region', 'holiday_date'], unique=True, postgresql_nulls_not_distinct=True)
    op.create_table('inbound_event',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'source', 'idempotency_key', name='uq_inbound_event_once')
    )
    op.create_index(op.f('ix_inbound_event_company_id'), 'inbound_event', ['company_id'], unique=False)
    op.create_table('item',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('base_uom', sa.String(length=16), nullable=False),
    sa.Column('traceability_mode', sa.String(length=16), nullable=False),
    sa.Column('standard_cost', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('lead_time_days', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("traceability_mode IN ('none', 'batch_lot', 'serial')", name='ck_item_traceability_mode'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'sku', name='uq_item_company_sku')
    )
    op.create_index(op.f('ix_item_company_id'), 'item', ['company_id'], unique=False)
    op.create_table('journal_entry',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('posting_date', sa.Date(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('exchange_rate', sa.Numeric(precision=20, scale=10), nullable=False),
    sa.Column('memo', sa.Text(), nullable=True),
    sa.Column('source_type', sa.String(length=32), nullable=True),
    sa.Column('source_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('(source_type IS NULL) = (source_id IS NULL)', name='ck_journal_entry_source_pair'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_journal_entry_company_id'), 'journal_entry', ['company_id'], unique=False)
    op.create_index(op.f('ix_journal_entry_source_id'), 'journal_entry', ['source_id'], unique=False)
    op.create_index(op.f('ix_journal_entry_source_type'), 'journal_entry', ['source_type'], unique=False)
    op.create_table('leave_type',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('paid', sa.Boolean(), nullable=False),
    sa.Column('cadence', sa.String(length=16), nullable=False),
    sa.Column('accrual_days', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('carry_forward_cap', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("cadence IN ('monthly', 'annual', 'per_cycle')", name='ck_leave_type_cadence'),
    sa.CheckConstraint('accrual_days >= 0', name='ck_leave_type_accrual'),
    sa.CheckConstraint('carry_forward_cap IS NULL OR carry_forward_cap >= 0', name='ck_leave_type_carry_forward'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_leave_type_company_code')
    )
    op.create_index(op.f('ix_leave_type_company_id'), 'leave_type', ['company_id'], unique=False)
    op.create_table('loan_policy',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('max_recovery_percent', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('stated_by', sa.String(length=64), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.CheckConstraint('max_recovery_percent >= 0 AND max_recovery_percent <= 100', name='ck_loan_policy_percent'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'effective_from', name='uq_loan_policy_effective')
    )
    op.create_index(op.f('ix_loan_policy_company_id'), 'loan_policy', ['company_id'], unique=False)
    op.create_table('location',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('type', sa.String(length=16), nullable=False),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("type IN ('warehouse', 'zone', 'aisle', 'bin')", name='ck_location_type'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['parent_id'], ['location.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_location_company_code')
    )
    op.create_index(op.f('ix_location_company_id'), 'location', ['company_id'], unique=False)
    op.create_index(op.f('ix_location_parent_id'), 'location', ['parent_id'], unique=False)
    op.create_table('match_tolerance',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('quantity_percent', sa.Numeric(precision=9, scale=4), nullable=False),
    sa.Column('price_percent', sa.Numeric(precision=9, scale=4), nullable=False),
    sa.Column('tax_percent', sa.Numeric(precision=9, scale=4), nullable=False),
    sa.CheckConstraint('price_percent >= 0', name='ck_match_tolerance_price'),
    sa.CheckConstraint('quantity_percent >= 0', name='ck_match_tolerance_quantity'),
    sa.CheckConstraint('tax_percent >= 0', name='ck_match_tolerance_tax'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', name='uq_match_tolerance_company')
    )
    op.create_index(op.f('ix_match_tolerance_company_id'), 'match_tolerance', ['company_id'], unique=False)
    op.create_table('mrp_run',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('run_on', sa.Date(), nullable=False),
    sa.Column('start_on', sa.Date(), nullable=False),
    sa.Column('horizon_days', sa.Integer(), nullable=False),
    sa.Column('bucket_days', sa.Integer(), nullable=False),
    sa.Column('demand_sources', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('bucket_days > 0', name='ck_mrp_run_bucket'),
    sa.CheckConstraint('horizon_days > 0', name='ck_mrp_run_horizon'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_mrp_run_company_id'), 'mrp_run', ['company_id'], unique=False)
    op.create_index(op.f('ix_mrp_run_run_on'), 'mrp_run', ['run_on'], unique=False)
    op.create_table('outbound_delivery',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('destination', sa.String(length=200), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('sending', 'sent', 'failed')", name='ck_outbound_delivery_status'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outbound_delivery_company_id'), 'outbound_delivery', ['company_id'], unique=False)
    op.create_table('overtime_rule',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('day_type', sa.String(length=16), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('threshold_minutes', sa.Integer(), nullable=False),
    sa.Column('multiplier', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('note', sa.String(length=160), nullable=True),
    sa.CheckConstraint("day_type IN ('working', 'rest', 'holiday')", name='ck_overtime_rule_day_type'),
    sa.CheckConstraint('multiplier > 0', name='ck_overtime_rule_multiplier'),
    sa.CheckConstraint('threshold_minutes >= 0', name='ck_overtime_rule_threshold'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'day_type', 'effective_from', name='uq_overtime_rule_start')
    )
    op.create_index(op.f('ix_overtime_rule_company_id'), 'overtime_rule', ['company_id'], unique=False)
    op.create_table('party',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('tax_id', sa.String(length=32), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_party_company_code')
    )
    op.create_index(op.f('ix_party_company_id'), 'party', ['company_id'], unique=False)
    op.create_table('payroll_component',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('basis', sa.String(length=24), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('rate_percent', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('taxable', sa.Boolean(), nullable=False),
    sa.Column('order_no', sa.Integer(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('pack_version', sa.String(length=32), nullable=True),
    sa.Column('account_code', sa.String(length=32), nullable=True),
    sa.CheckConstraint("(basis IN ('fixed', 'per_worked_day')) = (amount IS NOT NULL)", name='ck_payroll_component_amount_basis'),
    sa.CheckConstraint("(basis IN ('percent_of_basic', 'percent_of_gross')) = (rate_percent IS NOT NULL)", name='ck_payroll_component_rate_basis'),
    sa.CheckConstraint("basis IN ('fixed', 'percent_of_basic', 'percent_of_gross', 'per_worked_day', 'loan_schedule')", name='ck_payroll_component_basis'),
    sa.CheckConstraint("kind IN ('earning', 'deduction', 'employer_contribution')", name='ck_payroll_component_kind'),
    sa.CheckConstraint("source IN ('pack', 'company')", name='ck_payroll_component_source'),
    sa.CheckConstraint('amount IS NULL OR amount >= 0', name='ck_payroll_component_amount'),
    sa.CheckConstraint('char_length(code) > 0', name='ck_payroll_component_code'),
    sa.CheckConstraint('char_length(name) > 0', name='ck_payroll_component_name'),
    sa.CheckConstraint('order_no >= 0', name='ck_payroll_component_order'),
    sa.CheckConstraint('rate_percent IS NULL OR (rate_percent >= 0 AND rate_percent <= 100)', name='ck_payroll_component_rate'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', 'effective_from', name='uq_payroll_component_effective')
    )
    op.create_index(op.f('ix_payroll_component_company_id'), 'payroll_component', ['company_id'], unique=False)
    op.create_table('pipeline_stage',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('is_won', sa.Boolean(), nullable=False),
    sa.Column('is_lost', sa.Boolean(), nullable=False),
    sa.CheckConstraint('NOT (is_won AND is_lost)', name='ck_pipeline_stage_won_or_lost'),
    sa.CheckConstraint('position >= 0', name='ck_pipeline_stage_position'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'name', name='uq_pipeline_stage_company_name'),
    sa.UniqueConstraint('company_id', 'position', name='uq_pipeline_stage_company_position')
    )
    op.create_index(op.f('ix_pipeline_stage_company_id'), 'pipeline_stage', ['company_id'], unique=False)
    op.create_table('pos_shift',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('terminal', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('opening_float', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('opened_on', sa.Date(), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('opened_by', sa.String(length=64), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_by', sa.String(length=64), nullable=True),
    sa.Column('expected_cash', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('counted_cash', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('variance', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('variance_reason', sa.String(length=200), nullable=True),
    sa.CheckConstraint("status = 'open' OR counted_cash IS NOT NULL", name='ck_pos_shift_closed_has_count'),
    sa.CheckConstraint("status IN ('open', 'closed')", name='ck_pos_shift_status'),
    sa.CheckConstraint('counted_cash IS NULL OR counted_cash >= 0', name='ck_pos_shift_count'),
    sa.CheckConstraint('opening_float >= 0', name='ck_pos_shift_float'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_pos_shift_company_id'), 'pos_shift', ['company_id'], unique=False)
    op.create_index(op.f('ix_pos_shift_opened_on'), 'pos_shift', ['opened_on'], unique=False)
    op.create_index(op.f('ix_pos_shift_terminal'), 'pos_shift', ['terminal'], unique=False)
    op.create_index('uq_pos_shift_open_terminal', 'pos_shift', ['company_id', 'terminal'], unique=True, postgresql_where=sa.text("status = 'open'"))
    op.create_table('report_definition',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('capability', sa.String(length=64), nullable=False),
    sa.Column('schedule', sa.String(length=64), nullable=False),
    sa.Column('period', sa.String(length=16), server_default='month', nullable=False),
    sa.Column('recipients', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('registered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_report_definition_code')
    )
    op.create_index(op.f('ix_report_definition_company_id'), 'report_definition', ['company_id'], unique=False)
    op.create_table('role',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_role_company_code')
    )
    op.create_index(op.f('ix_role_company_id'), 'role', ['company_id'], unique=False)
    op.create_table('shift',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('starts_at', sa.Time(), nullable=False),
    sa.Column('ends_at', sa.Time(), nullable=False),
    sa.Column('break_minutes', sa.Integer(), nullable=False),
    sa.Column('late_grace_minutes', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('break_minutes >= 0', name='ck_shift_break_minutes'),
    sa.CheckConstraint('late_grace_minutes >= 0', name='ck_shift_late_grace_minutes'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_shift_company_code')
    )
    op.create_index(op.f('ix_shift_company_id'), 'shift', ['company_id'], unique=False)
    op.create_table('user_membership',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('member_company_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['member_company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'member_company_id', name='uq_user_membership_once')
    )
    op.create_index(op.f('ix_user_membership_member_company_id'), 'user_membership', ['member_company_id'], unique=False)
    op.create_index(op.f('ix_user_membership_user_id'), 'user_membership', ['user_id'], unique=False)
    op.create_table('user_recovery_code',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_hash')
    )
    op.create_index(op.f('ix_user_recovery_code_user_id'), 'user_recovery_code', ['user_id'], unique=False)
    op.create_table('user_session',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('active_company_id', sa.Uuid(), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('mfa_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_agent', sa.String(length=256), nullable=True),
    sa.Column('address', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['active_company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_user_session_active_company_id'), 'user_session', ['active_company_id'], unique=False)
    op.create_index(op.f('ix_user_session_user_id'), 'user_session', ['user_id'], unique=False)
    op.create_table('user_totp',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('secret', sa.String(length=64), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('work_center',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('capacity_minutes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('capacity_period', sa.String(length=8), nullable=False),
    sa.Column('downtime_percent', sa.Numeric(precision=9, scale=4), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("capacity_period IN ('day', 'week', 'month')", name='ck_work_center_period'),
    sa.CheckConstraint('capacity_minutes > 0', name='ck_work_center_capacity'),
    sa.CheckConstraint('downtime_percent >= 0 AND downtime_percent < 100', name='ck_work_center_downtime'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_work_center_code')
    )
    op.create_index(op.f('ix_work_center_company_id'), 'work_center', ['company_id'], unique=False)
    op.create_table('account_mapping',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'key', name='uq_account_mapping_key')
    )
    op.create_index(op.f('ix_account_mapping_account_id'), 'account_mapping', ['account_id'], unique=False)
    op.create_index(op.f('ix_account_mapping_company_id'), 'account_mapping', ['company_id'], unique=False)
    op.create_table('advanced_plan',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('basic_run_id', sa.Uuid(), nullable=False),
    sa.Column('run_on', sa.Date(), nullable=False),
    sa.Column('start_on', sa.Date(), nullable=False),
    sa.Column('horizon_days', sa.Integer(), nullable=False),
    sa.Column('bucket_days', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('bucket_days > 0', name='ck_advanced_plan_bucket'),
    sa.CheckConstraint('horizon_days > 0', name='ck_advanced_plan_horizon'),
    sa.ForeignKeyConstraint(['basic_run_id'], ['mrp_run.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_advanced_plan_basic_run_id'), 'advanced_plan', ['basic_run_id'], unique=False)
    op.create_index(op.f('ix_advanced_plan_company_id'), 'advanced_plan', ['company_id'], unique=False)
    op.create_index(op.f('ix_advanced_plan_run_on'), 'advanced_plan', ['run_on'], unique=False)
    op.create_table('approval_level',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workflow_id', sa.Uuid(), nullable=False),
    sa.Column('level_no', sa.Integer(), nullable=False),
    sa.Column('threshold_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('role', sa.String(length=64), nullable=False),
    sa.CheckConstraint('level_no >= 1', name='ck_approval_level_starts_at_one'),
    sa.CheckConstraint('threshold_amount >= 0', name='ck_approval_level_threshold'),
    sa.ForeignKeyConstraint(['workflow_id'], ['approval_workflow.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workflow_id', 'level_no', name='uq_approval_level_order')
    )
    op.create_index(op.f('ix_approval_level_workflow_id'), 'approval_level', ['workflow_id'], unique=False)
    op.create_table('approval_request',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('workflow_id', sa.Uuid(), nullable=False),
    sa.Column('doc_type', sa.String(length=64), nullable=False),
    sa.Column('document_id', sa.String(length=64), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('current_level', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('pending', 'approved', 'rejected', 'returned')", name='ck_approval_request_state'),
    sa.CheckConstraint('amount >= 0', name='ck_approval_request_amount'),
    sa.CheckConstraint('current_level >= 1', name='ck_approval_request_level'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['workflow_id'], ['approval_workflow.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_approval_request_company_id'), 'approval_request', ['company_id'], unique=False)
    op.create_index(op.f('ix_approval_request_workflow_id'), 'approval_request', ['workflow_id'], unique=False)
    op.create_table('batch',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('expiry_date', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('item_id', 'code', name='uq_batch_item_code')
    )
    op.create_index(op.f('ix_batch_company_id'), 'batch', ['company_id'], unique=False)
    op.create_index(op.f('ix_batch_item_id'), 'batch', ['item_id'], unique=False)
    op.create_table('biometric_pull',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=False),
    sa.Column('inbound_event_id', sa.Uuid(), nullable=True),
    sa.Column('window_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('window_to', sa.DateTime(timezone=True), nullable=False),
    sa.Column('pulled_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('readings', sa.Integer(), nullable=False),
    sa.Column('recorded', sa.Integer(), nullable=False),
    sa.Column('duplicates', sa.Integer(), nullable=False),
    sa.Column('parked', sa.Integer(), nullable=False),
    sa.CheckConstraint('readings >= 0', name='ck_biometric_pull_readings'),
    sa.CheckConstraint('recorded >= 0 AND duplicates >= 0 AND parked >= 0', name='ck_biometric_pull_counts'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['device_id'], ['biometric_device.id'], ),
    sa.ForeignKeyConstraint(['inbound_event_id'], ['inbound_event.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'device_id', 'window_from', 'window_to', name='uq_biometric_pull_window'),
    sa.UniqueConstraint('inbound_event_id')
    )
    op.create_index(op.f('ix_biometric_pull_company_id'), 'biometric_pull', ['company_id'], unique=False)
    op.create_index(op.f('ix_biometric_pull_device_id'), 'biometric_pull', ['device_id'], unique=False)
    op.create_table('bom',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('memo', sa.String(length=200), nullable=True),
    sa.CheckConstraint("status IN ('draft', 'released')", name='ck_bom_status'),
    sa.CheckConstraint('version >= 1', name='ck_bom_version_starts_at_one'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'item_id', 'version', name='uq_bom_item_version')
    )
    op.create_index(op.f('ix_bom_company_id'), 'bom', ['company_id'], unique=False)
    op.create_index(op.f('ix_bom_item_id'), 'bom', ['item_id'], unique=False)
    op.create_table('coupon_redemption',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('coupon_id', sa.Uuid(), nullable=False),
    sa.Column('document_type', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('discount_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('redeemed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('discount_amount >= 0', name='ck_redemption_not_negative'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['coupon_id'], ['coupon.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('coupon_id', 'document_type', 'document_id', name='uq_coupon_once_per_document')
    )
    op.create_index(op.f('ix_coupon_redemption_company_id'), 'coupon_redemption', ['company_id'], unique=False)
    op.create_index(op.f('ix_coupon_redemption_coupon_id'), 'coupon_redemption', ['coupon_id'], unique=False)
    op.create_index(op.f('ix_coupon_redemption_document_id'), 'coupon_redemption', ['document_id'], unique=False)
    op.create_table('customer',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('party_id', sa.Uuid(), nullable=False),
    sa.Column('payment_terms_days', sa.Integer(), nullable=False),
    sa.Column('transaction_currency', sa.String(length=3), nullable=True),
    sa.Column('credit_limit', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('tier', sa.String(length=32), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('credit_limit IS NULL OR credit_limit >= 0', name='ck_customer_credit_limit'),
    sa.CheckConstraint('payment_terms_days >= 0', name='ck_customer_payment_terms'),
    sa.CheckConstraint('transaction_currency IS NULL OR char_length(transaction_currency) = 3', name='ck_customer_currency_length'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['party_id'], ['party.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'party_id', name='uq_customer_company_party')
    )
    op.create_index(op.f('ix_customer_company_id'), 'customer', ['company_id'], unique=False)
    op.create_index(op.f('ix_customer_party_id'), 'customer', ['party_id'], unique=False)
    op.create_table('delivery_attempt',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('delivery_id', sa.Uuid(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('attempted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('sending', 'sent', 'failed')", name='ck_delivery_attempt_status'),
    sa.ForeignKeyConstraint(['delivery_id'], ['outbound_delivery.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('delivery_id', 'attempt', name='uq_delivery_attempt_number')
    )
    op.create_index(op.f('ix_delivery_attempt_delivery_id'), 'delivery_attempt', ['delivery_id'], unique=False)
    op.create_table('employee',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('party_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('hire_date', sa.Date(), nullable=False),
    sa.Column('date_of_birth', sa.Date(), nullable=True),
    sa.Column('personal_email', sa.String(length=160), nullable=True),
    sa.Column('mobile_number', sa.String(length=40), nullable=True),
    sa.Column('home_address', sa.String(length=255), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['party_id'], ['party.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_employee_company_number'),
    sa.UniqueConstraint('company_id', 'party_id', name='uq_employee_company_party')
    )
    op.create_index(op.f('ix_employee_company_id'), 'employee', ['company_id'], unique=False)
    op.create_index(op.f('ix_employee_party_id'), 'employee', ['party_id'], unique=False)
    op.create_table('field_permission',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('role_id', sa.Uuid(), nullable=False),
    sa.Column('entity', sa.String(length=64), nullable=False),
    sa.Column('field', sa.String(length=64), nullable=False),
    sa.Column('can_read', sa.Boolean(), nullable=False),
    sa.Column('can_write', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['role.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('role_id', 'entity', 'field', name='uq_field_permission_once')
    )
    op.create_index(op.f('ix_field_permission_role_id'), 'field_permission', ['role_id'], unique=False)
    op.create_table('item_variant',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'sku', name='uq_item_variant_company_sku'),
    sa.UniqueConstraint('item_id', 'attributes', name='uq_item_variant_attributes')
    )
    op.create_index(op.f('ix_item_variant_company_id'), 'item_variant', ['company_id'], unique=False)
    op.create_index(op.f('ix_item_variant_item_id'), 'item_variant', ['item_id'], unique=False)
    op.create_table('journal_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('entry_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('account', sa.String(length=64), nullable=False),
    sa.Column('debit', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('credit', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('party_id', sa.Uuid(), nullable=True),
    sa.Column('party', sa.String(length=64), nullable=True),
    sa.CheckConstraint('NOT (debit > 0 AND credit > 0)', name='ck_journal_line_one_side_only'),
    sa.CheckConstraint('debit + credit > 0', name='ck_journal_line_not_zero'),
    sa.CheckConstraint('debit >= 0 AND credit >= 0', name='ck_journal_line_no_negative'),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ),
    sa.ForeignKeyConstraint(['entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['party_id'], ['party.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_journal_line_account_id'), 'journal_line', ['account_id'], unique=False)
    op.create_index(op.f('ix_journal_line_entry_id'), 'journal_line', ['entry_id'], unique=False)
    op.create_index(op.f('ix_journal_line_party_id'), 'journal_line', ['party_id'], unique=False)
    op.create_table('mrp_requirement',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('bucket_start', sa.Date(), nullable=False),
    sa.Column('bucket_end', sa.Date(), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('gross', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('available', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('supply', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('net', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('lead_time_days', sa.Integer(), nullable=False),
    sa.Column('release_on', sa.Date(), nullable=True),
    sa.Column('constrained', sa.Boolean(), nullable=False),
    sa.Column('constrained_by', sa.Text(), nullable=True),
    sa.CheckConstraint('gross >= 0', name='ck_mrp_requirement_gross'),
    sa.CheckConstraint('level >= 0', name='ck_mrp_requirement_level'),
    sa.CheckConstraint('net >= 0', name='ck_mrp_requirement_net'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['mrp_run.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_mrp_requirement_bucket_start'), 'mrp_requirement', ['bucket_start'], unique=False)
    op.create_index(op.f('ix_mrp_requirement_company_id'), 'mrp_requirement', ['company_id'], unique=False)
    op.create_index(op.f('ix_mrp_requirement_item_id'), 'mrp_requirement', ['item_id'], unique=False)
    op.create_index(op.f('ix_mrp_requirement_run_id'), 'mrp_requirement', ['run_id'], unique=False)
    op.create_table('party_role',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('party_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.CheckConstraint("role IN ('customer', 'supplier', 'employee')", name='ck_party_role_known'),
    sa.ForeignKeyConstraint(['party_id'], ['party.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('party_id', 'role', name='uq_party_role_once')
    )
    op.create_index(op.f('ix_party_role_party_id'), 'party_role', ['party_id'], unique=False)
    op.create_table('payroll_run',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('from_date', sa.Date(), nullable=False),
    sa.Column('to_date', sa.Date(), nullable=False),
    sa.Column('cutoff_date', sa.Date(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('supersedes_id', sa.Uuid(), nullable=True),
    sa.Column('superseded_by_id', sa.Uuid(), nullable=True),
    sa.Column('structure_as_of', sa.Date(), nullable=False),
    sa.Column('pack_versions', sa.String(length=64), nullable=True),
    sa.Column('computed_by', sa.String(length=64), nullable=True),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_by', sa.String(length=64), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('correction_actor', sa.String(length=64), nullable=True),
    sa.Column('correction_reason', sa.String(length=255), nullable=True),
    sa.Column('journal_entry_id', sa.Uuid(), nullable=True),
    sa.Column('reversal_entry_id', sa.Uuid(), nullable=True),
    sa.Column('posted_on', sa.Date(), nullable=True),
    sa.Column('posted_by', sa.String(length=64), nullable=True),
    sa.CheckConstraint("state IN ('draft', 'computed', 'approved')", name='ck_payroll_run_state'),
    sa.CheckConstraint('cutoff_date BETWEEN from_date AND to_date', name='ck_payroll_run_cutoff'),
    sa.CheckConstraint('revision >= 1', name='ck_payroll_run_revision'),
    sa.CheckConstraint('to_date >= from_date', name='ck_payroll_run_bounds'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['journal_entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['reversal_entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['superseded_by_id'], ['payroll_run.id'], ),
    sa.ForeignKeyConstraint(['supersedes_id'], ['payroll_run.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'period', 'revision', name='uq_payroll_run_revision')
    )
    op.create_index(op.f('ix_payroll_run_company_id'), 'payroll_run', ['company_id'], unique=False)
    op.create_index(op.f('ix_payroll_run_period'), 'payroll_run', ['period'], unique=False)
    op.create_table('permission',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('role_id', sa.Uuid(), nullable=False),
    sa.Column('capability', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['role.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('role_id', 'capability', name='uq_permission_once')
    )
    op.create_index(op.f('ix_permission_role_id'), 'permission', ['role_id'], unique=False)
    op.create_table('physical_count',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=False),
    sa.Column('state', sa.String(length=8), nullable=False),
    sa.Column('counted_by', sa.String(length=64), nullable=False),
    sa.Column('posting_date', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('open', 'posted')", name='ck_physical_count_state'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_physical_count_company_id'), 'physical_count', ['company_id'], unique=False)
    op.create_index(op.f('ix_physical_count_location_id'), 'physical_count', ['location_id'], unique=False)
    op.create_table('pos_sync_report',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('terminal', sa.String(length=64), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('oversell_allowed', sa.Boolean(), nullable=False),
    sa.Column('queued_from', sa.Date(), nullable=True),
    sa.Column('queued_to', sa.Date(), nullable=True),
    sa.Column('queued', sa.Integer(), nullable=False),
    sa.Column('accepted', sa.Integer(), nullable=False),
    sa.Column('duplicates', sa.Integer(), nullable=False),
    sa.Column('rejected', sa.Integer(), nullable=False),
    sa.Column('outcomes', sa.Text(), nullable=False),
    sa.Column('differences', sa.Text(), nullable=False),
    sa.CheckConstraint('accepted >= 0 AND duplicates >= 0 AND rejected >= 0', name='ck_pos_sync_report_counts'),
    sa.CheckConstraint('queued >= 0', name='ck_pos_sync_report_queued'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_pos_sync_report_company_id'), 'pos_sync_report', ['company_id'], unique=False)
    op.create_index(op.f('ix_pos_sync_report_terminal'), 'pos_sync_report', ['terminal'], unique=False)
    op.create_table('price_rule',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('tier', sa.String(length=32), nullable=True),
    sa.Column('campaign', sa.String(length=40), nullable=True),
    sa.Column('min_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('max_quantity', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('discount_type', sa.String(length=16), nullable=False),
    sa.Column('discount_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint("discount_type <> 'percent' OR discount_value <= 100", name='ck_price_rule_percent_within_a_hundred'),
    sa.CheckConstraint("discount_type IN ('percent', 'amount')", name='ck_price_rule_discount_type'),
    sa.CheckConstraint('discount_value >= 0', name='ck_price_rule_value_not_negative'),
    sa.CheckConstraint('max_quantity IS NULL OR max_quantity >= min_quantity', name='ck_price_rule_band_downside_up'),
    sa.CheckConstraint('min_quantity > 0', name='ck_price_rule_min_quantity'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_price_rule_company_code')
    )
    op.create_index(op.f('ix_price_rule_company_id'), 'price_rule', ['company_id'], unique=False)
    op.create_index(op.f('ix_price_rule_item_id'), 'price_rule', ['item_id'], unique=False)
    op.create_table('report_run',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('definition_id', sa.Uuid(), nullable=False),
    sa.Column('requested_by', sa.String(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('period', sa.Date(), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('produced', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('delivered_to', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('withheld_recipients', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.CheckConstraint("status IN ('ok', 'failed', 'skipped')", name='ck_report_run_status'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['definition_id'], ['report_definition.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_report_run_company_id'), 'report_run', ['company_id'], unique=False)
    op.create_index(op.f('ix_report_run_definition_id'), 'report_run', ['definition_id'], unique=False)
    op.create_table('role_assignment',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('subject', sa.String(length=64), nullable=False),
    sa.Column('role_id', sa.Uuid(), nullable=False),
    sa.Column('assigned_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['role_id'], ['role.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('subject', 'role_id', name='uq_role_assignment_once')
    )
    op.create_index(op.f('ix_role_assignment_company_id'), 'role_assignment', ['company_id'], unique=False)
    op.create_index(op.f('ix_role_assignment_subject'), 'role_assignment', ['subject'], unique=False)
    op.create_table('serial',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('unreceived', 'in_stock', 'issued')", name='ck_serial_status'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('item_id', 'code', name='uq_serial_item_code')
    )
    op.create_index(op.f('ix_serial_company_id'), 'serial', ['company_id'], unique=False)
    op.create_index(op.f('ix_serial_item_id'), 'serial', ['item_id'], unique=False)
    op.create_index(op.f('ix_serial_location_id'), 'serial', ['location_id'], unique=False)
    op.create_table('supplier',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('party_id', sa.Uuid(), nullable=False),
    sa.Column('payment_terms_days', sa.Integer(), nullable=False),
    sa.Column('transaction_currency', sa.String(length=3), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('payment_terms_days >= 0', name='ck_supplier_payment_terms'),
    sa.CheckConstraint('transaction_currency IS NULL OR char_length(transaction_currency) = 3', name='ck_supplier_currency_length'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['party_id'], ['party.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'party_id', name='uq_supplier_company_party')
    )
    op.create_index(op.f('ix_supplier_company_id'), 'supplier', ['company_id'], unique=False)
    op.create_index(op.f('ix_supplier_party_id'), 'supplier', ['party_id'], unique=False)
    op.create_table('uom_conversion',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('from_uom', sa.String(length=16), nullable=False),
    sa.Column('to_uom', sa.String(length=16), nullable=False),
    sa.Column('factor', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint('factor > 0', name='ck_uom_conversion_positive'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('item_id', 'from_uom', 'to_uom', name='uq_uom_conversion_direction')
    )
    op.create_index(op.f('ix_uom_conversion_company_id'), 'uom_conversion', ['company_id'], unique=False)
    op.create_index(op.f('ix_uom_conversion_item_id'), 'uom_conversion', ['item_id'], unique=False)
    op.create_table('work_center_rate',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('work_center_id', sa.Uuid(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('hourly_rate', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint('hourly_rate > 0', name='ck_work_center_rate'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['work_center_id'], ['work_center.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('work_center_id', 'effective_from', name='uq_work_center_rate_date')
    )
    op.create_index(op.f('ix_work_center_rate_company_id'), 'work_center_rate', ['company_id'], unique=False)
    op.create_index(op.f('ix_work_center_rate_work_center_id'), 'work_center_rate', ['work_center_id'], unique=False)
    op.create_table('advanced_plan_row',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('plan_id', sa.Uuid(), nullable=False),
    sa.Column('requirement_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('basic_bucket_start', sa.Date(), nullable=False),
    sa.Column('basic_release_on', sa.Date(), nullable=True),
    sa.Column('planned_release_on', sa.Date(), nullable=False),
    sa.Column('moved_buckets', sa.Integer(), nullable=False),
    sa.Column('unresolved', sa.Boolean(), nullable=False),
    sa.Column('constraint', sa.Text(), nullable=True),
    sa.Column('work_center', sa.String(length=32), nullable=True),
    sa.Column('load_minutes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('bucket_load_minutes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('capacity_minutes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint('moved_buckets >= 0', name='ck_advanced_plan_row_moved'),
    sa.CheckConstraint('quantity >= 0', name='ck_advanced_plan_row_quantity'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['plan_id'], ['advanced_plan.id'], ),
    sa.ForeignKeyConstraint(['requirement_id'], ['mrp_requirement.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_advanced_plan_row_basic_bucket_start'), 'advanced_plan_row', ['basic_bucket_start'], unique=False)
    op.create_index(op.f('ix_advanced_plan_row_company_id'), 'advanced_plan_row', ['company_id'], unique=False)
    op.create_index(op.f('ix_advanced_plan_row_item_id'), 'advanced_plan_row', ['item_id'], unique=False)
    op.create_index(op.f('ix_advanced_plan_row_plan_id'), 'advanced_plan_row', ['plan_id'], unique=False)
    op.create_index(op.f('ix_advanced_plan_row_planned_release_on'), 'advanced_plan_row', ['planned_release_on'], unique=False)
    op.create_index(op.f('ix_advanced_plan_row_requirement_id'), 'advanced_plan_row', ['requirement_id'], unique=False)
    op.create_table('approval_decision',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('request_id', sa.Uuid(), nullable=False),
    sa.Column('level_no', sa.Integer(), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("action IN ('approve', 'reject', 'return')", name='ck_approval_decision_action'),
    sa.ForeignKeyConstraint(['request_id'], ['approval_request.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_approval_decision_request_id'), 'approval_decision', ['request_id'], unique=False)
    op.create_table('attendance_event',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('direction', sa.String(length=8), nullable=False),
    sa.Column('source', sa.String(length=24), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=True),
    sa.Column('reason', sa.String(length=255), nullable=True),
    sa.CheckConstraint("direction IN ('in', 'out')", name='ck_attendance_event_direction'),
    sa.CheckConstraint("source <> 'manual' OR (actor IS NOT NULL AND reason IS NOT NULL)", name='ck_attendance_event_manual_is_attributed'),
    sa.CheckConstraint("source <> 'manual' OR (char_length(actor) > 0 AND char_length(reason) > 0)", name='ck_attendance_event_manual_is_stated'),
    sa.CheckConstraint("source IN ('manual', 'biometric device')", name='ck_attendance_event_source'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'employee_id', 'at', 'direction', name='uq_attendance_event_punch')
    )
    op.create_index(op.f('ix_attendance_event_company_id'), 'attendance_event', ['company_id'], unique=False)
    op.create_index(op.f('ix_attendance_event_employee_id'), 'attendance_event', ['employee_id'], unique=False)
    op.create_table('biometric_device_user',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=False),
    sa.Column('device_user_id', sa.String(length=64), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('mapped_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['device_id'], ['biometric_device.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'device_id', 'device_user_id', name='uq_biometric_device_user')
    )
    op.create_index(op.f('ix_biometric_device_user_company_id'), 'biometric_device_user', ['company_id'], unique=False)
    op.create_index(op.f('ix_biometric_device_user_device_id'), 'biometric_device_user', ['device_id'], unique=False)
    op.create_index(op.f('ix_biometric_device_user_employee_id'), 'biometric_device_user', ['employee_id'], unique=False)
    op.create_table('bom_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('bom_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('scrap_percent', sa.Numeric(precision=9, scale=4), nullable=True),
    sa.Column('operation_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('line_no >= 1', name='ck_bom_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_bom_line_quantity'),
    sa.CheckConstraint('scrap_percent IS NULL OR (scrap_percent >= 0 AND scrap_percent <= 100)', name='ck_bom_line_scrap'),
    sa.ForeignKeyConstraint(['bom_id'], ['bom.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('bom_id', 'line_no', name='uq_bom_line_no')
    )
    op.create_index(op.f('ix_bom_line_bom_id'), 'bom_line', ['bom_id'], unique=False)
    op.create_index(op.f('ix_bom_line_company_id'), 'bom_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_bom_line_item_id'), 'bom_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_bom_line_operation_id'), 'bom_line', ['operation_id'], unique=False)
    op.create_table('customer_address',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('line1', sa.String(length=160), nullable=False),
    sa.Column('line2', sa.String(length=160), nullable=True),
    sa.Column('city', sa.String(length=80), nullable=True),
    sa.Column('region', sa.String(length=80), nullable=True),
    sa.Column('postal_code', sa.String(length=16), nullable=True),
    sa.Column('country', sa.String(length=2), nullable=True),
    sa.Column('is_primary', sa.Boolean(), nullable=False),
    sa.CheckConstraint("kind IN ('billing', 'shipping')", name='ck_customer_address_kind'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_customer_address_company_id'), 'customer_address', ['company_id'], unique=False)
    op.create_index(op.f('ix_customer_address_customer_id'), 'customer_address', ['customer_id'], unique=False)
    op.create_index('uq_customer_primary_address_per_kind', 'customer_address', ['customer_id', 'kind'], unique=True, postgresql_where=sa.text('is_primary'))
    op.create_table('customer_contact',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('position', sa.String(length=80), nullable=True),
    sa.Column('email', sa.String(length=160), nullable=True),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('is_primary', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_customer_contact_company_id'), 'customer_contact', ['company_id'], unique=False)
    op.create_index(op.f('ix_customer_contact_customer_id'), 'customer_contact', ['customer_id'], unique=False)
    op.create_index('uq_customer_primary_contact', 'customer_contact', ['customer_id'], unique=True, postgresql_where=sa.text('is_primary'))
    op.create_table('employee_asset',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('description', sa.String(length=160), nullable=False),
    sa.Column('identifier', sa.String(length=64), nullable=True),
    sa.Column('assigned_on', sa.Date(), nullable=False),
    sa.Column('returned_on', sa.Date(), nullable=True),
    sa.CheckConstraint('returned_on IS NULL OR returned_on >= assigned_on', name='ck_employee_asset_window'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_employee_asset_company_id'), 'employee_asset', ['company_id'], unique=False)
    op.create_index(op.f('ix_employee_asset_employee_id'), 'employee_asset', ['employee_id'], unique=False)
    op.create_table('employee_bank_account',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('bank_code', sa.String(length=32), nullable=False),
    sa.Column('account_number', sa.String(length=32), nullable=False),
    sa.Column('holder_name', sa.String(length=160), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('stated_by', sa.String(length=64), nullable=False),
    sa.CheckConstraint("account_number ~ '^[0-9]+$'", name='ck_employee_bank_account_digits'),
    sa.CheckConstraint('char_length(bank_code) > 0', name='ck_employee_bank_code'),
    sa.CheckConstraint('char_length(holder_name) > 0', name='ck_employee_bank_holder'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'effective_from', name='uq_employee_bank_account_effective')
    )
    op.create_index(op.f('ix_employee_bank_account_company_id'), 'employee_bank_account', ['company_id'], unique=False)
    op.create_index(op.f('ix_employee_bank_account_employee_id'), 'employee_bank_account', ['employee_id'], unique=False)
    op.create_table('employee_loan',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('reference', sa.String(length=32), nullable=False),
    sa.Column('principal', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('instalment_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('started_on', sa.Date(), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=False),
    sa.Column('settled_on', sa.Date(), nullable=True),
    sa.Column('settled_by', sa.String(length=64), nullable=True),
    sa.CheckConstraint('char_length(actor) > 0', name='ck_employee_loan_actor'),
    sa.CheckConstraint('char_length(reason) > 0', name='ck_employee_loan_reason'),
    sa.CheckConstraint('instalment_amount > 0', name='ck_employee_loan_instalment'),
    sa.CheckConstraint('principal > 0', name='ck_employee_loan_principal'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'reference', name='uq_employee_loan_reference')
    )
    op.create_index(op.f('ix_employee_loan_company_id'), 'employee_loan', ['company_id'], unique=False)
    op.create_index(op.f('ix_employee_loan_employee_id'), 'employee_loan', ['employee_id'], unique=False)
    op.create_table('employee_movement',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('effective_date', sa.Date(), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=False),
    sa.Column('department', sa.String(length=64), nullable=True),
    sa.Column('cost_centre', sa.String(length=64), nullable=True),
    sa.Column('location', sa.String(length=64), nullable=True),
    sa.Column('manager_id', sa.Uuid(), nullable=True),
    sa.Column('basic_salary', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('contract_type', sa.String(length=32), nullable=True),
    sa.CheckConstraint("kind IN ('joining', 'transfer', 'promotion', 'exit')", name='ck_employee_movement_kind'),
    sa.CheckConstraint('char_length(actor) > 0', name='ck_employee_movement_actor'),
    sa.CheckConstraint('char_length(reason) > 0', name='ck_employee_movement_reason'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['manager_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_employee_movement_company_id'), 'employee_movement', ['company_id'], unique=False)
    op.create_index(op.f('ix_employee_movement_employee_id'), 'employee_movement', ['employee_id'], unique=False)
    op.create_index('uq_employee_movement_exit', 'employee_movement', ['employee_id'], unique=True, postgresql_where=sa.text("kind = 'exit'"))
    op.create_index('uq_employee_movement_joining', 'employee_movement', ['employee_id'], unique=True, postgresql_where=sa.text("kind = 'joining'"))
    op.create_table('employment_contract',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('contract_type', sa.String(length=32), nullable=False),
    sa.Column('basic_salary', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('transaction_currency', sa.String(length=3), nullable=True),
    sa.Column('supersedes_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('basic_salary >= 0', name='ck_employment_contract_salary'),
    sa.CheckConstraint('transaction_currency IS NULL OR char_length(transaction_currency) = 3', name='ck_employment_contract_currency_length'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['supersedes_id'], ['employment_contract.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'effective_from', name='uq_employment_contract_start')
    )
    op.create_index(op.f('ix_employment_contract_company_id'), 'employment_contract', ['company_id'], unique=False)
    op.create_index(op.f('ix_employment_contract_employee_id'), 'employment_contract', ['employee_id'], unique=False)
    op.create_table('item_barcode',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('variant_id', sa.Uuid(), nullable=True),
    sa.Column('value', sa.String(length=64), nullable=False),
    sa.Column('symbology', sa.String(length=8), nullable=False),
    sa.CheckConstraint("symbology IN ('ean', 'upc', 'qr')", name='ck_item_barcode_symbology'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['item_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('value', name='uq_item_barcode_value')
    )
    op.create_index(op.f('ix_item_barcode_company_id'), 'item_barcode', ['company_id'], unique=False)
    op.create_index(op.f('ix_item_barcode_item_id'), 'item_barcode', ['item_id'], unique=False)
    op.create_index(op.f('ix_item_barcode_variant_id'), 'item_barcode', ['variant_id'], unique=False)
    op.create_table('leave_entry',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('leave_type_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('days', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('on_date', sa.Date(), nullable=False),
    sa.Column('period', sa.String(length=9), nullable=True),
    sa.Column('expires_on', sa.Date(), nullable=True),
    sa.Column('source', sa.String(length=64), nullable=True),
    sa.CheckConstraint("kind IN ('accrual', 'carry_forward', 'leave_taken', 'adjustment')", name='ck_leave_entry_kind'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['leave_type_id'], ['leave_type.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_leave_entry_company_id'), 'leave_entry', ['company_id'], unique=False)
    op.create_index(op.f('ix_leave_entry_employee_id'), 'leave_entry', ['employee_id'], unique=False)
    op.create_index(op.f('ix_leave_entry_leave_type_id'), 'leave_entry', ['leave_type_id'], unique=False)
    op.create_index('uq_leave_entry_period', 'leave_entry', ['employee_id', 'leave_type_id', 'kind', 'period'], unique=True, postgresql_where=sa.text('period IS NOT NULL'))
    op.create_table('mrp_suggestion',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('requirement_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('needed_by', sa.Date(), nullable=False),
    sa.Column('release_on', sa.Date(), nullable=True),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('converted_type', sa.String(length=24), nullable=True),
    sa.Column('converted_id', sa.Uuid(), nullable=True),
    sa.Column('converted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('converted_by', sa.String(length=64), nullable=True),
    sa.CheckConstraint("kind IN ('produce', 'purchase')", name='ck_mrp_suggestion_kind'),
    sa.CheckConstraint("state = 'open' OR (converted_type IS NOT NULL AND converted_id IS NOT NULL)", name='ck_mrp_suggestion_converted_names_its_document'),
    sa.CheckConstraint("state IN ('open', 'converted')", name='ck_mrp_suggestion_state'),
    sa.CheckConstraint('quantity > 0', name='ck_mrp_suggestion_quantity'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['requirement_id'], ['mrp_requirement.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['mrp_run.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('requirement_id', name='uq_mrp_suggestion_requirement')
    )
    op.create_index(op.f('ix_mrp_suggestion_company_id'), 'mrp_suggestion', ['company_id'], unique=False)
    op.create_index(op.f('ix_mrp_suggestion_item_id'), 'mrp_suggestion', ['item_id'], unique=False)
    op.create_index(op.f('ix_mrp_suggestion_needed_by'), 'mrp_suggestion', ['needed_by'], unique=False)
    op.create_index(op.f('ix_mrp_suggestion_requirement_id'), 'mrp_suggestion', ['requirement_id'], unique=False)
    op.create_index(op.f('ix_mrp_suggestion_run_id'), 'mrp_suggestion', ['run_id'], unique=False)
    op.create_table('opportunity',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('owner', sa.String(length=80), nullable=False),
    sa.Column('expected_close', sa.Date(), nullable=True),
    sa.Column('stage_id', sa.Uuid(), nullable=False),
    sa.Column('lost_reason', sa.String(length=240), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.ForeignKeyConstraint(['stage_id'], ['pipeline_stage.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_opportunity_company_id'), 'opportunity', ['company_id'], unique=False)
    op.create_index(op.f('ix_opportunity_customer_id'), 'opportunity', ['customer_id'], unique=False)
    op.create_index(op.f('ix_opportunity_stage_id'), 'opportunity', ['stage_id'], unique=False)
    op.create_table('org_placement',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('manager_id', sa.Uuid(), nullable=True),
    sa.Column('department', sa.String(length=64), nullable=True),
    sa.Column('cost_centre', sa.String(length=64), nullable=True),
    sa.Column('location', sa.String(length=64), nullable=True),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.CheckConstraint('manager_id IS NULL OR manager_id <> employee_id', name='ck_org_placement_not_own_manager'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['manager_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'effective_from', name='uq_org_placement_start')
    )
    op.create_index(op.f('ix_org_placement_company_id'), 'org_placement', ['company_id'], unique=False)
    op.create_index(op.f('ix_org_placement_employee_id'), 'org_placement', ['employee_id'], unique=False)
    op.create_table('parked_punch',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=False),
    sa.Column('pull_id', sa.Uuid(), nullable=True),
    sa.Column('device_user_id', sa.String(length=64), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('direction', sa.String(length=8), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_employee_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint("direction IS NULL OR direction IN ('in', 'out')", name='ck_parked_punch_direction'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['device_id'], ['biometric_device.id'], ),
    sa.ForeignKeyConstraint(['pull_id'], ['biometric_pull.id'], ),
    sa.ForeignKeyConstraint(['resolved_employee_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_parked_punch_company_id'), 'parked_punch', ['company_id'], unique=False)
    op.create_index(op.f('ix_parked_punch_device_id'), 'parked_punch', ['device_id'], unique=False)
    op.create_index(op.f('ix_parked_punch_device_user_id'), 'parked_punch', ['device_user_id'], unique=False)
    op.create_table('payment_batch',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('scheduled_on', sa.Date(), nullable=False),
    sa.Column('total_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('approval_request_id', sa.Uuid(), nullable=True),
    sa.Column('approved_by', sa.String(length=64), nullable=True),
    sa.Column('executed_on', sa.Date(), nullable=True),
    sa.Column('executed_by', sa.String(length=64), nullable=True),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'pending', 'approved', 'rejected', 'returned', 'executed')", name='ck_payment_batch_status'),
    sa.CheckConstraint('total_amount >= 0', name='ck_payment_batch_total'),
    sa.ForeignKeyConstraint(['approval_request_id'], ['approval_request.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_payment_batch_company_number')
    )
    op.create_index(op.f('ix_payment_batch_approval_request_id'), 'payment_batch', ['approval_request_id'], unique=False)
    op.create_index(op.f('ix_payment_batch_company_id'), 'payment_batch', ['company_id'], unique=False)
    op.create_table('physical_count_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('count_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('system_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('counted_quantity', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('variance', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('counted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['count_id'], ['physical_count.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('count_id', 'item_id', name='uq_physical_count_line_item')
    )
    op.create_index(op.f('ix_physical_count_line_count_id'), 'physical_count_line', ['count_id'], unique=False)
    op.create_index(op.f('ix_physical_count_line_item_id'), 'physical_count_line', ['item_id'], unique=False)
    op.create_table('pos_sale',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('terminal', sa.String(length=32), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=True),
    sa.Column('sold_on', sa.Date(), nullable=False),
    sa.Column('shift_id', sa.Uuid(), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('net_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('gross_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('journal_entry_id', sa.Uuid(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('voided_by', sa.String(length=64), nullable=True),
    sa.Column('void_reason', sa.String(length=200), nullable=True),
    sa.Column('reversal_entry_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('open', 'completed', 'void')", name='ck_pos_sale_status'),
    sa.CheckConstraint('gross_amount >= 0', name='ck_pos_sale_gross'),
    sa.CheckConstraint('net_amount >= 0', name='ck_pos_sale_net'),
    sa.CheckConstraint('tax_amount >= 0', name='ck_pos_sale_tax'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.ForeignKeyConstraint(['journal_entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.ForeignKeyConstraint(['reversal_entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['shift_id'], ['pos_shift.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_pos_sale_company_number')
    )
    op.create_index(op.f('ix_pos_sale_company_id'), 'pos_sale', ['company_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_customer_id'), 'pos_sale', ['customer_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_journal_entry_id'), 'pos_sale', ['journal_entry_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_location_id'), 'pos_sale', ['location_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_reversal_entry_id'), 'pos_sale', ['reversal_entry_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_shift_id'), 'pos_sale', ['shift_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_sold_on'), 'pos_sale', ['sold_on'], unique=False)
    op.create_table('purchase_requisition',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('requested_by', sa.String(length=64), nullable=False),
    sa.Column('needed_by', sa.Date(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('memo', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('revision_no', sa.Integer(), nullable=False),
    sa.Column('revision_of_id', sa.Uuid(), nullable=True),
    sa.Column('approval_request_id', sa.Uuid(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'pending', 'approved', 'rejected', 'returned')", name='ck_requisition_status'),
    sa.CheckConstraint('(revision_no = 1) = (revision_of_id IS NULL)', name='ck_requisition_revision_pair'),
    sa.CheckConstraint('revision_no >= 1', name='ck_requisition_revision'),
    sa.ForeignKeyConstraint(['approval_request_id'], ['approval_request.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['revision_of_id'], ['purchase_requisition.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_requisition_company_number')
    )
    op.create_index(op.f('ix_purchase_requisition_approval_request_id'), 'purchase_requisition', ['approval_request_id'], unique=False)
    op.create_index(op.f('ix_purchase_requisition_company_id'), 'purchase_requisition', ['company_id'], unique=False)
    op.create_index(op.f('ix_purchase_requisition_revision_of_id'), 'purchase_requisition', ['revision_of_id'], unique=True)
    op.create_table('recurring_template',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('cycle', sa.String(length=16), nullable=False),
    sa.Column('starts_on', sa.Date(), nullable=False),
    sa.Column('ends_on', sa.Date(), nullable=True),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("cycle IN ('weekly', 'monthly', 'quarterly', 'annual')", name='ck_recurring_template_cycle'),
    sa.CheckConstraint('ends_on IS NULL OR ends_on >= starts_on', name='ck_recurring_template_window'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_recurring_template_company_code')
    )
    op.create_index(op.f('ix_recurring_template_company_id'), 'recurring_template', ['company_id'], unique=False)
    op.create_index(op.f('ix_recurring_template_customer_id'), 'recurring_template', ['customer_id'], unique=False)
    op.create_table('roster_entry',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('shift_id', sa.Uuid(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('cycle', sa.String(length=32), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['shift_id'], ['shift.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'effective_from', name='uq_roster_entry_start')
    )
    op.create_index(op.f('ix_roster_entry_company_id'), 'roster_entry', ['company_id'], unique=False)
    op.create_index(op.f('ix_roster_entry_employee_id'), 'roster_entry', ['employee_id'], unique=False)
    op.create_table('roster_override',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('on_date', sa.Date(), nullable=False),
    sa.Column('shift_id', sa.Uuid(), nullable=True),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=False),
    sa.CheckConstraint('char_length(actor) > 0', name='ck_roster_override_actor'),
    sa.CheckConstraint('char_length(reason) > 0', name='ck_roster_override_reason'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['shift_id'], ['shift.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'on_date', name='uq_roster_override_day')
    )
    op.create_index(op.f('ix_roster_override_company_id'), 'roster_override', ['company_id'], unique=False)
    op.create_index(op.f('ix_roster_override_employee_id'), 'roster_override', ['employee_id'], unique=False)
    op.create_table('routing_operation',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('bom_id', sa.Uuid(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('work_center_code', sa.String(length=32), nullable=True),
    sa.Column('setup_minutes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('run_minutes_per_unit', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('run_basis_units', sa.Integer(), nullable=False),
    sa.CheckConstraint('run_basis_units >= 1', name='ck_routing_basis_units'),
    sa.CheckConstraint('run_minutes_per_unit > 0', name='ck_routing_run_positive'),
    sa.CheckConstraint('sequence >= 1', name='ck_routing_sequence_starts_at_one'),
    sa.CheckConstraint('setup_minutes >= 0', name='ck_routing_setup_not_negative'),
    sa.ForeignKeyConstraint(['bom_id'], ['bom.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('bom_id', 'sequence', name='uq_routing_operation_sequence')
    )
    op.create_index(op.f('ix_routing_operation_bom_id'), 'routing_operation', ['bom_id'], unique=False)
    op.create_index(op.f('ix_routing_operation_company_id'), 'routing_operation', ['company_id'], unique=False)
    op.create_index(op.f('ix_routing_operation_work_center_code'), 'routing_operation', ['work_center_code'], unique=False)
    op.create_table('stock_ledger_entry',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('variant_id', sa.Uuid(), nullable=True),
    sa.Column('location_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('batch_id', sa.Uuid(), nullable=True),
    sa.Column('serial_id', sa.Uuid(), nullable=True),
    sa.Column('source_type', sa.String(length=32), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('posting_date', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source_type IS NOT NULL AND source_type <> '' AND source_id IS NOT NULL", name='ck_stock_entry_source'),
    sa.CheckConstraint('(quantity > 0 AND value >= 0) OR (quantity < 0 AND value <= 0)', name='ck_stock_entry_value_direction'),
    sa.CheckConstraint('char_length(currency) = 3', name='ck_stock_entry_currency'),
    sa.CheckConstraint('quantity <> 0', name='ck_stock_entry_moves_something'),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.ForeignKeyConstraint(['serial_id'], ['serial.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['item_variant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_stock_ledger_entry_batch_id'), 'stock_ledger_entry', ['batch_id'], unique=False)
    op.create_index(op.f('ix_stock_ledger_entry_company_id'), 'stock_ledger_entry', ['company_id'], unique=False)
    op.create_index(op.f('ix_stock_ledger_entry_item_id'), 'stock_ledger_entry', ['item_id'], unique=False)
    op.create_index(op.f('ix_stock_ledger_entry_location_id'), 'stock_ledger_entry', ['location_id'], unique=False)
    op.create_index(op.f('ix_stock_ledger_entry_posting_date'), 'stock_ledger_entry', ['posting_date'], unique=False)
    op.create_index(op.f('ix_stock_ledger_entry_serial_id'), 'stock_ledger_entry', ['serial_id'], unique=False)
    op.create_index(op.f('ix_stock_ledger_entry_source_id'), 'stock_ledger_entry', ['source_id'], unique=False)
    op.create_index(op.f('ix_stock_ledger_entry_source_type'), 'stock_ledger_entry', ['source_type'], unique=False)
    op.create_index(op.f('ix_stock_ledger_entry_variant_id'), 'stock_ledger_entry', ['variant_id'], unique=False)
    op.create_table('supplier_bank_account',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('bank_name', sa.String(length=120), nullable=False),
    sa.Column('account_name', sa.String(length=160), nullable=False),
    sa.Column('account_number', sa.String(length=40), nullable=False),
    sa.Column('swift', sa.String(length=11), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('is_primary', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('supplier_id', 'bank_name', 'account_number', name='uq_supplier_bank_once')
    )
    op.create_index(op.f('ix_supplier_bank_account_company_id'), 'supplier_bank_account', ['company_id'], unique=False)
    op.create_index(op.f('ix_supplier_bank_account_supplier_id'), 'supplier_bank_account', ['supplier_id'], unique=False)
    op.create_index('uq_supplier_primary_bank_account', 'supplier_bank_account', ['supplier_id'], unique=True, postgresql_where=sa.text('is_primary'))
    op.create_table('supplier_contact',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('position', sa.String(length=80), nullable=True),
    sa.Column('email', sa.String(length=160), nullable=True),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('is_primary', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_supplier_contact_company_id'), 'supplier_contact', ['company_id'], unique=False)
    op.create_index(op.f('ix_supplier_contact_supplier_id'), 'supplier_contact', ['supplier_id'], unique=False)
    op.create_index('uq_supplier_primary_contact', 'supplier_contact', ['supplier_id'], unique=True, postgresql_where=sa.text('is_primary'))
    op.create_table('supplier_portal_account',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('subject', sa.String(length=64), nullable=False),
    sa.Column('linked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('linked_by', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'subject', name='uq_supplier_portal_account_subject')
    )
    op.create_index(op.f('ix_supplier_portal_account_company_id'), 'supplier_portal_account', ['company_id'], unique=False)
    op.create_index(op.f('ix_supplier_portal_account_subject'), 'supplier_portal_account', ['subject'], unique=False)
    op.create_index(op.f('ix_supplier_portal_account_supplier_id'), 'supplier_portal_account', ['supplier_id'], unique=False)
    op.create_table('supplier_tax_identifier',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('value', sa.String(length=32), nullable=False),
    sa.CheckConstraint("kind IN ('tin', 'vat', 'branch_code', 'withholding')", name='ck_supplier_tax_identifier_kind'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('supplier_id', 'kind', 'value', name='uq_supplier_tax_identifier_once')
    )
    op.create_index(op.f('ix_supplier_tax_identifier_company_id'), 'supplier_tax_identifier', ['company_id'], unique=False)
    op.create_index(op.f('ix_supplier_tax_identifier_supplier_id'), 'supplier_tax_identifier', ['supplier_id'], unique=False)
    op.create_table('work_order',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('bom_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=8), nullable=False),
    sa.Column('created_on', sa.Date(), nullable=False),
    sa.Column('due_on', sa.Date(), nullable=True),
    sa.Column('memo', sa.String(length=200), nullable=True),
    sa.CheckConstraint("source IN ('manual', 'mrp')", name='ck_work_order_source'),
    sa.CheckConstraint("status IN ('planned', 'released', 'in_progress', 'completed', 'closed')", name='ck_work_order_status'),
    sa.CheckConstraint('quantity > 0', name='ck_work_order_quantity'),
    sa.ForeignKeyConstraint(['bom_id'], ['bom.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_work_order_number')
    )
    op.create_index(op.f('ix_work_order_bom_id'), 'work_order', ['bom_id'], unique=False)
    op.create_index(op.f('ix_work_order_company_id'), 'work_order', ['company_id'], unique=False)
    op.create_index(op.f('ix_work_order_due_on'), 'work_order', ['due_on'], unique=False)
    op.create_index(op.f('ix_work_order_item_id'), 'work_order', ['item_id'], unique=False)
    op.create_index(op.f('ix_work_order_number'), 'work_order', ['number'], unique=False)
    op.create_table('leave_request',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('leave_type_id', sa.Uuid(), nullable=False),
    sa.Column('from_date', sa.Date(), nullable=False),
    sa.Column('to_date', sa.Date(), nullable=False),
    sa.Column('days', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('requested_by', sa.String(length=64), nullable=False),
    sa.Column('approval_request_id', sa.Uuid(), nullable=True),
    sa.Column('entry_id', sa.Uuid(), nullable=True),
    sa.Column('override_actor', sa.String(length=64), nullable=True),
    sa.Column('override_reason', sa.String(length=255), nullable=True),
    sa.Column('cancelled_by', sa.String(length=64), nullable=True),
    sa.Column('cancel_reason', sa.String(length=255), nullable=True),
    sa.CheckConstraint("state IN ('pending', 'approved', 'rejected', 'returned', 'cancelled')", name='ck_leave_request_state'),
    sa.CheckConstraint('(override_actor IS NULL) = (override_reason IS NULL)', name='ck_leave_request_override_named'),
    sa.CheckConstraint('char_length(requested_by) > 0', name='ck_leave_request_applicant'),
    sa.CheckConstraint('days > 0', name='ck_leave_request_days'),
    sa.CheckConstraint('to_date >= from_date', name='ck_leave_request_range'),
    sa.ForeignKeyConstraint(['approval_request_id'], ['approval_request.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['entry_id'], ['leave_entry.id'], ),
    sa.ForeignKeyConstraint(['leave_type_id'], ['leave_type.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_leave_request_approval_request_id'), 'leave_request', ['approval_request_id'], unique=False)
    op.create_index(op.f('ix_leave_request_company_id'), 'leave_request', ['company_id'], unique=False)
    op.create_index(op.f('ix_leave_request_employee_id'), 'leave_request', ['employee_id'], unique=False)
    op.create_index(op.f('ix_leave_request_leave_type_id'), 'leave_request', ['leave_type_id'], unique=False)
    op.create_table('loan_recovery',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('loan_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('on_date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('deferred', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('policy_id', sa.Uuid(), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('settled_loan', sa.Boolean(), nullable=False),
    sa.CheckConstraint('amount >= 0', name='ck_loan_recovery_amount'),
    sa.CheckConstraint('deferred >= 0', name='ck_loan_recovery_deferred'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['employee_loan.id'], ),
    sa.ForeignKeyConstraint(['policy_id'], ['loan_policy.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('loan_id', 'period', name='uq_loan_recovery_period')
    )
    op.create_index(op.f('ix_loan_recovery_company_id'), 'loan_recovery', ['company_id'], unique=False)
    op.create_index(op.f('ix_loan_recovery_employee_id'), 'loan_recovery', ['employee_id'], unique=False)
    op.create_index(op.f('ix_loan_recovery_loan_id'), 'loan_recovery', ['loan_id'], unique=False)
    op.create_index(op.f('ix_loan_recovery_period'), 'loan_recovery', ['period'], unique=False)
    op.create_table('opportunity_move',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('opportunity_id', sa.Uuid(), nullable=False),
    sa.Column('from_stage_id', sa.Uuid(), nullable=True),
    sa.Column('to_stage_id', sa.Uuid(), nullable=False),
    sa.Column('actor', sa.String(length=80), nullable=False),
    sa.Column('reason', sa.String(length=240), nullable=True),
    sa.Column('moved_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['from_stage_id'], ['pipeline_stage.id'], ),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunity.id'], ),
    sa.ForeignKeyConstraint(['to_stage_id'], ['pipeline_stage.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_opportunity_move_company_id'), 'opportunity_move', ['company_id'], unique=False)
    op.create_index(op.f('ix_opportunity_move_opportunity_id'), 'opportunity_move', ['opportunity_id'], unique=False)
    op.create_table('payroll_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('contract_id', sa.Uuid(), nullable=True),
    sa.Column('basic_salary', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('daily_rate', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('period_days', sa.Integer(), nullable=False),
    sa.Column('employed_days', sa.Integer(), nullable=False),
    sa.Column('worked_days', sa.Integer(), nullable=False),
    sa.Column('paid_leave_days', sa.Integer(), nullable=False),
    sa.Column('unpaid_leave_days', sa.Integer(), nullable=False),
    sa.Column('holiday_days', sa.Integer(), nullable=False),
    sa.Column('absent_days', sa.Integer(), nullable=False),
    sa.Column('overtime_minutes', sa.Integer(), nullable=False),
    sa.Column('late_minutes', sa.Integer(), nullable=False),
    sa.Column('reference_minutes', sa.Integer(), nullable=False),
    sa.Column('basic_pay', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('gross', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('taxable_gross', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('deductions_total', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('employer_contributions_total', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('net', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('incomplete', sa.Boolean(), nullable=False),
    sa.Column('flag_reason', sa.String(length=500), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['contract_id'], ['employment_contract.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['payroll_run.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'employee_id', name='uq_payroll_line_employee')
    )
    op.create_index(op.f('ix_payroll_line_company_id'), 'payroll_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_payroll_line_employee_id'), 'payroll_line', ['employee_id'], unique=False)
    op.create_index(op.f('ix_payroll_line_run_id'), 'payroll_line', ['run_id'], unique=False)
    op.create_table('pos_drawer_movement',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('terminal', sa.String(length=32), nullable=False),
    sa.Column('movement_type', sa.String(length=16), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.Column('sale_id', sa.Uuid(), nullable=True),
    sa.Column('shift_id', sa.Uuid(), nullable=True),
    sa.Column('moved_on', sa.Date(), nullable=False),
    sa.Column('moved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.CheckConstraint("movement_type IN ('paid_in', 'paid_out')", name='ck_pos_drawer_type'),
    sa.CheckConstraint('amount > 0', name='ck_pos_drawer_amount'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['sale_id'], ['pos_sale.id'], ),
    sa.ForeignKeyConstraint(['shift_id'], ['pos_shift.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_pos_drawer_movement_company_id'), 'pos_drawer_movement', ['company_id'], unique=False)
    op.create_index(op.f('ix_pos_drawer_movement_moved_on'), 'pos_drawer_movement', ['moved_on'], unique=False)
    op.create_index(op.f('ix_pos_drawer_movement_sale_id'), 'pos_drawer_movement', ['sale_id'], unique=False)
    op.create_index(op.f('ix_pos_drawer_movement_shift_id'), 'pos_drawer_movement', ['shift_id'], unique=False)
    op.create_index(op.f('ix_pos_drawer_movement_terminal'), 'pos_drawer_movement', ['terminal'], unique=False)
    op.create_table('pos_sale_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('sale_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('variant_id', sa.Uuid(), nullable=True),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('barcode', sa.String(length=64), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_rule_code', sa.String(length=32), nullable=True),
    sa.Column('rule_code', sa.String(length=80), nullable=True),
    sa.Column('rule_priority', sa.Integer(), nullable=True),
    sa.Column('movement_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('line_no >= 1', name='ck_pos_sale_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_pos_sale_line_quantity'),
    sa.CheckConstraint('tax_amount >= 0', name='ck_pos_sale_line_tax'),
    sa.CheckConstraint('unit_price >= 0', name='ck_pos_sale_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['movement_id'], ['stock_ledger_entry.id'], ),
    sa.ForeignKeyConstraint(['sale_id'], ['pos_sale.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['item_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sale_id', 'line_no', name='uq_pos_sale_line_no')
    )
    op.create_index(op.f('ix_pos_sale_line_company_id'), 'pos_sale_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_line_item_id'), 'pos_sale_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_line_movement_id'), 'pos_sale_line', ['movement_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_line_sale_id'), 'pos_sale_line', ['sale_id'], unique=False)
    op.create_index(op.f('ix_pos_sale_line_variant_id'), 'pos_sale_line', ['variant_id'], unique=False)
    op.create_table('pos_tender',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('sale_id', sa.Uuid(), nullable=False),
    sa.Column('tender_no', sa.Integer(), nullable=False),
    sa.Column('tender_type', sa.String(length=16), nullable=False),
    sa.Column('tendered', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('applied', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('reference', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("tender_type IN ('cash', 'card', 'gateway')", name='ck_pos_tender_type'),
    sa.CheckConstraint('applied <= tendered', name='ck_pos_tender_within_tendered'),
    sa.CheckConstraint('applied >= 0', name='ck_pos_tender_applied'),
    sa.CheckConstraint('tender_no >= 1', name='ck_pos_tender_starts_at_one'),
    sa.CheckConstraint('tendered > 0', name='ck_pos_tender_tendered'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['sale_id'], ['pos_sale.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sale_id', 'tender_no', name='uq_pos_tender_no')
    )
    op.create_index(op.f('ix_pos_tender_company_id'), 'pos_tender', ['company_id'], unique=False)
    op.create_index(op.f('ix_pos_tender_sale_id'), 'pos_tender', ['sale_id'], unique=False)
    op.create_table('purchase_requisition_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('requisition_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('estimated_unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint('estimated_unit_price >= 0', name='ck_requisition_line_price'),
    sa.CheckConstraint('line_no >= 1', name='ck_requisition_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_requisition_line_quantity'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['requisition_id'], ['purchase_requisition.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('requisition_id', 'line_no', name='uq_requisition_line_no')
    )
    op.create_index(op.f('ix_purchase_requisition_line_company_id'), 'purchase_requisition_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_purchase_requisition_line_item_id'), 'purchase_requisition_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_purchase_requisition_line_requisition_id'), 'purchase_requisition_line', ['requisition_id'], unique=False)
    op.create_table('quotation',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('opportunity_id', sa.Uuid(), nullable=True),
    sa.Column('issued_on', sa.Date(), nullable=False),
    sa.Column('valid_until', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunity.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_quotation_company_number')
    )
    op.create_index(op.f('ix_quotation_company_id'), 'quotation', ['company_id'], unique=False)
    op.create_index(op.f('ix_quotation_customer_id'), 'quotation', ['customer_id'], unique=False)
    op.create_index('uq_quotation_opportunity', 'quotation', ['opportunity_id'], unique=True, postgresql_where=sa.text('opportunity_id IS NOT NULL'))
    op.create_table('recurring_template_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('template_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=True),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_rule_code', sa.String(length=32), nullable=True),
    sa.CheckConstraint('line_no >= 1', name='ck_recurring_template_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_recurring_template_line_quantity'),
    sa.CheckConstraint('unit_price >= 0', name='ck_recurring_template_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['template_id'], ['recurring_template.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('template_id', 'line_no', name='uq_recurring_template_line_no')
    )
    op.create_index(op.f('ix_recurring_template_line_company_id'), 'recurring_template_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_recurring_template_line_item_id'), 'recurring_template_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_recurring_template_line_template_id'), 'recurring_template_line', ['template_id'], unique=False)
    op.create_table('rfq',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('requisition_id', sa.Uuid(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('issued_on', sa.Date(), nullable=False),
    sa.Column('response_deadline', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('issued', 'closed')", name='ck_rfq_status'),
    sa.CheckConstraint('response_deadline >= issued_on', name='ck_rfq_deadline_after_issue'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['requisition_id'], ['purchase_requisition.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_rfq_company_number')
    )
    op.create_index(op.f('ix_rfq_company_id'), 'rfq', ['company_id'], unique=False)
    op.create_index(op.f('ix_rfq_requisition_id'), 'rfq', ['requisition_id'], unique=False)
    op.create_table('work_order_cost',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('material_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('labour_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('total_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('expected_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('variance_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('capitalised_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('costed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('entry_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('labour_value >= 0', name='ck_work_order_cost_labour'),
    sa.CheckConstraint('material_value >= 0', name='ck_work_order_cost_material'),
    sa.CheckConstraint('total_value >= 0', name='ck_work_order_cost_total'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_order.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('work_order_id', name='uq_work_order_cost_order')
    )
    op.create_index(op.f('ix_work_order_cost_company_id'), 'work_order_cost', ['company_id'], unique=False)
    op.create_index(op.f('ix_work_order_cost_entry_id'), 'work_order_cost', ['entry_id'], unique=False)
    op.create_index(op.f('ix_work_order_cost_work_order_id'), 'work_order_cost', ['work_order_id'], unique=False)
    op.create_table('work_order_operation',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('work_center_code', sa.String(length=32), nullable=True),
    sa.Column('setup_minutes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('run_minutes_per_unit', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('planned_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint('planned_quantity > 0', name='ck_work_order_operation_quantity'),
    sa.CheckConstraint('run_minutes_per_unit > 0', name='ck_work_order_operation_run'),
    sa.CheckConstraint('sequence >= 1', name='ck_work_order_operation_sequence'),
    sa.CheckConstraint('setup_minutes >= 0', name='ck_work_order_operation_setup'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_order.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('work_order_id', 'sequence', name='uq_work_order_operation_seq')
    )
    op.create_index(op.f('ix_work_order_operation_company_id'), 'work_order_operation', ['company_id'], unique=False)
    op.create_index(op.f('ix_work_order_operation_work_center_code'), 'work_order_operation', ['work_center_code'], unique=False)
    op.create_index(op.f('ix_work_order_operation_work_order_id'), 'work_order_operation', ['work_order_id'], unique=False)
    op.create_table('work_order_receipt',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('movement_id', sa.Uuid(), nullable=False),
    sa.Column('posted_on', sa.Date(), nullable=False),
    sa.Column('completes', sa.Boolean(), nullable=False),
    sa.CheckConstraint('quantity > 0', name='ck_work_order_receipt_quantity'),
    sa.CheckConstraint('value >= 0', name='ck_work_order_receipt_value'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.ForeignKeyConstraint(['movement_id'], ['stock_ledger_entry.id'], ),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_order.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_work_order_receipt_company_id'), 'work_order_receipt', ['company_id'], unique=False)
    op.create_index(op.f('ix_work_order_receipt_item_id'), 'work_order_receipt', ['item_id'], unique=False)
    op.create_index(op.f('ix_work_order_receipt_location_id'), 'work_order_receipt', ['location_id'], unique=False)
    op.create_index(op.f('ix_work_order_receipt_movement_id'), 'work_order_receipt', ['movement_id'], unique=False)
    op.create_index(op.f('ix_work_order_receipt_posted_on'), 'work_order_receipt', ['posted_on'], unique=False)
    op.create_index(op.f('ix_work_order_receipt_work_order_id'), 'work_order_receipt', ['work_order_id'], unique=False)
    op.create_table('work_order_requirement',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('quantity_required', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('path', sa.String(length=400), nullable=True),
    sa.CheckConstraint('level >= 1', name='ck_work_order_requirement_level'),
    sa.CheckConstraint('quantity_required > 0', name='ck_work_order_requirement_quantity'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_order.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_work_order_requirement_company_id'), 'work_order_requirement', ['company_id'], unique=False)
    op.create_index(op.f('ix_work_order_requirement_item_id'), 'work_order_requirement', ['item_id'], unique=False)
    op.create_index(op.f('ix_work_order_requirement_work_order_id'), 'work_order_requirement', ['work_order_id'], unique=False)
    op.create_table('job_card',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('operator', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=8), nullable=False),
    sa.Column('opened_on', sa.Date(), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_by', sa.String(length=64), nullable=True),
    sa.CheckConstraint("status IN ('open', 'closed')", name='ck_job_card_status'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['operation_id'], ['work_order_operation.id'], ),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_order.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_job_card_company_id'), 'job_card', ['company_id'], unique=False)
    op.create_index(op.f('ix_job_card_opened_on'), 'job_card', ['opened_on'], unique=False)
    op.create_index(op.f('ix_job_card_operation_id'), 'job_card', ['operation_id'], unique=False)
    op.create_index(op.f('ix_job_card_work_order_id'), 'job_card', ['work_order_id'], unique=False)
    op.create_table('payroll_line_component',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('line_id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('basis', sa.String(length=24), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('order_no', sa.Integer(), nullable=False),
    sa.Column('taxable', sa.Boolean(), nullable=False),
    sa.Column('account_code', sa.String(length=32), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('component_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['component_id'], ['payroll_component.id'], ),
    sa.ForeignKeyConstraint(['line_id'], ['payroll_line.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('line_id', 'code', name='uq_payroll_line_component_code')
    )
    op.create_index(op.f('ix_payroll_line_component_company_id'), 'payroll_line_component', ['company_id'], unique=False)
    op.create_index(op.f('ix_payroll_line_component_line_id'), 'payroll_line_component', ['line_id'], unique=False)
    op.create_table('payroll_line_input',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('line_id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('on_date', sa.Date(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('worked_minutes', sa.Integer(), nullable=False),
    sa.Column('scheduled_minutes', sa.Integer(), nullable=False),
    sa.Column('late_minutes', sa.Integer(), nullable=False),
    sa.Column('overtime_minutes', sa.Integer(), nullable=False),
    sa.Column('multiplier', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('source_type', sa.String(length=24), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint("kind IN ('worked', 'paid_leave', 'unpaid_leave', 'holiday', 'absent', 'not_worked')", name='ck_payroll_line_input_kind'),
    sa.CheckConstraint('overtime_minutes >= 0', name='ck_payroll_line_input_overtime'),
    sa.CheckConstraint('worked_minutes >= 0', name='ck_payroll_line_input_worked'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['line_id'], ['payroll_line.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('line_id', 'on_date', name='uq_payroll_line_input_day')
    )
    op.create_index(op.f('ix_payroll_line_input_company_id'), 'payroll_line_input', ['company_id'], unique=False)
    op.create_index(op.f('ix_payroll_line_input_employee_id'), 'payroll_line_input', ['employee_id'], unique=False)
    op.create_index(op.f('ix_payroll_line_input_line_id'), 'payroll_line_input', ['line_id'], unique=False)
    op.create_table('purchase_order',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('requisition_id', sa.Uuid(), nullable=False),
    sa.Column('rfq_id', sa.Uuid(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('required_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('revision_no', sa.Integer(), nullable=False),
    sa.Column('revision_of_id', sa.Uuid(), nullable=True),
    sa.Column('over_award_reason', sa.Text(), nullable=True),
    sa.Column('close_reason', sa.Text(), nullable=True),
    sa.Column('approved_by', sa.String(length=64), nullable=True),
    sa.Column('approval_request_id', sa.Uuid(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'pending', 'approved', 'acknowledged', 'returned', 'rejected', 'closed')", name='ck_purchase_order_status'),
    sa.CheckConstraint('(revision_no = 1) = (revision_of_id IS NULL)', name='ck_purchase_order_revision_pair'),
    sa.CheckConstraint('revision_no >= 1', name='ck_purchase_order_revision'),
    sa.ForeignKeyConstraint(['approval_request_id'], ['approval_request.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['requisition_id'], ['purchase_requisition.id'], ),
    sa.ForeignKeyConstraint(['revision_of_id'], ['purchase_order.id'], ),
    sa.ForeignKeyConstraint(['rfq_id'], ['rfq.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_purchase_order_company_number')
    )
    op.create_index(op.f('ix_purchase_order_approval_request_id'), 'purchase_order', ['approval_request_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_company_id'), 'purchase_order', ['company_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_requisition_id'), 'purchase_order', ['requisition_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_revision_of_id'), 'purchase_order', ['revision_of_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_rfq_id'), 'purchase_order', ['rfq_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_supplier_id'), 'purchase_order', ['supplier_id'], unique=False)
    op.create_table('quotation_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('quotation_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('rule_code', sa.String(length=80), nullable=True),
    sa.Column('rule_priority', sa.Integer(), nullable=True),
    sa.Column('priced_on', sa.Date(), nullable=False),
    sa.CheckConstraint('line_no >= 1', name='ck_quotation_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_quotation_line_quantity'),
    sa.CheckConstraint('unit_price >= 0', name='ck_quotation_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['quotation_id'], ['quotation.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('quotation_id', 'line_no', name='uq_quotation_line_no')
    )
    op.create_index(op.f('ix_quotation_line_company_id'), 'quotation_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_quotation_line_item_id'), 'quotation_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_quotation_line_quotation_id'), 'quotation_line', ['quotation_id'], unique=False)
    op.create_table('rfq_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('rfq_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('requisition_line_id', sa.Uuid(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.CheckConstraint('line_no >= 1', name='ck_rfq_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_rfq_line_quantity'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['requisition_line_id'], ['purchase_requisition_line.id'], ),
    sa.ForeignKeyConstraint(['rfq_id'], ['rfq.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('rfq_id', 'line_no', name='uq_rfq_line_no')
    )
    op.create_index(op.f('ix_rfq_line_company_id'), 'rfq_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_rfq_line_requisition_line_id'), 'rfq_line', ['requisition_line_id'], unique=False)
    op.create_index(op.f('ix_rfq_line_rfq_id'), 'rfq_line', ['rfq_id'], unique=False)
    op.create_table('rfq_response',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('rfq_id', sa.Uuid(), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('received_on', sa.Date(), nullable=False),
    sa.Column('late', sa.Boolean(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('lead_time_days', sa.Integer(), nullable=True),
    sa.Column('valid_until', sa.Date(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('lead_time_days IS NULL OR lead_time_days >= 0', name='ck_rfq_response_lead_time'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['rfq_id'], ['rfq.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('rfq_id', 'supplier_id', name='uq_rfq_response_once')
    )
    op.create_index(op.f('ix_rfq_response_company_id'), 'rfq_response', ['company_id'], unique=False)
    op.create_index(op.f('ix_rfq_response_rfq_id'), 'rfq_response', ['rfq_id'], unique=False)
    op.create_index(op.f('ix_rfq_response_supplier_id'), 'rfq_response', ['supplier_id'], unique=False)
    op.create_table('rfq_supplier',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('rfq_id', sa.Uuid(), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['rfq_id'], ['rfq.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('rfq_id', 'supplier_id', name='uq_rfq_supplier_once')
    )
    op.create_index(op.f('ix_rfq_supplier_company_id'), 'rfq_supplier', ['company_id'], unique=False)
    op.create_index(op.f('ix_rfq_supplier_rfq_id'), 'rfq_supplier', ['rfq_id'], unique=False)
    op.create_index(op.f('ix_rfq_supplier_supplier_id'), 'rfq_supplier', ['supplier_id'], unique=False)
    op.create_table('sales_order',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('quotation_id', sa.Uuid(), nullable=True),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('ordered_on', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('confirmed_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'confirmed')", name='ck_sales_order_status'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.ForeignKeyConstraint(['quotation_id'], ['quotation.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_sales_order_company_number')
    )
    op.create_index(op.f('ix_sales_order_company_id'), 'sales_order', ['company_id'], unique=False)
    op.create_index(op.f('ix_sales_order_customer_id'), 'sales_order', ['customer_id'], unique=False)
    op.create_index(op.f('ix_sales_order_quotation_id'), 'sales_order', ['quotation_id'], unique=False)
    op.create_index('uq_sales_order_quotation', 'sales_order', ['quotation_id'], unique=True, postgresql_where=sa.text('quotation_id IS NOT NULL'))
    op.create_table('work_order_issue',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('requirement_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('movement_id', sa.Uuid(), nullable=False),
    sa.Column('posted_on', sa.Date(), nullable=False),
    sa.Column('overridden', sa.Boolean(), nullable=False),
    sa.Column('override_actor', sa.String(length=64), nullable=True),
    sa.Column('override_reason', sa.String(length=200), nullable=True),
    sa.CheckConstraint('NOT overridden OR (override_reason IS NOT NULL AND override_actor IS NOT NULL)', name='ck_work_order_issue_override'),
    sa.CheckConstraint('quantity > 0', name='ck_work_order_issue_quantity'),
    sa.CheckConstraint('value >= 0', name='ck_work_order_issue_value'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.ForeignKeyConstraint(['movement_id'], ['stock_ledger_entry.id'], ),
    sa.ForeignKeyConstraint(['requirement_id'], ['work_order_requirement.id'], ),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_order.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_work_order_issue_company_id'), 'work_order_issue', ['company_id'], unique=False)
    op.create_index(op.f('ix_work_order_issue_item_id'), 'work_order_issue', ['item_id'], unique=False)
    op.create_index(op.f('ix_work_order_issue_location_id'), 'work_order_issue', ['location_id'], unique=False)
    op.create_index(op.f('ix_work_order_issue_movement_id'), 'work_order_issue', ['movement_id'], unique=False)
    op.create_index(op.f('ix_work_order_issue_posted_on'), 'work_order_issue', ['posted_on'], unique=False)
    op.create_index(op.f('ix_work_order_issue_requirement_id'), 'work_order_issue', ['requirement_id'], unique=False)
    op.create_index(op.f('ix_work_order_issue_work_order_id'), 'work_order_issue', ['work_order_id'], unique=False)
    op.create_table('credit_decision',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.Column('limit_amount', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('exposure', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('order_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('acknowledged_by', sa.String(length=64), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("acknowledged_by IS NULL OR outcome = 'breached'", name='ck_credit_decision_acknowledgement'),
    sa.CheckConstraint("mode IN ('off', 'warn', 'block')", name='ck_credit_decision_mode'),
    sa.CheckConstraint("outcome IN ('within_limit', 'breached')", name='ck_credit_decision_outcome'),
    sa.CheckConstraint('exposure >= 0', name='ck_credit_decision_exposure'),
    sa.CheckConstraint('order_value >= 0', name='ck_credit_decision_order_value'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['sales_order.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_credit_decision_company_id'), 'credit_decision', ['company_id'], unique=False)
    op.create_index(op.f('ix_credit_decision_order_id'), 'credit_decision', ['order_id'], unique=False)
    op.create_table('goods_receipt',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=False),
    sa.Column('received_on', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('over_receipt_reason', sa.Text(), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'posted')", name='ck_goods_receipt_status'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['purchase_order.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_goods_receipt_company_number')
    )
    op.create_index(op.f('ix_goods_receipt_company_id'), 'goods_receipt', ['company_id'], unique=False)
    op.create_index(op.f('ix_goods_receipt_location_id'), 'goods_receipt', ['location_id'], unique=False)
    op.create_index(op.f('ix_goods_receipt_order_id'), 'goods_receipt', ['order_id'], unique=False)
    op.create_index(op.f('ix_goods_receipt_supplier_id'), 'goods_receipt', ['supplier_id'], unique=False)
    op.create_table('job_card_entry',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('card_id', sa.Uuid(), nullable=False),
    sa.Column('booked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('setup_minutes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('run_minutes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('produced_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('rejected_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('note', sa.String(length=200), nullable=True),
    sa.Column('corrects_entry_id', sa.Uuid(), nullable=True),
    sa.Column('recorded_by', sa.String(length=64), nullable=True),
    sa.Column('overrun_acknowledged_by', sa.String(length=64), nullable=True),
    sa.Column('overrun_reason', sa.String(length=200), nullable=True),
    sa.CheckConstraint('produced_quantity >= 0', name='ck_job_card_entry_produced'),
    sa.CheckConstraint('rejected_quantity >= 0', name='ck_job_card_entry_rejected'),
    sa.CheckConstraint('run_minutes >= 0', name='ck_job_card_entry_run'),
    sa.CheckConstraint('setup_minutes + run_minutes > 0', name='ck_job_card_entry_time'),
    sa.CheckConstraint('setup_minutes >= 0', name='ck_job_card_entry_setup'),
    sa.ForeignKeyConstraint(['card_id'], ['job_card.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['corrects_entry_id'], ['job_card_entry.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_job_card_entry_card_id'), 'job_card_entry', ['card_id'], unique=False)
    op.create_index(op.f('ix_job_card_entry_company_id'), 'job_card_entry', ['company_id'], unique=False)
    op.create_index(op.f('ix_job_card_entry_corrects_entry_id'), 'job_card_entry', ['corrects_entry_id'], unique=False)
    op.create_table('pick_list',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('created_on', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['sales_order.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_pick_list_company_number'),
    sa.UniqueConstraint('order_id', name='uq_pick_list_order')
    )
    op.create_index(op.f('ix_pick_list_company_id'), 'pick_list', ['company_id'], unique=False)
    op.create_index(op.f('ix_pick_list_order_id'), 'pick_list', ['order_id'], unique=False)
    op.create_table('purchase_order_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('received_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('rfq_line_id', sa.Uuid(), nullable=False),
    sa.Column('requisition_line_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('line_no >= 1', name='ck_purchase_order_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_purchase_order_line_quantity'),
    sa.CheckConstraint('received_quantity >= 0', name='ck_purchase_order_line_received'),
    sa.CheckConstraint('unit_price >= 0', name='ck_purchase_order_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['purchase_order.id'], ),
    sa.ForeignKeyConstraint(['requisition_line_id'], ['purchase_requisition_line.id'], ),
    sa.ForeignKeyConstraint(['rfq_line_id'], ['rfq_line.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('order_id', 'line_no', name='uq_purchase_order_line_no')
    )
    op.create_index(op.f('ix_purchase_order_line_company_id'), 'purchase_order_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_line_item_id'), 'purchase_order_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_line_order_id'), 'purchase_order_line', ['order_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_line_requisition_line_id'), 'purchase_order_line', ['requisition_line_id'], unique=False)
    op.create_index(op.f('ix_purchase_order_line_rfq_line_id'), 'purchase_order_line', ['rfq_line_id'], unique=False)
    op.create_table('rfq_response_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('response_id', sa.Uuid(), nullable=False),
    sa.Column('rfq_line_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint('line_no >= 1', name='ck_rfq_response_line_starts_at_one'),
    sa.CheckConstraint('unit_price >= 0', name='ck_rfq_response_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['response_id'], ['rfq_response.id'], ),
    sa.ForeignKeyConstraint(['rfq_line_id'], ['rfq_line.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('response_id', 'line_no', name='uq_rfq_response_line_no'),
    sa.UniqueConstraint('response_id', 'rfq_line_id', name='uq_rfq_response_line_once')
    )
    op.create_index(op.f('ix_rfq_response_line_company_id'), 'rfq_response_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_rfq_response_line_response_id'), 'rfq_response_line', ['response_id'], unique=False)
    op.create_index(op.f('ix_rfq_response_line_rfq_line_id'), 'rfq_response_line', ['rfq_line_id'], unique=False)
    op.create_table('sales_order_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('rule_code', sa.String(length=80), nullable=True),
    sa.Column('rule_priority', sa.Integer(), nullable=True),
    sa.Column('priced_on', sa.Date(), nullable=False),
    sa.Column('shipped_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint('line_no >= 1', name='ck_sales_order_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_sales_order_line_quantity'),
    sa.CheckConstraint('shipped_quantity >= 0', name='ck_sales_order_line_shipped'),
    sa.CheckConstraint('unit_price >= 0', name='ck_sales_order_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['sales_order.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('order_id', 'line_no', name='uq_sales_order_line_no')
    )
    op.create_index(op.f('ix_sales_order_line_company_id'), 'sales_order_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_sales_order_line_item_id'), 'sales_order_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_sales_order_line_order_id'), 'sales_order_line', ['order_id'], unique=False)
    op.create_table('shipment',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=False),
    sa.Column('shipped_on', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['sales_order.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_shipment_company_number')
    )
    op.create_index(op.f('ix_shipment_company_id'), 'shipment', ['company_id'], unique=False)
    op.create_index(op.f('ix_shipment_location_id'), 'shipment', ['location_id'], unique=False)
    op.create_index(op.f('ix_shipment_order_id'), 'shipment', ['order_id'], unique=False)
    op.create_table('customer_invoice',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=True),
    sa.Column('shipment_id', sa.Uuid(), nullable=True),
    sa.Column('invoice_date', sa.Date(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('net_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('gross_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_rule_code', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('journal_entry_id', sa.Uuid(), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'posted')", name='ck_customer_invoice_status'),
    sa.CheckConstraint('gross_amount >= 0', name='ck_customer_invoice_gross'),
    sa.CheckConstraint('net_amount >= 0', name='ck_customer_invoice_net'),
    sa.CheckConstraint('tax_amount >= 0', name='ck_customer_invoice_tax'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.ForeignKeyConstraint(['journal_entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['sales_order.id'], ),
    sa.ForeignKeyConstraint(['shipment_id'], ['shipment.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_customer_invoice_company_number')
    )
    op.create_index(op.f('ix_customer_invoice_company_id'), 'customer_invoice', ['company_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_customer_id'), 'customer_invoice', ['customer_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_due_date'), 'customer_invoice', ['due_date'], unique=False)
    op.create_index(op.f('ix_customer_invoice_journal_entry_id'), 'customer_invoice', ['journal_entry_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_order_id'), 'customer_invoice', ['order_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_shipment_id'), 'customer_invoice', ['shipment_id'], unique=False)
    op.create_index('uq_customer_invoice_duplicate', 'customer_invoice', ['company_id', 'customer_id', 'order_id', 'gross_amount'], unique=True, postgresql_where=sa.text('order_id IS NOT NULL'))
    op.create_table('goods_receipt_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('receipt_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('order_line_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('rejected_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('batch_code', sa.String(length=64), nullable=True),
    sa.Column('serial_code', sa.String(length=64), nullable=True),
    sa.Column('movement_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('line_no >= 1', name='ck_goods_receipt_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_goods_receipt_line_quantity'),
    sa.CheckConstraint('rejected_quantity >= 0', name='ck_goods_receipt_line_rejected'),
    sa.CheckConstraint('unit_price >= 0', name='ck_goods_receipt_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['movement_id'], ['stock_ledger_entry.id'], ),
    sa.ForeignKeyConstraint(['order_line_id'], ['purchase_order_line.id'], ),
    sa.ForeignKeyConstraint(['receipt_id'], ['goods_receipt.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('receipt_id', 'line_no', name='uq_goods_receipt_line_no'),
    sa.UniqueConstraint('receipt_id', 'order_line_id', name='uq_goods_receipt_line_once')
    )
    op.create_index(op.f('ix_goods_receipt_line_company_id'), 'goods_receipt_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_goods_receipt_line_movement_id'), 'goods_receipt_line', ['movement_id'], unique=False)
    op.create_index(op.f('ix_goods_receipt_line_order_line_id'), 'goods_receipt_line', ['order_line_id'], unique=False)
    op.create_index(op.f('ix_goods_receipt_line_receipt_id'), 'goods_receipt_line', ['receipt_id'], unique=False)
    op.create_table('pick_list_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('pick_list_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('order_line_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('picked_quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.CheckConstraint('line_no >= 1', name='ck_pick_list_line_starts_at_one'),
    sa.CheckConstraint('picked_quantity <= quantity', name='ck_pick_list_line_not_over_picked'),
    sa.CheckConstraint('picked_quantity >= 0', name='ck_pick_list_line_picked_not_negative'),
    sa.CheckConstraint('quantity > 0', name='ck_pick_list_line_quantity'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['order_line_id'], ['sales_order_line.id'], ),
    sa.ForeignKeyConstraint(['pick_list_id'], ['pick_list.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('pick_list_id', 'line_no', name='uq_pick_list_line_no'),
    sa.UniqueConstraint('pick_list_id', 'order_line_id', name='uq_pick_list_line_once')
    )
    op.create_index(op.f('ix_pick_list_line_company_id'), 'pick_list_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_pick_list_line_item_id'), 'pick_list_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_pick_list_line_order_line_id'), 'pick_list_line', ['order_line_id'], unique=False)
    op.create_index(op.f('ix_pick_list_line_pick_list_id'), 'pick_list_line', ['pick_list_id'], unique=False)
    op.create_table('shipment_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('shipment_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('order_line_id', sa.Uuid(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('movement_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('line_no >= 1', name='ck_shipment_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_shipment_line_quantity'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['movement_id'], ['stock_ledger_entry.id'], ),
    sa.ForeignKeyConstraint(['order_line_id'], ['sales_order_line.id'], ),
    sa.ForeignKeyConstraint(['shipment_id'], ['shipment.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('shipment_id', 'line_no', name='uq_shipment_line_no'),
    sa.UniqueConstraint('shipment_id', 'order_line_id', name='uq_shipment_line_once')
    )
    op.create_index(op.f('ix_shipment_line_company_id'), 'shipment_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_shipment_line_movement_id'), 'shipment_line', ['movement_id'], unique=False)
    op.create_index(op.f('ix_shipment_line_order_line_id'), 'shipment_line', ['order_line_id'], unique=False)
    op.create_index(op.f('ix_shipment_line_shipment_id'), 'shipment_line', ['shipment_id'], unique=False)
    op.create_table('supplier_invoice',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('supplier_reference', sa.String(length=64), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=True),
    sa.Column('receipt_id', sa.Uuid(), nullable=True),
    sa.Column('invoice_date', sa.Date(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('net_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('gross_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('journal_entry_id', sa.Uuid(), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'posted')", name='ck_supplier_invoice_status'),
    sa.CheckConstraint('gross_amount >= 0', name='ck_supplier_invoice_gross'),
    sa.CheckConstraint('net_amount >= 0', name='ck_supplier_invoice_net'),
    sa.CheckConstraint('tax_amount >= 0', name='ck_supplier_invoice_tax'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['journal_entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['purchase_order.id'], ),
    sa.ForeignKeyConstraint(['receipt_id'], ['goods_receipt.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_supplier_invoice_company_number'),
    sa.UniqueConstraint('company_id', 'supplier_id', 'supplier_reference', 'gross_amount', name='uq_supplier_invoice_duplicate')
    )
    op.create_index(op.f('ix_supplier_invoice_company_id'), 'supplier_invoice', ['company_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_due_date'), 'supplier_invoice', ['due_date'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_journal_entry_id'), 'supplier_invoice', ['journal_entry_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_order_id'), 'supplier_invoice', ['order_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_receipt_id'), 'supplier_invoice', ['receipt_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_supplier_id'), 'supplier_invoice', ['supplier_id'], unique=False)
    op.create_table('customer_invoice_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=True),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_rule_code', sa.String(length=32), nullable=True),
    sa.Column('order_line_id', sa.Uuid(), nullable=True),
    sa.Column('shipment_line_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('line_no >= 1', name='ck_customer_invoice_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_customer_invoice_line_quantity'),
    sa.CheckConstraint('tax_amount >= 0', name='ck_customer_invoice_line_tax'),
    sa.CheckConstraint('unit_price >= 0', name='ck_customer_invoice_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['customer_invoice.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['order_line_id'], ['sales_order_line.id'], ),
    sa.ForeignKeyConstraint(['shipment_line_id'], ['shipment_line.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('invoice_id', 'line_no', name='uq_customer_invoice_line_no')
    )
    op.create_index(op.f('ix_customer_invoice_line_company_id'), 'customer_invoice_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_line_invoice_id'), 'customer_invoice_line', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_line_item_id'), 'customer_invoice_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_line_order_line_id'), 'customer_invoice_line', ['order_line_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_line_shipment_line_id'), 'customer_invoice_line', ['shipment_line_id'], unique=False)
    op.create_table('customer_invoice_settlement',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('settled_on', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('source_type', sa.String(length=32), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount > 0', name='ck_customer_settlement_amount'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['customer_invoice.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('invoice_id', 'source_type', 'source_id', name='uq_customer_settlement_once_per_source')
    )
    op.create_index(op.f('ix_customer_invoice_settlement_company_id'), 'customer_invoice_settlement', ['company_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_settlement_invoice_id'), 'customer_invoice_settlement', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_customer_invoice_settlement_settled_on'), 'customer_invoice_settlement', ['settled_on'], unique=False)
    op.create_index(op.f('ix_customer_invoice_settlement_source_id'), 'customer_invoice_settlement', ['source_id'], unique=False)
    op.create_table('debit_note',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=32), nullable=False),
    sa.Column('supplier_id', sa.Uuid(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('location_id', sa.Uuid(), nullable=True),
    sa.Column('note_date', sa.Date(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('net_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('gross_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('over_note_reason', sa.Text(), nullable=True),
    sa.Column('unapplied_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('journal_entry_id', sa.Uuid(), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind = 'adjustment') OR (location_id IS NOT NULL)", name='ck_debit_note_return_has_location'),
    sa.CheckConstraint("kind IN ('return', 'adjustment')", name='ck_debit_note_kind'),
    sa.CheckConstraint("status IN ('draft', 'posted')", name='ck_debit_note_status'),
    sa.CheckConstraint('gross_amount >= 0', name='ck_debit_note_gross'),
    sa.CheckConstraint('net_amount >= 0', name='ck_debit_note_net'),
    sa.CheckConstraint('tax_amount >= 0', name='ck_debit_note_tax'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['supplier_invoice.id'], ),
    sa.ForeignKeyConstraint(['journal_entry_id'], ['journal_entry.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['location.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'number', name='uq_debit_note_company_number')
    )
    op.create_index(op.f('ix_debit_note_company_id'), 'debit_note', ['company_id'], unique=False)
    op.create_index(op.f('ix_debit_note_invoice_id'), 'debit_note', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_debit_note_journal_entry_id'), 'debit_note', ['journal_entry_id'], unique=False)
    op.create_index(op.f('ix_debit_note_location_id'), 'debit_note', ['location_id'], unique=False)
    op.create_index(op.f('ix_debit_note_supplier_id'), 'debit_note', ['supplier_id'], unique=False)
    op.create_table('dunning_reminder',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('level_code', sa.String(length=32), nullable=False),
    sa.Column('run_key', sa.String(length=16), nullable=False),
    sa.Column('days_past_due', sa.Integer(), nullable=False),
    sa.Column('open_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('channel', sa.String(length=8), nullable=False),
    sa.Column('destination', sa.String(length=200), nullable=True),
    sa.Column('reminder_on', sa.Date(), nullable=False),
    sa.Column('delivery_id', sa.Uuid(), nullable=True),
    sa.Column('delivered', sa.Boolean(), nullable=False),
    sa.Column('failure', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('days_past_due >= 0', name='ck_dunning_reminder_days'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['delivery_id'], ['outbound_delivery.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['customer_invoice.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'invoice_id', 'level_code', 'run_key', name='uq_dunning_reminder_once_per_run')
    )
    op.create_index(op.f('ix_dunning_reminder_company_id'), 'dunning_reminder', ['company_id'], unique=False)
    op.create_index(op.f('ix_dunning_reminder_delivery_id'), 'dunning_reminder', ['delivery_id'], unique=False)
    op.create_index(op.f('ix_dunning_reminder_invoice_id'), 'dunning_reminder', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_dunning_reminder_run_key'), 'dunning_reminder', ['run_key'], unique=False)
    op.create_table('gateway_payment',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('reference', sa.String(length=64), nullable=False),
    sa.Column('event_key', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('fee', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('paid_on', sa.Date(), nullable=False),
    sa.Column('invoice_number', sa.String(length=32), nullable=True),
    sa.Column('invoice_id', sa.Uuid(), nullable=True),
    sa.Column('customer_id', sa.Uuid(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('journal_entry_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('settled', 'partial', 'parked', 'failed')", name='ck_gateway_payment_status'),
    sa.CheckConstraint('amount > 0', name='ck_gateway_payment_amount'),
    sa.CheckConstraint('fee <= amount', name='ck_gateway_payment_fee_within_amount'),
    sa.CheckConstraint('fee >= 0', name='ck_gateway_payment_fee'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['customer_invoice.id'], ),
    sa.ForeignKeyConstraint(['journal_entry_id'], ['journal_entry.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'reference', name='uq_gateway_payment_company_reference')
    )
    op.create_index(op.f('ix_gateway_payment_company_id'), 'gateway_payment', ['company_id'], unique=False)
    op.create_index(op.f('ix_gateway_payment_customer_id'), 'gateway_payment', ['customer_id'], unique=False)
    op.create_index(op.f('ix_gateway_payment_invoice_id'), 'gateway_payment', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_gateway_payment_journal_entry_id'), 'gateway_payment', ['journal_entry_id'], unique=False)
    op.create_table('match_run',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('quantity_ok', sa.Boolean(), nullable=False),
    sa.Column('price_ok', sa.Boolean(), nullable=False),
    sa.Column('tax_ok', sa.Boolean(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('matched_on', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('matched', 'partial', 'failed')", name='ck_match_run_status'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['supplier_invoice.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_match_run_company_id'), 'match_run', ['company_id'], unique=False)
    op.create_index(op.f('ix_match_run_created_at'), 'match_run', ['created_at'], unique=False)
    op.create_index(op.f('ix_match_run_invoice_id'), 'match_run', ['invoice_id'], unique=False)
    op.create_table('payment_batch_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('batch_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('journal_entry_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('amount > 0', name='ck_payment_batch_line_amount'),
    sa.CheckConstraint('line_no >= 1', name='ck_payment_batch_line_starts_at_one'),
    sa.ForeignKeyConstraint(['batch_id'], ['payment_batch.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['supplier_invoice.id'], ),
    sa.ForeignKeyConstraint(['journal_entry_id'], ['journal_entry.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('batch_id', 'invoice_id', name='uq_payment_batch_line_once'),
    sa.UniqueConstraint('batch_id', 'line_no', name='uq_payment_batch_line_no')
    )
    op.create_index(op.f('ix_payment_batch_line_batch_id'), 'payment_batch_line', ['batch_id'], unique=False)
    op.create_index(op.f('ix_payment_batch_line_company_id'), 'payment_batch_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_payment_batch_line_invoice_id'), 'payment_batch_line', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_payment_batch_line_journal_entry_id'), 'payment_batch_line', ['journal_entry_id'], unique=False)
    op.create_table('recurring_invoice_run',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('template_id', sa.Uuid(), nullable=False),
    sa.Column('period_key', sa.String(length=16), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('generated_on', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['customer_invoice.id'], ),
    sa.ForeignKeyConstraint(['template_id'], ['recurring_template.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'template_id', 'period_key', name='uq_recurring_run_period_once')
    )
    op.create_index(op.f('ix_recurring_invoice_run_company_id'), 'recurring_invoice_run', ['company_id'], unique=False)
    op.create_index(op.f('ix_recurring_invoice_run_invoice_id'), 'recurring_invoice_run', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_recurring_invoice_run_template_id'), 'recurring_invoice_run', ['template_id'], unique=False)
    op.create_table('supplier_invoice_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('order_line_id', sa.Uuid(), nullable=True),
    sa.Column('receipt_line_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('line_no >= 1', name='ck_supplier_invoice_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_supplier_invoice_line_quantity'),
    sa.CheckConstraint('tax_amount >= 0', name='ck_supplier_invoice_line_tax'),
    sa.CheckConstraint('unit_price >= 0', name='ck_supplier_invoice_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['supplier_invoice.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['order_line_id'], ['purchase_order_line.id'], ),
    sa.ForeignKeyConstraint(['receipt_line_id'], ['goods_receipt_line.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('invoice_id', 'line_no', name='uq_supplier_invoice_line_no')
    )
    op.create_index(op.f('ix_supplier_invoice_line_company_id'), 'supplier_invoice_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_line_invoice_id'), 'supplier_invoice_line', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_line_item_id'), 'supplier_invoice_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_line_order_line_id'), 'supplier_invoice_line', ['order_line_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_line_receipt_line_id'), 'supplier_invoice_line', ['receipt_line_id'], unique=False)
    op.create_table('supplier_invoice_settlement',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('settled_on', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('source_type', sa.String(length=32), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount > 0', name='ck_settlement_amount'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['supplier_invoice.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_supplier_invoice_settlement_company_id'), 'supplier_invoice_settlement', ['company_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_settlement_invoice_id'), 'supplier_invoice_settlement', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_settlement_settled_on'), 'supplier_invoice_settlement', ['settled_on'], unique=False)
    op.create_index(op.f('ix_supplier_invoice_settlement_source_id'), 'supplier_invoice_settlement', ['source_id'], unique=False)
    op.create_table('debit_note_line',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('note_id', sa.Uuid(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('uom', sa.String(length=16), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('movement_id', sa.Uuid(), nullable=True),
    sa.Column('invoice_line_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint('line_no >= 1', name='ck_debit_note_line_starts_at_one'),
    sa.CheckConstraint('quantity > 0', name='ck_debit_note_line_quantity'),
    sa.CheckConstraint('tax_amount >= 0', name='ck_debit_note_line_tax'),
    sa.CheckConstraint('unit_price >= 0', name='ck_debit_note_line_price'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_line_id'], ['supplier_invoice_line.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['item.id'], ),
    sa.ForeignKeyConstraint(['movement_id'], ['stock_ledger_entry.id'], ),
    sa.ForeignKeyConstraint(['note_id'], ['debit_note.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('note_id', 'line_no', name='uq_debit_note_line_no')
    )
    op.create_index(op.f('ix_debit_note_line_company_id'), 'debit_note_line', ['company_id'], unique=False)
    op.create_index(op.f('ix_debit_note_line_invoice_line_id'), 'debit_note_line', ['invoice_line_id'], unique=False)
    op.create_index(op.f('ix_debit_note_line_item_id'), 'debit_note_line', ['item_id'], unique=False)
    op.create_index(op.f('ix_debit_note_line_movement_id'), 'debit_note_line', ['movement_id'], unique=False)
    op.create_index(op.f('ix_debit_note_line_note_id'), 'debit_note_line', ['note_id'], unique=False)
    op.create_table('match_hold',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('opened_on', sa.Date(), nullable=False),
    sa.Column('opened_reason', sa.Text(), nullable=False),
    sa.Column('resolved_on', sa.Date(), nullable=True),
    sa.Column('resolved_by', sa.String(length=64), nullable=True),
    sa.Column('resolve_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('held', 'released')", name='ck_match_hold_state'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['supplier_invoice.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['match_run.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_match_hold_company_id'), 'match_hold', ['company_id'], unique=False)
    op.create_index(op.f('ix_match_hold_invoice_id'), 'match_hold', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_match_hold_run_id'), 'match_hold', ['run_id'], unique=False)
    op.create_index('uq_match_hold_open', 'match_hold', ['invoice_id'], unique=True, postgresql_where=sa.text("state = 'held'"), sqlite_where=sa.text("state = 'held'"))


def downgrade() -> None:
    op.drop_index('uq_match_hold_open', table_name='match_hold', postgresql_where=sa.text("state = 'held'"), sqlite_where=sa.text("state = 'held'"))
    op.drop_index(op.f('ix_match_hold_run_id'), table_name='match_hold')
    op.drop_index(op.f('ix_match_hold_invoice_id'), table_name='match_hold')
    op.drop_index(op.f('ix_match_hold_company_id'), table_name='match_hold')
    op.drop_table('match_hold')
    op.drop_index(op.f('ix_debit_note_line_note_id'), table_name='debit_note_line')
    op.drop_index(op.f('ix_debit_note_line_movement_id'), table_name='debit_note_line')
    op.drop_index(op.f('ix_debit_note_line_item_id'), table_name='debit_note_line')
    op.drop_index(op.f('ix_debit_note_line_invoice_line_id'), table_name='debit_note_line')
    op.drop_index(op.f('ix_debit_note_line_company_id'), table_name='debit_note_line')
    op.drop_table('debit_note_line')
    op.drop_index(op.f('ix_supplier_invoice_settlement_source_id'), table_name='supplier_invoice_settlement')
    op.drop_index(op.f('ix_supplier_invoice_settlement_settled_on'), table_name='supplier_invoice_settlement')
    op.drop_index(op.f('ix_supplier_invoice_settlement_invoice_id'), table_name='supplier_invoice_settlement')
    op.drop_index(op.f('ix_supplier_invoice_settlement_company_id'), table_name='supplier_invoice_settlement')
    op.drop_table('supplier_invoice_settlement')
    op.drop_index(op.f('ix_supplier_invoice_line_receipt_line_id'), table_name='supplier_invoice_line')
    op.drop_index(op.f('ix_supplier_invoice_line_order_line_id'), table_name='supplier_invoice_line')
    op.drop_index(op.f('ix_supplier_invoice_line_item_id'), table_name='supplier_invoice_line')
    op.drop_index(op.f('ix_supplier_invoice_line_invoice_id'), table_name='supplier_invoice_line')
    op.drop_index(op.f('ix_supplier_invoice_line_company_id'), table_name='supplier_invoice_line')
    op.drop_table('supplier_invoice_line')
    op.drop_index(op.f('ix_recurring_invoice_run_template_id'), table_name='recurring_invoice_run')
    op.drop_index(op.f('ix_recurring_invoice_run_invoice_id'), table_name='recurring_invoice_run')
    op.drop_index(op.f('ix_recurring_invoice_run_company_id'), table_name='recurring_invoice_run')
    op.drop_table('recurring_invoice_run')
    op.drop_index(op.f('ix_payment_batch_line_journal_entry_id'), table_name='payment_batch_line')
    op.drop_index(op.f('ix_payment_batch_line_invoice_id'), table_name='payment_batch_line')
    op.drop_index(op.f('ix_payment_batch_line_company_id'), table_name='payment_batch_line')
    op.drop_index(op.f('ix_payment_batch_line_batch_id'), table_name='payment_batch_line')
    op.drop_table('payment_batch_line')
    op.drop_index(op.f('ix_match_run_invoice_id'), table_name='match_run')
    op.drop_index(op.f('ix_match_run_created_at'), table_name='match_run')
    op.drop_index(op.f('ix_match_run_company_id'), table_name='match_run')
    op.drop_table('match_run')
    op.drop_index(op.f('ix_gateway_payment_journal_entry_id'), table_name='gateway_payment')
    op.drop_index(op.f('ix_gateway_payment_invoice_id'), table_name='gateway_payment')
    op.drop_index(op.f('ix_gateway_payment_customer_id'), table_name='gateway_payment')
    op.drop_index(op.f('ix_gateway_payment_company_id'), table_name='gateway_payment')
    op.drop_table('gateway_payment')
    op.drop_index(op.f('ix_dunning_reminder_run_key'), table_name='dunning_reminder')
    op.drop_index(op.f('ix_dunning_reminder_invoice_id'), table_name='dunning_reminder')
    op.drop_index(op.f('ix_dunning_reminder_delivery_id'), table_name='dunning_reminder')
    op.drop_index(op.f('ix_dunning_reminder_company_id'), table_name='dunning_reminder')
    op.drop_table('dunning_reminder')
    op.drop_index(op.f('ix_debit_note_supplier_id'), table_name='debit_note')
    op.drop_index(op.f('ix_debit_note_location_id'), table_name='debit_note')
    op.drop_index(op.f('ix_debit_note_journal_entry_id'), table_name='debit_note')
    op.drop_index(op.f('ix_debit_note_invoice_id'), table_name='debit_note')
    op.drop_index(op.f('ix_debit_note_company_id'), table_name='debit_note')
    op.drop_table('debit_note')
    op.drop_index(op.f('ix_customer_invoice_settlement_source_id'), table_name='customer_invoice_settlement')
    op.drop_index(op.f('ix_customer_invoice_settlement_settled_on'), table_name='customer_invoice_settlement')
    op.drop_index(op.f('ix_customer_invoice_settlement_invoice_id'), table_name='customer_invoice_settlement')
    op.drop_index(op.f('ix_customer_invoice_settlement_company_id'), table_name='customer_invoice_settlement')
    op.drop_table('customer_invoice_settlement')
    op.drop_index(op.f('ix_customer_invoice_line_shipment_line_id'), table_name='customer_invoice_line')
    op.drop_index(op.f('ix_customer_invoice_line_order_line_id'), table_name='customer_invoice_line')
    op.drop_index(op.f('ix_customer_invoice_line_item_id'), table_name='customer_invoice_line')
    op.drop_index(op.f('ix_customer_invoice_line_invoice_id'), table_name='customer_invoice_line')
    op.drop_index(op.f('ix_customer_invoice_line_company_id'), table_name='customer_invoice_line')
    op.drop_table('customer_invoice_line')
    op.drop_index(op.f('ix_supplier_invoice_supplier_id'), table_name='supplier_invoice')
    op.drop_index(op.f('ix_supplier_invoice_receipt_id'), table_name='supplier_invoice')
    op.drop_index(op.f('ix_supplier_invoice_order_id'), table_name='supplier_invoice')
    op.drop_index(op.f('ix_supplier_invoice_journal_entry_id'), table_name='supplier_invoice')
    op.drop_index(op.f('ix_supplier_invoice_due_date'), table_name='supplier_invoice')
    op.drop_index(op.f('ix_supplier_invoice_company_id'), table_name='supplier_invoice')
    op.drop_table('supplier_invoice')
    op.drop_index(op.f('ix_shipment_line_shipment_id'), table_name='shipment_line')
    op.drop_index(op.f('ix_shipment_line_order_line_id'), table_name='shipment_line')
    op.drop_index(op.f('ix_shipment_line_movement_id'), table_name='shipment_line')
    op.drop_index(op.f('ix_shipment_line_company_id'), table_name='shipment_line')
    op.drop_table('shipment_line')
    op.drop_index(op.f('ix_pick_list_line_pick_list_id'), table_name='pick_list_line')
    op.drop_index(op.f('ix_pick_list_line_order_line_id'), table_name='pick_list_line')
    op.drop_index(op.f('ix_pick_list_line_item_id'), table_name='pick_list_line')
    op.drop_index(op.f('ix_pick_list_line_company_id'), table_name='pick_list_line')
    op.drop_table('pick_list_line')
    op.drop_index(op.f('ix_goods_receipt_line_receipt_id'), table_name='goods_receipt_line')
    op.drop_index(op.f('ix_goods_receipt_line_order_line_id'), table_name='goods_receipt_line')
    op.drop_index(op.f('ix_goods_receipt_line_movement_id'), table_name='goods_receipt_line')
    op.drop_index(op.f('ix_goods_receipt_line_company_id'), table_name='goods_receipt_line')
    op.drop_table('goods_receipt_line')
    op.drop_index('uq_customer_invoice_duplicate', table_name='customer_invoice', postgresql_where=sa.text('order_id IS NOT NULL'))
    op.drop_index(op.f('ix_customer_invoice_shipment_id'), table_name='customer_invoice')
    op.drop_index(op.f('ix_customer_invoice_order_id'), table_name='customer_invoice')
    op.drop_index(op.f('ix_customer_invoice_journal_entry_id'), table_name='customer_invoice')
    op.drop_index(op.f('ix_customer_invoice_due_date'), table_name='customer_invoice')
    op.drop_index(op.f('ix_customer_invoice_customer_id'), table_name='customer_invoice')
    op.drop_index(op.f('ix_customer_invoice_company_id'), table_name='customer_invoice')
    op.drop_table('customer_invoice')
    op.drop_index(op.f('ix_shipment_order_id'), table_name='shipment')
    op.drop_index(op.f('ix_shipment_location_id'), table_name='shipment')
    op.drop_index(op.f('ix_shipment_company_id'), table_name='shipment')
    op.drop_table('shipment')
    op.drop_index(op.f('ix_sales_order_line_order_id'), table_name='sales_order_line')
    op.drop_index(op.f('ix_sales_order_line_item_id'), table_name='sales_order_line')
    op.drop_index(op.f('ix_sales_order_line_company_id'), table_name='sales_order_line')
    op.drop_table('sales_order_line')
    op.drop_index(op.f('ix_rfq_response_line_rfq_line_id'), table_name='rfq_response_line')
    op.drop_index(op.f('ix_rfq_response_line_response_id'), table_name='rfq_response_line')
    op.drop_index(op.f('ix_rfq_response_line_company_id'), table_name='rfq_response_line')
    op.drop_table('rfq_response_line')
    op.drop_index(op.f('ix_purchase_order_line_rfq_line_id'), table_name='purchase_order_line')
    op.drop_index(op.f('ix_purchase_order_line_requisition_line_id'), table_name='purchase_order_line')
    op.drop_index(op.f('ix_purchase_order_line_order_id'), table_name='purchase_order_line')
    op.drop_index(op.f('ix_purchase_order_line_item_id'), table_name='purchase_order_line')
    op.drop_index(op.f('ix_purchase_order_line_company_id'), table_name='purchase_order_line')
    op.drop_table('purchase_order_line')
    op.drop_index(op.f('ix_pick_list_order_id'), table_name='pick_list')
    op.drop_index(op.f('ix_pick_list_company_id'), table_name='pick_list')
    op.drop_table('pick_list')
    op.drop_index(op.f('ix_job_card_entry_corrects_entry_id'), table_name='job_card_entry')
    op.drop_index(op.f('ix_job_card_entry_company_id'), table_name='job_card_entry')
    op.drop_index(op.f('ix_job_card_entry_card_id'), table_name='job_card_entry')
    op.drop_table('job_card_entry')
    op.drop_index(op.f('ix_goods_receipt_supplier_id'), table_name='goods_receipt')
    op.drop_index(op.f('ix_goods_receipt_order_id'), table_name='goods_receipt')
    op.drop_index(op.f('ix_goods_receipt_location_id'), table_name='goods_receipt')
    op.drop_index(op.f('ix_goods_receipt_company_id'), table_name='goods_receipt')
    op.drop_table('goods_receipt')
    op.drop_index(op.f('ix_credit_decision_order_id'), table_name='credit_decision')
    op.drop_index(op.f('ix_credit_decision_company_id'), table_name='credit_decision')
    op.drop_table('credit_decision')
    op.drop_index(op.f('ix_work_order_issue_work_order_id'), table_name='work_order_issue')
    op.drop_index(op.f('ix_work_order_issue_requirement_id'), table_name='work_order_issue')
    op.drop_index(op.f('ix_work_order_issue_posted_on'), table_name='work_order_issue')
    op.drop_index(op.f('ix_work_order_issue_movement_id'), table_name='work_order_issue')
    op.drop_index(op.f('ix_work_order_issue_location_id'), table_name='work_order_issue')
    op.drop_index(op.f('ix_work_order_issue_item_id'), table_name='work_order_issue')
    op.drop_index(op.f('ix_work_order_issue_company_id'), table_name='work_order_issue')
    op.drop_table('work_order_issue')
    op.drop_index('uq_sales_order_quotation', table_name='sales_order', postgresql_where=sa.text('quotation_id IS NOT NULL'))
    op.drop_index(op.f('ix_sales_order_quotation_id'), table_name='sales_order')
    op.drop_index(op.f('ix_sales_order_customer_id'), table_name='sales_order')
    op.drop_index(op.f('ix_sales_order_company_id'), table_name='sales_order')
    op.drop_table('sales_order')
    op.drop_index(op.f('ix_rfq_supplier_supplier_id'), table_name='rfq_supplier')
    op.drop_index(op.f('ix_rfq_supplier_rfq_id'), table_name='rfq_supplier')
    op.drop_index(op.f('ix_rfq_supplier_company_id'), table_name='rfq_supplier')
    op.drop_table('rfq_supplier')
    op.drop_index(op.f('ix_rfq_response_supplier_id'), table_name='rfq_response')
    op.drop_index(op.f('ix_rfq_response_rfq_id'), table_name='rfq_response')
    op.drop_index(op.f('ix_rfq_response_company_id'), table_name='rfq_response')
    op.drop_table('rfq_response')
    op.drop_index(op.f('ix_rfq_line_rfq_id'), table_name='rfq_line')
    op.drop_index(op.f('ix_rfq_line_requisition_line_id'), table_name='rfq_line')
    op.drop_index(op.f('ix_rfq_line_company_id'), table_name='rfq_line')
    op.drop_table('rfq_line')
    op.drop_index(op.f('ix_quotation_line_quotation_id'), table_name='quotation_line')
    op.drop_index(op.f('ix_quotation_line_item_id'), table_name='quotation_line')
    op.drop_index(op.f('ix_quotation_line_company_id'), table_name='quotation_line')
    op.drop_table('quotation_line')
    op.drop_index(op.f('ix_purchase_order_supplier_id'), table_name='purchase_order')
    op.drop_index(op.f('ix_purchase_order_rfq_id'), table_name='purchase_order')
    op.drop_index(op.f('ix_purchase_order_revision_of_id'), table_name='purchase_order')
    op.drop_index(op.f('ix_purchase_order_requisition_id'), table_name='purchase_order')
    op.drop_index(op.f('ix_purchase_order_company_id'), table_name='purchase_order')
    op.drop_index(op.f('ix_purchase_order_approval_request_id'), table_name='purchase_order')
    op.drop_table('purchase_order')
    op.drop_index(op.f('ix_payroll_line_input_line_id'), table_name='payroll_line_input')
    op.drop_index(op.f('ix_payroll_line_input_employee_id'), table_name='payroll_line_input')
    op.drop_index(op.f('ix_payroll_line_input_company_id'), table_name='payroll_line_input')
    op.drop_table('payroll_line_input')
    op.drop_index(op.f('ix_payroll_line_component_line_id'), table_name='payroll_line_component')
    op.drop_index(op.f('ix_payroll_line_component_company_id'), table_name='payroll_line_component')
    op.drop_table('payroll_line_component')
    op.drop_index(op.f('ix_job_card_work_order_id'), table_name='job_card')
    op.drop_index(op.f('ix_job_card_operation_id'), table_name='job_card')
    op.drop_index(op.f('ix_job_card_opened_on'), table_name='job_card')
    op.drop_index(op.f('ix_job_card_company_id'), table_name='job_card')
    op.drop_table('job_card')
    op.drop_index(op.f('ix_work_order_requirement_work_order_id'), table_name='work_order_requirement')
    op.drop_index(op.f('ix_work_order_requirement_item_id'), table_name='work_order_requirement')
    op.drop_index(op.f('ix_work_order_requirement_company_id'), table_name='work_order_requirement')
    op.drop_table('work_order_requirement')
    op.drop_index(op.f('ix_work_order_receipt_work_order_id'), table_name='work_order_receipt')
    op.drop_index(op.f('ix_work_order_receipt_posted_on'), table_name='work_order_receipt')
    op.drop_index(op.f('ix_work_order_receipt_movement_id'), table_name='work_order_receipt')
    op.drop_index(op.f('ix_work_order_receipt_location_id'), table_name='work_order_receipt')
    op.drop_index(op.f('ix_work_order_receipt_item_id'), table_name='work_order_receipt')
    op.drop_index(op.f('ix_work_order_receipt_company_id'), table_name='work_order_receipt')
    op.drop_table('work_order_receipt')
    op.drop_index(op.f('ix_work_order_operation_work_order_id'), table_name='work_order_operation')
    op.drop_index(op.f('ix_work_order_operation_work_center_code'), table_name='work_order_operation')
    op.drop_index(op.f('ix_work_order_operation_company_id'), table_name='work_order_operation')
    op.drop_table('work_order_operation')
    op.drop_index(op.f('ix_work_order_cost_work_order_id'), table_name='work_order_cost')
    op.drop_index(op.f('ix_work_order_cost_entry_id'), table_name='work_order_cost')
    op.drop_index(op.f('ix_work_order_cost_company_id'), table_name='work_order_cost')
    op.drop_table('work_order_cost')
    op.drop_index(op.f('ix_rfq_requisition_id'), table_name='rfq')
    op.drop_index(op.f('ix_rfq_company_id'), table_name='rfq')
    op.drop_table('rfq')
    op.drop_index(op.f('ix_recurring_template_line_template_id'), table_name='recurring_template_line')
    op.drop_index(op.f('ix_recurring_template_line_item_id'), table_name='recurring_template_line')
    op.drop_index(op.f('ix_recurring_template_line_company_id'), table_name='recurring_template_line')
    op.drop_table('recurring_template_line')
    op.drop_index('uq_quotation_opportunity', table_name='quotation', postgresql_where=sa.text('opportunity_id IS NOT NULL'))
    op.drop_index(op.f('ix_quotation_customer_id'), table_name='quotation')
    op.drop_index(op.f('ix_quotation_company_id'), table_name='quotation')
    op.drop_table('quotation')
    op.drop_index(op.f('ix_purchase_requisition_line_requisition_id'), table_name='purchase_requisition_line')
    op.drop_index(op.f('ix_purchase_requisition_line_item_id'), table_name='purchase_requisition_line')
    op.drop_index(op.f('ix_purchase_requisition_line_company_id'), table_name='purchase_requisition_line')
    op.drop_table('purchase_requisition_line')
    op.drop_index(op.f('ix_pos_tender_sale_id'), table_name='pos_tender')
    op.drop_index(op.f('ix_pos_tender_company_id'), table_name='pos_tender')
    op.drop_table('pos_tender')
    op.drop_index(op.f('ix_pos_sale_line_variant_id'), table_name='pos_sale_line')
    op.drop_index(op.f('ix_pos_sale_line_sale_id'), table_name='pos_sale_line')
    op.drop_index(op.f('ix_pos_sale_line_movement_id'), table_name='pos_sale_line')
    op.drop_index(op.f('ix_pos_sale_line_item_id'), table_name='pos_sale_line')
    op.drop_index(op.f('ix_pos_sale_line_company_id'), table_name='pos_sale_line')
    op.drop_table('pos_sale_line')
    op.drop_index(op.f('ix_pos_drawer_movement_terminal'), table_name='pos_drawer_movement')
    op.drop_index(op.f('ix_pos_drawer_movement_shift_id'), table_name='pos_drawer_movement')
    op.drop_index(op.f('ix_pos_drawer_movement_sale_id'), table_name='pos_drawer_movement')
    op.drop_index(op.f('ix_pos_drawer_movement_moved_on'), table_name='pos_drawer_movement')
    op.drop_index(op.f('ix_pos_drawer_movement_company_id'), table_name='pos_drawer_movement')
    op.drop_table('pos_drawer_movement')
    op.drop_index(op.f('ix_payroll_line_run_id'), table_name='payroll_line')
    op.drop_index(op.f('ix_payroll_line_employee_id'), table_name='payroll_line')
    op.drop_index(op.f('ix_payroll_line_company_id'), table_name='payroll_line')
    op.drop_table('payroll_line')
    op.drop_index(op.f('ix_opportunity_move_opportunity_id'), table_name='opportunity_move')
    op.drop_index(op.f('ix_opportunity_move_company_id'), table_name='opportunity_move')
    op.drop_table('opportunity_move')
    op.drop_index(op.f('ix_loan_recovery_period'), table_name='loan_recovery')
    op.drop_index(op.f('ix_loan_recovery_loan_id'), table_name='loan_recovery')
    op.drop_index(op.f('ix_loan_recovery_employee_id'), table_name='loan_recovery')
    op.drop_index(op.f('ix_loan_recovery_company_id'), table_name='loan_recovery')
    op.drop_table('loan_recovery')
    op.drop_index(op.f('ix_leave_request_leave_type_id'), table_name='leave_request')
    op.drop_index(op.f('ix_leave_request_employee_id'), table_name='leave_request')
    op.drop_index(op.f('ix_leave_request_company_id'), table_name='leave_request')
    op.drop_index(op.f('ix_leave_request_approval_request_id'), table_name='leave_request')
    op.drop_table('leave_request')
    op.drop_index(op.f('ix_work_order_number'), table_name='work_order')
    op.drop_index(op.f('ix_work_order_item_id'), table_name='work_order')
    op.drop_index(op.f('ix_work_order_due_on'), table_name='work_order')
    op.drop_index(op.f('ix_work_order_company_id'), table_name='work_order')
    op.drop_index(op.f('ix_work_order_bom_id'), table_name='work_order')
    op.drop_table('work_order')
    op.drop_index(op.f('ix_supplier_tax_identifier_supplier_id'), table_name='supplier_tax_identifier')
    op.drop_index(op.f('ix_supplier_tax_identifier_company_id'), table_name='supplier_tax_identifier')
    op.drop_table('supplier_tax_identifier')
    op.drop_index(op.f('ix_supplier_portal_account_supplier_id'), table_name='supplier_portal_account')
    op.drop_index(op.f('ix_supplier_portal_account_subject'), table_name='supplier_portal_account')
    op.drop_index(op.f('ix_supplier_portal_account_company_id'), table_name='supplier_portal_account')
    op.drop_table('supplier_portal_account')
    op.drop_index('uq_supplier_primary_contact', table_name='supplier_contact', postgresql_where=sa.text('is_primary'))
    op.drop_index(op.f('ix_supplier_contact_supplier_id'), table_name='supplier_contact')
    op.drop_index(op.f('ix_supplier_contact_company_id'), table_name='supplier_contact')
    op.drop_table('supplier_contact')
    op.drop_index('uq_supplier_primary_bank_account', table_name='supplier_bank_account', postgresql_where=sa.text('is_primary'))
    op.drop_index(op.f('ix_supplier_bank_account_supplier_id'), table_name='supplier_bank_account')
    op.drop_index(op.f('ix_supplier_bank_account_company_id'), table_name='supplier_bank_account')
    op.drop_table('supplier_bank_account')
    op.drop_index(op.f('ix_stock_ledger_entry_variant_id'), table_name='stock_ledger_entry')
    op.drop_index(op.f('ix_stock_ledger_entry_source_type'), table_name='stock_ledger_entry')
    op.drop_index(op.f('ix_stock_ledger_entry_source_id'), table_name='stock_ledger_entry')
    op.drop_index(op.f('ix_stock_ledger_entry_serial_id'), table_name='stock_ledger_entry')
    op.drop_index(op.f('ix_stock_ledger_entry_posting_date'), table_name='stock_ledger_entry')
    op.drop_index(op.f('ix_stock_ledger_entry_location_id'), table_name='stock_ledger_entry')
    op.drop_index(op.f('ix_stock_ledger_entry_item_id'), table_name='stock_ledger_entry')
    op.drop_index(op.f('ix_stock_ledger_entry_company_id'), table_name='stock_ledger_entry')
    op.drop_index(op.f('ix_stock_ledger_entry_batch_id'), table_name='stock_ledger_entry')
    op.drop_table('stock_ledger_entry')
    op.drop_index(op.f('ix_routing_operation_work_center_code'), table_name='routing_operation')
    op.drop_index(op.f('ix_routing_operation_company_id'), table_name='routing_operation')
    op.drop_index(op.f('ix_routing_operation_bom_id'), table_name='routing_operation')
    op.drop_table('routing_operation')
    op.drop_index(op.f('ix_roster_override_employee_id'), table_name='roster_override')
    op.drop_index(op.f('ix_roster_override_company_id'), table_name='roster_override')
    op.drop_table('roster_override')
    op.drop_index(op.f('ix_roster_entry_employee_id'), table_name='roster_entry')
    op.drop_index(op.f('ix_roster_entry_company_id'), table_name='roster_entry')
    op.drop_table('roster_entry')
    op.drop_index(op.f('ix_recurring_template_customer_id'), table_name='recurring_template')
    op.drop_index(op.f('ix_recurring_template_company_id'), table_name='recurring_template')
    op.drop_table('recurring_template')
    op.drop_index(op.f('ix_purchase_requisition_revision_of_id'), table_name='purchase_requisition')
    op.drop_index(op.f('ix_purchase_requisition_company_id'), table_name='purchase_requisition')
    op.drop_index(op.f('ix_purchase_requisition_approval_request_id'), table_name='purchase_requisition')
    op.drop_table('purchase_requisition')
    op.drop_index(op.f('ix_pos_sale_sold_on'), table_name='pos_sale')
    op.drop_index(op.f('ix_pos_sale_shift_id'), table_name='pos_sale')
    op.drop_index(op.f('ix_pos_sale_reversal_entry_id'), table_name='pos_sale')
    op.drop_index(op.f('ix_pos_sale_location_id'), table_name='pos_sale')
    op.drop_index(op.f('ix_pos_sale_journal_entry_id'), table_name='pos_sale')
    op.drop_index(op.f('ix_pos_sale_customer_id'), table_name='pos_sale')
    op.drop_index(op.f('ix_pos_sale_company_id'), table_name='pos_sale')
    op.drop_table('pos_sale')
    op.drop_index(op.f('ix_physical_count_line_item_id'), table_name='physical_count_line')
    op.drop_index(op.f('ix_physical_count_line_count_id'), table_name='physical_count_line')
    op.drop_table('physical_count_line')
    op.drop_index(op.f('ix_payment_batch_company_id'), table_name='payment_batch')
    op.drop_index(op.f('ix_payment_batch_approval_request_id'), table_name='payment_batch')
    op.drop_table('payment_batch')
    op.drop_index(op.f('ix_parked_punch_device_user_id'), table_name='parked_punch')
    op.drop_index(op.f('ix_parked_punch_device_id'), table_name='parked_punch')
    op.drop_index(op.f('ix_parked_punch_company_id'), table_name='parked_punch')
    op.drop_table('parked_punch')
    op.drop_index(op.f('ix_org_placement_employee_id'), table_name='org_placement')
    op.drop_index(op.f('ix_org_placement_company_id'), table_name='org_placement')
    op.drop_table('org_placement')
    op.drop_index(op.f('ix_opportunity_stage_id'), table_name='opportunity')
    op.drop_index(op.f('ix_opportunity_customer_id'), table_name='opportunity')
    op.drop_index(op.f('ix_opportunity_company_id'), table_name='opportunity')
    op.drop_table('opportunity')
    op.drop_index(op.f('ix_mrp_suggestion_run_id'), table_name='mrp_suggestion')
    op.drop_index(op.f('ix_mrp_suggestion_requirement_id'), table_name='mrp_suggestion')
    op.drop_index(op.f('ix_mrp_suggestion_needed_by'), table_name='mrp_suggestion')
    op.drop_index(op.f('ix_mrp_suggestion_item_id'), table_name='mrp_suggestion')
    op.drop_index(op.f('ix_mrp_suggestion_company_id'), table_name='mrp_suggestion')
    op.drop_table('mrp_suggestion')
    op.drop_index('uq_leave_entry_period', table_name='leave_entry', postgresql_where=sa.text('period IS NOT NULL'))
    op.drop_index(op.f('ix_leave_entry_leave_type_id'), table_name='leave_entry')
    op.drop_index(op.f('ix_leave_entry_employee_id'), table_name='leave_entry')
    op.drop_index(op.f('ix_leave_entry_company_id'), table_name='leave_entry')
    op.drop_table('leave_entry')
    op.drop_index(op.f('ix_item_barcode_variant_id'), table_name='item_barcode')
    op.drop_index(op.f('ix_item_barcode_item_id'), table_name='item_barcode')
    op.drop_index(op.f('ix_item_barcode_company_id'), table_name='item_barcode')
    op.drop_table('item_barcode')
    op.drop_index(op.f('ix_employment_contract_employee_id'), table_name='employment_contract')
    op.drop_index(op.f('ix_employment_contract_company_id'), table_name='employment_contract')
    op.drop_table('employment_contract')
    op.drop_index('uq_employee_movement_joining', table_name='employee_movement', postgresql_where=sa.text("kind = 'joining'"))
    op.drop_index('uq_employee_movement_exit', table_name='employee_movement', postgresql_where=sa.text("kind = 'exit'"))
    op.drop_index(op.f('ix_employee_movement_employee_id'), table_name='employee_movement')
    op.drop_index(op.f('ix_employee_movement_company_id'), table_name='employee_movement')
    op.drop_table('employee_movement')
    op.drop_index(op.f('ix_employee_loan_employee_id'), table_name='employee_loan')
    op.drop_index(op.f('ix_employee_loan_company_id'), table_name='employee_loan')
    op.drop_table('employee_loan')
    op.drop_index(op.f('ix_employee_bank_account_employee_id'), table_name='employee_bank_account')
    op.drop_index(op.f('ix_employee_bank_account_company_id'), table_name='employee_bank_account')
    op.drop_table('employee_bank_account')
    op.drop_index(op.f('ix_employee_asset_employee_id'), table_name='employee_asset')
    op.drop_index(op.f('ix_employee_asset_company_id'), table_name='employee_asset')
    op.drop_table('employee_asset')
    op.drop_index('uq_customer_primary_contact', table_name='customer_contact', postgresql_where=sa.text('is_primary'))
    op.drop_index(op.f('ix_customer_contact_customer_id'), table_name='customer_contact')
    op.drop_index(op.f('ix_customer_contact_company_id'), table_name='customer_contact')
    op.drop_table('customer_contact')
    op.drop_index('uq_customer_primary_address_per_kind', table_name='customer_address', postgresql_where=sa.text('is_primary'))
    op.drop_index(op.f('ix_customer_address_customer_id'), table_name='customer_address')
    op.drop_index(op.f('ix_customer_address_company_id'), table_name='customer_address')
    op.drop_table('customer_address')
    op.drop_index(op.f('ix_bom_line_operation_id'), table_name='bom_line')
    op.drop_index(op.f('ix_bom_line_item_id'), table_name='bom_line')
    op.drop_index(op.f('ix_bom_line_company_id'), table_name='bom_line')
    op.drop_index(op.f('ix_bom_line_bom_id'), table_name='bom_line')
    op.drop_table('bom_line')
    op.drop_index(op.f('ix_biometric_device_user_employee_id'), table_name='biometric_device_user')
    op.drop_index(op.f('ix_biometric_device_user_device_id'), table_name='biometric_device_user')
    op.drop_index(op.f('ix_biometric_device_user_company_id'), table_name='biometric_device_user')
    op.drop_table('biometric_device_user')
    op.drop_index(op.f('ix_attendance_event_employee_id'), table_name='attendance_event')
    op.drop_index(op.f('ix_attendance_event_company_id'), table_name='attendance_event')
    op.drop_table('attendance_event')
    op.drop_index(op.f('ix_approval_decision_request_id'), table_name='approval_decision')
    op.drop_table('approval_decision')
    op.drop_index(op.f('ix_advanced_plan_row_requirement_id'), table_name='advanced_plan_row')
    op.drop_index(op.f('ix_advanced_plan_row_planned_release_on'), table_name='advanced_plan_row')
    op.drop_index(op.f('ix_advanced_plan_row_plan_id'), table_name='advanced_plan_row')
    op.drop_index(op.f('ix_advanced_plan_row_item_id'), table_name='advanced_plan_row')
    op.drop_index(op.f('ix_advanced_plan_row_company_id'), table_name='advanced_plan_row')
    op.drop_index(op.f('ix_advanced_plan_row_basic_bucket_start'), table_name='advanced_plan_row')
    op.drop_table('advanced_plan_row')
    op.drop_index(op.f('ix_work_center_rate_work_center_id'), table_name='work_center_rate')
    op.drop_index(op.f('ix_work_center_rate_company_id'), table_name='work_center_rate')
    op.drop_table('work_center_rate')
    op.drop_index(op.f('ix_uom_conversion_item_id'), table_name='uom_conversion')
    op.drop_index(op.f('ix_uom_conversion_company_id'), table_name='uom_conversion')
    op.drop_table('uom_conversion')
    op.drop_index(op.f('ix_supplier_party_id'), table_name='supplier')
    op.drop_index(op.f('ix_supplier_company_id'), table_name='supplier')
    op.drop_table('supplier')
    op.drop_index(op.f('ix_serial_location_id'), table_name='serial')
    op.drop_index(op.f('ix_serial_item_id'), table_name='serial')
    op.drop_index(op.f('ix_serial_company_id'), table_name='serial')
    op.drop_table('serial')
    op.drop_index(op.f('ix_role_assignment_subject'), table_name='role_assignment')
    op.drop_index(op.f('ix_role_assignment_company_id'), table_name='role_assignment')
    op.drop_table('role_assignment')
    op.drop_index(op.f('ix_report_run_definition_id'), table_name='report_run')
    op.drop_index(op.f('ix_report_run_company_id'), table_name='report_run')
    op.drop_table('report_run')
    op.drop_index(op.f('ix_price_rule_item_id'), table_name='price_rule')
    op.drop_index(op.f('ix_price_rule_company_id'), table_name='price_rule')
    op.drop_table('price_rule')
    op.drop_index(op.f('ix_pos_sync_report_terminal'), table_name='pos_sync_report')
    op.drop_index(op.f('ix_pos_sync_report_company_id'), table_name='pos_sync_report')
    op.drop_table('pos_sync_report')
    op.drop_index(op.f('ix_physical_count_location_id'), table_name='physical_count')
    op.drop_index(op.f('ix_physical_count_company_id'), table_name='physical_count')
    op.drop_table('physical_count')
    op.drop_index(op.f('ix_permission_role_id'), table_name='permission')
    op.drop_table('permission')
    op.drop_index(op.f('ix_payroll_run_period'), table_name='payroll_run')
    op.drop_index(op.f('ix_payroll_run_company_id'), table_name='payroll_run')
    op.drop_table('payroll_run')
    op.drop_index(op.f('ix_party_role_party_id'), table_name='party_role')
    op.drop_table('party_role')
    op.drop_index(op.f('ix_mrp_requirement_run_id'), table_name='mrp_requirement')
    op.drop_index(op.f('ix_mrp_requirement_item_id'), table_name='mrp_requirement')
    op.drop_index(op.f('ix_mrp_requirement_company_id'), table_name='mrp_requirement')
    op.drop_index(op.f('ix_mrp_requirement_bucket_start'), table_name='mrp_requirement')
    op.drop_table('mrp_requirement')
    op.drop_index(op.f('ix_journal_line_party_id'), table_name='journal_line')
    op.drop_index(op.f('ix_journal_line_entry_id'), table_name='journal_line')
    op.drop_index(op.f('ix_journal_line_account_id'), table_name='journal_line')
    op.drop_table('journal_line')
    op.drop_index(op.f('ix_item_variant_item_id'), table_name='item_variant')
    op.drop_index(op.f('ix_item_variant_company_id'), table_name='item_variant')
    op.drop_table('item_variant')
    op.drop_index(op.f('ix_field_permission_role_id'), table_name='field_permission')
    op.drop_table('field_permission')
    op.drop_index(op.f('ix_employee_party_id'), table_name='employee')
    op.drop_index(op.f('ix_employee_company_id'), table_name='employee')
    op.drop_table('employee')
    op.drop_index(op.f('ix_delivery_attempt_delivery_id'), table_name='delivery_attempt')
    op.drop_table('delivery_attempt')
    op.drop_index(op.f('ix_customer_party_id'), table_name='customer')
    op.drop_index(op.f('ix_customer_company_id'), table_name='customer')
    op.drop_table('customer')
    op.drop_index(op.f('ix_coupon_redemption_document_id'), table_name='coupon_redemption')
    op.drop_index(op.f('ix_coupon_redemption_coupon_id'), table_name='coupon_redemption')
    op.drop_index(op.f('ix_coupon_redemption_company_id'), table_name='coupon_redemption')
    op.drop_table('coupon_redemption')
    op.drop_index(op.f('ix_bom_item_id'), table_name='bom')
    op.drop_index(op.f('ix_bom_company_id'), table_name='bom')
    op.drop_table('bom')
    op.drop_index(op.f('ix_biometric_pull_device_id'), table_name='biometric_pull')
    op.drop_index(op.f('ix_biometric_pull_company_id'), table_name='biometric_pull')
    op.drop_table('biometric_pull')
    op.drop_index(op.f('ix_batch_item_id'), table_name='batch')
    op.drop_index(op.f('ix_batch_company_id'), table_name='batch')
    op.drop_table('batch')
    op.drop_index(op.f('ix_approval_request_workflow_id'), table_name='approval_request')
    op.drop_index(op.f('ix_approval_request_company_id'), table_name='approval_request')
    op.drop_table('approval_request')
    op.drop_index(op.f('ix_approval_level_workflow_id'), table_name='approval_level')
    op.drop_table('approval_level')
    op.drop_index(op.f('ix_advanced_plan_run_on'), table_name='advanced_plan')
    op.drop_index(op.f('ix_advanced_plan_company_id'), table_name='advanced_plan')
    op.drop_index(op.f('ix_advanced_plan_basic_run_id'), table_name='advanced_plan')
    op.drop_table('advanced_plan')
    op.drop_index(op.f('ix_account_mapping_company_id'), table_name='account_mapping')
    op.drop_index(op.f('ix_account_mapping_account_id'), table_name='account_mapping')
    op.drop_table('account_mapping')
    op.drop_index(op.f('ix_work_center_company_id'), table_name='work_center')
    op.drop_table('work_center')
    op.drop_table('user_totp')
    op.drop_index(op.f('ix_user_session_user_id'), table_name='user_session')
    op.drop_index(op.f('ix_user_session_active_company_id'), table_name='user_session')
    op.drop_table('user_session')
    op.drop_index(op.f('ix_user_recovery_code_user_id'), table_name='user_recovery_code')
    op.drop_table('user_recovery_code')
    op.drop_index(op.f('ix_user_membership_user_id'), table_name='user_membership')
    op.drop_index(op.f('ix_user_membership_member_company_id'), table_name='user_membership')
    op.drop_table('user_membership')
    op.drop_index(op.f('ix_shift_company_id'), table_name='shift')
    op.drop_table('shift')
    op.drop_index(op.f('ix_role_company_id'), table_name='role')
    op.drop_table('role')
    op.drop_index(op.f('ix_report_definition_company_id'), table_name='report_definition')
    op.drop_table('report_definition')
    op.drop_index('uq_pos_shift_open_terminal', table_name='pos_shift', postgresql_where=sa.text("status = 'open'"))
    op.drop_index(op.f('ix_pos_shift_terminal'), table_name='pos_shift')
    op.drop_index(op.f('ix_pos_shift_opened_on'), table_name='pos_shift')
    op.drop_index(op.f('ix_pos_shift_company_id'), table_name='pos_shift')
    op.drop_table('pos_shift')
    op.drop_index(op.f('ix_pipeline_stage_company_id'), table_name='pipeline_stage')
    op.drop_table('pipeline_stage')
    op.drop_index(op.f('ix_payroll_component_company_id'), table_name='payroll_component')
    op.drop_table('payroll_component')
    op.drop_index(op.f('ix_party_company_id'), table_name='party')
    op.drop_table('party')
    op.drop_index(op.f('ix_overtime_rule_company_id'), table_name='overtime_rule')
    op.drop_table('overtime_rule')
    op.drop_index(op.f('ix_outbound_delivery_company_id'), table_name='outbound_delivery')
    op.drop_table('outbound_delivery')
    op.drop_index(op.f('ix_mrp_run_run_on'), table_name='mrp_run')
    op.drop_index(op.f('ix_mrp_run_company_id'), table_name='mrp_run')
    op.drop_table('mrp_run')
    op.drop_index(op.f('ix_match_tolerance_company_id'), table_name='match_tolerance')
    op.drop_table('match_tolerance')
    op.drop_index(op.f('ix_location_parent_id'), table_name='location')
    op.drop_index(op.f('ix_location_company_id'), table_name='location')
    op.drop_table('location')
    op.drop_index(op.f('ix_loan_policy_company_id'), table_name='loan_policy')
    op.drop_table('loan_policy')
    op.drop_index(op.f('ix_leave_type_company_id'), table_name='leave_type')
    op.drop_table('leave_type')
    op.drop_index(op.f('ix_journal_entry_source_type'), table_name='journal_entry')
    op.drop_index(op.f('ix_journal_entry_source_id'), table_name='journal_entry')
    op.drop_index(op.f('ix_journal_entry_company_id'), table_name='journal_entry')
    op.drop_table('journal_entry')
    op.drop_index(op.f('ix_item_company_id'), table_name='item')
    op.drop_table('item')
    op.drop_index(op.f('ix_inbound_event_company_id'), table_name='inbound_event')
    op.drop_table('inbound_event')
    op.drop_index('uq_holiday_company_region_day', table_name='holiday', postgresql_nulls_not_distinct=True)
    op.drop_index(op.f('ix_holiday_company_id'), table_name='holiday')
    op.drop_table('holiday')
    op.drop_index(op.f('ix_fx_rate_company_id'), table_name='fx_rate')
    op.drop_table('fx_rate')
    op.drop_index(op.f('ix_dunning_level_company_id'), table_name='dunning_level')
    op.drop_table('dunning_level')
    op.drop_index(op.f('ix_currency_company_id'), table_name='currency')
    op.drop_table('currency')
    op.drop_index(op.f('ix_coupon_company_id'), table_name='coupon')
    op.drop_table('coupon')
    op.drop_index(op.f('ix_biometric_device_company_id'), table_name='biometric_device')
    op.drop_table('biometric_device')
    op.drop_index(op.f('ix_audit_log_occurred_at'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_entity_id'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_company_id'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index(op.f('ix_approval_workflow_company_id'), table_name='approval_workflow')
    op.drop_table('approval_workflow')
    op.drop_index(op.f('ix_api_idempotency_key_company_id'), table_name='api_idempotency_key')
    op.drop_table('api_idempotency_key')
    op.drop_index(op.f('ix_accounting_period_company_id'), table_name='accounting_period')
    op.drop_table('accounting_period')
    op.drop_index(op.f('ix_account_parent_id'), table_name='account')
    op.drop_index(op.f('ix_account_company_id'), table_name='account')
    op.drop_table('account')
    op.drop_index('uq_job_run_once', table_name='job_run', postgresql_where=sa.text("status = 'succeeded'"))
    op.drop_index('ix_job_run_job_started', table_name='job_run')
    op.drop_table('job_run')
    op.drop_table('company')
    op.drop_table('app_user')
