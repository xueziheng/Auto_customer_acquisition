"""收件方原件保留真实来源；固定自有邮箱、逐次授权、单 UID，无正文读取。"""
import sys

import pytest

from connectors.qq_mail.sent_alias import verify_sent_alias
from infra.standalone.qq_trial_settings import QQTrialLaunchSettings
from shared.schemas.qq_trial import QQTrialScope, QQTrialStopped
from tests.unit.test_qq_alias_folding import CANONICAL, MARKER, PROVIDER, header
from tests.unit.test_qq_bounded_trial import trial_scope

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="需 Linux prctl 父进程保护；独立 Linux 容器验收"
)

def delivery():
    return {'employee_id':'emp_receipt','user_id':'usr_receipt',
        'authorization_source':'explicit_user_request','authorization_ref':'synthetic-owned-receipt',
        'folder':'INBOX','uidvalidity':7,'uid':1,'timeout_seconds':10,'header_bytes':16384}


def launch_values():
    scope=trial_scope()
    fields=('trial_id','route','alias_evidence_id','classifier_user_id','authorization_ref',
        'authorization_source','canonical_header','starts_at','expires_at','limits')
    data=scope.model_dump()
    return {**{k:data[k] for k in fields},'enabled':True,
        'alias_evidence_id':'qae_01ARZ3NDEKTSV4RRFFQ69G5FAV',
        'sent':{'folder':'Sent','uidvalidity':7,'first_uid':1,'last_uid':1,'timeout_seconds':10},
        'delivery':delivery()}


def test_verified_receipt_preserves_distinct_source_original_bytes_and_hash():
    raw=header()
    proof=verify_sent_alias((raw,),source='controlled_recipient_imap',canonical_header=CANONICAL,
        idempotency_header=MARKER,sender='1249583590@qq.com',recipient='986647123@qq.com')
    assert proof.source=='controlled_recipient_imap'
    assert proof.raw_headers is raw and proof.provider_header==PROVIDER


def test_launch_keeps_explicit_reader_identity_and_exact_read_scope():
    value=QQTrialLaunchSettings.model_validate(launch_values())
    assert value.delivery.employee_id=='emp_receipt'
    assert value.delivery.user_id=='usr_receipt'
    assert value.delivery.uid==1 and value.delivery.folder=='INBOX'


def test_persisted_trial_keeps_reader_and_source_for_provenance():
    value=trial_scope(alias_source='controlled_recipient_imap',delivery_employee_id='emp_receipt',delivery_user_id='usr_receipt')
    restored=QQTrialScope.model_validate(value.model_dump())
    assert restored.alias_source=='controlled_recipient_imap'
    assert restored.delivery_user_id=='usr_receipt'


@pytest.mark.parametrize('field,value', [('uid',0),('uid',True),('folder','Sent'),
    ('authorization_source','implicit'),('authorization_ref',''),('employee_id',''),('user_id','')])
def test_unsafe_or_implicit_delivery_read_is_rejected(field,value):
    data=launch_values()
    data['delivery'][field]=value
    with pytest.raises(ValueError):
        QQTrialLaunchSettings.model_validate(data)


async def test_delivery_collection_uses_recipient_login_one_exact_header_and_no_search(monkeypatch):
    from connectors.qq_mail.delivery_collect import (
        DeliveryReadRequest,
        QQDeliveryAliasCollector,
    )
    from tests.unit.qq_p1_imap import p1_mailbox
    from tests.unit.test_qq_sent_collect import SyntheticSecret, allowed
    req=DeliveryReadRequest(CANONICAL,MARKER,'1249583590@qq.com','986647123@qq.com','INBOX',7,1,10,16384)
    raw=(f'Message-ID: {PROVIDER}\r\nFrom: 1249583590@qq.com\r\nTo: 986647123@qq.com\r\n'
        f'X-TradeOS-Idempotency-V1: {MARKER}\r\n\r\n').encode()
    with p1_mailbox(monkeypatch,raw) as mailbox:
        mailbox['expected_login']='986647123@qq.com'
        mailbox['reply']=raw
        proof=await QQDeliveryAliasCollector(SyntheticSecret(),allowed).collect(req)
        assert proof.source=='controlled_recipient_imap' and proof.raw_headers==raw
        commands=[c for _,c in mailbox['commands']]
        assert sum(c.startswith('UID FETCH') for c in commands)==1
        assert not any('SEARCH' in c or 'BODY.PEEK[]' in c or c.startswith('APPEND') for c in commands)


