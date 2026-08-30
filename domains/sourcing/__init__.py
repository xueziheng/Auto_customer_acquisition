"""寻源域公共入口。"""

from domains.sourcing.service import SourcingService
from domains.sourcing.service_impl import SourcingServiceImpl

__all__ = ("SourcingService", "SourcingServiceImpl")
