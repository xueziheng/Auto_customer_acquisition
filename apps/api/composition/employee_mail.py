"""保留 API 公开类型；本人连接器由各进程独立装配。"""
from infra.outbound_mail import EmployeeMailConnectors

__all__ = ["EmployeeMailConnectors"]
