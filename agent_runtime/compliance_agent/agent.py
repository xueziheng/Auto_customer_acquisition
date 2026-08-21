"""受限合规巡检能力：只记录带原文证据的事后发现。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.guardrails.rails import guard_phase1_change_set
from shared.errors import ValidationError
from shared.schemas.identifiers import ChangeSetId, new_id

_MAX_OUTPUT_BYTES = 200_000
_MAX_TOTAL_SAMPLE_BYTES = 1_000_000
_AUDIT_KEYS = frozenset({"policy_version", "owner_ref", "checks", "samples"})
_SAMPLE_KEYS = frozenset(
    {"sample_id", "kind", "content", "occurred_at", "source_artifact_ref"}
)
_FINDING_KEYS = frozenset(
    {
        "sample_id",
        "check_id",
        "severity",
        "summary",
        "evidence_quote",
        "recommended_action",
    }
)
_SAMPLE_KINDS = frozenset(
    {"sent_message", "data_record", "image_asset", "policy_snapshot"}
)
_SEVERITIES = frozenset({"info", "warning", "critical"})
_CHECK_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")

_SYSTEM_PROMPT = """你是 TradeOS 的事后合规与数据质量巡检能力，不是事前闸门。
输入只包含已批准的抽样内容、检查项和政策版本。只输出一个 JSON 对象，唯一顶层键
为 findings，禁止 Markdown、解释或额外键。

