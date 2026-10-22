"""真实隔离 PG，合成工具账本；不注册或执行任何实际采集工具。"""

from dataclasses import replace

import pytest
from sqlalchemy import select, update

from infra.db.qq_alias import record_verified_alias
from infra.db.qq_trial_tables import QQSentAliasRow
from infra.db.tables import RawArtifactRow, ToolCallRow
from shared.schemas.qq_trial import QQTrialStopped
from tests.integration.test_qq_trial_ledger import trial  # noqa: F401


@pytest.mark.parametrize("change", ["recipient_export", "manual_tool", "failed_tool", "wrong_artifact", "wrong_hash"])
async def test_manual_or_unbound_evidence_cannot_satisfy_collector_gate(trial, change):  # noqa: F811 - pytest按名称复用fixture
    gate, sessions, _, _, _, proof, artifact, call = trial
    scope = gate.scope
    if change == "recipient_export":
        proof = replace(proof, source="recipient_export")
    else:
        async with sessions.begin() as s:
            if change == "wrong_hash":
                await s.execute(update(RawArtifactRow).where(
                    RawArtifactRow.tenant_id == scope.route.tenant_id,
                    RawArtifactRow.artifact_id == artifact,
                ).values(content_hash="0" * 64))
            else:
                values = {
                    "manual_tool": {"tool_id": "inbox.outbound.recover"},
                    "failed_tool": {"status": "failed_permanent", "provider_ref": None,
                                    "error_category": "provider_permanent"},
                    "wrong_artifact": {"provider_ref": "art_unrelated_fixture"},
                }[change]
                await s.execute(update(ToolCallRow).where(
                    ToolCallRow.tenant_id == scope.route.tenant_id,
                    ToolCallRow.tool_call_id == call,
                ).values(**values))
    with pytest.raises(QQTrialStopped) as error:
        await record_verified_alias(sessions, scope, proof, artifact_id=artifact,
                                    tool_call_id=call, authorize=gate.authorize)
    assert error.value.reason == (
        "alias_source_not_collectable" if change == "recipient_export"
        else "alias_provenance_missing"
    )
    async with sessions() as s:
        rows = list((await s.scalars(select(QQSentAliasRow).where(
            QQSentAliasRow.tenant_id == scope.route.tenant_id,
            QQSentAliasRow.attempt_id == scope.attempt_id,
        ))).all())
    assert len(rows) == 1 and rows[0].artifact_id == artifact


async def test_recipient_export_is_rejected_before_database_or_authorization():
    from tests.unit.test_qq_alias_folding import header, verify
    from tests.unit.test_qq_bounded_trial import trial_scope

    def factory():
        pytest.fail("手动导出证据不应进入数据库")

    async def authorize():
        pytest.fail("手动导出证据不能触发试验授权检查")

    with pytest.raises(QQTrialStopped) as error:
        await record_verified_alias(factory, trial_scope(), verify(header()),
                                    artifact_id="art_fixture", tool_call_id="tcl_fixture",
                                    authorize=authorize)
    assert error.value.reason == "alias_source_not_collectable"


@pytest.mark.parametrize("trial", [{"folded_headers": True}], indirect=True)
async def test_folded_sender_evidence_is_reverified_against_persisted_marker(trial):  # noqa: F811 - pytest按名称复用fixture
    gate, sessions, _, _, _, proof, artifact, call = trial
    scope = gate.scope
    assert b"X-TradeOS-Idempotency-V1:\r\n " in proof.raw_headers
    await record_verified_alias(sessions, scope, proof, artifact_id=artifact,
                                tool_call_id=call, authorize=gate.authorize)
