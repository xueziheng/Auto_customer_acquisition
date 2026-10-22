"""QQ入站与自动发送分离；配置和构造阶段零邮箱/模型IO。"""
from dataclasses import replace

import pytest

from apps.scheduler_worker.config import SchedulerWorkerConfig
from apps.scheduler_worker.pilot import UnconfiguredDnsStep
from apps.scheduler_worker.runtime import SchedulerRuntimeFactory
from apps.scheduler_worker.standalone import _reply_ports
from infra.standalone.automatic_followup import reply_employee_binding
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import new_id
from tests.unit.test_pilot_profile import make_config
from tests.unit.test_standalone_capability_binding import bootstrap, unused_factory
from tests.unit.test_standalone_model_settings import settings


def qq_settings(tmp_path, **patch):
    return StandaloneModelSettings.model_validate({
        **settings(), "limits": {**settings()["limits"], "max_output_tokens": 512}, "reply_enabled": True, "qq_reply": {
            "employee_id": new_id("emp"), "user_id": new_id("usr"),
            "classifier_employee_id": new_id("emp"),
            "mailbox_directory": str(tmp_path / "does-not-exist"),
        }, **patch,
    })


def test_qq_reply_constructs_without_gmail_and_without_reading_mailbox(tmp_path):
    profile = make_config(tmp_path).model_copy(update={"object_port": 19091})
    config = qq_settings(tmp_path)
    ports = _reply_ports(profile, config, None)
    assert profile.gmail is None
    assert ports.provider is None and ports.adapter_factory is not None
    assert ports.profile.mailbox_alias == "qq-project-reply"
    assert reply_employee_binding(profile, config) == config.qq_reply.classifier_employee_id
    assert reply_employee_binding(profile, config) != config.qq_reply.employee_id
    assert not config.automatic_send_enabled
    env = profile.runtime_environment()
    composition = replace(bootstrap(), reply_factory=unused_factory)
    factory = SchedulerRuntimeFactory(env,
        pilot_config=SchedulerWorkerConfig.from_pilot_environ(env),
        bootstrap=composition, standalone_research=True, secret_resolver=profile,
        unconfigured_dns_step=UnconfiguredDnsStep(), inbound_ports=ports)
    assert isinstance(factory, SchedulerRuntimeFactory)
    assert not composition.campaign_enabled
    assert composition.gmail_transport is None


@pytest.mark.parametrize("patch", [
    {"reply_enabled": False},
    {"automatic_send_enabled": True},
    {"automatic_send_enabled": "true"},
])
def test_qq_requires_reply_enabled_and_forbids_automatic_send(tmp_path, patch):
    with pytest.raises(ValueError):
        qq_settings(tmp_path, **patch)


def test_qq_binding_rejects_changed_test_pair_and_same_classifier_owner(tmp_path):
    config = qq_settings(tmp_path)
    base = config.model_dump()
    base["qq_reply"]["mailbox_directory"] = str(tmp_path)
    base["secret_ref"] = "TEST_MODEL"
    for patch in [
        {"address": "111111111@qq.com"},
        {"recipient": "555555555@qq.com"},
        {"classifier_employee_id": config.qq_reply.employee_id},
    ]:
        with pytest.raises(ValueError):
            StandaloneModelSettings.model_validate({**base, "qq_reply": {**base["qq_reply"], **patch}})


def test_default_send_is_closed_independently_of_reply():
    assert not StandaloneModelSettings.model_validate(settings()).automatic_send_enabled
    assert not StandaloneModelSettings.model_validate({**settings(), "limits": {**settings()["limits"], "max_output_tokens": 512}, "reply_enabled": True}).automatic_send_enabled
