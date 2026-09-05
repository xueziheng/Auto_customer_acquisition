from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_runtime.skill_router.router import SkillManifest
from shared.errors import ValidationError

INVALID_REGISTRY = "技能注册表无效"
NOT_READY = "技能注册表尚未就绪"
NOT_FOUND = "未找到指定技能"
INVALID_LIMIT = "技能数量上限必须是正整数"


def _router_type() -> type[Any]:
    try:
        from agent_runtime.skill_router.service import FileSkillRouter
    except ModuleNotFoundError:
        pytest.fail("FileSkillRouter 尚未实现")
    return FileSkillRouter


def _manifest(
    skill_id: str = "demand.infer_buyer_need",
    version: str = "1.0.0",
    *,
    triggers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "skill_id": skill_id,
        "version": version,
        "domain": skill_id.split(".", 1)[0],
        "description": f"{skill_id} 的测试技能",
        "triggers": triggers if triggers is not None else ["company_event_detected"],
        "inputs": {"company_profile": "object"},
        "outputs": {"hypotheses": "list"},
        "allowed_tools": ["web.search"],
        "blocked_tools": ["email.send"],
        "evidence_required": ["source_url"],
        "risk_level": "medium",
        "cost_class": "low",
        "freshness_days": 7,
        "prompt_ref": "prompt.md",
        "upstream_ref": "upstream_nexscope/demand.md",
        "eval_refs": ["evals/hypothesis/vague_signal_cases"],
        "evals": ["no_unsupported_claim"],
    }


def _write_manifest(
    canonical: Path,
    manifest: dict[str, Any],
    *,
    version_layout: bool = True,
    directory_skill_id: str | None = None,
    directory_version: str | None = None,
) -> Path:
    skill_id = directory_skill_id or str(manifest["skill_id"])
    version = directory_version or str(manifest["version"])
    skill_dir = canonical / skill_id
    manifest_dir = skill_dir / version if version_layout else skill_dir
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "prompt.md").write_text("受控 prompt", encoding="utf-8")
    path = manifest_dir / "manifest.yaml"
    path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def skill_registry_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    canonical = root / "canonical"
    _write_manifest(canonical, _manifest(version="1.0.0"))
    newer = _manifest(version="1.1.0")
    newer["description"] = "新版需求推断"
    newer["inputs"] = {"company_profile": "object", "public_evidence": "list"}
    newer["outputs"] = {"hypotheses": "list", "warnings": "list"}
    newer["evals"] = ["no_unsupported_claim", "evidence_attached"]
    _write_manifest(canonical, newer)
    return root


def _assert_invalid_load(root: Path) -> None:
    router = _router_type()()
    with pytest.raises(ValidationError, match=f"^{INVALID_REGISTRY}$") as caught:
        router.load_registry(str(root))
    assert caught.value.context == {}
    assert caught.value.__cause__ is None


def test_registry_selects_exact_trigger_and_preserves_old_version(
    skill_registry_dir: Path,
) -> None:
    router = _router_type()()

    assert router.load_registry(str(skill_registry_dir)) == 2
    assert router.get("demand.infer_buyer_need").version == "1.1.0"
    assert router.get("demand.infer_buyer_need", "1.0.0").version == "1.0.0"
    assert router.get("demand.infer_buyer_need") == SkillManifest(
        skill_id="demand.infer_buyer_need",
        version="1.1.0",
        domain="demand",
        description="新版需求推断",
        triggers=("company_event_detected",),
        inputs={"company_profile": "object", "public_evidence": "list"},
        outputs={"hypotheses": "list", "warnings": "list"},
        allowed_tools=("web.search",),
        blocked_tools=("email.send",),
        evidence_required=("source_url",),
        risk_level="medium",
        cost_class="low",
        freshness_days=7,
        prompt_ref="prompt.md",
        upstream_ref="upstream_nexscope/demand.md",
        eval_refs=("evals/hypothesis/vague_signal_cases",),
        evals=("no_unsupported_claim", "evidence_attached"),
    )
    assert [item.version for item in router.select("company_event_detected")] == [
        "1.1.0"
    ]
    assert router.select("unregistered_trigger") == []


def test_registry_accepts_canonical_root_and_loads_existing_manifests() -> None:
    router = _router_type()()
    project_root = Path(__file__).resolve().parents[2]

    assert router.load_registry(str(project_root / "skills" / "canonical")) == 3
    demand = router.get("demand.infer_buyer_need")
    assert demand.description == "依据企业公开信号推断可能的采购需求，生成需求假设"
    assert demand.upstream_ref is None
    assert demand.inputs == {
        "company_profile": "object",
        "public_evidence": "list",
    }
    assert demand.outputs == {"hypotheses": "list"}
    assert demand.evals == (
        "no_unsupported_claim",
        "evidence_attached",
        "fact_inference_separated",
        "no_probability_output",
    )


