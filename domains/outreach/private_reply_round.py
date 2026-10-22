"""已批准的单封私有测试按原发送关联区分轮次，不放宽普通客户回复门禁。"""

from domains.outreach.schemas import PrivateReplyTestHistory, ReplyStatusSnapshot
from shared.errors import ValidationError
from shared.schemas.identifiers import CampaignId


def private_round_has_reply(
    original: ReplyStatusSnapshot,
    history: PrivateReplyTestHistory,
    campaign_id: CampaignId,
) -> bool:
    """仅可忽略已分类且精确关联其他私有轮次的历史回信。"""
    if not isinstance(history, PrivateReplyTestHistory):
        raise ValidationError("私有测试回复历史无效")
    if (
        history.tenant_id != original.tenant_id
        or history.contact_point_id != original.contact_point_id
        or history.account_id != original.account_id
    ):
        raise ValidationError("私有测试回复范围不匹配")
    if not history.messages:
        # 普通读取器已确认有回复；历史端口不能以空列表推翻这个事实。
        return True
    known_categories = {
        "clear_interest", "willing_to_continue", "requests_materials",
        "requests_quote", "requests_sample", "provides_specification",
        "no_current_need", "future_need_possible", "refers_other_contact",
        "auto_reply",
    }
    for message in history.messages:
        if (
            not message.source_is_private_sent
            or message.source_campaign_id is None
            or message.category not in known_categories
        ):
            return True
        if message.category != "auto_reply" and message.source_campaign_id == campaign_id:
            return True
    return False
