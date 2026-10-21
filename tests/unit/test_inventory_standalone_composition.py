"""实际 standalone 工厂在无 Tavily 时仍装配库存与页面准备。"""

from apps.scheduler_worker.inventory_ports import InventoryRuntimePorts
from apps.scheduler_worker.standalone import _create_standalone_factory
from infra.standalone.settings import StandaloneModelSettings
from tests.unit.test_pilot_profile import make_config
from tests.unit.test_standalone_model_settings import settings


def test_actual_standalone_factory_accepts_page_ports_without_tavily(
    monkeypatch, tmp_path
):
    from apps.scheduler_worker import standalone

    captured = {}

    def bootstrap(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(standalone, "CanonicalSchedulerBootstrap", bootstrap)
    monkeypatch.setattr(
        standalone, "SchedulerRuntimeFactory", lambda *args, **kwargs: kwargs
    )
    profile = make_config(tmp_path)
    result = _create_standalone_factory(
        profile,
        StandaloneModelSettings.model_validate(settings()),
        object(),
        instance_id="test-inventory",
        inventory_ports=InventoryRuntimePorts(object(), object(), 1024),
        inventory_control=(object(), "tn_control"),
    )
    assert result is not None
    assert captured["research_enabled"] is False
    assert callable(captured["inventory_factory"])
    assert callable(captured["preparation_factory"])
