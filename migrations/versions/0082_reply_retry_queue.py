"""普通恢复排队不倒计时；保留旧测试期限及其一次续办证据。"""

import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None
TABLE = "reply_classification_resumptions"


def upgrade() -> None:
    op.alter_column("reply_classification_retries", "expires_at", nullable=True)
    op.create_table(
        TABLE,
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("message_id", sa.String(40), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("original_idempotency_key", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("operator_employee_id", sa.String(40), nullable=False),
        sa.Column("operator_user_id", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            [
                "reply_classification_retries.tenant_id",
                "reply_classification_retries.message_id",
            ],
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_reply_resume_key"),
    )
    predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    for name, kind in (("guard", "RESTRICTIVE"), ("allow", "PERMISSIVE")):
        op.execute(
            f"CREATE POLICY tradeos_tenant_{name} ON {TABLE} AS {kind} FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})"
        )
    op.execute("""CREATE FUNCTION reply_resume_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable reply resumption'; END $$""")
    op.execute(
        f"CREATE TRIGGER reply_resume_immutable BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW EXECUTE FUNCTION reply_resume_immutable()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        f"LOCK TABLE reply_classification_retries, {TABLE} IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {TABLE}) OR EXISTS (SELECT 1 FROM reply_classification_retries WHERE expires_at IS NULL)"
        )
    ):
        raise RuntimeError("恢复或续办证据已存在，拒绝丢失语义的回退")
    op.drop_table(TABLE)
    op.execute("DROP FUNCTION reply_resume_immutable()")
    op.alter_column("reply_classification_retries", "expires_at", nullable=False)