@pytest.mark.parametrize('change',['uidvalidity','marker','canonical_copy','revoked'])
async def test_delivery_collector_rejects_stale_scope_and_unbound_original(monkeypatch,change):
    from dataclasses import replace

    from connectors.qq_mail.delivery_collect import (
        DeliveryReadRequest,
        QQDeliveryAliasCollector,
    )
    from tests.unit.qq_p1_imap import p1_mailbox
    from tests.unit.test_qq_sent_collect import SyntheticSecret
    req=DeliveryReadRequest(CANONICAL,MARKER,'1249583590@qq.com','986647123@qq.com','INBOX',7,1,10,16384)
    raw=header()
    if change=='uidvalidity':req=replace(req,uidvalidity=8)
    if change=='marker':raw=header('pilot.'+'b'*64)
    if change=='canonical_copy':raw=raw.replace(PROVIDER.encode(),CANONICAL.encode())
    async def authorized():return change!='revoked'
    with p1_mailbox(monkeypatch,raw) as mailbox:
        mailbox['reply']=raw
        with pytest.raises(QQTrialStopped):
            await QQDeliveryAliasCollector(SyntheticSecret(),authorized).collect(req)
        if change in {'uidvalidity','revoked'}:
            assert not any(c.startswith('UID FETCH') for _,c in mailbox['commands'])


@pytest.mark.parametrize('source',['controlled_recipient_imap','sender_sent','recipient_export'])
async def test_delivery_handler_archives_only_its_exact_receipt_source(source):
    from dataclasses import replace
    from types import SimpleNamespace

    from connectors.qq_mail.delivery_collect import DeliveryReadRequest
    from tool_gateway.errors import ToolGatewayError
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from tool_gateway.handlers.qq_delivery_alias import DeliveryAliasReadHandler
    from tool_gateway.pipeline import PreparedToolCall
    proof=verify_sent_alias((header(),),source='controlled_recipient_imap',canonical_header=CANONICAL,
        idempotency_header=MARKER,sender='1249583590@qq.com',recipient='986647123@qq.com')
    proof=replace(proof,source=source)
    archived=[]
    class Collector:
        async def collect(self,request):return proof
    class Archiver:
        async def archive(self,tenant,raw,*,maximum_bytes):
            archived.append(raw)
            return SimpleNamespace(artifact_id='art_01ARZ3NDEKTSV4RRFFQ69G5FAV')
    async def authorized():return True
    req=DeliveryReadRequest(CANONICAL,MARKER,'1249583590@qq.com','986647123@qq.com','INBOX',7,1,10,16384)
    h=DeliveryAliasReadHandler(tenant='tn_fixture',user='usr_receipt',attempt='mat_fixture',request=req,
        authorization_binding=b'synthetic-receipt-binding',authorize=authorized,collector=Collector(),
        archiver=Archiver(),fingerprints=HmacFingerprintProvider('v1',b'f'*32))
    prepared=PreparedToolCall('a'*64,'v1',{'attempt_id':'mat_fixture'},req)
    if source=='controlled_recipient_imap':
        result=await h.execute('tn_fixture',prepared)
        assert result=={'provider_ref':'art_01ARZ3NDEKTSV4RRFFQ69G5FAV'}
        assert archived==[header()]
    else:
        with pytest.raises(ToolGatewayError):
            await h.execute('tn_fixture',prepared)
        assert archived==[]
