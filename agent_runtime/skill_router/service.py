"""从受控文件目录加载技能 manifest 的确定性路由实现。"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from yaml.nodes import MappingNode

from agent_runtime.skill_router.router import SkillManifest
from shared.errors import ValidationError

_LOGGER = logging.getLogger(__name__)

_INVALID_REGISTRY = "技能注册表无效"
_NOT_READY = "技能注册表尚未就绪"
_NOT_FOUND = "未找到指定技能"
_INVALID_LIMIT = "技能数量上限必须是正整数"
_TRUNCATED = "匹配技能数量超过上限，已截断"

_MANIFEST_NAME = "manifest.yaml"
_MANIFEST_LIMIT = 262_144
_SKILL_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_TOOL_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_DOMAINS = frozenset(
    {
        "demand",
        "prospecting",
        "outreach",
        "qualification",
        "sourcing",
        "costing",
        "team",
        "compliance",
    }
)
_RISK_LEVELS = frozenset({"low", "medium", "high"})
_COST_CLASSES = frozenset({"free", "low", "medium", "high"})
_REQUIRED_FIELDS = frozenset(
    {
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
    }
)
_OPTIONAL_FIELDS = frozenset({"freshness_days", "upstream_ref"})
_EXCLUDED_CANONICAL_NAMES = frozenset(
    {"manifests", "schema", "schemas", "upstream", "upstream_nexscope"}
)


class _RegistryInvalid(Exception):
    """内部校验失败；消息永不跨过公开边界。"""


class _UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader 加重复映射键拒绝。"""


