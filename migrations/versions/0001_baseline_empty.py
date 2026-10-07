"""空基线迁移：锚定 alembic 版本链起点。

Phase 0 只建迁移基建，不引入业务 schema。领域表不在此处创建——每张表
随所属切片（HANDBOOK 切片 2 起）作为独立迁移落地。本迁移为空 no-op，
作用是让版本链从 0001 起步，供后续迁移以它为父版本。

这是有意的边界划分，不是占位：工程基建不提前实现业务域。
"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空操作：Phase 0 不创建业务表（边界见模块 docstring）。"""


def downgrade() -> None:
    """空操作：与 upgrade 对应，无任何 schema 变更可回退。"""
