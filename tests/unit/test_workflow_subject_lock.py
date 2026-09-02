"""Workflow subject advisory lock 的纯 identity 契约。"""

from infra.db.workflow_subject_lock import workflow_subject_lock_identity
from shared.schemas.identifiers import TenantId


def test_lock_identity_is_unambiguous_across_tuple_boundaries() -> None:
    """旧冒号拼接会碰撞的两个 tuple 必须映射为不同 identity。"""

    first = workflow_subject_lock_identity(
        TenantId("tn_lock:a"),
        "b",
        "c",
    )
    second = workflow_subject_lock_identity(
        TenantId("tn_lock"),
        "a:b",
        "c",
    )

    assert first != second
    assert first == workflow_subject_lock_identity(
        TenantId("tn_lock:a"),
        "b",
        "c",
    )
