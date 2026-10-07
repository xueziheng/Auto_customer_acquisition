"""受控联系人外部端口仅接受具名fixture，逐次调用独立计数。"""

from datetime import UTC, datetime

import pytest

from infra.controlled.config import ControlledError


async def test_contacts_reject_unknown_and_count_each_real_call(tmp_path):
    from infra.controlled.contacts import ControlledContactProvider

    provider = ControlledContactProvider(
        tmp_path / "mail.sqlite", tenant_id="owned", now=lambda: datetime.now(UTC)
    )
    with pytest.raises(ControlledError):
        await provider.verify("foreign", "buyer0@example.test")
    with pytest.raises(ControlledError):
        await provider.verify("owned", "unknown@example.test")
    await provider.verify("owned", "buyer0@example.test")
    await provider.verify("owned", "buyer0@example.test")
    assert provider.list_calls() == ("contact.verify", "contact.verify")
