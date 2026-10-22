"""授权请求与交付准备项同事务落库，复用企业角色强制RLS。"""
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None
_TABLES = ("inventory_followup_requests", "customer_preparation_items")
_PREDICATE = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"


def upgrade() -> None:
    op.execute('\nCREATE TABLE inventory_followup_requests (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tkey_hash VARCHAR(64) NOT NULL, \n\t"authorization" JSONB NOT NULL, \n\trevoked BOOLEAN NOT NULL, \n\tPRIMARY KEY (tenant_id, key_hash), \n\tCONSTRAINT fk_followup_request FOREIGN KEY(tenant_id, key_hash) REFERENCES inventory_requests (tenant_id, key_hash)\n)\n\n')
    op.execute("\nCREATE TABLE customer_preparation_items (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tallocation_id VARCHAR(40) NOT NULL, \n\tkey_hash VARCHAR(64) NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\tstage VARCHAR(30) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tlease_token VARCHAR(40), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\texternal_started BOOLEAN NOT NULL, \n\tblocked_reason VARCHAR(60), \n\tresult_refs JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (tenant_id, allocation_id), \n\tCONSTRAINT fk_preparation_request FOREIGN KEY(tenant_id, key_hash) REFERENCES inventory_followup_requests (tenant_id, key_hash), \n\tCONSTRAINT fk_preparation_allocation FOREIGN KEY(tenant_id, allocation_id) REFERENCES candidate_allocations (tenant_id, allocation_id), \n\tCONSTRAINT ck_preparation_state CHECK (state IN ('queued','processing','blocked','enrolled')), \n\tCONSTRAINT ck_preparation_version CHECK (version >= 0)\n)\n\n")
    for name in _TABLES:
        op.execute(f"ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{name} FORCE ROW LEVEL SECURITY")
        for policy, mode in (("tradeos_tenant_allow", "PERMISSIVE"), ("tradeos_tenant_guard", "RESTRICTIVE")):
            op.execute(f"CREATE POLICY {policy} ON public.{name} AS {mode} FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})")
    tables = ", ".join("public." + name for name in _TABLES)
    op.execute("DO $block$ DECLARE account record; BEGIN FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$' LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE " + tables + " TO %I', account.rolname); END LOOP; END $block$")


def downgrade() -> None:
    for name in reversed(_TABLES):
        op.drop_table(name)
