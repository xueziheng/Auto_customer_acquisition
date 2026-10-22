"""组织域对外 DTO 与严格 Playbook 提交边界。"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from shared.schemas.browser_probe import BrowserSourceView
from shared.schemas.discovery_sources import DiscoverySourceDefinition
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    PlaybookActivationId,
    PlaybookVersionId,
)
from shared.schemas.provenance import Provenance
from shared.schemas.provider_usage import DataSourceUsageView


class DataSourceCredentialsDelete(BaseModel):
    """删除同一精确版本的全部凭证字段，保留版本墓碑防止旧请求覆盖。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    expected_revision: int = Field(strict=True, ge=0)


class DataSourceUsageCheck(BaseModel):
    """只检查用户当前看到的凭证版本，不接受密钥、租户或外部 URL。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    expected_revision: int = Field(strict=True, ge=1)


class DataSourceCredentialsUpdate(BaseModel):
    """平台凭证只写命令；不允许省略字段或在错误中回显输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    expected_revision: int = Field(strict=True, ge=0)
    credentials: dict[str, SecretStr] = Field(min_length=1, max_length=8, repr=False)

    @field_validator("credentials")
    @classmethod
    def validate_credentials(cls, values: dict[str, SecretStr]) -> dict[str, SecretStr]:
        for name, secret in values.items():
            value = secret.get_secret_value()
            if (
                re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
                or not 1 <= len(value) <= 8192 or value != value.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError("数据源凭证格式无效")
        return values


class DataSourceConfigurationView(BaseModel):
    """只含配置与验证事实，不包含凭证、引用或哈希。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    configured: bool = False
    revision: int = Field(default=0, ge=0)
    configuration_version: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None
    test_status: Literal["not_run", "passed", "failed", "inconclusive"] = "not_run"
    last_test_scope: Literal["account", "sample"] | None = None
    tested_at: datetime | None = None


class DataSourceNoteUpdate(BaseModel):
    """来源管理备注只接收纯文本和备注版本，不承载凭证或系统运行状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    expected_revision: int = Field(strict=True, ge=0)
    text: str = Field(strict=True, max_length=2000, repr=False)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in value):
            raise ValueError("备注格式无效")
        return value.strip()


class DataSourceNoteView(BaseModel):
    """人工管理备注及修改人、时间；与供应商验证事实分开。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    text: str = Field(default="", max_length=2000, repr=False)
    revision: int = Field(default=0, strict=True, ge=0)
    updated_at: datetime | None = None
    updated_by: str | None = None


class DataSourceBrowserCheck(BaseModel):
    """只允许空命令，站点与会话由受信部署绑定。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class DataSourceView(DataSourceConfigurationView):
    """目录能力与配置状态分开，保存不能表示接口已经启用。"""

    definition: DiscoverySourceDefinition
    can_edit: bool
    status_kind: Literal["api", "session", "website"] = "api"
    can_check_browser: bool = False
    browser: BrowserSourceView | None = None
    can_check_usage: bool = False
    usage: DataSourceUsageView = Field(default_factory=DataSourceUsageView)
    can_edit_note: bool = False
    note: DataSourceNoteView = Field(default_factory=DataSourceNoteView)


