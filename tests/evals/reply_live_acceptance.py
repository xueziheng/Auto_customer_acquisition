"""冻结回复语料与业务闭环共用实际 standalone/Gateway；不创建替代分类器。"""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from shared.schemas.identifiers import MessageId, RunId, new_id
from tests.evals.reply_evals_runner import REPLIES_DIR, run_reply_evals
from tests.evals.reply_gateway_evals import (
    GatewayReplyEvalClassifier,
    GatewayReplyEvalInput,
)
from tests.integration.test_reply_completion import advance, configure_playbook
from tests.integration.test_standalone_reply_chain import (
    BODY,
    locked,
    probe,
    rows,
)
from tests.unit.test_email_inbound import mime


def corpus_hashes() -> dict[str, str]:
    """输入和期望均只读；摘要用来确认本次验收没有改题。"""
    return {
        str(path.relative_to(REPLIES_DIR)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for pattern in ("*/*/input.json", "*/*/expected.json")
        for path in sorted(REPLIES_DIR.glob(pattern))
    }


async def prepare_corpus(
    chain: dict[str, Any], worker: Any
) -> tuple[Any, dict[str, GatewayReplyEvalInput], list[str]]:
    """仅向受控 Gmail 写原始输入；由原入站事件创建真实消息及规范 Run。"""
    inputs = [
        json.loads(path.read_text())
        for path in sorted(REPLIES_DIR.glob("*/*/input.json"))
    ]
    assert len(inputs) == 160
    assert len({item["message_id"] for item in inputs}) == 160
    external = {}
    for item in inputs:
        rfc_id = f"<{new_id('msg')}@example.test>"
        external[item["message_id"]] = rfc_id
        await chain["provider"].receive_inbound(
            mime(
                body=item["body"],
                headers="Content-Type: text/plain; charset=utf-8",
                message_id=rfc_id,
                reply=chain["outbound"],
                date=format_datetime(datetime.now(UTC)),
            ),
            internal_date=datetime.now(UTC),
        )
    tenant = chain["route"].tenant_id
    canonical = []
    for _ in range(20):
        await worker.inbound_driver.scan_once()
        await worker.outbox.drain()
        canonical = await rows(
            chain,
            "SELECT m.message_id,m.external_message_id,w.run_id FROM messages m "
            "JOIN workflow_runs w ON w.tenant_id=m.tenant_id AND w.subject_ref=m.message_id "
            "WHERE m.tenant_id=:t AND w.tenant_id=:t AND m.direction='inbound' "
            "AND w.workflow_type='reply_qualification'",
        )
        if len(canonical) == 160:
            break
    assert len(canonical) == 160, "语料未完整建立规范入站关联；未调用回复模型"
    assert len({row.run_id for row in canonical}) == 160
    by_external = {row.external_message_id: row for row in canonical}
    # 验收读取原 engine 已装配的两个端口，避免另建一个相似但不同的分类组合。
    step = worker.workflow._handlers["reply_qualification.classify"]
    messages = {}
    for case_id, rfc_id in external.items():
        row = by_external[rfc_id]
        content = await step._content_reader.load(tenant, MessageId(row.message_id))
        assert content is not None and content.projected
        assert content.subject == "(current reply)"
        messages[case_id] = GatewayReplyEvalInput(row.message_id, content)
    return step._classifier, messages, [row.run_id for row in canonical]


async def receive_business_reply(chain: dict[str, Any], worker: Any, body: str) -> None:
    """等待指定新回复落库并到终态；bootstrap 后的重复历史页不能算作收件完成。"""
    rfc_id = f"<{new_id('msg')}@example.test>"
    await chain["provider"].receive_inbound(
        mime(
            body=body,
            headers="Content-Type: text/plain; charset=utf-8",
            message_id=rfc_id,
            reply=chain["outbound"],
            date=format_datetime(datetime.now(UTC)),
        ),
        internal_date=datetime.now(UTC),
    )
    tenant = chain["route"].tenant_id
    for _ in range(20):
        await worker.inbound_driver.scan_once()
        await advance(worker, tenant, 8)
        async with chain["factory"]() as db:
            status = await db.scalar(
                text(
                    "SELECT w.status FROM messages m JOIN workflow_runs w "
                    "ON w.tenant_id=m.tenant_id AND w.subject_ref=m.message_id "
                    "WHERE m.tenant_id=:t AND w.tenant_id=:t AND m.external_message_id=:m "
                    "AND w.workflow_type='reply_qualification'"
                ),
                {"t": tenant, "m": rfc_id},
            )
        if status in {"completed", "failed", "cancelled"}:
            assert status == "completed", "指定业务回复未完成"
            return
    raise AssertionError("指定业务回复未在限定收件页数内完成")


async def positive_chain(chain: dict[str, Any], worker: Any) -> None:
    """真实模型提取到 Need、原文 Provenance 和已接受接管。"""
    await receive_business_reply(chain, worker, BODY)
    needs = await rows(
        chain,
        "SELECT need_id,product_category,quantity FROM validated_needs WHERE tenant_id=:t",
    )
    assert len(needs) == 1
    assert needs[0].product_category["value"] == "hinges"
    assert str(needs[0].quantity["value"]) == "5000"
    handoffs = await rows(
        chain,
        "SELECT h.handoff_id,h.customer_verbatim,p.source_id FROM handoffs h "
        "JOIN provenance_records p ON p.tenant_id=h.tenant_id AND p.entity_id=h.handoff_id "
        "WHERE h.tenant_id=:t AND p.tenant_id=:t AND p.entity_type='handoff' AND p.field_name='customer_verbatim'",
    )
    assert len(handoffs) == 1
    assert handoffs[0].customer_verbatim in BODY
    assert handoffs[0].source_id == needs[0].quantity["provenance"]["source_id"]
    client = chain["client"]
    assert (
        await client.get(f"/api/crm/handoffs/{handoffs[0].handoff_id}")
    ).status_code == 200
    accepted = await client.post(
        f"/api/crm/handoffs/{handoffs[0].handoff_id}/accept", headers=chain["headers"]
    )
    assert accepted.status_code == 204
    await advance(worker, chain["route"].tenant_id)
    assert await rows(chain, "SELECT accepted_by FROM handoffs WHERE tenant_id=:t") == [
        (chain["staff"][0].employee_id,)
    ]


async def unsubscribe_chain(chain: dict[str, Any], worker: Any) -> None:
    """沿已发送的同一合成序列检查明确个人退订及零新增需求。"""
    before = {
        table: await rows(chain, f"SELECT count(*) FROM {table} WHERE tenant_id=:t")
        for table in ("validated_needs", "opportunities", "handoffs")
    }
    await receive_business_reply(chain, worker, "Please unsubscribe me.")
    classifications = await rows(
        chain,
        "SELECT category,suppress_scope FROM conversation_classifications WHERE tenant_id=:t AND category='unsubscribe'",
    )
    assert classifications == [("unsubscribe", "contact")]
    suppressed = await rows(
        chain,
        "SELECT account_id,contact_point_id FROM outreach_suppressions WHERE tenant_id=:t",
    )
    assert len(suppressed) == 1
    assert (
        suppressed[0].contact_point_id is not None and suppressed[0].account_id is None
    )
    assert await rows(
        chain, "SELECT state FROM outreach_enrollments WHERE tenant_id=:t"
    ) == [("replied",)]
    for table, count in before.items():
        assert (
            await rows(chain, f"SELECT count(*) FROM {table} WHERE tenant_id=:t")
            == count
        )


async def invocation_ledger(chain: dict[str, Any]) -> dict[str, Any]:
    """直接保存账本计量；缺失值保持 null，绝不补零或推算费用。"""
    async with chain["factory"]() as db:
        records = (
            (
                await db.execute(
                    text(
                        "SELECT invocation_id,run_id,employee_id,capability,model,configuration_version,state,"
                        "input_tokens,cached_input_tokens,output_tokens,slot_released FROM model_invocations "
                        "WHERE tenant_id=:t ORDER BY created_at,invocation_id"
                    ),
                    {"t": chain["route"].tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return {"calls": len(records), "records": [dict(row) for row in records]}


async def run_acceptance(chain: dict[str, Any], report_path: Path) -> dict[str, Any]:
    """一次 probe、原样160题、两个业务场景；失败保留证据且不重发模型请求。"""
    before = corpus_hashes()
    model = chain["model"]
    report: dict[str, Any] = {
        "real_model": chain["model_provider"] is None,
        "gmail": "controlled",
        "tenant_id": str(chain["route"].tenant_id),
        "model": model.model,
        "configuration_version": model.configuration_version,
        "projection": "production MIME/current-expression; neutral subject",
        "expected_cases": 160,
        "canonical_runs": 0,
        "corpus": None,
        "business": {"positive": "not_run", "unsubscribe": "not_run"},
        "complete": False,
        "error": None,
    }

    async def exercise(worker: Any) -> None:
        await configure_playbook(chain, worker)
        await probe(chain, worker)
        assert (await invocation_ledger(chain))["calls"] == 1
        classifier, messages, runs = await prepare_corpus(chain, worker)
        report["canonical_runs"] = len(runs)
        try:
            report["corpus"] = asdict(
                await run_reply_evals(GatewayReplyEvalClassifier(classifier, messages))
            )
        finally:
            # 这些 Run 只供逐题分类评估；取消后才推进两个业务场景，避免二次派发。
            for run_id in runs:
                await worker.workflow.cancel(
                    chain["route"].tenant_id, RunId(run_id), "独立语料评估结束"
                )
        for name, action in (
            ("positive", positive_chain),
            ("unsubscribe", unsubscribe_chain),
        ):
            try:
                await action(chain, worker)
                report["business"][name] = "passed"
            except Exception as error:  # noqa: BLE001 - 报告只记录安全异常类别
                report["business"][name] = type(error).__name__

    try:
        await locked(chain, exercise)
    except Exception as error:  # noqa: BLE001 - 不记录可能携带模型或凭证原文的异常
        report["error"] = type(error).__name__
    finally:
        report["ledger"] = await invocation_ledger(chain)
        report["corpus_unchanged"] = before == corpus_hashes()
        report["corpus_hashes"] = before
        corpus = report["corpus"]
        report["complete"] = bool(
            report["real_model"]
            and report["error"] is None
            and report["corpus_unchanged"]
            and corpus is not None
            and corpus["total"] == 160
            and corpus["errors"] == 0
            and corpus["overall_accuracy"] == 1
            and corpus["extract_recall"] == 1
            and corpus["action_contract_accuracy"] == 1
            and corpus["suppress_scope_accuracy"] == 1
            and report["business"] == {"positive": "passed", "unsubscribe": "passed"}
            and report["ledger"]["calls"] == 163
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
