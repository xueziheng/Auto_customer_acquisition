"""父进程构造必须关闭、非Linux不得spawn、参数严格。"""

import pytest

from shared.schemas.evidence_read import EvidenceProbeLimits, QuoteEvidenceError
from tests.unit.test_evidence_text_profiles import parse_limits


def probe_limits(**changes):
    return EvidenceProbeLimits(
        **{
            "cpu_seconds": 1,
            "address_space_bytes": 67108864,
            "wall_timeout_ms": 5000,
            "allocation_chunk_bytes": 1048576,
            "maximum_probe_bytes": 100663296,
            "maximum_result_bytes": 65536,
            "termination_grace_ms": 200,
            **changes,
        }
    )


async def test_parser_starts_closed_and_refuses_content_before_probe():
    from connectors.evidence_text.client import LinuxEvidenceTextParser

    parser = LinuxEvidenceTextParser(limits=parse_limits(), probe_limits=probe_limits())
    assert parser.capability().status == "unavailable"
    with pytest.raises(QuoteEvidenceError) as caught:
        await parser.parse(
            b"Content-Type: text/plain\n\ncontrolled",
            profile="rfc822-plain-v1",
            page=None,
        )
    assert caught.value.code == "parse_unavailable"
    await parser.aclose()


async def test_non_linux_probe_never_spawns(monkeypatch):
    from connectors.evidence_text import client

    monkeypatch.setattr(client.platform, "system", lambda: "Darwin")

    async def forbidden(*args, **kwargs):
        pytest.fail("非Linux不得启动子进程")

    monkeypatch.setattr(client.asyncio, "create_subprocess_exec", forbidden)
    parser = client.LinuxEvidenceTextParser(
        limits=parse_limits(), probe_limits=probe_limits()
    )
    report = await parser.probe()
    assert report.status == "unavailable" and report.failure == "platform"


@pytest.mark.parametrize("value", [True, 0, -1, "1"])
def test_parse_and_probe_limits_are_strict(value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_limits(cpu_seconds=value)
    with pytest.raises(ValidationError):
        probe_limits(maximum_probe_bytes=value)
