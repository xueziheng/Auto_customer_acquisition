"""公共候选库存、公司独立分配和租约任务（ADR0091）。"""

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None
_TABLES = (
    "public_candidates",
    "inventory_quotas",
    "inventory_requests",
    "candidate_allocations",
    "candidate_allocation_emails",
    "acquisition_jobs",
    "acquisition_attempts",
)
_PREDICATE = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"


def upgrade() -> None:
    """固定schema创建并强制固定企业角色RLS。"""
    op.execute(
        "\nCREATE TABLE public_candidates (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tcandidate_id VARCHAR(40) NOT NULL, \n\tcanonical_domain VARCHAR(253) NOT NULL, \n\tpayload JSONB NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (tenant_id, candidate_id), \n\tCONSTRAINT uq_public_candidate_domain UNIQUE (tenant_id, canonical_domain)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE inventory_quotas (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tdaily_limit INTEGER NOT NULL, \n\tquota_day DATE NOT NULL, \n\tused_today INTEGER NOT NULL, \n\tPRIMARY KEY (tenant_id), \n\tCONSTRAINT ck_inventory_quota_nonnegative CHECK (daily_limit >= 0 AND used_today >= 0)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE inventory_requests (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tkey_hash VARCHAR(64) NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tjob_id VARCHAR(40), \n\tPRIMARY KEY (tenant_id, key_hash)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE candidate_allocations (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tallocation_id VARCHAR(40) NOT NULL, \n\tcanonical_domain VARCHAR(253) NOT NULL, \n\temployee_id VARCHAR(40) NOT NULL, \n\tkey_hash VARCHAR(64) NOT NULL, \n\tallocated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tpayload JSONB NOT NULL, \n\tPRIMARY KEY (tenant_id, allocation_id), \n\tCONSTRAINT uq_candidate_allocation_domain UNIQUE (tenant_id, canonical_domain), \n\tCONSTRAINT fk_allocation_request FOREIGN KEY(tenant_id, key_hash) REFERENCES inventory_requests (tenant_id, key_hash)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE candidate_allocation_emails (\n\ttenant_id VARCHAR(40) NOT NULL, \n\temail VARCHAR(320) NOT NULL, \n\tallocation_id VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (tenant_id, email), \n\tCONSTRAINT fk_allocation_email FOREIGN KEY(tenant_id, allocation_id) REFERENCES candidate_allocations (tenant_id, allocation_id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE acquisition_jobs (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tjob_id VARCHAR(40) NOT NULL, \n\tkey_hash VARCHAR(64) NOT NULL, \n\temployee_id VARCHAR(40) NOT NULL, \n\tquery JSONB NOT NULL, \n\trequested_count INTEGER NOT NULL, \n\tremaining_count INTEGER NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tlease_token VARCHAR(40), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfailure_reason VARCHAR(30), \n\tPRIMARY KEY (tenant_id, job_id), \n\tCONSTRAINT fk_acquisition_request FOREIGN KEY(tenant_id, key_hash) REFERENCES inventory_requests (tenant_id, key_hash), \n\tCONSTRAINT uq_acquisition_request UNIQUE (tenant_id, key_hash), \n\tCONSTRAINT ck_acquisition_status CHECK (status IN ('queued','processing','complete','failed')), \n\tCONSTRAINT ck_acquisition_counts CHECK (remaining_count >= 0 AND requested_count >= remaining_count AND attempts >= 0)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE acquisition_attempts (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tattempt_id VARCHAR(40) NOT NULL, \n\tjob_id VARCHAR(40) NOT NULL, \n\tsource_id VARCHAR(80) NOT NULL, \n\toutcome VARCHAR(30) NOT NULL, \n\tvalid_new_count INTEGER NOT NULL, \n\tattempted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (tenant_id, attempt_id), \n\tCONSTRAINT fk_acquisition_attempt_job FOREIGN KEY(tenant_id, job_id) REFERENCES acquisition_jobs (tenant_id, job_id)\n)\n\n"
    )
    for name in _TABLES:
        op.execute(f"ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{name} FORCE ROW LEVEL SECURITY")
        for policy, mode in (
            ("tradeos_tenant_allow", "PERMISSIVE"),
            ("tradeos_tenant_guard", "RESTRICTIVE"),
        ):
            op.execute(
                f"CREATE POLICY {policy} ON public.{name} AS {mode} FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
            )
    tables = ", ".join("public." + name for name in _TABLES)
    op.execute(
        "DO $block$ DECLARE account record; BEGIN FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$' LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        + tables
        + " TO %I', account.rolname); END LOOP; END $block$"
    )


def downgrade() -> None:
    """逆依赖删除全部新增表。"""
    for name in reversed(_TABLES):
        op.drop_table(name)
