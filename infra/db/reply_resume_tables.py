"""旧测试期限请求的不可变续办凭据；普通新请求不使用该表。"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.tables import Base


class ReplyResumeRow(Base):
    __tablename__ = "reply_classification_resumptions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id", "retry_sequence"],
            [
                "reply_classification_retries.tenant_id",
                "reply_classification_retries.message_id",
                "reply_classification_retries.sequence",
            ],
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_reply_resume_key"),
    )
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    retry_sequence: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    run_id: Mapped[str] = mapped_column(String(32))
    original_idempotency_key: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    operator_employee_id: Mapped[str] = mapped_column(String(40))
    operator_user_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
