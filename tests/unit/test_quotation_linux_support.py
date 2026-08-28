"""构建白名单契约；不替代真实受限Linux工厂链。"""

import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration import quote_evidence_linux_support as support


@pytest.mark.parametrize("quotation", [True, False])
def test_runtime_image_uses_explicit_b2_whitelist_without_changing_a_chain(
    monkeypatch, quotation
):
    seen = {}

    def build(**kwargs):
        with tarfile.open(fileobj=kwargs["fileobj"]) as archive:
            seen["names"] = archive.getnames()
        seen["target"] = kwargs["target"]
        return SimpleNamespace(id="sha256:" + "a" * 64), []

    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda _: object(), build=build), close=lambda: None
    )
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    kwargs = {"chain": True, "quotation": True} if quotation else {"chain": True}
    support.build_parser_image("sha256:" + "b" * 64, **kwargs)
    assert seen["target"] == ("quotation" if quotation else "chain")
    assert ("apps/api/composition/runtime.py" in seen["names"]) is quotation
    assert ("apps/scheduler_worker/runtime.py" in seen["names"]) is quotation
    assert ("connectors/object_store/quote_pdf.py" in seen["names"]) is quotation
    assert (
        "tests/integration/quotation_runtime_linux_cases.py" in seen["names"]
    ) is quotation
    assert not any(
        ".env" in name or ".git" in name or name.startswith(("apps/web/", "tmp/"))
        for name in seen["names"]
    )


@pytest.mark.parametrize(
    "kwargs", [{"quotation": True}, {"chain": True, "quotation": True}]
)
def test_quotation_image_requires_chain_and_explicit_accepted_base(monkeypatch, kwargs):
    def forbidden():
        pytest.fail("无效构建请求不得访问Docker")

    monkeypatch.setattr(support.docker, "from_env", forbidden)
    with pytest.raises(ValueError):
        support.build_parser_image(**kwargs)


def test_original_stages_keep_fixed_a_entrypoints_and_b2_has_separate_entry():
    dockerfile = (
        Path(__file__).parents[1] / "fixtures/quote_evidence/linux/Dockerfile"
    ).read_text()
    stages = {}
    for block in dockerfile.split("\nFROM ")[1:]:
        header, body = block.split("\n", 1)
        stages[header.split(" AS ")[1]] = body
    assert (
        'CMD ["python", "-m", "tests.integration.quote_evidence_linux_support"]'
        in stages["chain"]
    )
    assert (
        'CMD ["python", "-m", "tests.integration.quotation_runtime_linux_support"]'
        in stages["quotation"]
    )
    for stage in ("parser", "refreshed"):
        assert (
            '"tests.integration.quotation_runtime_linux_support"' not in stages[stage]
        )
        assert "/tests/integration/evidence_parser_linux_cases.py" in stages[stage]
