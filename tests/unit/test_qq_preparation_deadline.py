"""F1 合成回归：计时与进程回收是真实值，任何外网仍由运行器阻断。"""
import asyncio
import json
import ssl
import subprocess
import threading
from dataclasses import replace
from time import monotonic

import pytest

from connectors._deadline_process import CLEANUP_GRACE_SECONDS, DeadlineProcess
from connectors.qq_mail.sent_collect import QQSentAliasCollector
from shared.schemas.qq_trial import QQTrialStopped
from tests.unit.qq_p1_imap import p1_mailbox
from tests.unit.test_qq_sent_collect import SyntheticSecret, allowed, headers, request


def assert_reaped(mailbox):
    assert mailbox["processes"]
    assert all(process.poll() is not None for process in mailbox["processes"])


def enable_tls(mailbox,tmp_path):
    key,cert=tmp_path/"synthetic.key",tmp_path/"synthetic.crt"
    subprocess.run(["/usr/bin/openssl","req","-x509","-newkey","rsa:2048","-nodes",
        "-keyout",str(key),"-out",str(cert),"-days","1","-subj","/CN=imap.qq.com",
        "-addext","subjectAltName=DNS:imap.qq.com"],check=True,capture_output=True,timeout=10)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert,key)
    mailbox["tls_context"],mailbox["tls_cert"]=context,cert


@pytest.mark.parametrize("mode",["line","literal","missing_newline","greeting"])
@pytest.mark.parametrize("tls",[False,True])
async def test_continuous_data_cannot_extend_deadline(monkeypatch,tmp_path,mode,tls,record_property):
    req=replace(request(),timeout_seconds=1)
    before_threads={t.ident for t in threading.enumerate()}
    with p1_mailbox(monkeypatch,headers(req),allow_disconnect=True) as mailbox:
        if tls:
            enable_tls(mailbox,tmp_path)
        mailbox["slow"]=mode
        started=monotonic()
        with pytest.raises(QQTrialStopped):
            await QQSentAliasCollector(SyntheticSecret(),allowed).collect(req)
        elapsed=monotonic()-started
        assert .8 <= elapsed <= 1+CLEANUP_GRACE_SECONDS
        assert_reaped(mailbox)
        assert sum(c.startswith("UID SEARCH") for _,c in mailbox["commands"])<=1
        record_property("deadline_measurement",json.dumps({"stage":mode,"tls":tls,"budget":1,"elapsed":elapsed,
            "cleanup_grace":CLEANUP_GRACE_SECONDS,"all_children_reaped":True}))
    assert {t.ident for t in threading.enumerate()}==before_threads


@pytest.mark.parametrize("mode",["fragment","eof"])
@pytest.mark.parametrize("tls",[False,True])
async def test_fragmented_success_or_partial_eof(monkeypatch,tmp_path,mode,tls):
    req=request()
    with p1_mailbox(monkeypatch,headers(req),allow_disconnect=True) as mailbox:
        if tls:
            enable_tls(mailbox,tmp_path)
        mailbox["slow"]=mode
        if mode=="fragment":
            result=await QQSentAliasCollector(SyntheticSecret(),allowed).collect(req)
            assert result.raw_headers==headers(req)
        else:
            with pytest.raises(QQTrialStopped):
                await QQSentAliasCollector(SyntheticSecret(),allowed).collect(req)
        assert_reaped(mailbox)


@pytest.mark.parametrize("mode",["dns","tcp","tls","close"])
@pytest.mark.parametrize("cancel",[False,True])
async def test_constructor_and_cleanup_have_process_ownership(monkeypatch,tmp_path,mode,cancel,record_property):
    req=replace(request(),timeout_seconds=2)
    marker=tmp_path/"stage"
    with p1_mailbox(monkeypatch,headers(req),allow_disconnect=True) as mailbox:
        if mode=="tls":
            mailbox["slow"]="tls"
        original=DeadlineProcess._command
        def command(self,fd):
            args=original(self,fd)
            assert args[2]=="tests.unit.qq_deadline_child"
            return [*args[:-1],mode,str(marker)]
        monkeypatch.setattr(DeadlineProcess,"_command",command)
        started=monotonic()
        task=asyncio.create_task(QQSentAliasCollector(SyntheticSecret(),allowed).collect(req))
        async with asyncio.timeout(1.5):
            while not marker.exists():
                await asyncio.sleep(.01)
        assert marker.read_text()==mode
        if cancel:
            task.cancel()
            # 重复取消不能中断拥有者的 kill/reap 收尾。
            asyncio.get_running_loop().call_soon(task.cancel)
        with pytest.raises(asyncio.CancelledError if cancel else QQTrialStopped):
            await task
        elapsed=monotonic()-started
        assert elapsed <= req.timeout_seconds+CLEANUP_GRACE_SECONDS
        assert_reaped(mailbox)
        await asyncio.sleep(.05)
        assert not marker.with_suffix(".late").exists()
        record_property("deadline_measurement",json.dumps({"stage":mode,"cancel":cancel,
            "budget":req.timeout_seconds,"elapsed":elapsed,"all_children_reaped":True}))


async def test_expired_deadline_does_not_start_child(monkeypatch):
    from connectors._deadline_process import PreparationDeadline
    deadline=PreparationDeadline(.01)
    await asyncio.sleep(.02)
    monkeypatch.setattr(DeadlineProcess,"_start",lambda self: pytest.fail("late process start"))
    with pytest.raises(TimeoutError):
        DeadlineProcess(deadline,"sent")


async def test_database_authorization_can_only_shorten_deadline():
    from connectors._deadline_process import PreparationDeadline
    deadline=PreparationDeadline(5)
    started=monotonic()
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(deadline.remaining()) as timeout:
                deadline.watch(timeout)
                deadline.shorten(monotonic()+.1)
                deadline.shorten(monotonic()+10)
                await asyncio.sleep(2)
        assert .08 <= monotonic()-started < .5
        assert deadline.aborted
    finally:
        deadline.disarm()
