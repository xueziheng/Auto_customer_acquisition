"""回复评估运行器验收：真实消费 tests/evals/replies 全部用例。

机制断言（模型无关）：160 条全部处理且零错误、指标诚实可重算、模型端口对
每条用例真实收到消息正文、smoke 基线指标既非 0 也非 1（运行器不伪造、不为
通过语料硬编码）。smoke 指标**不**作为模型验收达标（真实 provider 接入后再
启用准确率门槛）。
"""

from __future__ import annotations

from agent_runtime.qualification_agent.agent import QualificationAgent
from tests.evals.reply_evals_runner import KeywordBaselinePort, run_reply_evals


class _AlwaysErrorClassifier:
    model = "controlled-error-v1"

    async def classify(self, *, message: dict[str, str]) -> object:
        del message
        raise RuntimeError("controlled classifier failure")


def _smoke_agent() -> tuple[QualificationAgent, KeywordBaselinePort]:
    port = KeywordBaselinePort()
    agent = QualificationAgent(
        model="keyword-baseline-v1", model_client=port, gateway=None, guardrails=None
    )
    return agent, port


async def test_runner_consumes_all_corpus_cases_with_zero_errors() -> None:
    agent, _port = _smoke_agent()
    report = await run_reply_evals(agent)
    assert report.total >= 160
    assert report.errors == 0
    assert report.smoke is True  # 无真实 provider：明确标记 smoke，不得当作验收


async def test_reported_metrics_are_honest_recomputation() -> None:
    agent, _port = _smoke_agent()
    report = await run_reply_evals(agent)
    recomputed = sum(
        1 for r in report.results if r.predicted_category == r.expected_category
    )
    assert recomputed / report.total == report.overall_accuracy
    for correct, total in report.per_category_accuracy.values():
        assert 0 <= correct <= total


async def test_model_port_receives_real_message_text_for_every_case() -> None:
    agent, port = _smoke_agent()
    report = await run_reply_evals(agent)
    assert len(port.calls) == report.total
    assert any("Please unsubscribe me from your emails." in call for call in port.calls)
    assert any("I am out of the office until Friday" in call for call in port.calls)


async def test_metrics_reflect_genuine_baseline_not_hardcoding() -> None:
    """通用关键词基线既非 0 也非 1：运行器不伪造、不为通过语料硬编码。"""
    agent, _port = _smoke_agent()
    report = await run_reply_evals(agent)
    assert 0 < report.overall_accuracy < 1.0


async def test_critical_acceptance_metrics_are_reported() -> None:
    """HANDBOOK 验收度量必须真实报告：退订/自动回复/投诉/提取。"""
    agent, _port = _smoke_agent()
    report = await run_reply_evals(agent)
    assert 0.0 <= report.unsubscribe_recall <= 1.0
    assert 0.0 <= report.auto_reply_false_stop_rate <= 1.0
    assert 0.0 <= report.complaint_recall <= 1.0
    assert 0.0 <= report.extract_recall <= 1.0
    assert 0.0 <= report.action_contract_accuracy <= 1.0
    assert 0.0 <= report.suppress_scope_accuracy <= 1.0


async def test_runner_validates_expected_actions_and_all_account_scope_cases() -> None:
    """评估必须消费 expected 的动作/范围，且四条 account 语义不能降级。"""
    agent, _port = _smoke_agent()
    report = await run_reply_evals(agent)
    account_results = tuple(
        result
        for result in report.results
        if result.expected_suppress_scope == "account"
    )

    assert len(account_results) == 4
    assert {result.case_path for result in account_results} == {
        "unsubscribe/case_06",
        "unsubscribe/case_10",
        "unsubscribe/case_13",
        "unsubscribe/case_18",
    }
    assert all(result.predicted_suppress_scope == "account" for result in account_results)
    assert all(result.action_contract_passed is True for result in account_results)
    assert report.suppress_scope_accuracy == 1.0
    assert report.action_contract_accuracy == (
        sum(result.action_contract_passed is True for result in report.results)
        / report.total
    )


async def test_classifier_errors_fail_every_relevant_metric_denominator() -> None:
    """单条错误不能被 continue 排除，从而虚增类别、动作、scope 或提取指标。"""
    report = await run_reply_evals(_AlwaysErrorClassifier())  # type: ignore[arg-type]

    assert report.errors == report.total
    assert report.overall_accuracy == 0.0
    assert report.unsubscribe_recall == 0.0
    assert report.complaint_recall == 0.0
    assert report.extract_recall == 0.0
    assert report.action_contract_accuracy == 0.0
    assert report.suppress_scope_accuracy == 0.0
    assert all(correct == 0 for correct, _total in report.per_category_accuracy.values())
