"""首次询盘必须有显式对话绑定，不以假Enrollment满足旧接口。"""
import pytest

from domains.conversations.schemas import ReplyWorkAction, ReplyWorkActionRequest
from shared.errors import ValidationError
from shared.schemas.identifiers import new_id


def test_inquiry_reply_work_requires_binding_when_cold_source_absent():
    message = new_id("msg")
    args = {"message_id": message, "outbound_message_id": None, "enrollment_id": None,
        "account_id": new_id("acc"), "contact_point_id": new_id("cp"),
        "action": ReplyWorkAction.START_QUALIFICATION,
        "idempotency_key": f"reply:start_qualification:{message}"}
    with pytest.raises(ValidationError):
        ReplyWorkActionRequest(**args)
    request = ReplyWorkActionRequest(**args, dialogue_binding_id=new_id("dlg"))
    assert request.enrollment_id is None and request.outbound_message_id is None
