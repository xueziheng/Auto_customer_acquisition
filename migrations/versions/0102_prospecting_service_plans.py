"""三档服务计划、保守用量、耐久探索批次与筛选证据。"""

import sqlalchemy as sa
from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

_TABLES = (
    "prospecting_screening_evidence",
    "prospecting_discovery_cycles",
    "prospecting_service_reservations",
    "prospecting_service_plans",
)


def upgrade() -> None:
    """独立表保留历史研究/Campaign解释，不回填未授权计划。"""
    op.create_table(
        "prospecting_service_plans",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("period_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidates_reserved", sa.Integer(), nullable=False),
        sa.Column("searches_reserved", sa.Integer(), nullable=False),
        sa.Column("contacts_reserved", sa.Integer(), nullable=False),
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False),
        sa.Column("target_kind", sa.String(24), nullable=False, server_default="legacy_research"),
        sa.Column("inventory_query", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("employee_id", sa.String(64), nullable=True),
        sa.Column("proposal_id", sa.String(64), nullable=True),
        sa.Column("acting_user_id", sa.String(64), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_cycle_key", sa.String(200), nullable=True),
        sa.Column("assigned_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_prospecting_service_plans"),
        sa.CheckConstraint(
            'length(trim(tenant_id)) > 0 AND length(trim(assigned_by)) > 0',
            name="ck_prospecting_plan_identity",
        ),
        sa.CheckConstraint(
            "tier IN ('basic', 'standard', 'advanced') AND version >= 1",
            name="ck_prospecting_plan_tier",
        ),
        sa.CheckConstraint(
            'period_ends_at > period_started_at',
            name="ck_prospecting_plan_period",
        ),
        sa.CheckConstraint(
            'candidates_reserved >= 0 AND searches_reserved >= 0 AND contacts_reserved >= 0',
            name="ck_prospecting_plan_usage",
        ),
        sa.CheckConstraint(
            "NOT schedule_enabled OR (acting_user_id IS NOT NULL AND next_run_at IS NOT NULL AND ((target_kind = 'legacy_research' AND proposal_id IS NOT NULL) OR (target_kind = 'inventory_request' AND inventory_query IS NOT NULL AND employee_id IS NOT NULL)))",
            name="ck_prospecting_plan_schedule",
        ),
    )
    op.create_table(
        "prospecting_service_reservations",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("reservation_key", sa.String(200), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("execution_usage", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("candidates", sa.Integer(), nullable=False),
        sa.Column("searches", sa.Integer(), nullable=False),
        sa.Column("contacts", sa.Integer(), nullable=False),
        sa.Column("period_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "reservation_key", name="pk_prospecting_service_reservations"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["prospecting_service_plans.tenant_id"],
            name="fk_prospecting_reservation_plan",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            'length(trim(reservation_key)) > 0',
            name="ck_prospecting_reservation_key",
        ),
        sa.CheckConstraint(
            "tier IN ('basic', 'standard', 'advanced')",
            name="ck_prospecting_reservation_tier",
        ),
        sa.CheckConstraint(
            'candidates >= 0 AND searches >= 0 AND contacts >= 0 AND candidates + searches + contacts > 0',
            name="ck_prospecting_reservation_usage",
        ),
    )
    op.create_table(
        "prospecting_discovery_cycles",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("cycle_key", sa.String(200), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("target_kind", sa.String(24), nullable=False, server_default="legacy_research"),
        sa.Column("inventory_query", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("employee_id", sa.String(64), nullable=True),
        sa.Column("request_key_hash", sa.String(64), nullable=True),
        sa.Column("proposal_id", sa.String(64), nullable=True),
        sa.Column("acting_user_id", sa.String(64), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_version", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "cycle_key", name="pk_prospecting_discovery_cycles"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["prospecting_service_plans.tenant_id"],
            name="fk_prospecting_cycle_plan",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "plan_version >= 1 AND length(trim(cycle_key)) > 0 AND length(trim(acting_user_id)) > 0 AND ((target_kind = 'legacy_research' AND proposal_id IS NOT NULL) OR (target_kind = 'inventory_request' AND inventory_query IS NOT NULL AND employee_id IS NOT NULL))",
            name="ck_prospecting_cycle_identity",
        ),
        sa.CheckConstraint(
            "(run_id IS NULL AND request_key_hash IS NULL AND bound_at IS NULL) OR (target_kind = 'legacy_research' AND run_id IS NOT NULL AND request_key_hash IS NULL AND bound_at IS NOT NULL) OR (target_kind = 'inventory_request' AND run_id IS NULL AND request_key_hash IS NOT NULL AND bound_at IS NOT NULL)",
            name="ck_prospecting_cycle_binding",
        ),
        sa.CheckConstraint(
            '(superseded_at IS NULL AND superseded_by_version IS NULL) OR (superseded_at IS NOT NULL AND superseded_by_version IS NOT NULL AND superseded_by_version > plan_version AND start_attempted_at IS NULL AND run_id IS NULL AND request_key_hash IS NULL)',
            name="ck_prospecting_cycle_superseded",
        ),
        sa.CheckConstraint(
            '(run_id IS NULL AND request_key_hash IS NULL) OR start_attempted_at IS NOT NULL',
            name="ck_prospecting_cycle_start",
        ),
    )
    op.create_table(
        "prospecting_screening_evidence",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "account_id",
            "content_hash",
            name="pk_prospecting_screening_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["prospect_accounts.tenant_id", "prospect_accounts.account_id"],
            name="fk_prospecting_screening_account",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            'length(content_hash) = 64',
            name="ck_prospecting_screening_hash",
        ),
    )

    if op.get_bind().dialect.name == "postgresql":
        predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
        for name in _TABLES:
            op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
            for policy, mode in (("tradeos_tenant_allow", "PERMISSIVE"), ("tradeos_tenant_guard", "RESTRICTIVE")):
                op.execute(f"CREATE POLICY {policy} ON public.{name} AS {mode} FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})")
            op.execute(f"DO $block$ DECLARE account record; BEGIN FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{{48}}$' LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{name} TO %I', account.rolname); END LOOP; END $block$")


def downgrade() -> None:
    """仅空表可降级，避免丢失账本或允许旧调用重新消费额度。"""
    connection = op.get_bind()
    # 运维迁移只检查是否存在数据，不读取跨租户业务内容。
    for name in _TABLES:
        table = sa.table(name, sa.column("tenant_id"))
        if connection.scalar(
            sa.select(sa.exists().where(table.c.tenant_id.is_not(None)))
        ):
            raise RuntimeError("prospecting_service_data_prevents_downgrade")
    for name in _TABLES:
        op.drop_table(name)
