"""保留原API导出；两个进程复用同一租户事务分配适配器。"""
from infra.db.customer_assignment import CustomerAssignmentBridge, _NoContactHasher

__all__ = ("CustomerAssignmentBridge", "_NoContactHasher")
