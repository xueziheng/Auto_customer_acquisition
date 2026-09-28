"""登录名兼容邮箱；保留旧账号与会话，拒绝会损坏邮箱账号的降级。"""

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None

_LEGACY = "username ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'"
_ATOM = "[a-z0-9!#$%&''*+/=?^_`{|}~-]+"
_LABEL = "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_EMAIL = (
    f"username ~ '^{_ATOM}(\\.{_ATOM})*@{_LABEL}(\\.{_LABEL})+$'"
    " AND length(username) <= 254 AND length(split_part(username, '@', 1)) <= 64"
)


def upgrade() -> None:
    op.drop_constraint("ck_auth_accounts_username", "auth_accounts", type_="check")
    for table in ("auth_accounts", "auth_sessions"):
        op.alter_column(table, "username", type_=sa.String(254), existing_type=sa.String(64))
    op.create_check_constraint(
        "ck_auth_accounts_username", "auth_accounts", f"({_LEGACY}) OR ({_EMAIL})"
    )


def downgrade() -> None:
    # 管理迁移扫描全部租户只用于拒绝数据损失，不读取或返回账号原文。
    incompatible = op.get_bind().scalar(sa.text(
        f"SELECT EXISTS (SELECT 1 FROM auth_accounts WHERE NOT ({_LEGACY}))"
    ))
    if incompatible:
        raise RuntimeError("email_login_accounts_prevent_downgrade")
    op.drop_constraint("ck_auth_accounts_username", "auth_accounts", type_="check")
    for table in ("auth_sessions", "auth_accounts"):
        op.alter_column(table, "username", type_=sa.String(64), existing_type=sa.String(254))
    op.create_check_constraint("ck_auth_accounts_username", "auth_accounts", _LEGACY)