def _construct_unique_mapping(
    loader: _UniqueSafeLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise _RegistryInvalid from error
        if duplicate:
            raise _RegistryInvalid
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class _ParsedVersion:
    major: str
    minor: str
    patch: str
    prerelease: tuple[str, ...] | None


def _parse_version(version: str) -> _ParsedVersion:
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise _RegistryInvalid
    prerelease = match.group(4)
    return _ParsedVersion(
        major=match.group(1),
        minor=match.group(2),
        patch=match.group(3),
        prerelease=tuple(prerelease.split(".")) if prerelease is not None else None,
    )


def _compare_prerelease(
    left: tuple[str, ...] | None, right: tuple[str, ...] | None
) -> int:
    if left is None:
        return 0 if right is None else 1
    if right is None:
        return -1
    for left_part, right_part in zip(left, right, strict=False):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            left_number = (len(left_part), left_part)
            right_number = (len(right_part), right_part)
            return 1 if left_number > right_number else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_part > right_part else -1
    return (len(left) > len(right)) - (len(left) < len(right))


def _compare_versions(left: str, right: str) -> int:
    left_version = _parse_version(left)
    right_version = _parse_version(right)
    left_core = tuple(
        (len(identifier), identifier)
        for identifier in (left_version.major, left_version.minor, left_version.patch)
    )
    right_core = tuple(
        (len(identifier), identifier)
        for identifier in (
            right_version.major,
            right_version.minor,
            right_version.patch,
        )
    )
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    precedence = _compare_prerelease(left_version.prerelease, right_version.prerelease)
    if precedence:
        return precedence
    return (left > right) - (left < right)


def _ensure_acyclic(value: object, active: set[int] | None = None) -> None:
    if not isinstance(value, (dict, list)):
        return
    active_ids = active if active is not None else set()
    value_id = id(value)
    if value_id in active_ids:
        raise _RegistryInvalid
    active_ids.add(value_id)
    children: Sequence[object]
    if isinstance(value, dict):
        children = [*value.keys(), *value.values()]
    else:
        children = value
    for child in children:
        _ensure_acyclic(child, active_ids)
    active_ids.remove(value_id)


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _resolve_inside(path: Path, parent: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not _is_within(resolved, parent):
        raise _RegistryInvalid
    return resolved


def _canonical_dir(skills_dir: str) -> Path:
    requested = Path(skills_dir)
    if requested.name == "canonical":
        canonical = requested
    else:
        canonical = requested / "canonical"
    resolved = canonical.resolve(strict=True)
    if not resolved.is_dir():
        raise _RegistryInvalid
    return resolved


def _manifest_paths(canonical: Path) -> list[tuple[Path, str, str | None]]:
    candidates: list[tuple[Path, str, str | None]] = []
    for raw_skill_dir in sorted(canonical.iterdir(), key=lambda item: item.name):
        if raw_skill_dir.name.startswith("._"):
            continue
        if raw_skill_dir.name in _EXCLUDED_CANONICAL_NAMES:
            continue
        if not raw_skill_dir.is_dir():
            if raw_skill_dir.name == _MANIFEST_NAME:
                raise _RegistryInvalid
            continue
        skill_dir = _resolve_inside(raw_skill_dir, canonical)
        direct_manifest = skill_dir / _MANIFEST_NAME
        found = False
        if direct_manifest.exists():
            candidates.append(
                (_resolve_inside(direct_manifest, canonical), raw_skill_dir.name, None)
            )
            found = True
        for raw_child in sorted(skill_dir.iterdir(), key=lambda item: item.name):
            if raw_child.name.startswith("._") or not raw_child.is_dir():
                continue
            version_dir = _resolve_inside(raw_child, canonical)
            version_manifest = version_dir / _MANIFEST_NAME
            if not version_manifest.exists():
                raise _RegistryInvalid
            if any(
                child.is_dir() and not child.name.startswith("._")
                for child in version_dir.iterdir()
            ):
                raise _RegistryInvalid
            candidates.append(
                (
                    _resolve_inside(version_manifest, canonical),
                    raw_skill_dir.name,
                    raw_child.name,
                )
            )
            found = True
        if not found:
            raise _RegistryInvalid
    return candidates


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise _RegistryInvalid
    with path.open("rb") as stream:
        content = stream.read(_MANIFEST_LIMIT + 1)
    if len(content) > _MANIFEST_LIMIT:
        raise _RegistryInvalid
    parsed = yaml.load(content.decode("utf-8"), Loader=_UniqueSafeLoader)
    _ensure_acyclic(parsed)
    if type(parsed) is not dict:
        raise _RegistryInvalid
    return parsed


def _string(value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise _RegistryInvalid
    return value


def _string_list(value: object, *, require_nonempty: bool = False) -> tuple[str, ...]:
    if type(value) is not list or (require_nonempty and not value):
        raise _RegistryInvalid
    values = tuple(_string(item) for item in value)
    return values


def _string_map(value: object) -> dict[str, str]:
    if type(value) is not dict:
        raise _RegistryInvalid
    result: dict[str, str] = {}
    for key, item in value.items():
        result[_string(key)] = _string(item)
    return result


def _validate_prompt_ref(value: object, manifest_dir: Path) -> str:
    prompt_ref = _string(value)
    if "\\" in prompt_ref:
        raise _RegistryInvalid
    relative = PurePosixPath(prompt_ref)
    if (
        relative.is_absolute()
        or len(relative.parts) != 1
        or relative.parts[0] in {".", ".."}
    ):
        raise _RegistryInvalid
    unresolved_prompt = manifest_dir / prompt_ref
    if unresolved_prompt.is_symlink():
        raise _RegistryInvalid
    prompt = unresolved_prompt.resolve(strict=True)
    if prompt.parent != manifest_dir.resolve(strict=True) or not prompt.is_file():
        raise _RegistryInvalid
    return prompt_ref


def _validate_eval_refs(value: object) -> tuple[str, ...]:
    refs = _string_list(value, require_nonempty=True)
    for ref in refs:
        if "\\" in ref or ref.startswith("/"):
            raise _RegistryInvalid
        parts = ref.split("/")
        if (
            len(parts) < 2
            or parts[0] != "evals"
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise _RegistryInvalid
    return refs


def _manifest_from_data(
    data: Mapping[str, Any],
    *,
    manifest_dir: Path,
    directory_skill_id: str,
    directory_version: str | None,
) -> SkillManifest:
    keys = frozenset(data)
    if not _REQUIRED_FIELDS <= keys or keys - _REQUIRED_FIELDS - _OPTIONAL_FIELDS:
        raise _RegistryInvalid

    skill_id = _string(data["skill_id"])
    if _SKILL_ID.fullmatch(skill_id) is None or skill_id != directory_skill_id:
        raise _RegistryInvalid
    version = _string(data["version"])
    _parse_version(version)
    if directory_version is not None and version != directory_version:
        raise _RegistryInvalid

    domain = _string(data["domain"])
    if domain not in _DOMAINS or skill_id.split(".", 1)[0] != domain:
        raise _RegistryInvalid
    description = _string(data["description"])
    allowed_tools = _string_list(data["allowed_tools"])
    blocked_tools = _string_list(data["blocked_tools"])
    if any(
        _TOOL_ID.fullmatch(tool_id) is None
        for tool_id in (*allowed_tools, *blocked_tools)
    ):
        raise _RegistryInvalid

    freshness_days = data.get("freshness_days", 7)
    if type(freshness_days) is not int or freshness_days <= 0:
        raise _RegistryInvalid
    upstream_ref = _string(data["upstream_ref"]) if "upstream_ref" in data else None

    return SkillManifest(
        skill_id=skill_id,
        version=version,
        domain=domain,
        triggers=_string_list(data["triggers"]),
        inputs=_string_map(data["inputs"]),
        outputs=_string_map(data["outputs"]),
        allowed_tools=allowed_tools,
        blocked_tools=blocked_tools,
        evidence_required=_string_list(data["evidence_required"]),
        risk_level=_enum_string(data["risk_level"], _RISK_LEVELS),
        cost_class=_enum_string(data["cost_class"], _COST_CLASSES),
        freshness_days=freshness_days,
        prompt_ref=_validate_prompt_ref(data["prompt_ref"], manifest_dir),
        eval_refs=_validate_eval_refs(data["eval_refs"]),
        description=description,
        upstream_ref=upstream_ref,
        evals=_string_list(data["evals"]),
    )


def _enum_string(value: object, allowed: frozenset[str]) -> str:
    result = _string(value)
    if result not in allowed:
        raise _RegistryInvalid
    return result


class FileSkillRouter:
    """只从本地 canonical 目录加载并精确选择技能。"""

    def __init__(self) -> None:
        self._registry: dict[tuple[str, str], SkillManifest] = {}
        self._ready = False

    def load_registry(self, skills_dir: str) -> int:
        """校验并原子替换文件注册表；失败不改变旧快照。"""
        try:
            canonical = _canonical_dir(skills_dir)
            loaded: dict[tuple[str, str], SkillManifest] = {}
            paths = _manifest_paths(canonical)
            if not paths:
                raise _RegistryInvalid
            for path, directory_skill_id, directory_version in paths:
                data = _load_yaml(path)
                manifest = _manifest_from_data(
                    data,
                    manifest_dir=path.parent,
                    directory_skill_id=directory_skill_id,
                    directory_version=directory_version,
                )
                key = (manifest.skill_id, manifest.version)
                if key in loaded:
                    raise _RegistryInvalid
                loaded[key] = manifest
        # 此处是配置解析的公开边界，必须屏蔽路径、YAML 与系统异常文本。
        except Exception:  # noqa: BLE001
            raise ValidationError(_INVALID_REGISTRY) from None

        self._registry = loaded
        self._ready = True
        return len(loaded)

    def select(self, trigger: str, *, max_skills: int = 3) -> list[SkillManifest]:
        """从每个技能的最新版本中按 trigger 精确选择。"""
        self._require_ready()
        if type(max_skills) is not int or max_skills <= 0:
            raise ValidationError(_INVALID_LIMIT)

        latest = [self._latest(skill_id) for skill_id in self._skill_ids()]
        matching = [manifest for manifest in latest if trigger in manifest.triggers]
        matching.sort(key=lambda manifest: (len(manifest.triggers), manifest.skill_id))
        if len(matching) > max_skills:
            _LOGGER.warning(_TRUNCATED)
        return copy.deepcopy(matching[:max_skills])

    def get(self, skill_id: str, version: str | None = None) -> SkillManifest:
        """取得指定完整版本，省略版本时返回最高 SemVer。"""
        self._require_ready()
        if version is None:
            if skill_id not in self._skill_ids():
                raise ValidationError(_NOT_FOUND)
            return copy.deepcopy(self._latest(skill_id))
        manifest = self._registry.get((skill_id, version))
        if manifest is None:
            raise ValidationError(_NOT_FOUND)
        return copy.deepcopy(manifest)

    def _require_ready(self) -> None:
        if not self._ready:
            raise ValidationError(_NOT_READY)

    def _skill_ids(self) -> set[str]:
        return {skill_id for skill_id, _version in self._registry}

    def _latest(self, skill_id: str) -> SkillManifest:
        versions = [
            version
            for registered_skill_id, version in self._registry
            if registered_skill_id == skill_id
        ]
        latest_version = max(versions, key=cmp_to_key(_compare_versions))
        return self._registry[(skill_id, latest_version)]
