"""DemandSignal 持久化切片契约基座（2026-08-17 计划 Task 2；纯单元）。

RED 已确认：初版（静态 import）在 collection 阶段以 ``ImportError: cannot
import name 'DemandUnitOfWork'`` 终止（整文件不收集，工具 rc=2）——契约未实现。
现改用 importlib 动态解析（check_boundaries 的 domain-internals 规则禁止测试
静态 import 域内 repository；Task 1 同款 fallback）：模块级
``DemandUnitOfWork = _repository.DemandUnitOfWork`` 在属性缺失时同样于
**collection 阶段**以 AttributeError 失败（rc=2）。三个用例在实现后全部通过。
"""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime
from typing import Any, get_type_hints

import pytest

from domains.demand.schemas import SignalCaptureRequest
from infra.db.outbox import EVENT_REGISTRY
from shared.events.catalog import DemandSignalCaptured

_repository = importlib.import_module("domains.demand.repository")
DemandSignalRepository = _repository.DemandSignalRepository
NeedHypothesisRepository = _repository.NeedHypothesisRepository
ValidatedNeedRepository = _repository.ValidatedNeedRepository
DemandUnitOfWork = _repository.DemandUnitOfWork

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def test_signal_capture_request_requires_source_id_and_extracted_by() -> None:
    request = SignalCaptureRequest(
        signal_type="product_line_expansion",
        entity_name="Acme Manufacturing",
        raw_observation="Acme announced a new production facility.",
        observed_at=NOW,
        source_type="web_page",
        source_id="sha256:pagehash001",
        extracted_by="model-v1",
        source_url="https://example.com/acme",
        page_hash="sha256:pagehash001",
        possible_need="stainless steel hinges",
    )
    assert request.source_id == "sha256:pagehash001"
    assert request.extracted_by == "model-v1"
    # 必填性（GREEN 阶段执行）：省略任一必填字段都必须 TypeError——
    # 若实现给了默认值，以下断言即失败
    base: dict[str, Any] = {
        "signal_type": "product_line_expansion",
        "entity_name": "Acme Manufacturing",
        "raw_observation": "Acme announced a new production facility.",
        "observed_at": NOW,
        "source_type": "web_page",
    }
    with pytest.raises(TypeError):
        SignalCaptureRequest(**base)
    with pytest.raises(TypeError):
        SignalCaptureRequest(**base, source_id="sha256:pagehash001")
    with pytest.raises(TypeError):
        SignalCaptureRequest(**base, extracted_by="model-v1")


def test_repository_and_uow_protocol_shapes() -> None:
    """核心 Protocol 形状（独立复审 P2）：逐一精确断言参数名与协议成员。

    期望值全部手写（不由被测代码生成）；find_duplicate 必须精确为
    self, tenant_id, entity_name, signal_type, source_type, source_id
    （5 列来源身份，规格 §4；page_hash 可空 tuple 方案已否决）。
    """
    expected_params: dict[str, list[str]] = {
        "add": ["self", "signal"],
        "get": ["self", "tenant_id", "signal_id"],
        "find_duplicate": [
            "self",
            "tenant_id",
            "entity_name",
            "signal_type",
            "source_type",
            "source_id",
        ],
        "discard": ["self", "tenant_id", "signal_id", "reason"],
        "list_unlinked": ["self", "tenant_id", "limit"],
    }
    for method, expected in expected_params.items():
        signature = inspect.signature(getattr(DemandSignalRepository, method))
        assert list(signature.parameters) == expected, method

    protocol_attrs = set(
        getattr(DemandUnitOfWork, "__protocol_attrs__", ())
    )
    assert {"signals", "bus", "__aenter__", "__aexit__"} <= protocol_attrs


def test_demand_signal_captured_registered_in_event_registry() -> None:
    assert EVENT_REGISTRY["DemandSignalCaptured"] is DemandSignalCaptured


def test_hypothesis_and_need_repositories_gain_get_for_update() -> None:
    """契约纠偏（spec D7）：两个 repo 各增 get_for_update，参数名精确。"""
    expected = {
        "add": ["self", "hypothesis"],
        "get": ["self", "tenant_id", "hypothesis_id"],
        "update": ["self", "hypothesis"],
        "get_for_update": ["self", "tenant_id", "hypothesis_id"],
        "find_active_by_account_and_category": [
            "self",
            "tenant_id",
            "account_id",
            "category",
        ],
        "list_for_outreach": ["self", "tenant_id", "countries", "limit"],
    }
    for method, params in expected.items():
        signature = inspect.signature(getattr(NeedHypothesisRepository, method))
        assert list(signature.parameters) == params, method
    expected_need = {
        "add": ["self", "need"],
        "get": ["self", "tenant_id", "need_id"],
        "update": ["self", "need"],
        "get_for_update": ["self", "tenant_id", "need_id"],
        "append_field_history": [
            "self",
            "tenant_id",
            "need_id",
            "field_name",
            "old_value",
            "new_value",
            "source_message_id",
            "changed_by",
        ],
        "list_sourcing_ready": ["self", "tenant_id", "limit"],
        "list_by_account": ["self", "tenant_id", "account_id"],
    }
    for method, params in expected_need.items():
        signature = inspect.signature(getattr(ValidatedNeedRepository, method))
        assert list(signature.parameters) == params, method
    # P1-1/P2-8：返回契约锁死——add 必须 bool；get_for_update 必须含 None 的联合注解
    assert get_type_hints(NeedHypothesisRepository.add)["return"] is bool
    assert "None" in str(
        get_type_hints(NeedHypothesisRepository.get_for_update)["return"]
    )


def test_demand_uow_protocol_gains_hypotheses_and_needs() -> None:
    """UoW 协议成员（spec D12）：signals/hypotheses/needs/bus，无 clusters。"""
    attrs = set(getattr(DemandUnitOfWork, "__protocol_attrs__", ()))
    assert {"signals", "hypotheses", "needs", "bus", "__aenter__", "__aexit__"} <= attrs
    # spec D12 / Phase 1：UoW 不得有 clusters 成员（NeedCluster 整体非本片范围）
    assert "clusters" not in attrs
