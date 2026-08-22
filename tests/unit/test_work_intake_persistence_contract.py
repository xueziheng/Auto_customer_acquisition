"""员工工作上传 ORM 必须保留原件、提取、人工确认三层版本。"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

from infra.db.tables import Base


def test_work_intake_metadata_keeps_three_tenant_bound_layers() -> None:
    expected_columns = {
        "work_uploads": {
            "tenant_id",
            "upload_id",
            "artifact_id",
            "employee_id",
            "source_kind",
            "status",
            "occurred_at",
            "customer_timezone",
            "account_id",
            "opportunity_id",
            "need_id",
            "created_at",
        },
        "extracted_facts": {
            "tenant_id",
            "extraction_id",
            "upload_id",
            "payload",
            "extracted_by",
            "created_at",
        },
        "employee_confirmations": {
            "tenant_id",
            "confirmation_id",
            "extraction_id",
            "revision",
            "payload",
            "confirmed_by",
            "confirmed_at",
        },
    }

    for table_name, columns in expected_columns.items():
        assert table_name in Base.metadata.tables
        assert set(Base.metadata.tables[table_name].columns.keys()) == columns


def test_each_extraction_has_only_one_final_confirmation() -> None:
    table = Base.metadata.tables["employee_confirmations"]
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("tenant_id", "extraction_id") in unique_columns
