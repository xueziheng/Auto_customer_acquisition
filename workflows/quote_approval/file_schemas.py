"""B2文件应用的技术wrapper；调用ID不进入报价域。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, field_validator, model_validator

from domains.quotations.schemas import QuoteFileView
from shared.errors import TradeOSError
from shared.schemas.quote_facts import fact_utc
from tool_gateway.file_rate_limit import CallId, FileDTO
from tool_gateway.handlers.quote_files import (
    FILE_FAILURE_MESSAGES,
    QuoteFileFailureCode,
    QuoteIdentifier,
)


class QuoteFileRecoveryCommand(FileDTO):
    """显式恢复只接受原调用ID，不接自由key。"""

    quote_id: QuoteIdentifier
    original_generation_call_id: CallId


class QuoteFileRecoveryResult(FileDTO):
    """只找回metadata，旧调用仍待自行完成。"""

    outcome: Literal["metadata_recovered_original_unresolved"]
    file: QuoteFileView
    original_generation_call_id: CallId
    recovery_call_id: CallId
    original_status_at_check: Literal["executing"]
    original_ledger_modified: Literal[False]
    checked_at: Annotated[datetime, AfterValidator(fact_utc)]

    @field_validator("original_ledger_modified", mode="before")
    @classmethod
    def unchanged(cls, value: object) -> object:
        """Literal的数值相等性不能把0伪装成技术布尔事实。"""
        if value is not False:
            raise ValueError("原调用账本修改标记无效")
        return value


class QuoteFileApiError(FileDTO):
    """错误消息只能使用固定表，不收任意底层异常文本。"""

    code: QuoteFileFailureCode
    message: str
    tool_call_id: CallId | None
    original_generation_call_id: CallId | None
    retry_after_seconds: Annotated[int, Field(ge=1, le=86400)] | None

    @model_validator(mode="after")
    def fixed_message(self) -> "QuoteFileApiError":
        if self.message != FILE_FAILURE_MESSAGES[self.code]:
            raise ValueError("文件错误消息无效")
        return self


class QuoteFileApplicationError(TradeOSError):
    """应用安全错误，技术ID只在明确字段。"""

    def __init__(self, detail: QuoteFileApiError) -> None:
        self.detail = detail
        super().__init__(detail.message)
