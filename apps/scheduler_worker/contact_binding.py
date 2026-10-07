"""联系人组合仅借用本runtime资源，连接池与凭证指纹仍由runtime拥有。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.schemas.identifiers import TenantId, UserId
from tool_gateway.fingerprint import HmacFingerprintProvider


@dataclass(frozen=True)
class ContactRuntimeResources:
    """在canonical Outreach形成后传入；不得构造另一组业务服务。"""

    sessions: async_sessionmaker[AsyncSession]
    tenant_id: TenantId
    tool_user: UserId
    fingerprints: HmacFingerprintProvider
    lease_duration: timedelta
    now: Callable[[], datetime]
