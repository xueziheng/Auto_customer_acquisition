"""受限员工工作提取能力：只形成带原文证据的待确认变更。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.guardrails.rails import guard_phase1_change_set
from domains.demand.service import mutable_need_field_names
from shared.errors import ValidationError
from shared.schemas.identifiers import ChangeSetId, new_id

_MAX_OUTPUT_BYTES = 500_000
_MAX_CONTENT_BYTES = 1_000_000
_UPLOAD_KEYS = frozenset(
    {
        "upload_id",
        "artifact_ref",
        "source_message_id",
        "employee_id",
        "account_id",
        "opportunity_id",
        "need_id",
        "source_kind",
        "source_content",
        "occurred_at",
        "customer_timezone",
    }
)
_OUTPUT_KEYS = frozenset(
    {"facts", "need_field_updates", "commitments", "progress_note"}
)
_FACT_KEYS = frozenset({"fact_type", "value", "evidence_quote"})
_NEED_FIELD_KEYS = frozenset({"field_name", "value", "evidence_quote"})
_COMMITMENT_KEYS = frozenset(
    {"commitment_type", "action", "due_at", "due_at_uncertain", "verbatim"}
)
_PROGRESS_KEYS = frozenset({"summary", "evidence_quotes"})
_SOURCE_KINDS = frozenset(
    {
        "chat_transcript",
        "email_text",
        "pdf_text",
        "spreadsheet_text",
        "audio_transcript",
        "image_ocr",
    }
)
_FACT_TYPES = frozenset(
    {"customer_statement", "employee_statement", "customer_reaction", "activity"}
)
_COMMITMENT_TYPES = frozenset({"employee", "customer"})
_ARTIFACT_REF = re.compile(r"art_[0-9A-HJKMNP-TV-Z]{26}")
_CURRENCY = re.compile(r"[A-Z]{3}")

_SYSTEM_PROMPT = """你是 TradeOS 的员工工作提取能力。输入来自已进入 Artifact Store
的员工上传原件。你只能提取原文明确支持的事实、已验证需求字段、双方承诺和进展摘要；
每项必须带逐字存在于 source_content 的 evidence_quote 或 verbatim。禁止补写事实、概率、
置信度、业务动作或客户可见内容。

