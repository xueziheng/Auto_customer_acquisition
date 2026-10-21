"""Campaign 内的窄范围客户准备授权；确认仅声明适用范围，不代表完成法律评估。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.schemas.identifiers import EmployeeId


class CustomerAgentKnowledgeRef(BaseModel):
    """本企业人工确认的精确资料版本，仅供内部适配。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    document_id: str = Field(min_length=1, max_length=40)
    revision_id: str = Field(min_length=1, max_length=40)


class CustomerAgentPolicyInput(BaseModel):
    """由真人提交的企业法人职务邮箱范围；确认身份与时间只能由服务补充。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    prepare_and_enroll: Literal[True] = True
    owner_employee_id: EmployeeId
    assessment_ref: str = Field(min_length=1, max_length=512)
    confirmed_refs: tuple[CustomerAgentKnowledgeRef, ...] = Field(default=(), max_length=5, strict=False)
    basis: Literal["legitimate_interest"] = "legitimate_interest"
    subject_type: Literal["legal_entity"] = "legal_entity"
    contact_type: Literal["role_based"] = "role_based"

    @field_validator("assessment_ref")
    @classmethod
    def require_reference(cls, value: str) -> str:
        """拒绝空白或控制字符，保留真人引用而不生成法律结论。"""
        if not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("处理依据引用无效")
        return value

    @field_validator("owner_employee_id")
    @classmethod
    def require_employee(cls, value: EmployeeId) -> EmployeeId:
        """只接收员工安全标识；当前企业在职资格由执行时重新读取。"""
        import re
        if re.fullmatch(r"emp_[0-7][0-9A-HJKMNP-TV-Z]{25}", value) is None:
            raise ValueError("负责人标识无效")
        return value


class CustomerAgentPolicy(CustomerAgentPolicyInput):
    """当前认证员工确认的范围事实，随不可变 Campaign 版本存储。"""

    confirmed_by: EmployeeId
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        """确认时间必须有时区，不能接受客户端的本地时间猜测。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("范围确认时间缺少时区")
        return value
