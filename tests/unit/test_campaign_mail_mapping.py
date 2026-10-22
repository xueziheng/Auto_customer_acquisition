"""同 QQ scheduler 只解析原真实owner绑定；技术调用者不取得邮箱权限。"""
from dataclasses import replace

import pytest

from shared.errors import PermissionDenied
from tests.unit.test_email_send_handler import _context, _Gmail, _handler, _preflight
from tool_gateway.handlers.email_send import OutboundMailOwner


async def test_owner_snapshot_is_pinned_and_rechecked_before_credential_resolution():
    preflight = _preflight()
    original = OutboundMailOwner(preflight.tenant_id, 'real-employee', ('attempt', 'sender@example.com', 'identity', '1', 'mailbox', 'binding-v1', 'user', 'lock'))
    class Owners:
        owner = original
        async def bind(self, preflight, address): return self.owner
        async def check(self, owner):
            if self.owner != owner: raise PermissionDenied('当前绑定改变')
    class Connectors:
        def __init__(self): self.calls = []
        async def resolve_owned(self, owner, address):
            self.calls.append(owner)
            return _Gmail()
        async def resolve(self, *args): raise AssertionError('不能降级技术员工解析')
    owners, connectors = Owners(), Connectors()
    handler, _, _, gmail = _handler(preflight, connectors=connectors)
    handler._owner_resolver = owners
    prepared = await handler.prepare(_context(preflight), preflight)
    assert prepared.payload.employee_id == 'real-employee' and connectors.calls == []
    owners.owner = replace(original, proof=(*original.proof[:5], 'binding-v2', *original.proof[6:]))
    changed = await handler.prepare(_context(preflight), preflight)
    assert prepared.request_fingerprint != changed.request_fingerprint
    with pytest.raises(PermissionDenied):
        await handler.execute(preflight.tenant_id, prepared)
    assert connectors.calls == [] and gmail.calls == []
    owners.owner = original
    await handler.execute(preflight.tenant_id, prepared)
    assert connectors.calls == [original] and gmail.calls == []


async def test_legacy_gmail_scheduler_and_formal_qq_composition_can_coexist():
    class Connectors:
        async def resolve(self,*args): raise AssertionError('原 Gmail 不转入 QQ')
    preflight = _preflight()
    handler, _, _, gmail = _handler(preflight, connectors=Connectors())
    handler._legacy_gmail_for_unmapped = True
    prepared = await handler.prepare(_context(preflight),preflight)
    await handler.execute(preflight.tenant_id,prepared)
    assert len(gmail.calls) == 1