def test_registry_uses_semver_precedence_and_stable_build_tiebreak(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    versions = [
        "1.0.0-01a",
        "1.0.0-1alpha",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.0+abc",
        "1.0.0+xyz",
    ]
    router = _router_type()()
    observed: list[str] = []
    for version in versions:
        _write_manifest(root, _manifest(version=version))
        router.load_registry(str(root))
        observed.append(router.get("demand.infer_buyer_need").version)

    assert observed == versions
    assert router.get("demand.infer_buyer_need", "1.0.0+abc").version == ("1.0.0+abc")


def test_select_prefers_fewer_declared_triggers_then_skill_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    canonical = tmp_path / "canonical"
    _write_manifest(
        canonical,
        _manifest("demand.zeta", triggers=["shared", "secondary"]),
        version_layout=False,
    )
    _write_manifest(
        canonical,
        _manifest("demand.beta", triggers=["shared"]),
        version_layout=False,
    )
    _write_manifest(
        canonical,
        _manifest("demand.alpha", triggers=["shared"]),
        version_layout=False,
    )
    router = _router_type()()
    router.load_registry(str(canonical))
    caplog.set_level(logging.WARNING, logger="agent_runtime.skill_router.service")

    selected = router.select("shared", max_skills=2)

    assert [manifest.skill_id for manifest in selected] == [
        "demand.alpha",
        "demand.beta",
    ]
    assert [record.getMessage() for record in caplog.records] == [
        "匹配技能数量超过上限，已截断"
    ]


@pytest.mark.parametrize("max_skills", [0, -1, True, False, 1.0, "1", None])
def test_select_rejects_non_positive_or_non_integer_limits(
    skill_registry_dir: Path, max_skills: object
) -> None:
    router = _router_type()()
    router.load_registry(str(skill_registry_dir))

    with pytest.raises(ValidationError, match=f"^{INVALID_LIMIT}$"):
        router.select("company_event_detected", max_skills=max_skills)  # type: ignore[arg-type]


def test_get_and_select_are_not_available_before_first_successful_load() -> None:
    router = _router_type()()

    with pytest.raises(ValidationError, match=f"^{NOT_READY}$"):
        router.get("demand.infer_buyer_need")
    with pytest.raises(ValidationError, match=f"^{NOT_READY}$"):
        router.select("company_event_detected")


def test_get_rejects_unknown_skill_or_version(skill_registry_dir: Path) -> None:
    router = _router_type()()
    router.load_registry(str(skill_registry_dir))

    with pytest.raises(ValidationError, match=f"^{NOT_FOUND}$"):
        router.get("demand.unknown")
    with pytest.raises(ValidationError, match=f"^{NOT_FOUND}$"):
        router.get("demand.infer_buyer_need", "9.9.9")


def test_failed_reload_keeps_previous_complete_snapshot(
    skill_registry_dir: Path, tmp_path: Path
) -> None:
    router = _router_type()()
    router.load_registry(str(skill_registry_dir))
    invalid_root = tmp_path / "invalid" / "canonical"
    _write_manifest(invalid_root, _manifest("demand.replacement"))
    (invalid_root / "demand.replacement" / "1.0.0" / "manifest.yaml").write_text(
        "skill_id: [",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=f"^{INVALID_REGISTRY}$"):
        router.load_registry(str(invalid_root))

    assert router.get("demand.infer_buyer_need").version == "1.1.0"
    with pytest.raises(ValidationError, match=f"^{NOT_FOUND}$"):
        router.get("demand.replacement")


def test_returned_manifests_cannot_mutate_registry(skill_registry_dir: Path) -> None:
    router = _router_type()()
    router.load_registry(str(skill_registry_dir))

    returned = router.get("demand.infer_buyer_need")
    returned.inputs["company_profile"] = "tampered"
    returned.outputs["injected"] = "string"
    selected = router.select("company_event_detected")[0]
    selected.inputs.clear()

    preserved = router.get("demand.infer_buyer_need")
    assert preserved.inputs == {
        "company_profile": "object",
        "public_evidence": "list",
    }
    assert preserved.outputs == {"hypotheses": "list", "warnings": "list"}


def test_registry_rejects_duplicate_id_and_complete_version(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    _write_manifest(canonical, _manifest(), version_layout=False)
    _write_manifest(canonical, _manifest(), version_layout=True)

    _assert_invalid_load(canonical)


REQUIRED_FIELDS = (
    "skill_id",
    "version",
    "domain",
    "description",
    "triggers",
    "inputs",
    "outputs",
    "allowed_tools",
    "blocked_tools",
    "evidence_required",
    "risk_level",
    "cost_class",
    "prompt_ref",
    "eval_refs",
    "evals",
)


@pytest.mark.parametrize("missing_field", REQUIRED_FIELDS)
def test_registry_rejects_each_missing_required_field(
    tmp_path: Path, missing_field: str
) -> None:
    canonical = tmp_path / "canonical"
    manifest = _manifest()
    del manifest[missing_field]
    _write_manifest(
        canonical,
        manifest,
        directory_skill_id="demand.infer_buyer_need",
        directory_version="1.0.0",
    )

    _assert_invalid_load(canonical)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("skill_id", "Demand.Infer"),
        ("version", "1.0"),
        ("version", "01.0.0"),
        ("version", "1.0.0-alpha.01"),
        ("version", "1٢.0.0"),
        ("version", "1.0.0-1٢"),
        ("version", "v1.0.0"),
        ("domain", "sales"),
        ("description", 3),
        ("triggers", "company_event_detected"),
        ("triggers", [3]),
        ("inputs", []),
        ("inputs", {"company_profile": 3}),
        ("outputs", {3: "list"}),
        ("allowed_tools", ["Web.Search"]),
        ("allowed_tools", ["web/search"]),
        ("blocked_tools", "email.send"),
        ("evidence_required", [3]),
        ("risk_level", "critical"),
        ("cost_class", "unlimited"),
        ("freshness_days", 0),
        ("freshness_days", True),
        ("upstream_ref", None),
        ("eval_refs", []),
        ("eval_refs", [""]),
        ("eval_refs", ["tests/evals/example"]),
        ("eval_refs", ["evals/../outside"]),
        ("evals", "no_unsupported_claim"),
        ("evals", [3]),
    ],
)
def test_registry_rejects_invalid_schema_values(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    canonical = tmp_path / "canonical"
    manifest = _manifest()
    manifest[field] = bad_value
    _write_manifest(canonical, manifest)

    _assert_invalid_load(canonical)


def test_registry_rejects_unknown_fields(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    manifest = _manifest()
    manifest["unexpected"] = "value"
    _write_manifest(canonical, manifest)

    _assert_invalid_load(canonical)


@pytest.mark.parametrize(
    "prompt_ref",
    ["../prompt.md", "/tmp/prompt.md", "missing.md"],
)
def test_registry_rejects_prompt_outside_manifest_directory_or_not_regular(
    tmp_path: Path, prompt_ref: str
) -> None:
    canonical = tmp_path / "canonical"
    manifest = _manifest()
    manifest["prompt_ref"] = prompt_ref
    _write_manifest(canonical, manifest)

    _assert_invalid_load(canonical)


def test_registry_accepts_nested_prompt_inside_manifest_directory(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    manifest = _manifest()
    manifest["prompt_ref"] = "prompts/system.md"
    path = _write_manifest(canonical, manifest)
    nested_prompt = path.parent / "prompts" / "system.md"
    nested_prompt.parent.mkdir()
    nested_prompt.write_text("nested prompt", encoding="utf-8")
    router = _router_type()()

    assert router.load_registry(str(canonical)) == 1
    assert router.get("demand.infer_buyer_need").prompt_ref == "prompts/system.md"


def test_registry_rejects_prompt_symlink_outside_manifest_directory(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    manifest_path = _write_manifest(canonical, _manifest())
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    prompt = manifest_path.parent / "prompt.md"
    prompt.unlink()
    prompt.symlink_to(outside)

    _assert_invalid_load(canonical)


def test_registry_rejects_prompt_symlink_to_sibling_file(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    manifest_path = _write_manifest(canonical, _manifest())
    prompt = manifest_path.parent / "prompt.md"
    target = manifest_path.parent / "prompt-target.md"
    target.write_text("target", encoding="utf-8")
    prompt.unlink()
    prompt.symlink_to(target.name)

    _assert_invalid_load(canonical)


def test_registry_rejects_manifest_link_outside_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    manifest_path = _write_manifest(canonical, _manifest())
    outside = tmp_path / "outside.yaml"
    outside.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    manifest_path.unlink()
    manifest_path.symlink_to(outside)

    _assert_invalid_load(canonical)


def test_registry_rejects_unsupported_deep_manifest_layout(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    deep = canonical / "demand.infer_buyer_need" / "1.0.0" / "nested"
    deep.mkdir(parents=True)
    (deep / "manifest.yaml").write_text(
        yaml.safe_dump(_manifest(), allow_unicode=True), encoding="utf-8"
    )

    _assert_invalid_load(canonical)


def test_registry_rejects_manifest_at_canonical_root(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    _write_manifest(canonical, _manifest())
    (canonical / "manifest.yaml").write_text(
        yaml.safe_dump(_manifest(), allow_unicode=True), encoding="utf-8"
    )

    _assert_invalid_load(canonical)


@pytest.mark.parametrize("kind", ["duplicate", "unknown_tag", "recursive_alias"])
def test_registry_rejects_unsafe_or_ambiguous_yaml(tmp_path: Path, kind: str) -> None:
    canonical = tmp_path / "canonical"
    path = _write_manifest(canonical, _manifest())
    yaml_text = path.read_text(encoding="utf-8")
    if kind == "duplicate":
        yaml_text += "skill_id: demand.duplicate\n"
    elif kind == "unknown_tag":
        yaml_text = "\n".join(
            "description: !!python/object/apply:builtins.str [unsafe]"
            if line.startswith("description:")
            else line
            for line in yaml_text.splitlines()
        )
    else:
        yaml_text = yaml_text.replace(
            "triggers:\n- company_event_detected\n",
            "triggers: &loop [*loop]\n",
        )
    path.write_text(yaml_text, encoding="utf-8")

    _assert_invalid_load(canonical)


def test_registry_rejects_acyclic_alias_dag_with_bounded_work(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    path = _write_manifest(canonical, _manifest())
    levels = ["dag_0: &dag_0 [leaf, leaf]"]
    levels.extend(
        f"dag_{level}: &dag_{level} [*dag_{level - 1}, *dag_{level - 1}]"
        for level in range(1, 28)
    )
    path.write_text("\n".join(levels), encoding="utf-8")
    script = """
import sys
from agent_runtime.skill_router.service import FileSkillRouter
from shared.errors import ValidationError

try:
    FileSkillRouter().load_registry(sys.argv[1])
except ValidationError as error:
    raise SystemExit(0 if str(error) == "技能注册表无效" else 2)
raise SystemExit(3)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(canonical)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("acyclic alias DAG 校验超时")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, (stdout, stderr)


def test_registry_rejects_manifest_larger_than_read_limit(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    path = _write_manifest(canonical, _manifest())
    path.write_bytes(b"x" * 262_145)

    _assert_invalid_load(canonical)


def test_registry_rejects_empty_registry(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir(parents=True)

    _assert_invalid_load(canonical)


def test_registry_ignores_malformed_appledouble_entries(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    path = _write_manifest(canonical, _manifest())
    (canonical / "._manifest.yaml").write_text(
        "!!python/object/apply:builtins.eval [unsafe]", encoding="utf-8"
    )
    ignored_root = canonical / "._malformed"
    ignored_root.mkdir()
    (ignored_root / "manifest.yaml").write_text("invalid: [", encoding="utf-8")
    (path.parent / "._manifest.yaml").write_text("invalid: [", encoding="utf-8")
    ignored_nested = path.parent / "._nested"
    ignored_nested.mkdir()
    (ignored_nested / "manifest.yaml").write_text("invalid: [", encoding="utf-8")
    router = _router_type()()

    assert router.load_registry(str(canonical)) == 1
    assert router.get("demand.infer_buyer_need").version == "1.0.0"


def test_version_subdirectory_must_match_manifest_version(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    path = _write_manifest(canonical, _manifest(version="1.0.0"))
    wrong_dir = path.parent.parent / "2.0.0"
    path.parent.rename(wrong_dir)

    _assert_invalid_load(canonical)


def test_skill_directory_must_match_manifest_id(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    path = _write_manifest(canonical, _manifest())
    path.parent.parent.rename(canonical / "demand.other")

    _assert_invalid_load(canonical)


def test_manifest_load_does_not_require_eval_reference_to_exist(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    manifest = _manifest()
    manifest["eval_refs"] = ["evals/not_present/sample"]
    _write_manifest(canonical, manifest)
    router = _router_type()()

    assert router.load_registry(str(canonical)) == 1


def test_registry_accepts_semver_numeric_identifiers_without_length_limit(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    version = f"{'9' * 5_000}.0.0"
    _write_manifest(
        canonical,
        _manifest(version=version),
        version_layout=False,
    )
    router = _router_type()()

    assert router.load_registry(str(canonical)) == 1
    assert router.get("demand.infer_buyer_need").version == version


def test_skill_manifest_new_fields_have_compatibility_defaults() -> None:
    manifest = SkillManifest(
        skill_id="demand.compatible",
        version="1.0.0",
        domain="demand",
        triggers=(),
        allowed_tools=(),
        blocked_tools=(),
        evidence_required=(),
        risk_level="low",
        cost_class="free",
        prompt_ref="prompt.md",
    )

    assert manifest.description == ""
    assert manifest.upstream_ref is None
    assert manifest.evals == ()
