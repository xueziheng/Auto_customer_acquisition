"""人工恢复的独立单调预算；不触碰原 QQ 试验的停止态和计数。"""

from datetime import timedelta

from sqlalchemy import select

from infra.db.reply_retry import SqlReplyRetryRepository
from infra.db.reply_retry_tables import ReplyRetryRow
from shared.schemas.identifiers import MessageId, TenantId
from shared.schemas.model_invocation import ModelGenerationError


class SqlReplyRetryBudget:
    def __init__(
        self,
        repository: SqlReplyRetryRepository,
        tenant: TenantId,
        message: MessageId,
        *,
        sequence: int = 1,
    ) -> None:
        self.repository, self.tenant, self.message = repository, tenant, message
        self.sequence = sequence

    async def check(self) -> None:
        async with self.repository.factory.begin() as db:
            row = await self.repository.current(
                db, self.tenant, self.message, model=True
            )
            if row.sequence != self.sequence:
                raise ModelGenerationError("permission")
            if (
                row.model_started_at is not None
                and self.repository.now()
                >= row.model_started_at + timedelta(seconds=60)
            ):
                raise ModelGenerationError("unknown")

    async def start(self) -> None:
        async with self.repository.factory.begin() as db:
            row = await self.repository.current(
                db, self.tenant, self.message, lock=True, model=True
            )
            if (
                row.sequence != self.sequence
                or row.classifications != 0
                or row.state != "queued"
            ):
                raise ModelGenerationError("permission")
            row.classifications, row.state, row.model_started_at = (
                1,
                "running",
                self.repository.now(),
            )

    async def reserve_round(self, input_bytes: int) -> int:
        await self.check()
        async with self.repository.factory.begin() as db:
            row = await self.repository.current(
                db, self.tenant, self.message, lock=True, model=True
            )
            if (
                row.sequence != self.sequence
                or type(input_bytes) is not int
                or input_bytes < 0
                or row.classifications != 1
                or row.model_started_at is None
                or self.repository.now() >= row.model_started_at + timedelta(seconds=60)
                or row.provider_pending
                or row.provider_requests >= 4
                or row.input_bytes + input_bytes > 32768
                or row.output_tokens >= 512
            ):
                raise ModelGenerationError("permission")
            row.provider_requests += 1
            row.input_bytes += input_bytes
            row.provider_pending = True
            return 512 - row.output_tokens

    async def finish_round(self, tokens: int, requested: int) -> None:
        # 先保留实际用量，再查撤权/到期；失去权限不能抹去已发生的计费。
        async with self.repository.factory.begin() as db:
            row = await db.scalar(
                select(ReplyRetryRow)
                .where(
                    ReplyRetryRow.tenant_id == self.tenant,
                    ReplyRetryRow.message_id == self.message,
                    ReplyRetryRow.sequence == self.sequence,
                )
                .with_for_update()
            )
            if (
                row is None
                or not row.provider_pending
                or type(tokens) is not int
                or not 0 <= tokens <= requested
                or row.output_tokens + tokens > 512
            ):
                raise ModelGenerationError("unknown")
            row.output_tokens += tokens
            row.provider_pending = False
        await self.check()

    async def fail(self) -> None:
        async with self.repository.factory.begin() as db:
            row = await db.scalar(
                select(ReplyRetryRow)
                .where(
                    ReplyRetryRow.tenant_id == self.tenant,
                    ReplyRetryRow.message_id == self.message,
                    ReplyRetryRow.sequence == self.sequence,
                )
                .with_for_update()
            )
            if row is not None and row.state != "classified":
                row.state = "failed"
