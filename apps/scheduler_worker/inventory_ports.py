"""公开页面端口独立于搜索供应商；调用者管理对象存储生命周期。"""

from dataclasses import dataclass

from artifact_store.transport import BoundedObjectBlobTransport, ObjectBlobTransport
from connectors.web_search.transport import PublicPageTransport


@dataclass(frozen=True)
class InventoryRuntimePorts:
    page_transport: PublicPageTransport
    object_transport: ObjectBlobTransport
    maximum_artifact_bytes: int
    bounded_object_transport: BoundedObjectBlobTransport | None = None
