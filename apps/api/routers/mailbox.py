"""本人全邮箱列表、完整线程分页与同步请求；身份来自当前登录会话。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from apps.api.dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from apps.api.identity import RequestIdentity
from domains.conversations.mailbox import (
    MailboxActor,
    MailboxService,
    MailboxView,
    MailMessagePage,
    MailThreadPage,
)

router = APIRouter(prefix="/inbox/mailboxes", tags=["private-mailbox"])
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]


def service(
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> MailboxService:
    if dependencies.mailbox is None:
        raise HTTPException(503, "邮箱服务未配置")
    return dependencies.mailbox


Service = Annotated[MailboxService, Depends(service)]


def actor(identity: RequestIdentity) -> MailboxActor:
    return MailboxActor(
        tenant_id=identity.tenant_id, employee_id=identity.employee.employee_id
    )


@router.get("", response_model=list[MailboxView])
async def list_mailboxes(
    identity: Identity, mailbox: Service, response: Response
) -> list[MailboxView]:
    response.headers["Cache-Control"] = "no-store"
    return await mailbox.mailboxes(actor(identity))


@router.get("/{mailbox_id}/threads", response_model=MailThreadPage)
async def list_threads(
    mailbox_id: str,
    identity: Identity,
    mailbox: Service,
    response: Response,
    search: Annotated[str, Query(max_length=200)] = "",
    label: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MailThreadPage:
    response.headers["Cache-Control"] = "no-store"
    return await mailbox.threads(
        actor(identity),
        mailbox_id,
        search=search,
        label=label,
        offset=offset,
        limit=limit,
    )


@router.get("/{mailbox_id}/threads/{thread_id}", response_model=MailMessagePage)
async def thread_messages(
    mailbox_id: str,
    thread_id: str,
    identity: Identity,
    mailbox: Service,
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MailMessagePage:
    response.headers["Cache-Control"] = "no-store"
    return await mailbox.messages(
        actor(identity), mailbox_id, thread_id, offset=offset, limit=limit
    )


@router.post("/{mailbox_id}/sync", status_code=202)
async def request_sync(
    mailbox_id: str, identity: Identity, mailbox: Service
) -> dict[str, str]:
    await mailbox.request_sync(actor(identity), mailbox_id)
    return {"status": "requested"}