class DataSourcesView(BaseModel):
    """平台当前获客来源目录与非秘密配置投影。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    generated_at: datetime
    sources: tuple[DataSourceView, ...]

_DECIMAL_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_ACTION_PATTERN = re.compile(r"[a-z][a-z0-9._:-]{0,127}\Z")
_REMOVAL_PREFIXES = ("!", "-", "remove:", "delete:", "disable:")


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _parse_decimal_string(value: object) -> Decimal:
    """只接受不会在边界丢失精度的 Decimal 或十进制字符串。"""
    if isinstance(value, (bool, int, float)):
        # Pydantic v2 只把 ValueError 归入结构化字段校验；TypeError 会逸出为 500。
        raise ValueError("金额必须是十进制字符串")  # noqa: TRY004
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str) and _DECIMAL_PATTERN.fullmatch(value):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - 正则已先行约束
            raise ValueError("金额必须是有效十进制字符串") from exc
    else:
        raise ValueError("金额必须是非负十进制字符串")
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("金额必须是非负有限 Decimal")
    _, digits, exponent = parsed.as_tuple()
    if not isinstance(exponent, int):  # finite 已保证，显式收窄 DecimalTuple 类型
        raise TypeError("金额必须是有限 Decimal")
    fractional_digits = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)
    if integer_digits + fractional_digits > 28 or fractional_digits > 12:
        raise ValueError("金额最多 28 位数字且小数不超过 12 位")
    return parsed


DecimalStringInput = Annotated[
    Decimal,
    BeforeValidator(_parse_decimal_string),
    WithJsonSchema({"type": "string", "pattern": _DECIMAL_PATTERN.pattern}),
]


def _normalize_label(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是字符串")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or _has_control(normalized):
        raise ValueError(f"{field_name} 不能为空或包含控制字符")
    return " ".join(normalized.split()).casefold()


def _normalize_list(values: list[str], *, field_name: str) -> list[str]:
    return sorted({_normalize_label(value, field_name=field_name) for value in values})


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须是带时区时间")
    return value.astimezone(UTC)


class PlaybookProposalCreate(BaseModel):
    """不携带租户、身份或幂等键的 Playbook 业务内容。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    company_type: str = Field(max_length=200)
    minimum_deal_amount: DecimalStringInput
    minimum_deal_currency: str
    excluded_categories: list[str] = Field(default_factory=list, max_length=200)
    sourcing_regions: list[str] = Field(default_factory=list, max_length=200)
    excluded_countries: list[str] = Field(default_factory=list, max_length=200)
    monthly_budget_credits: int | None = Field(default=None, ge=0)
    approval_requirements: list[str] = Field(default_factory=list, max_length=200)
    supply_capabilities_note: str | None = Field(default=None, max_length=4_000)

    @field_validator("company_type", mode="before")
    @classmethod
    def normalize_company_type(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return _normalize_label(value, field_name="company_type").replace(" ", "_")

    @field_validator("minimum_deal_currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFKC", value).strip().upper()
        if (
            len(normalized) != 3
            or not normalized.isascii()
            or not normalized.isalpha()
        ):
            raise ValueError("币种必须是三位 ASCII 字母")
        return normalized

    @field_validator(
        "excluded_categories", "sourcing_regions", "excluded_countries"
    )
    @classmethod
    def normalize_categorical_list(
        cls, values: list[str], info: object
    ) -> list[str]:
        field_name = getattr(info, "field_name", "列表项")
        return _normalize_list(values, field_name=field_name)

    @field_validator("approval_requirements")
    @classmethod
    def normalize_approval_requirements(cls, values: list[str]) -> list[str]:
        normalized: set[str] = set()
        for value in values:
            action = _normalize_label(value, field_name="approval_requirements")
            if action.startswith(_REMOVAL_PREFIXES) or not _ACTION_PATTERN.fullmatch(
                action
            ):
                raise ValueError("额外审批动作必须是可加严的 action ID")
            normalized.add(action)
        return sorted(normalized)

    @field_validator("supply_capabilities_note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized or _has_control(normalized):
            raise ValueError("供应能力说明不能为空或包含控制字符")
        return normalized


class _FrozenModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class PlaybookApprovalFact(_FrozenModel):
    approval_id: ApprovalId
    approval_type: str
    change_set_ref: str
    decided_by: EmployeeId
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def validate_decided_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="decided_at")


class PlaybookVersionView(_FrozenModel):
    playbook_version_id: PlaybookVersionId
    version_number: int = Field(ge=1)
    content_hash: str
    base_version_id: PlaybookVersionId | None
    base_content_hash: str | None
    company_type: str
    minimum_deal_amount: str
    minimum_deal_currency: str
    excluded_categories: tuple[str, ...]
    sourcing_regions: tuple[str, ...]
    excluded_countries: tuple[str, ...]
    monthly_budget_credits: int | None
    approval_requirements: tuple[str, ...]
    supply_capabilities_note: str | None
    proposed_by: EmployeeId
    proposed_at: datetime
    content_provenance: Provenance
    change_set_ref: str

    @model_validator(mode="after")
    def validate_pairs_and_time(self) -> PlaybookVersionView:
        if (self.base_version_id is None) != (self.base_content_hash is None):
            raise ValueError("base_version_id 与 base_content_hash 必须同时存在或为空")
        _aware_utc(self.proposed_at, field_name="proposed_at")
        return self


class PlaybookProposalResult(_FrozenModel):
    playbook_version_id: PlaybookVersionId
    version_number: int = Field(ge=1)
    content_hash: str
    change_set_ref: str


class PlaybookChangeSnapshot(_FrozenModel):
    base: PlaybookVersionView | None
    current: PlaybookVersionView | None
    candidate: PlaybookVersionView
    base_is_current: bool


class PlaybookActivationView(_FrozenModel):
    activation_id: PlaybookActivationId
    playbook_version_id: PlaybookVersionId
    content_hash: str
    approval_id: ApprovalId
    change_set_ref: str
    approved_by: EmployeeId
    approved_at: datetime
    activated_by: str
    activated_at: datetime

    @model_validator(mode="after")
    def validate_times(self) -> PlaybookActivationView:
        approved_at = _aware_utc(self.approved_at, field_name="approved_at")
        activated_at = _aware_utc(self.activated_at, field_name="activated_at")
        if approved_at > activated_at:
            raise ValueError("approved_at 不能晚于 activated_at")
        return self


__all__ = (
    "DecimalStringInput",
    "PlaybookActivationView",
    "PlaybookApprovalFact",
    "PlaybookChangeSnapshot",
    "PlaybookProposalCreate",
    "PlaybookProposalResult",
    "PlaybookVersionView",
)
