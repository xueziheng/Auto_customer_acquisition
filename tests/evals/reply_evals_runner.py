"""回复评估运行器：真实遍历 tests/evals/replies 全部用例，跑分类器并计算指标。

- 运行器只依赖 ``agent_runtime.qualification_agent.agent.ReplyClassifier`` 窄端口；
- 指标 = 诚实重算（按类别/关键类），由调用方注入分类器（生产 provider 接入后
  在此重跑——每次更换模型或 prompt 必须重跑，tests/AGENTS.md）；
- 本模块内的 ``KeywordBaselinePort`` 只是标注/runner smoke fixture：**不得进入
  生产路径，其指标不得当作模型验收达标**（报告带 provider 标识与 smoke 标注）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.qualification_agent.agent import ReplyClassifier
from shared.schemas.identifiers import TenantId

REPLIES_DIR = Path(__file__).resolve().parent / "replies"
_EVAL_TENANT = TenantId("tn_evals_reply")


def _stop_categories() -> frozenset[str]:
    """动作含 stop_sequence 的类别（误停序列指标用；测试侧 importlib 约定）。"""
    models = importlib.import_module("domains.conversations.models")
    return frozenset(
        category.value
        for category, actions in models.REPLY_ACTIONS.items()
        if "stop_sequence" in actions
    )


@dataclass(frozen=True)
class ReplyCaseResult:
    case_path: str
    expected_category: str
    predicted_category: str | None
    error: str | None


@dataclass(frozen=True)
class ReplyEvalReport:
    """评估报告：provider 标识 + 指标。指标只反映注入分类器的表现。"""

    provider: str
    total: int
    errors: int
    overall_accuracy: float
    per_category_accuracy: dict[str, tuple[int, int]]
    unsubscribe_recall: float
    auto_reply_false_stop_rate: float
    complaint_recall: float
    extract_recall: float
    results: tuple[ReplyCaseResult, ...]

    @property
    def smoke(self) -> bool:
        """True = 无真实 provider 的 smoke 运行，指标不得当作验收达标。"""
        return self.provider.startswith("keyword-baseline")


def _corpus_cases() -> list[tuple[str, dict[str, object], dict[str, object]]]:
    cases: list[tuple[str, dict[str, object], dict[str, object]]] = []
    for category_dir in sorted(REPLIES_DIR.iterdir()):
        if not category_dir.is_dir():
            continue
        for case_dir in sorted(category_dir.iterdir()):
            if not case_dir.is_dir():
                continue
            input_payload = json.loads((case_dir / "input.json").read_text(encoding="utf-8"))
            expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
            cases.append((f"{category_dir.name}/{case_dir.name}", input_payload, expected))
    return cases


async def run_reply_evals(classifier: ReplyClassifier) -> ReplyEvalReport:
    """遍历全部用例：分类器按消息产出结果，与 expected 比对并计算指标。"""
    provider = getattr(classifier, "model", type(classifier).__name__)
    stop_categories = _stop_categories()
    results: list[ReplyCaseResult] = []
    expected_by_category: dict[str, int] = {}
    correct_by_category: dict[str, int] = {}
    unsubscribe_total = 0
    unsubscribe_hit = 0
    auto_reply_total = 0
    auto_reply_false_stop = 0
    complaint_total = 0
    complaint_hit = 0
    extract_expected = 0
    extract_hit = 0

    for case_path, input_payload, expected in _corpus_cases():
        expected_category = str(expected["category"])
        expected_by_category[expected_category] = (
            expected_by_category.get(expected_category, 0) + 1
        )
        message = {
            "message_id": str(input_payload["message_id"]),
            "subject": str(input_payload["subject"]),
            "body": str(input_payload["body"]),
        }
        predicted: str | None = None
        error: str | None = None
        try:
            result = await classifier.classify(message=message)
            predicted = result.category.value
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批评估
            error = type(exc).__name__
        results.append(ReplyCaseResult(case_path, expected_category, predicted, error))
        if error is not None:
            continue
        assert predicted is not None
        if predicted == expected_category:
            correct_by_category[expected_category] = (
                correct_by_category.get(expected_category, 0) + 1
            )
        if expected_category == "unsubscribe":
            unsubscribe_total += 1
            unsubscribe_hit += 1 if predicted == "unsubscribe" else 0
        if expected_category == "auto_reply":
            auto_reply_total += 1
            if predicted in stop_categories:
                auto_reply_false_stop += 1
        if expected_category == "complaint":
            complaint_total += 1
            complaint_hit += 1 if predicted == "complaint" else 0
        # 提取指标：expected.extract 的字段 + value 是否被分类器候选命中
        raw_extract = expected.get("extract")
        if isinstance(raw_extract, list):
            for item in raw_extract:
                if not isinstance(item, dict):
                    continue
                extract_expected += 1
                if any(
                    candidate.field == item.get("field")
                    and candidate.value == item.get("value")
                    for candidate in result.candidate_fields
                ):
                    extract_hit += 1

    total = len(results)
    errors = sum(1 for r in results if r.error is not None)
    correct = sum(correct_by_category.values())
    per_category = {
        category: (correct_by_category.get(category, 0), count)
        for category, count in expected_by_category.items()
    }
    return ReplyEvalReport(
        provider=provider,
        total=total,
        errors=errors,
        overall_accuracy=correct / total if total else 0.0,
        per_category_accuracy=per_category,
        unsubscribe_recall=unsubscribe_hit / unsubscribe_total if unsubscribe_total else 0.0,
        auto_reply_false_stop_rate=(
            auto_reply_false_stop / auto_reply_total if auto_reply_total else 0.0
        ),
        complaint_recall=complaint_hit / complaint_total if complaint_total else 0.0,
        extract_recall=extract_hit / extract_expected if extract_expected else 0.0,
        results=tuple(results),
    )


class KeywordBaselinePort:
    """标注/runner smoke fixture：通用关键词基线（非语料查表，非生产模型）。

    只在 tests/evals 使用；其指标是 smoke 指标，**不得**当作模型验收达标。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def model(self) -> str:
        return "keyword-baseline-v1"

    async def classify_reply(
        self, *, system_prompt: str, message: dict[str, str]
    ) -> str:
        del system_prompt
        self.calls.append(f"{message.get('subject', '')} {message.get('body', '')}")
        text = self.calls[-1].lower()
        category = self._classify(text)
        return json.dumps({"category": category, "candidate_fields": []})

    @staticmethod
    def _classify(text: str) -> str:
        if any(
            keyword in text
            for keyword in (
                "unsubscribe", "remove me from", "take me off", "remove us",
                "stop sending", "do not contact", "cease", "mailing list",
                "distribution list", "delete my email", "do-not-contact",
                "remove my", "remove me",
            )
        ):
            return "unsubscribe"
        if any(
            keyword in text
            for keyword in (
                "out of office", "automated", "auto reply", "on vacation",
                "annual leave", "parental leave", "not monitored",
                "courtesy notification", "support ticket", "will get back to you",
                "limited access", "business hours", "traveling", "unavailable",
                "high volume", "forwarded to", "in meetings", "away from my desk",
            )
        ):
            return "auto_reply"
        if any(
            keyword in text
            for keyword in (
                "spam", "harass", "reporting you", "complaint", "consented",
                "legal department", "privacy", "abusive", "unprofessional",
            )
        ):
            return "complaint"
        if any(
            keyword in text
            for keyword in (
                "delivery status notification", "delivery has failed",
                "could not be delivered", "recipient", "550 5.1.1", "mailbox",
                "smtp error", "dns lookup", "host not found",
                "blocked by recipient",
            )
        ):
            return "bounce"
        if "quote" in text or "price" in text or "pricing" in text:
            return "requests_quote"
        if "sample" in text:
            return "requests_sample"
        if any(
            keyword in text
            for keyword in ("catalog", "brochure", "datasheet", "documentation")
        ):
            return "requests_materials"
        if any(
            keyword in text
            for keyword in ("spec", "pieces of", " mm", "quantity", "requirement")
        ):
            return "provides_specification"
        if "interested" in text:
            return "clear_interest"
        if "continue" in text or "keep the conversation" in text:
            return "willing_to_continue"
        if any(
            keyword in text
            for keyword in (
                "no need", "not interested", "not a fit", "decline",
                "no interest",
            )
        ):
            return "rejection"
        if any(
            keyword in text
            for keyword in (
                "next year", "few months", "future", "follow up", " later",
                "next season", "next quarter", "six months",
            )
        ):
            return "future_need_possible"
        if any(
            keyword in text
            for keyword in (
                "right now", "at this time", "at the moment", "at present",
                "for now",
            )
        ):
            return "no_current_need"
        if any(
            keyword in text
            for keyword in (
                "contact our", "talk to", "forwarded your", "colleague",
                "purchasing manager", "sourcing team", "office manager",
                "head office", "technical team lead", "import coordinator",
            )
        ):
            return "refers_other_contact"
        return "auto_reply"


def main() -> int:
    """独立入口：python -m tests.evals.reply_evals_runner（smoke 运行）。"""
    from agent_runtime.qualification_agent.agent import QualificationAgent

    agent = QualificationAgent(
        model="keyword-baseline-v1",
        model_client=KeywordBaselinePort(),
        gateway=None,
        guardrails=None,
    )
    report = asyncio.run(run_reply_evals(agent))
    print(f"回复评估（smoke，provider={report.provider}，非验收达标）")
    print(f"  总用例 {report.total}，错误 {report.errors}，总体准确率 {report.overall_accuracy:.3f}")
    print(f"  退订召回 {report.unsubscribe_recall:.3f}，自动回复误停率 "
          f"{report.auto_reply_false_stop_rate:.3f}，投诉召回 {report.complaint_recall:.3f}，"
          f"提取召回 {report.extract_recall:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
