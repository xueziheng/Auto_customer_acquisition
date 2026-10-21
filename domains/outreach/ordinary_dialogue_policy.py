"""精确 Campaign 版本中的普通对话授权；不授权任何商业承诺。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.schemas.identifiers import EmployeeId


class DialogueProductRef(BaseModel):
    """须重读客户视图和当前版本的已确认产品引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    product_id: str = Field(min_length=1, max_length=40)
    customer_view_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OrdinaryDialoguePolicyInput(BaseModel):
    """真人选择的有界范围；模型调用额度与 token 上限均为独立硬上限。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    owner_employee_id: EmployeeId
    assessment_ref: str = Field(min_length=1, max_length=512)
    product_refs: tuple[DialogueProductRef, ...] = Field(min_length=1, max_length=5, strict=False)
    expires_at: datetime = Field(strict=False)
    max_rounds: int = Field(gt=0, le=20)
    max_model_calls: int = Field(gt=0, le=40)
    max_no_progress_rounds: int = Field(gt=0, le=5)
    max_input_bytes: int = Field(gt=0, le=32768)
    max_output_tokens: int = Field(gt=0, le=4096)

    @field_validator("assessment_ref", "owner_employee_id")
    @classmethod
    def safe_reference(cls, value: str) -> str:
        """引用必须非空且不可携带控制字符，员工当前资格由授权服务检查。"""
        if not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("普通对话范围引用无效")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        """有效期必须明确时区，不能将本地时间猜成 UTC。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("普通对话有效期缺少时区")
        return value


class OrdinaryDialoguePolicy(OrdinaryDialoguePolicyInput):
    """服务根据实际认证人和时钟补充确认事实，随批准版本不可变保存。"""

    confirmed_by: EmployeeId
    confirmed_at: datetime
