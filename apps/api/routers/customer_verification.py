"""本人客户单联系点验证；不接受邮箱正文、Provider配置或验证结果。"""

from typing import Annotated

from fastapi import APIRouter, Depends

from domains.prospecting.customer_verification import (
    CustomerVerificationCommand,
    CustomerVerificationResult,
    CustomerVerificationView,
)
from shared.errors import TransientError
from shared.schemas.identifiers import CandidateAllocationId

from ..composition.customer_verification import CustomerVerificationApplication
from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity

router = APIRouter(prefix="/customers", tags=["customers"])
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]
Dependencies = Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)]


def _application(dependencies: ConfiguredApiDependencies) -> CustomerVerificationApplication:
    application = getattr(dependencies, "customer_verification", None)
    if application is None:
        raise TransientError("客户邮箱验证服务尚未配置")
    return application


@router.get("/{allocation_id}/verification", response_model=CustomerVerificationView)
async def read(allocation_id: str, identity: Identity, dependencies: Dependencies) -> CustomerVerificationView:
    return await _application(dependencies).read(identity, CandidateAllocationId(allocation_id))


@router.post("/{allocation_id}/verification", response_model=CustomerVerificationResult)
async def verify(allocation_id: str, body: CustomerVerificationCommand, identity: Identity, dependencies: Dependencies) -> CustomerVerificationResult:
    return await _application(dependencies).verify(identity, CandidateAllocationId(allocation_id), body)
