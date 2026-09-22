"""Gmail独立正文读取：profile锚定、固定bootstrap、history，不推进业务cursor。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from connectors.gmail.client import SecretResolver
from connectors.gmail.inbound_cursor import decode_cursor, encode_cursor
from connectors.gmail.inbound_mime import parse_inbound_message
from connectors.gmail.inbound_transport import (
    GmailInboundApiTransport,
    GmailInboundHttpTransport,
)
from connectors.gmail.transport import (
    GmailApiHttpTransport,
    GmailHttpStatusError,
    GmailMalformedResponse,
    GmailNetworkError,
    GmailResponseTooLarge,
)
from shared.schemas.email_inbound import (
    MIME_BYTES,
    PAGE_BYTES,
    PAGE_ITEMS,
    InboundError,
    InboundRoute,
    ProviderInboundItem,
    ProviderInboundPage,
)
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError


class GmailInboundReader:
    """凭证只在fetch内懒解析；实例只由Gateway handler执行阶段调用。"""

    def __init__(
        self,
        route: InboundRoute,
        transport: GmailInboundHttpTransport,
        resolver: SecretResolver,
        secret_ref: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if isinstance(transport, GmailApiHttpTransport) and not isinstance(
            transport, GmailInboundApiTransport
        ):
            raise InboundError()
        self.route = route
        self._transport = transport
        self._resolver = resolver
        self._secret_ref = secret_ref
        self._now = now

    def __repr__(self) -> str:
        return "GmailInboundReader()"

    async def fetch_inbound_page(
        self, mailbox_alias: str, cursor: str, page_limit: int
    ) -> ProviderInboundPage:
        """每轮最多一页refs，全部交付前保持原history_start。"""
        if (
            mailbox_alias != self.route.mailbox_alias
            or type(page_limit) is not int
            or not 1 <= page_limit <= PAGE_ITEMS
        ):
            raise InboundError()
        state = decode_cursor(cursor, self.route)
        if state.phase == "initial" and state.bootstrap_started_at > self._now():
            raise InboundError()
        try:
            token = self._resolver.resolve(self._secret_ref)
            if not isinstance(token, str) or not token:
                raise InboundError()
            if state.phase == "initial":
                watermark = await self._transport.get_profile_history_id(token=token)
                state = state.model_copy(
                    update={"phase": "bootstrap", "history_start": watermark}
                )
                next_cursor = encode_cursor(state)
                decode_cursor(next_cursor, self.route)
                return ProviderInboundPage(
                    route=self.route,
                    starting_cursor=cursor,
                    next_cursor=next_cursor,
                    items=(),
                )
            if not state.loaded:
                if state.phase == "bootstrap":
                    refs, next_token = await self._transport.list_feedback_messages(
                        token=token,
                        after_epoch=state.after_epoch,
                        page_token=state.page_token,
                    )
                    end_history = state.history_start
                else:
                    assert state.history_start is not None
                    (
                        refs,
                        next_token,
                        end_history,
                    ) = await self._transport.list_feedback_history(
                        token=token,
                        start_history_id=state.history_start,
                        page_token=state.page_token,
                    )
                if len(refs) > 100 or (
                    next_token is not None and next_token == state.page_token
                ):
                    raise InboundError()
                state = state.model_copy(
                    update={
                        "pending": tuple(dict.fromkeys(refs)),
                        "page_token": next_token,
                        "resume_history": end_history,
                        "loaded": True,
                    }
                )
                decode_cursor(encode_cursor(state), self.route)
            items: list[ProviderInboundItem] = []
            used = 0
            pending = state.pending
            while pending and len(items) < page_limit:
                result = await self._transport.get_inbound_message(
                    token=token, message_ref=pending[0], maximum_bytes=MIME_BYTES
                )
                size = len(result.raw_mime or b"")
                if used + size > PAGE_BYTES:
                    break
                items.append(parse_inbound_message(pending[0], result))
                pending = pending[1:]
                used += size
            state = state.model_copy(update={"pending": pending})
            if not pending:
                if state.page_token is None:
                    state = state.model_copy(
                        update={
                            "phase": "history",
                            "history_start": state.resume_history
                            or state.history_start,
                        }
                    )
                state = state.model_copy(
                    update={"loaded": False, "resume_history": None}
                )
            next_cursor = encode_cursor(state)
            decode_cursor(next_cursor, self.route)
            return ProviderInboundPage(
                route=self.route,
                starting_cursor=cursor,
                next_cursor=next_cursor,
                items=tuple(items),
            )
        except GmailHttpStatusError as error:
            if error.status_code == 429:
                raise ToolGatewayError(
                    ToolErrorCategory.RATE_LIMITED,
                    retry_after_seconds=error.feedback_retry_after_seconds,
                ) from None
            if error.status_code >= 500:
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
            raise ToolGatewayError(
                ToolErrorCategory.PROVIDER_AUTH_REQUIRED
                if error.status_code in {401, 403}
                else ToolErrorCategory.PROVIDER_PERMANENT
            ) from None
        except (GmailMalformedResponse, GmailResponseTooLarge):
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT) from None
        except GmailNetworkError:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
        except ToolGatewayError:
            raise
        except Exception:  # noqa: BLE001 resolver及provider错误不暴露任何秘密
            raise InboundError() from None
