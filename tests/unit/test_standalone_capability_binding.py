"""显式独立研究接线不得被旧 pilot guard 重复拒绝；残缺接线继续关闭。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from apps.scheduler_worker.bootstrap import CanonicalSchedulerBootstrap
from apps.scheduler_worker.config import SchedulerWorkerConfig
from apps.scheduler_worker.pilot import UnconfiguredDnsStep
from apps.scheduler_worker.runtime import SchedulerRuntimeFactory
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy
from shared.errors import ValidationError
from shared.schemas.money import CurrencyCode, Money
from tests.unit.test_pilot_profile import make_config


def unused_factory(*args):
    pytest.fail("构造 scheduler 不得调用能力工厂")


def bootstrap():
    return CanonicalSchedulerBootstrap(
        ScoringPolicy(
            "test",
            (Money(Decimal(1000), CurrencyCode("USD")),),
            {i: "high" for i in range(1, 8)},
        ),
        HandoffPolicy(3600, 10),
        research_enabled=True,
        research_factory=unused_factory,
        assistant_factory=unused_factory,
    )


def build(profile, composition, *, enabled=True):
    env = profile.runtime_environment()
    return SchedulerRuntimeFactory(
        env,
        pilot_config=SchedulerWorkerConfig.from_pilot_environ(env),
        standalone_research=enabled,
        secret_resolver=profile,
        unconfigured_dns_step=UnconfiguredDnsStep(),
        bootstrap=composition,
    )


def test_explicit_standalone_research_does_not_hit_legacy_pilot_rejection(tmp_path):
    assert isinstance(
        build(make_config(tmp_path), bootstrap()), SchedulerRuntimeFactory
    )


@pytest.mark.parametrize(
    "case", ["legacy", "missing_assistant", "missing_research", "contacts", "gmail"]
)
def test_legacy_pilot_or_partial_research_still_rejected(tmp_path, case):
    profile = make_config(tmp_path)
    composition = bootstrap()
    with pytest.raises(ValidationError):
        if case == "missing_assistant":
            composition = replace(composition, assistant_factory=None)
        elif case == "missing_research":
            composition = replace(composition, research_factory=None)
        elif case == "contacts":
            composition = replace(composition, contacts_enabled=True)
        elif case == "gmail":
            from infra.pilot.config import PilotGmailConfig
            from shared.schemas.identifiers import new_id

            profile = profile.model_copy(
                update={
                    "gmail": PilotGmailConfig(
                        address="controlled@example.test",
                        credentials_file=tmp_path / "absent.json",
                        employee_id=new_id("emp"),
                    )
                }
            )
        build(profile, composition, enabled=case != "legacy")
