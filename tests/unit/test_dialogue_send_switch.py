"""实际独立配置的停发开关必须先于任何凭证与业务读取。"""
import pytest

from infra.standalone.settings import StandaloneModelSettings
from shared.errors import PermissionDenied


def settings(enabled):
    return StandaloneModelSettings(provider='compatible', model='synthetic', secret_ref='MODEL_API_KEY',
        configuration_version='test-v1', model_data_export_enabled=True, reply_enabled=True,
        automatic_send_enabled=enabled, limits={'window_seconds': 60, 'tenant_calls': 100,
        'employee_calls': 100, 'tenant_concurrency': 1, 'employee_concurrency': 1,
        'max_input_bytes': 32768, 'max_output_tokens': 1000, 'timeout_seconds': 30})


async def test_global_off_prevents_delivery_before_any_read_or_credential():
    from apps.scheduler_worker.dialogue_delivery import CurrentDialogueDelivery
    class Never:
        def __getattr__(self, key): raise AssertionError('停发后不能读取外部或凭证')
        def __call__(self, *args, **kwargs): raise AssertionError('停发后不能打开数据库')
    never = Never()
    ports = CurrentDialogueDelivery(never, 'tenant', never, never, never, never, never, never,
        settings=settings(False))
    with pytest.raises(PermissionDenied):
        await ports.current('tenant', 'message', 'attempt', 'lease')
    with pytest.raises(PermissionDenied):
        await ports.connector(never)


def test_formal_ordinary_config_supports_on_without_qq_private_test():
    assert settings(True).automatic_send_enabled
    assert settings(True).qq_reply is None

@pytest.mark.parametrize('enabled', [False, True])
def test_formal_mailbox_factory_does_not_require_private_qq_or_enable_cold(tmp_path, enabled):
    from apps.scheduler_worker.standalone import _create_standalone_factory
    from tests.unit.test_pilot_profile import make_config
    profile = make_config(tmp_path).model_copy(update={'object_port': 44553})
    factory = _create_standalone_factory(profile, settings(enabled), profile,
        instance_id='synthetic', qq_mail_directory=tmp_path / 'qq-mail')
    assert factory._inbound_ports is None
    assert factory._bootstrap.reply_factory is not None
    assert factory._bootstrap.campaign_enabled is False
    assert factory._reply_artifact_ports is not None

def test_formal_cold_actor_is_explicit_and_cannot_override_legacy_binding(tmp_path):
    from infra.pilot.config import PilotGmailConfig
    from infra.standalone.automatic_followup import reply_employee_binding
    from shared.schemas.identifiers import new_id
    from tests.unit.test_pilot_profile import make_config
    actor, other = new_id('emp'), new_id('emp')
    values = settings(True).model_dump()
    values['secret_ref'] = 'MODEL_API_KEY'
    values['reply_classifier_employee_id'] = actor
    configured = StandaloneModelSettings.model_validate(values)
    profile = make_config(tmp_path)
    assert reply_employee_binding(profile, configured, ordinary_mailboxes=True) == actor
    gmail = PilotGmailConfig(address='sender@example.test', credentials_file=tmp_path/'unused', employee_id=other)
    with pytest.raises(ValueError):
        reply_employee_binding(profile.model_copy(update={'gmail': gmail}), configured, ordinary_mailboxes=True)
