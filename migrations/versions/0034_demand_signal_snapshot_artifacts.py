"""需求信号持久化不可变网页快照引用。

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "demand_signals",
        sa.Column("snapshot_artifact_ref", sa.String(40), nullable=True),
    )
    op.drop_constraint(
        "ck_demand_signals_web_evidence",
        "demand_signals",
        type_="check",
    )
    op.drop_constraint(
        "ck_demand_signals_optional_nonblank",
        "demand_signals",
        type_="check",
    )
    op.execute(
        "UPDATE demand_signals AS signal "
        "SET snapshot_artifact_ref = artifact.artifact_id "
        "FROM raw_artifacts AS artifact "
        "WHERE signal.source_type = 'web_page' "
        "AND signal.snapshot_artifact_ref IS NULL "
        "AND artifact.tenant_id = signal.tenant_id "
        "AND artifact.kind = 'web_snapshot' "
        "AND artifact.content_hash = signal.page_hash"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM demand_signals "
        "WHERE source_type = 'web_page' AND snapshot_artifact_ref IS NULL) "
        "THEN RAISE EXCEPTION "
        "'web demand signal missing immutable snapshot artifact' "
        "USING ERRCODE = '23514'; END IF; END $$"
    )
    op.create_check_constraint(
        "ck_demand_signals_web_evidence",
        "demand_signals",
        "source_type <> 'web_page' OR "
        "(source_url IS NOT NULL AND btrim(source_url) <> '' AND "
        "page_hash IS NOT NULL AND btrim(page_hash) <> '' AND "
        "source_id = page_hash AND "
        "snapshot_artifact_ref IS NOT NULL AND "
        "snapshot_artifact_ref ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$')",
    )
    op.create_check_constraint(
        "ck_demand_signals_optional_nonblank",
        "demand_signals",
        "(possible_need IS NULL OR btrim(possible_need) <> '') AND "
        "(source_url IS NULL OR btrim(source_url) <> '') AND "
        "(page_hash IS NULL OR btrim(page_hash) <> '') AND "
        "(source_type = 'web_page' OR snapshot_artifact_ref IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_demand_signals_web_evidence",
        "demand_signals",
        type_="check",
    )
    op.drop_constraint(
        "ck_demand_signals_optional_nonblank",
        "demand_signals",
        type_="check",
    )
    op.drop_column("demand_signals", "snapshot_artifact_ref")
    op.create_check_constraint(
        "ck_demand_signals_web_evidence",
        "demand_signals",
        "source_type <> 'web_page' OR "
        "(source_url IS NOT NULL AND btrim(source_url) <> '' AND "
        "page_hash IS NOT NULL AND btrim(page_hash) <> '' AND "
        "source_id = page_hash)",
    )
    op.create_check_constraint(
        "ck_demand_signals_optional_nonblank",
        "demand_signals",
        "(possible_need IS NULL OR btrim(possible_need) <> '') AND "
        "(source_url IS NULL OR btrim(source_url) <> '') AND "
        "(page_hash IS NULL OR btrim(page_hash) <> '')",
    )
