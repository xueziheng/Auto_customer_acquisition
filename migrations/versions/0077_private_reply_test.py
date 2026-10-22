"""私有回信测试用途标记；只绑定现有客户/联系人/发件身份，不生成需求或验证事实。"""
import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None

_PREDICATE = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"


def _secure_table() -> None:
    """与现有租户角色规则一致；迁移只授权表内被RLS允许的行。"""
    op.execute("ALTER TABLE public.private_reply_tests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.private_reply_tests FORCE ROW LEVEL SECURITY")
    for policy, mode in (
        ("tradeos_tenant_allow", "PERMISSIVE"),
        ("tradeos_tenant_guard", "RESTRICTIVE"),
    ):
        op.execute(f"CREATE POLICY {policy} ON public.private_reply_tests AS {mode} FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})")
    op.execute("DO $block$ DECLARE account record; BEGIN FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$' LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.private_reply_tests TO %I', account.rolname); END LOOP; END $block$")


def upgrade() -> None:
    op.create_table(
        "private_reply_tests",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("contact_point_id", sa.String(40), nullable=False),
        sa.Column("sending_identity_id", sa.String(40), nullable=False),
        sa.Column("owner_id", sa.String(40), nullable=False),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "account_id", name="pk_private_reply_tests"),
        sa.UniqueConstraint("tenant_id", "contact_point_id", name="uq_private_reply_test_contact"),
        sa.ForeignKeyConstraint(["tenant_id", "account_id"], ["prospect_accounts.tenant_id", "prospect_accounts.account_id"], name="fk_private_reply_test_account"),
        sa.ForeignKeyConstraint(["tenant_id", "contact_point_id"], ["contact_points.tenant_id", "contact_points.contact_point_id"], name="fk_private_reply_test_contact"),
        sa.ForeignKeyConstraint(["tenant_id", "sending_identity_id"], ["sending_identities.tenant_id", "sending_identities.identity_id"], name="fk_private_reply_test_sender"),
        sa.ForeignKeyConstraint(["tenant_id", "owner_id"], ["employees.tenant_id", "employees.employee_id"], name="fk_private_reply_test_owner"),
        sa.ForeignKeyConstraint(["tenant_id", "created_by"], ["employees.tenant_id", "employees.employee_id"], name="fk_private_reply_test_creator"),
    )
    _secure_table()


def downgrade() -> None:
    op.execute(sa.text("LOCK TABLE private_reply_tests IN ACCESS EXCLUSIVE MODE"))
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM private_reply_tests)")):
        raise RuntimeError("私有测试用途已使用，不得抹除用途后回滚为正式业务")
    op.drop_table("private_reply_tests")
