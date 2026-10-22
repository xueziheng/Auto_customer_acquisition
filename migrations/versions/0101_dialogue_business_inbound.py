"""本人业务原件的精确线程元数据与普通对话入站收据。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0101'
down_revision = '0100'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('business_mail_messages', sa.Column('in_reply_to', sa.String(256)))
    op.add_column('business_mail_messages', sa.Column('reply_recipient', sa.String(254)))
    op.add_column('business_mail_messages', sa.Column('dialogue_feedback', postgresql.JSONB()))
    op.create_table('dialogue_business_receipts',
        sa.Column('tenant_id', sa.String(40), nullable=False),
        sa.Column('business_message_id', sa.String(40), nullable=False),
        sa.Column('binding_id', sa.String(40)),
        sa.Column('cold_attempt_id', sa.String(40)),
        sa.Column('canonical_message_id', sa.String(40)),
        sa.Column('result', sa.String(40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('tenant_id', 'business_message_id'),
        sa.ForeignKeyConstraint(['tenant_id','business_message_id'], ['business_mail_messages.tenant_id','business_mail_messages.message_id']),
        sa.ForeignKeyConstraint(['tenant_id','binding_id'], ['dialogue_bindings.tenant_id','dialogue_bindings.binding_id']),
        sa.ForeignKeyConstraint(['tenant_id','cold_attempt_id'], ['outreach_message_attempts.tenant_id','outreach_message_attempts.attempt_id']),
        sa.CheckConstraint('(binding_id IS NULL) <> (cold_attempt_id IS NULL)', name='ck_dialogue_business_source'),
        sa.ForeignKeyConstraint(['tenant_id','canonical_message_id'], ['messages.tenant_id','messages.message_id']),
        sa.CheckConstraint("result IN ('received','hard_bounce','soft_bounce','complaint','rejected')", name='ck_dialogue_business_result'))
    op.execute('ALTER TABLE dialogue_business_receipts ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE dialogue_business_receipts FORCE ROW LEVEL SECURITY')
    predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
    for policy, mode in (("tradeos_tenant_allow", "PERMISSIVE"), ("tradeos_tenant_guard", "RESTRICTIVE")):
        op.execute(f"CREATE POLICY {policy} ON public.dialogue_business_receipts AS {mode} FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})")
    op.execute("DO $block$ DECLARE account record; BEGIN FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$' LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.dialogue_business_receipts TO %I', account.rolname); END LOOP; END $block$")
    op.execute("CREATE FUNCTION protect_dialogue_business_headers() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (NEW.in_reply_to, NEW.reply_recipient, NEW.dialogue_feedback) IS DISTINCT FROM (OLD.in_reply_to, OLD.reply_recipient, OLD.dialogue_feedback) THEN RAISE EXCEPTION 'immutable dialogue source'; END IF; RETURN NEW; END $$")
    op.execute('CREATE TRIGGER dialogue_business_headers_immutable BEFORE UPDATE ON business_mail_messages FOR EACH ROW EXECUTE FUNCTION protect_dialogue_business_headers()')


def downgrade() -> None:
    op.execute('DROP TRIGGER dialogue_business_headers_immutable ON business_mail_messages')
    op.execute('DROP FUNCTION protect_dialogue_business_headers()')
    op.drop_table('dialogue_business_receipts')
    op.drop_column('business_mail_messages','dialogue_feedback')
    op.drop_column('business_mail_messages','reply_recipient')
    op.drop_column('business_mail_messages','in_reply_to')
