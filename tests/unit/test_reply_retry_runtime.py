"""恢复装配保留规范域服务，并显式关闭邮件扫描。"""

from dataclasses import fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from apps.scheduler_worker.reply_binding import ReplyRuntimeResources
from apps.scheduler_worker.reply_composition import CurrentEmployeeReplyFactory
from apps.scheduler_worker.runtime import SchedulerCoreServices
from apps.scheduler_worker.standalone import _reply_ports
from tests.unit.test_pilot_profile import make_config
from tests.unit.test_qq_reply_runtime import qq_settings


def test_recovery_factory_keeps_canonical_conversations():
    core = SchedulerCoreServices(
        **{f.name: object() for f in fields(SchedulerCoreServices)}
    )
    async def unused(*args, **kwargs):
        pytest.fail("构造阶段不能执行域动作")

    core = replace(core, conversations=SimpleNamespace(record_classification=unused))
    outreach = SimpleNamespace(stop_enrollment=unused, add_suppression=unused)
    bindings = []
    resources = ReplyRuntimeResources(
        object(),
        object(),
        object(),
        lambda: datetime.now(UTC),
        bind_classification_commit=bindings.append,
    )
    factory = CurrentEmployeeReplyFactory(
        "tenant", "employee", classifier=SimpleNamespace(classify=unused, model="test"), retries=object()
    )
    reply = factory(core, outreach, resources)
    assert reply.conversations is core.conversations
    assert len(bindings) == 1


def test_recovery_only_ports_never_activate_trial_or_scan_mail(tmp_path):
    profile = make_config(tmp_path).model_copy(update={"object_port": 19091})
    config = qq_settings(tmp_path)
    config = config.model_copy(
        update={"qq_reply": config.qq_reply.model_copy(update={"recovery_only": True})}
    )
    ports = _reply_ports(profile, config, None)
    assert ports.scan_enabled is False and ports.activate_adapter is None
    assert ports.adapter_factory is not None


async def test_deferred_commit_binding_is_once_and_original_gate_not_bypassed():
    from apps.scheduler_worker.reply_binding import DeferredReplyCommit

    calls = []

    async def original(db, message):
        calls.append("original")

    async def recovery(db, message):
        calls.append("recovery")

    gate = DeferredReplyCommit(original)
    await gate.commit(None, "msg")
    gate.bind(recovery)
    await gate.commit(None, "msg")
    with pytest.raises(ValueError):
        gate.bind(recovery)
    assert calls == ["original", "recovery"]
