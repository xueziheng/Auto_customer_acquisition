"""联系人late-binding只能显式启用，不得替换规范服务。"""

from decimal import Decimal

import pytest

from apps.scheduler_worker.bootstrap import CanonicalSchedulerBootstrap
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy
from shared.errors import ValidationError
from shared.schemas.money import CurrencyCode, Money


def test_contact_factory_requires_contacts_and_campaign():
    policy = ScoringPolicy(
        "controlled",
        (Money(Decimal(1000), CurrencyCode("USD")),),
        {n: "low" for n in range(1, 8)},
    )
    with pytest.raises(ValidationError):
        CanonicalSchedulerBootstrap(
            policy, HandoffPolicy(3600, 10), contacts_factory=lambda *args: None
        )


def test_contact_factory_binds_exact_runtime_and_rejects_unbound_or_invalid_ports():
    from unittest.mock import Mock

    from agent_runtime.model_client import StructuredJsonModelClient
    from apps.scheduler_worker.bootstrap import ContactRuntimePorts
    from apps.scheduler_worker.contact_binding import ContactRuntimeResources
    from apps.scheduler_worker.runtime import SchedulerCoreServices
    from connectors.gmail.client import SecretResolver
    from connectors.gmail.transport import GmailHttpTransport
    from domains.outreach.service import OutreachService
    from workflows.account_discovery.ports import ContactEnricher, ContactVerifier

    policy = ScoringPolicy(
        "controlled",
        (Money(Decimal(1000), CurrencyCode("USD")),),
        {n: "low" for n in range(1, 8)},
    )
    core = Mock(spec=SchedulerCoreServices)
    core.employee_scope = Mock()
    core.demand = Mock()
    core.prospecting = Mock()
    outreach = Mock(spec=OutreachService)
    resources = Mock(spec=ContactRuntimeResources)
    ports = ContactRuntimePorts(
        Mock(spec=StructuredJsonModelClient),
        "controlled",
        ("KE",),
        Mock(spec=ContactEnricher),
        Mock(spec=ContactVerifier),
    )
    factory = Mock(return_value=ports)
    kwargs = {
        "contacts_enabled": True,
        "campaign_enabled": True,
        "gmail_transport": Mock(spec=GmailHttpTransport),
        "secret_resolver": Mock(spec=SecretResolver),
    }
    bootstrap = CanonicalSchedulerBootstrap(
        policy,
        HandoffPolicy(3600, 10),
        contacts_factory=factory,
        **kwargs,
    )
    with pytest.raises(ValidationError, match="未绑定"):
        bootstrap.build_contacts(core, None, resources=resources)
    factory.assert_not_called()
    result = bootstrap.build_contacts(core, outreach, resources=resources)
    factory.assert_called_once_with(core, outreach, resources)
    assert result.prospecting is core.prospecting
    assert result.enricher is ports.enricher
    assert result.verifier is ports.verifier
    factory.return_value = None
    with pytest.raises(ValidationError, match="返回无效"):
        bootstrap.build_contacts(core, outreach, resources=resources)
    with pytest.raises(ValidationError, match="工厂配置"):
        CanonicalSchedulerBootstrap(
            policy,
            HandoffPolicy(3600, 10),
            contacts=ports,
            contacts_factory=factory,
            **kwargs,
        )
    static = CanonicalSchedulerBootstrap(
        policy,
        HandoffPolicy(3600, 10),
        contacts=ports,
        **kwargs,
    )
    assert static.build_contacts(core, outreach, resources=resources) is None