每个 finding 字段必须精确为 sample_id, check_id, severity, summary,
evidence_quote, recommended_action。sample_id 与 check_id 只能引用输入；
evidence_quote 必须逐字来自对应样本。severity 只能是 info、warning 或 critical。
recommended_action 只是通知负责人的审查建议，禁止输出或调用业务动作。不得给概率、
置信度数字，不得补写样本中不存在的事实。没有发现时输出 {"findings": []}。
"""


@runtime_checkable
class ComplianceAuditModelPort(Protocol):
    """结构化巡检端口；业务写入、通知发送与凭证均不属于该端口。"""

    async def inspect_samples(
        self, *, system_prompt: str, audit_scope: dict[str, object]
    ) -> str: ...


def _text(value: object, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field} 无效")
    return value


def _utc_timestamp(value: object) -> str:
    raw = _text(value, field="巡检样本时间", maximum=64)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ValidationError("巡检样本时间无效") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValidationError("巡检样本时间无效")
    return raw


class ComplianceAgent(CapabilityAgent):
    """核验模型发现的检查项引用和逐字证据，再生成低风险巡检记录。"""

    name = "compliance_agent"

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        self._model = _text(model, field="合规巡检模型标识", maximum=128)
        if not isinstance(model_client, ComplianceAuditModelPort):
            raise ValidationError("合规巡检模型端口无效")
        if not isinstance(guardrails, CredentialMarkerGuard):
            raise ValidationError("合规巡检输入护栏无效")
        self._model_port = model_client
        self._gateway = gateway
        self._input_guard = guardrails

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        """巡检受限样本并返回零业务动作的 ``compliance_finding``。"""
        del context
        if not isinstance(task, AgentTask):
            raise ValidationError("合规巡检任务无效")
        try:
            projection = self._safe_projection(task)
            self._input_guard.check(
                subject="合规巡检样本",
                body=json.dumps(projection, ensure_ascii=False, sort_keys=True),
            )
        except ValidationError:
            return self._empty(task, "合规巡检输入被安全边界拒绝")
        try:
            raw = await self._model_port.inspect_samples(
                system_prompt=_SYSTEM_PROMPT,
                audit_scope=projection,
            )
            findings = self._validate_output(raw, projection)
        except ValidationError as exc:
            return self._empty(task, f"模型输出被护栏拦截：{exc}")
        samples = projection["samples"]
        assert isinstance(samples, tuple)
        sample_by_id = {str(sample["sample_id"]): sample for sample in samples}
        changes: list[dict[str, object]] = []
        for finding in findings:
            sample = sample_by_id[finding["sample_id"]]
            changes.append(
                {
                    "domain": "compliance",
                    "operation": "compliance_finding",
                    "payload": {
                        "sample_id": finding["sample_id"],
                        "sample_kind": sample["kind"],
                        "check_id": finding["check_id"],
                        "severity": finding["severity"],
                        "summary": finding["summary"],
                        "evidence_quote": finding["evidence_quote"],
                        "source_artifact_ref": sample["source_artifact_ref"],
                        "occurred_at": sample["occurred_at"],
                        "policy_version": projection["policy_version"],
                        "owner_ref": projection["owner_ref"],
                        "recommended_action": finding["recommended_action"],
                        "generated_by": self._model,
                    },
                    "risk_level": "low",
                }
            )
        candidate = ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=changes,
            summary=f"已生成 {len(findings)} 条带证据的合规巡检发现",
        )
        return guard_phase1_change_set(candidate)

    @staticmethod
    def _safe_projection(task: AgentTask) -> dict[str, object]:
        if set(task.inputs) != {"audit_scope"}:
            raise ValidationError("合规巡检任务输入无效")
        scope = task.inputs.get("audit_scope")
        if not isinstance(scope, dict) or set(scope) != _AUDIT_KEYS:
            raise ValidationError("合规巡检任务输入无效")
        raw_checks = scope.get("checks")
        if not isinstance(raw_checks, (list, tuple)) or not 1 <= len(raw_checks) <= 50:
            raise ValidationError("合规巡检检查项无效")
        checks = tuple(
            _text(value, field="合规巡检检查项", maximum=64)
            for value in raw_checks
        )
        if len(checks) != len(set(checks)) or any(
            _CHECK_ID.fullmatch(value) is None for value in checks
        ):
            raise ValidationError("合规巡检检查项无效")
        raw_samples = scope.get("samples")
        if not isinstance(raw_samples, (list, tuple)) or not 1 <= len(raw_samples) <= 100:
            raise ValidationError("合规巡检样本无效")
        samples: list[dict[str, str]] = []
        sample_ids: set[str] = set()
        total_bytes = 0
        for item in raw_samples:
            if not isinstance(item, dict) or set(item) != _SAMPLE_KEYS:
                raise ValidationError("合规巡检样本无效")
            sample_id = _text(
                item.get("sample_id"), field="合规巡检样本引用", maximum=200
            )
            if sample_id in sample_ids:
                raise ValidationError("合规巡检样本重复")
            sample_ids.add(sample_id)
            kind = _text(item.get("kind"), field="合规巡检样本类型", maximum=32)
            if kind not in _SAMPLE_KINDS:
                raise ValidationError("合规巡检样本类型无效")
            content = _text(
                item.get("content"), field="合规巡检样本内容", maximum=100_000
            )
            total_bytes += len(content.encode("utf-8"))
            if total_bytes > _MAX_TOTAL_SAMPLE_BYTES:
                raise ValidationError("合规巡检样本总量超限")
            samples.append(
                {
                    "sample_id": sample_id,
                    "kind": kind,
                    "content": content,
                    "occurred_at": _utc_timestamp(item.get("occurred_at")),
                    "source_artifact_ref": _text(
                        item.get("source_artifact_ref"),
                        field="合规巡检原始资料引用",
                        maximum=200,
                    ),
                }
            )
        return {
            "policy_version": _text(
                scope.get("policy_version"), field="合规政策版本", maximum=100
            ),
            "owner_ref": _text(
                scope.get("owner_ref"), field="合规发现负责人", maximum=200
            ),
            "checks": checks,
            "samples": tuple(samples),
        }

    @staticmethod
    def _validate_output(
        raw: str, projection: dict[str, object]
    ) -> tuple[dict[str, str], ...]:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValidationError("合规巡检模型输出无效")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("合规巡检模型输出不是合法 JSON") from None
        if not isinstance(payload, dict) or set(payload) != {"findings"}:
            raise ValidationError("合规巡检模型输出含未授权字段")
        raw_findings = payload.get("findings")
        if not isinstance(raw_findings, list) or len(raw_findings) > 500:
            raise ValidationError("合规巡检发现无效")
        samples = projection["samples"]
        checks = projection["checks"]
        assert isinstance(samples, tuple) and isinstance(checks, tuple)
        sample_by_id = {str(item["sample_id"]): item for item in samples}
        findings: list[dict[str, str]] = []
        identities: set[tuple[str, str, str]] = set()
        for item in raw_findings:
            if not isinstance(item, dict) or set(item) != _FINDING_KEYS:
                raise ValidationError("合规巡检模型输出含未授权字段")
            sample_id = _text(
                item.get("sample_id"), field="合规发现样本引用", maximum=200
            )
            check_id = _text(
                item.get("check_id"), field="合规发现检查项", maximum=64
            )
            if sample_id not in sample_by_id or check_id not in checks:
                raise ValidationError("合规发现引用越界")
            severity = _text(
                item.get("severity"), field="合规发现严重度", maximum=16
            )
            if severity not in _SEVERITIES:
                raise ValidationError("合规发现严重度无效")
            evidence_quote = _text(
                item.get("evidence_quote"), field="合规发现原文", maximum=4_000
            )
            content = sample_by_id[sample_id]["content"]
            if evidence_quote not in content:
                raise ValidationError("合规发现证据不是原文")
            identity = (sample_id, check_id, evidence_quote)
            if identity in identities:
                raise ValidationError("合规发现重复")
            identities.add(identity)
            findings.append(
                {
                    "sample_id": sample_id,
                    "check_id": check_id,
                    "severity": severity,
                    "summary": _text(
                        item.get("summary"), field="合规发现摘要", maximum=4_000
                    ),
                    "evidence_quote": evidence_quote,
                    "recommended_action": _text(
                        item.get("recommended_action"),
                        field="合规发现复核建议",
                        maximum=4_000,
                    ),
                }
            )
        return tuple(findings)

    @staticmethod
    def _empty(task: AgentTask, summary: str) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary=summary,
        )


__all__ = ("ComplianceAgent", "ComplianceAuditModelPort")
