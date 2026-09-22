"""Web 内测认证、摘要会话与固定限流桶；不创建业务数据。"""

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE auth_accounts (
	tenant_id VARCHAR(32) NOT NULL,
	username VARCHAR(64) NOT NULL,
	employee_id VARCHAR(32) NOT NULL,
	password_hash VARCHAR(160) NOT NULL,
	enabled BOOLEAN NOT NULL,
	version INTEGER NOT NULL,
	failed_count INTEGER NOT NULL,
	failure_started_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_auth_accounts PRIMARY KEY (tenant_id, username),
	CONSTRAINT uq_auth_accounts_employee UNIQUE (tenant_id, employee_id),
	CONSTRAINT fk_auth_accounts_employee FOREIGN KEY(tenant_id, employee_id) REFERENCES employees (tenant_id, employee_id) ON DELETE RESTRICT,
	CONSTRAINT ck_auth_accounts_counters CHECK (version >= 1 AND failed_count BETWEEN 0 AND 5),
	CONSTRAINT ck_auth_accounts_username CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{0,63}$')
)

""")
    op.execute("""
CREATE TABLE auth_sessions (
	tenant_id VARCHAR(32) NOT NULL,
	token_digest VARCHAR(64) NOT NULL,
	csrf_digest VARCHAR(64) NOT NULL,
	username VARCHAR(64) NOT NULL,
	user_id VARCHAR(32) NOT NULL,
	account_version INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_auth_sessions PRIMARY KEY (tenant_id, token_digest),
	CONSTRAINT fk_auth_sessions_account FOREIGN KEY(tenant_id, username) REFERENCES auth_accounts (tenant_id, username) ON DELETE RESTRICT,
	CONSTRAINT ck_auth_sessions_digest CHECK (token_digest ~ '^[0-9a-f]{64}$' AND csrf_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_auth_sessions_validity CHECK (account_version >= 1 AND expires_at > created_at)
)

""")
    op.execute(
        """CREATE INDEX ix_auth_sessions_account ON auth_sessions (tenant_id, username, created_at)"""
    )
    op.execute("""
CREATE TABLE auth_rate_limits (
	tenant_id VARCHAR(32) NOT NULL,
	bucket VARCHAR(16) NOT NULL,
	count INTEGER NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_auth_rate_limits PRIMARY KEY (tenant_id, bucket),
	CONSTRAINT ck_auth_rate_limits_bucket CHECK (bucket IN ('attempts', 'unknown') AND count BETWEEN 0 AND 30)
)

""")


def downgrade() -> None:
    op.drop_table("auth_rate_limits")
    op.drop_table("auth_sessions")
    op.drop_table("auth_accounts")
