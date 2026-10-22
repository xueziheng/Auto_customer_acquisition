"""真实 IMAP 解析器的合成协议验证，只允许必要 Sent 头读取。"""
import sys
from dataclasses import replace

import pytest

from connectors.qq_mail.sent_collect import QQSentAliasCollector, SentReadRequest
from shared.schemas.qq_trial import QQTrialStopped
from tests.unit.qq_p1_imap import p1_mailbox

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="需 Linux prctl 父进程保护；独立 Linux 容器验收"
)

def request():
    marker="synthetic-p1."+"a"*64
    return SentReadRequest(f"<{marker}@messages.tradeos.invalid>",marker,
        "1249583590@qq.com","986647123@qq.com","Sent",7,1,1,10,16384)


def headers(req, alias="<synthetic-p1-server@qq.com>"):
    return (f"Message-ID: {alias}\r\nX-TradeOS-Idempotency-V1: {req.idempotency_header}\r\n"
        f"From: {req.sender}\r\nTo: {req.recipient}\r\n\r\n").encode()


class SyntheticSecret:
    def resolve(self,key):
        assert key=="QQ_INBOUND_AUTH"
        return "synthetic-p1-password"


async def allowed():
    return True


async def test_sent_collection_reads_one_header_no_body_or_append(monkeypatch):
    req=request()
    raw=headers(req)
    with p1_mailbox(monkeypatch,raw) as mailbox:
        result=await QQSentAliasCollector(SyntheticSecret(),allowed).collect(req)
        assert result.source=="sender_sent" and result.raw_headers==raw
        commands=[c for _,c in mailbox["commands"]]
        assert sum(c.startswith("UID SEARCH") for c in commands)==1
        assert sum(c.startswith("UID FETCH") for c in commands)==1
        assert all("BODY.PEEK[]" not in c and not c.startswith("APPEND") for c in commands)


@pytest.mark.parametrize("mode",["ambiguous","missing","range","marker","original_copy","uidvalidity","budget","revoked"])
async def test_sent_collection_stops_without_evidence(monkeypatch,mode):
    req=request()
    raw=headers(req)
    if mode=="marker":
        raw=raw.replace(b"a"*64,b"b"*64)
    if mode=="original_copy":
        raw=headers(req,req.canonical_header)
    if mode=="uidvalidity":
        req=replace(req,uidvalidity=8)
    if mode=="budget":
        req=replace(req,header_bytes=10)
    async def authorize():
        return mode!="revoked"
    with p1_mailbox(monkeypatch,raw,allow_disconnect=mode=="budget") as mailbox:
        mailbox["sent_search"]={"ambiguous":"1 2","missing":"","range":"2"}.get(mode,"1")
        with pytest.raises(QQTrialStopped):
            await QQSentAliasCollector(SyntheticSecret(),authorize).collect(req)
        commands=[c for _,c in mailbox["commands"]]
        assert all("BODY.PEEK[]" not in c for c in commands)
        if mode in {"ambiguous","missing","range","uidvalidity","revoked"}:
            assert not any(c.startswith("UID FETCH") for c in commands)
