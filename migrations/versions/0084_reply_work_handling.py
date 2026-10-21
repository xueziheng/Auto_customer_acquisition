"""追加人员处理记录；保留原工作项和历史未知信息。"""
import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None
TABLE = "conversation_reply_work_updates"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("action_id", sa.String(32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("expected_status", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column("note", sa.String(2000), nullable=False),
        sa.Column("handled_by", sa.String(40), nullable=False),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "action_id", "sequence", name="pk_reply_work_updates"),
        sa.UniqueConstraint("tenant_id", "action_id", "request_id", name="uq_reply_work_update_request"),
        sa.ForeignKeyConstraint(["tenant_id", "action_id"],
            ["conversation_reply_work.tenant_id", "conversation_reply_work.action_id"],
            ondelete="RESTRICT", name="fk_reply_work_update_action"),
        sa.CheckConstraint("sequence >= 1", name="ck_reply_work_update_sequence"),
        sa.CheckConstraint(
            "expected_status IN ('pending','in_progress') AND "
            "((disposition = 'started' AND expected_status = 'pending' AND status = 'in_progress' AND note = '') OR "
            "(disposition = 'waiting_customer' AND status = 'in_progress' AND length(trim(note)) > 0) OR "
            "(disposition = 'handled' AND status = 'completed' AND length(trim(note)) > 0) OR "
            "(disposition = 'cancelled' AND status = 'cancelled' AND length(trim(note)) > 0))",
            name="ck_reply_work_update_result",
        ),
    )
    predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    for name, kind in (("guard", "RESTRICTIVE"), ("allow", "PERMISSIVE")):
        op.execute(f"CREATE POLICY tradeos_tenant_{name} ON {TABLE} AS {kind} FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})")
    op.execute("""CREATE FUNCTION reply_work_update_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable reply handling record'; END $$""")
    op.execute(f"CREATE TRIGGER reply_work_update_immutable BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW EXECUTE FUNCTION reply_work_update_immutable()")
    op.execute(f"""DO $$ DECLARE account record; BEGIN
FOR account IN SELECT rolname FROM pg_catalog.pg_roles
 WHERE rolname ~ '^tradeos_t_[0-9a-f]{{48}}$'
LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {TABLE} TO %I', account.rolname);
END LOOP; END $$""")


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {TABLE})")):
        raise RuntimeError("已存在人员处理记录，拒绝丢失历史的回退")
    op.drop_table(TABLE)
    op.execute("DROP FUNCTION reply_work_update_immutable()")