只输出一个 JSON 对象，顶层键必须精确为 facts, need_field_updates, commitments,
progress_note。承诺的 due_at 必须依据 occurred_at 与 customer_timezone 解析为带时区的绝对
ISO 8601 时间；无法可靠解析时仍给出待确认时间并将 due_at_uncertain 设为 true。所有结果
都是待员工确认草稿，不能触发提醒、需求更新、商机推进或发送动作。
"""


@runtime_checkable
class TeamWorkExtractionModelPort(Protocol):
    """结构化工作提取端口；原件存取、确认和业务写入不属于该端口。"""

    async def extract_work(
        self, *, system_prompt: str, upload: dict[str, object]
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


def _content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("员工工作原文无效")
    if len(value.encode("utf-8")) > _MAX_CONTENT_BYTES or any(
        (ord(character) < 32 and character not in "\t\n\r")
        or ord(character) == 127
        for character in value
    ):
        raise ValidationError("员工工作原文无效")
    return value


def _aware_timestamp(value: object, *, field: str) -> str:
    raw = _text(value, field=field, maximum=64)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ValidationError(f"{field}必须是绝对时间") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field}必须是绝对时间")
    return raw


def _optional_ref(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, maximum=200)


def _need_value(field_name: str, value: object) -> object:
    if field_name == "quantity":
        raw = _text(value, field="需求数量", maximum=40)
        if not raw.isascii() or not raw.isdigit() or int(raw) < 0:
            raise ValidationError("需求数量无效")
        return raw
    if field_name == "required_by":
        raw = _text(value, field="需求日期", maximum=10)
        try:
            datetime.fromisoformat(raw)
        except ValueError:
            raise ValidationError("需求日期无效") from None
        return raw
    if field_name == "target_price":
        if not isinstance(value, dict) or set(value) != {"amount", "currency"}:
            raise ValidationError("目标价格无效")
        amount = _text(value.get("amount"), field="目标价格金额", maximum=100)
        currency = _text(value.get("currency"), field="目标价格币种", maximum=3)
        try:
            parsed_amount = Decimal(amount)
        except InvalidOperation:
            raise ValidationError("目标价格无效") from None
        if not parsed_amount.is_finite() or parsed_amount < 0:
            raise ValidationError("目标价格无效")
        if _CURRENCY.fullmatch(currency) is None:
            raise ValidationError("目标价格无效")
        return {"amount": amount, "currency": currency}
    return _text(value, field="需求字段值", maximum=4_000)


class TeamOperationsAgent(CapabilityAgent):
    """从受信原件投影提取证据，并强制所有结果等待员工确认。"""

    name = "team_operations"

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        self._model = _text(model, field="员工工作模型标识", maximum=128)
        if not isinstance(model_client, TeamWorkExtractionModelPort):
            raise ValidationError("员工工作提取模型端口无效")
        if not isinstance(guardrails, CredentialMarkerGuard):
            raise ValidationError("员工工作输入护栏无效")
        self._model_port = model_client
        self._gateway = gateway
        self._input_guard = guardrails

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        """生成待确认提取变更；任何边界失败均返回安全空变更集。"""
        del context
        if not isinstance(task, AgentTask):
            raise ValidationError("员工工作提取任务无效")
        try:
            projection = self._safe_projection(task)
            self._input_guard.check(
                subject="员工工作上传",
                body=str(projection["source_content"]),
            )
        except ValidationError:
            return self._empty(task, "员工工作输入被安全边界拒绝")
        try:
            raw = await self._model_port.extract_work(
                system_prompt=_SYSTEM_PROMPT,
                upload=projection,
            )
            output = self._validate_output(raw, projection)
        except ValidationError as exc:
            return self._empty(task, f"模型输出被护栏拦截：{exc}")
        candidate = ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=self._build_changes(projection, output),
            summary="已生成带原文证据的员工工作提取草稿，等待员工确认",
        )
        return guard_phase1_change_set(candidate)

    @staticmethod
    def _safe_projection(task: AgentTask) -> dict[str, object]:
        if set(task.inputs) != {"work_upload"}:
            raise ValidationError("员工工作提取任务输入无效")
        upload = task.inputs.get("work_upload")
        if not isinstance(upload, dict) or set(upload) != _UPLOAD_KEYS:
            raise ValidationError("员工工作提取任务输入无效")
        artifact_ref = _text(
            upload.get("artifact_ref"), field="员工工作原件引用", maximum=200
        )
        if _ARTIFACT_REF.fullmatch(artifact_ref) is None:
            raise ValidationError("员工工作原件引用无效")
        source_kind = _text(
            upload.get("source_kind"), field="员工工作来源类型", maximum=40
        )
        if source_kind not in _SOURCE_KINDS:
            raise ValidationError("员工工作来源类型无效")
        timezone = _text(
            upload.get("customer_timezone"), field="客户时区", maximum=100
        )
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError("客户时区无效") from None
        return {
            "upload_id": _text(
                upload.get("upload_id"), field="员工工作上传引用", maximum=200
            ),
            "artifact_ref": artifact_ref,
            "source_message_id": _text(
                upload.get("source_message_id"),
                field="员工工作消息引用",
                maximum=200,
            ),
            "employee_id": _text(
                upload.get("employee_id"), field="员工引用", maximum=200
            ),
            "account_id": _optional_ref(
                upload.get("account_id"), field="客户企业引用"
            ),
            "opportunity_id": _optional_ref(
                upload.get("opportunity_id"), field="贸易机会引用"
            ),
            "need_id": _optional_ref(upload.get("need_id"), field="需求引用"),
            "source_kind": source_kind,
            "source_content": _content(upload.get("source_content")),
            "occurred_at": _aware_timestamp(
                upload.get("occurred_at"), field="员工工作发生时间"
            ),
            "customer_timezone": timezone,
        }

    @staticmethod
    def _validate_output(
        raw: str, projection: dict[str, object]
    ) -> dict[str, object]:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValidationError("员工工作模型输出无效")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("员工工作模型输出不是合法 JSON") from None
        if not isinstance(payload, dict) or set(payload) != _OUTPUT_KEYS:
            raise ValidationError("员工工作模型输出含未授权字段")
        content = projection["source_content"]
        assert isinstance(content, str)

        raw_facts = payload.get("facts")
        if not isinstance(raw_facts, list) or len(raw_facts) > 200:
            raise ValidationError("员工工作事实无效")
        facts: list[dict[str, str]] = []
        for item in raw_facts:
            if not isinstance(item, dict) or set(item) != _FACT_KEYS:
                raise ValidationError("员工工作模型输出含未授权字段")
            fact_type = _text(
                item.get("fact_type"), field="员工工作事实类型", maximum=40
            )
            if fact_type not in _FACT_TYPES:
                raise ValidationError("员工工作事实类型无效")
            quote = TeamOperationsAgent._verbatim(
                item.get("evidence_quote"), content
            )
            facts.append(
                {
                    "fact_type": fact_type,
                    "value": _text(
                        item.get("value"), field="员工工作事实", maximum=8_000
                    ),
                    "evidence_quote": quote,
                }
            )

        raw_fields = payload.get("need_field_updates")
        if not isinstance(raw_fields, list) or len(raw_fields) > 50:
            raise ValidationError("需求字段提取无效")
        allowed_fields = frozenset(mutable_need_field_names())
        fields: list[dict[str, object]] = []
        field_names: set[str] = set()
        for item in raw_fields:
            if not isinstance(item, dict) or set(item) != _NEED_FIELD_KEYS:
                raise ValidationError("员工工作模型输出含未授权字段")
            name = _text(item.get("field_name"), field="需求字段名", maximum=64)
            if name not in allowed_fields or name in field_names:
                raise ValidationError("需求字段提取无效")
            field_names.add(name)
            fields.append(
                {
                    "name": name,
                    "value": _need_value(name, item.get("value")),
                    "quote": TeamOperationsAgent._verbatim(
                        item.get("evidence_quote"), content
                    ),
                }
            )
        if fields and projection["need_id"] is None:
            raise ValidationError("需求字段提取缺少需求引用")

        raw_commitments = payload.get("commitments")
        if not isinstance(raw_commitments, list) or len(raw_commitments) > 100:
            raise ValidationError("承诺提取无效")
        commitments: list[dict[str, object]] = []
        for item in raw_commitments:
            if not isinstance(item, dict) or set(item) != _COMMITMENT_KEYS:
                raise ValidationError("员工工作模型输出含未授权字段")
            commitment_type = _text(
                item.get("commitment_type"), field="承诺类型", maximum=16
            )
            if commitment_type not in _COMMITMENT_TYPES:
                raise ValidationError("承诺类型无效")
            uncertain = item.get("due_at_uncertain")
            if not isinstance(uncertain, bool):
                raise ValidationError("承诺到期时间不确定标记无效")
            commitments.append(
                {
                    "commitment_type": commitment_type,
                    "action": _text(
                        item.get("action"), field="承诺事项", maximum=8_000
                    ),
                    "due_at": _aware_timestamp(
                        item.get("due_at"), field="承诺到期时间"
                    ),
                    "due_at_uncertain": uncertain,
                    "verbatim": TeamOperationsAgent._verbatim(
                        item.get("verbatim"), content
                    ),
                }
            )

        raw_progress = payload.get("progress_note")
        progress: dict[str, object] | None
        if raw_progress is None:
            progress = None
        else:
            if not isinstance(raw_progress, dict) or set(raw_progress) != _PROGRESS_KEYS:
                raise ValidationError("员工工作模型输出含未授权字段")
            raw_quotes = raw_progress.get("evidence_quotes")
            if not isinstance(raw_quotes, list) or not 1 <= len(raw_quotes) <= 50:
                raise ValidationError("员工进展证据无效")
            quotes = tuple(
                TeamOperationsAgent._verbatim(value, content)
                for value in raw_quotes
            )
            if len(quotes) != len(set(quotes)):
                raise ValidationError("员工进展证据重复")
            progress = {
                "summary": _text(
                    raw_progress.get("summary"),
                    field="员工进展摘要",
                    maximum=8_000,
                ),
                "evidence_quotes": quotes,
            }
        return {
            "facts": tuple(facts),
            "need_field_updates": tuple(fields),
            "commitments": tuple(commitments),
            "progress_note": progress,
        }

    @staticmethod
    def _verbatim(value: object, content: str) -> str:
        quote = _text(value, field="员工工作原文证据", maximum=8_000)
        if quote not in content:
            raise ValidationError("员工工作提取证据不是原文")
        return quote

    def _build_changes(
        self, projection: dict[str, object], output: dict[str, object]
    ) -> list[dict[str, object]]:
        common = {
            "upload_id": projection["upload_id"],
            "artifact_ref": projection["artifact_ref"],
            "source_message_id": projection["source_message_id"],
            "confirmation_status": "pending",
            "confirmed_by": None,
            "generated_by": self._model,
        }
        changes: list[dict[str, object]] = []
        facts = output["facts"]
        assert isinstance(facts, tuple)
        if facts:
            evidenced_facts = tuple(
                {
                    **fact,
                    "provenance": {
                        "source_type": "upload",
                        "source_id": projection["artifact_ref"],
                        "source_message_id": projection["source_message_id"],
                        "evidence_quote": fact["evidence_quote"],
                        "extracted_by": self._model,
                        "confirmed_by": None,
                        "confirmed_at": None,
                    },
                }
                for fact in facts
            )
            changes.append(
                {
                    "domain": "team_operations",
                    "operation": "extract_facts",
                    "payload": {**common, "facts": evidenced_facts},
                    "risk_level": "medium",
                }
            )
        fields = output["need_field_updates"]
        assert isinstance(fields, tuple)
        if fields:
            changes.append(
                {
                    "domain": "demand",
                    "operation": "update_need_fields",
                    "payload": {
                        **common,
                        "need_id": projection["need_id"],
                        "message_id": projection["source_message_id"],
                        "fields": fields,
                    },
                    "risk_level": "medium",
                }
            )
        commitments = output["commitments"]
        assert isinstance(commitments, tuple)
        for commitment in commitments:
            assert isinstance(commitment, dict)
            changes.append(
                {
                    "domain": "commitments",
                    "operation": "create_commitment",
                    "payload": {
                        **common,
                        **commitment,
                        "owner": projection["employee_id"],
                        "account_id": projection["account_id"],
                        "opportunity_id": projection["opportunity_id"],
                        "status": (
                            "waiting_customer"
                            if commitment["commitment_type"] == "customer"
                            else "pending"
                        ),
                    },
                    "risk_level": "medium",
                }
            )
        progress = output["progress_note"]
        if isinstance(progress, dict):
            changes.append(
                {
                    "domain": "team_operations",
                    "operation": "progress_note",
                    "payload": {
                        **common,
                        **progress,
                        "employee_id": projection["employee_id"],
                        "account_id": projection["account_id"],
                        "opportunity_id": projection["opportunity_id"],
                    },
                    "risk_level": "medium",
                }
            )
        return changes

    @staticmethod
    def _empty(task: AgentTask, summary: str) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary=summary,
        )


__all__ = ("TeamOperationsAgent", "TeamWorkExtractionModelPort")
