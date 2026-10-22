"""仅隔离 fixture 创建太阳能风扇边界；审批与额度事实仍为受控测试数据。"""
from tests.integration.test_manual_email_send_api import _seed_campaign


async def seed_solar_campaign(factory, tenant, campaign_id, identity_id, boss, approval_id):
    await _seed_campaign(
        factory, tenant, campaign_id, identity_id, boss, approval_id,
        new_contact_limit=1, total_message_limit=1,
        name="太阳能风扇单封私有流程测试",
        markets=("ZZ",), target_entity_types=("private_test",),
        allowed_categories=("solar_fan",),
    )
