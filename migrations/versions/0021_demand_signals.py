"""demand_signals 表（DemandSignal capture/discard 最小切片）。

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_SIGNAL_TYPES = (
    "'public_rfq','tender_notice','inbound_inquiry','trade_show_request',"
    "'historical_unclosed_need','supplier_referral','product_line_expansion',"
    "'facility_expansion','new_market_entry','procurement_role_hiring',"
    "'distributor_change','new_certification','large_contract_won',"
    "'funding_or_merger','stockout_observed','negative_product_review',"
    "'supplier_complaint','marketplace_seller_activity','catalog_gap',"
    "'value_chain_adjacency','complementary_category'"
)
_SIGNAL_STATUSES = "'captured','linked_to_hypothesis','discarded'"
_SOURCE_TYPES = (
    "'conversation','web_page','upload','employee_input',"
    "'agent_inference','external_api'"
)


def upgrade() -> None:
    """创建 tenant-bound 信号表：PK(tenant,signal_id) + 来源身份 UNIQUE + 8 CHECK。"""
    op.create_table(
        "demand_signals",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("signal_id", sa.String(32), nullable=False),
        sa.Column("signal_type", sa.String(40), nullable=False),
        sa.Column("entity_name", sa.String(200), nullable=False),
        sa.Column("raw_observation", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default=sa.text("'captured'")
        ),
        sa.Column("possible_need", sa.Text(), nullable=True),
        sa.Column("account_id", sa.String(32), nullable=True),
        sa.Column("discard_reason", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("extracted_by", sa.String(64), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(32), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_url", sa.String(2000), nullable=True),
        sa.Column("page_hash", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "signal_id", name="pk_demand_signals"),
        sa.UniqueConstraint(
            "tenant_id",
            "entity_name",
            "signal_type",
            "source_type",
            "source_id",
            name="uq_demand_signals_source_identity",
        ),
        sa.CheckConstraint(
            f"signal_type IN ({_SIGNAL_TYPES})", name="ck_demand_signals_type"
        ),
        sa.CheckConstraint(
            f"status IN ({_SIGNAL_STATUSES})", name="ck_demand_signals_status"
        ),
        sa.CheckConstraint(
            f"source_type IN ({_SOURCE_TYPES})", name="ck_demand_signals_source_type"
        ),
        sa.CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_demand_signals_confirmed_pair",
        ),
        sa.CheckConstraint(
            "source_type <> 'web_page' OR "
            "(btrim(source_url) <> '' AND btrim(page_hash) <> '' "
            "AND source_id = page_hash)",
            name="ck_demand_signals_web_evidence",
        ),
        sa.CheckConstraint(
            "(status = 'discarded') = "
            "(discard_reason IS NOT NULL AND btrim(discard_reason) <> '')",
            name="ck_demand_signals_discard_reason",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(signal_id) <> '' AND "
            "btrim(entity_name) <> '' AND btrim(raw_observation) <> '' AND "
            "btrim(source_id) <> '' AND btrim(extracted_by) <> ''",
            name="ck_demand_signals_core_nonblank",
        ),
        sa.CheckConstraint(
            "(possible_need IS NULL OR btrim(possible_need) <> '') AND "
            "(source_url IS NULL OR btrim(source_url) <> '') AND "
            "(page_hash IS NULL OR btrim(page_hash) <> '')",
            name="ck_demand_signals_optional_nonblank",
        ),
    )


def downgrade() -> None:
    op.drop_table("demand_signals")
