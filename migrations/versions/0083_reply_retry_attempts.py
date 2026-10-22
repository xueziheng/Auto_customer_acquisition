"""每次显式恢复独立留痕；既有恢复和续办记录保持原含义。"""

import sqlalchemy as sa
from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None
RETRY = "reply_classification_retries"
RESUME = "reply_classification_resumptions"


def _drop_resume_fk() -> None:
    for key in sa.inspect(op.get_bind()).get_foreign_keys(RESUME):
        if key["referred_table"] == RETRY:
            assert key["name"] is not None
            op.drop_constraint(key["name"], RESUME, type_="foreignkey")


def upgrade() -> None:
    _drop_resume_fk()
    op.add_column(
        RETRY, sa.Column("sequence", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        RESUME,
        sa.Column("retry_sequence", sa.Integer(), nullable=False, server_default="1"),
    )
    primary = sa.inspect(op.get_bind()).get_pk_constraint(RETRY)["name"]
    assert primary is not None
    op.drop_constraint(primary, RETRY, type_="primary")
    op.create_primary_key(primary, RETRY, ["tenant_id", "message_id", "sequence"])
    op.drop_constraint("uq_reply_retry_run", RETRY, type_="unique")
    op.create_unique_constraint(
        "uq_reply_retry_run", RETRY, ["tenant_id", "run_id", "sequence"]
    )
    op.create_check_constraint("ck_reply_retry_sequence", RETRY, "sequence >= 1")
    op.create_foreign_key(
        "fk_reply_resume_attempt",
        RESUME,
        RETRY,
        ["tenant_id", "message_id", "retry_sequence"],
        ["tenant_id", "message_id", "sequence"],
    )
    op.execute("""CREATE FUNCTION reply_retry_sequence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.sequence IS DISTINCT FROM OLD.sequence THEN RAISE EXCEPTION 'immutable retry sequence'; END IF;
 RETURN NEW;
END $$""")
    op.execute(
        f"CREATE TRIGGER reply_retry_sequence_immutable BEFORE UPDATE ON {RETRY} FOR EACH ROW EXECUTE FUNCTION reply_retry_sequence_immutable()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(f"LOCK TABLE {RETRY}, {RESUME} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {RETRY} WHERE sequence <> 1) OR EXISTS (SELECT 1 FROM {RESUME} WHERE retry_sequence <> 1)"
        )
    ):
        raise RuntimeError("多次恢复证据已存在，拒绝丢失记录的回退")
    _drop_resume_fk()
    op.execute(f"DROP TRIGGER reply_retry_sequence_immutable ON {RETRY}")
    op.execute("DROP FUNCTION reply_retry_sequence_immutable()")
    primary = sa.inspect(op.get_bind()).get_pk_constraint(RETRY)["name"]
    assert primary is not None
    op.drop_constraint(primary, RETRY, type_="primary")
    op.create_primary_key(primary, RETRY, ["tenant_id", "message_id"])
    op.drop_constraint("uq_reply_retry_run", RETRY, type_="unique")
    op.create_unique_constraint("uq_reply_retry_run", RETRY, ["tenant_id", "run_id"])
    op.drop_constraint("ck_reply_retry_sequence", RETRY, type_="check")
    op.drop_column(RETRY, "sequence")
    op.drop_column(RESUME, "retry_sequence")
    op.create_foreign_key(
        "fk_reply_resume_attempt",
        RESUME,
        RETRY,
        ["tenant_id", "message_id"],
        ["tenant_id", "message_id"],
    )
