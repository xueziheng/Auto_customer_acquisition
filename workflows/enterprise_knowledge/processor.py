"""持久任务的单次推进；外部端口由应用层注入。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from agent_runtime.enterprise_knowledge.analyze import KnowledgeAnalysisResult
from domains.products.schemas import (
    KnowledgeClaim,
    KnowledgeDocumentDetail,
)
from domains.products.service import EnterpriseKnowledgeService
from shared.errors import InvalidStateTransition, PermissionDenied, ValidationError
from shared.schemas.identifiers import TenantId
from shared.schemas.model_invocation import ModelInputImage
from workflows.enterprise_knowledge.xlsx_rows import XLSX_MIME, organize_xlsx_rows

_LOGGER = logging.getLogger(__name__)
_ANALYSIS_STAGES = frozenset({
    "model_identity", "response_schema", "source_evidence", "output_guard",
    "response_schema_limit", "response_schema_missing", "response_schema_extra",
    "response_schema_json", "response_schema_value",
    "response_schema_limit_facts", "response_schema_limit_title",
    "response_schema_limit_label", "response_schema_limit_value",
    "response_schema_limit_source_quote", "response_schema_limit_inferences",
})


class KnowledgeProcessingFailure(RuntimeError):
    """仅传固定阶段分类，绝不传源文件或模型原文。"""
    def __init__(self, reason: str, *, uncertain: bool = False) -> None:
        super().__init__("企业资料处理未完成")
        self.reason, self.uncertain = reason, uncertain

class KnowledgeSourceReader(Protocol):
    async def read(self, claim: KnowledgeClaim) -> bytes: ...

@dataclass(frozen=True)
class KnowledgeInput:
    text: str
    images: tuple[ModelInputImage, ...] = ()
    image_source_pages: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()

class KnowledgeParser(Protocol):
    async def parse(self, content: bytes, mime_type: str) -> KnowledgeInput: ...

class KnowledgeAnalyzer(Protocol):
    async def analyze(self, claim: KnowledgeClaim, source: KnowledgeInput) -> KnowledgeAnalysisResult: ...

class KnowledgeExporter(Protocol):
    async def export(self, detail: KnowledgeDocumentDetail) -> None: ...

class EnterpriseKnowledgeDriver:
    """一次扫描只领取一个任务，所有状态由有事务的域服务提交。"""
    def __init__(
        self, *, tenant_id: TenantId, service: EnterpriseKnowledgeService,
        reader: KnowledgeSourceReader, parser: KnowledgeParser,
        analyzer: KnowledgeAnalyzer, exporter: KnowledgeExporter,
        lease_owner: str, lease_seconds: int, model_name: str,
    ) -> None:
        self._tenant, self._service = tenant_id, service
        self._reader, self._parser = reader, parser
        self._analyzer, self._exporter = analyzer, exporter
        self._owner, self._lease, self._model = lease_owner, lease_seconds, model_name

    async def _fail(self, claim: KnowledgeClaim, reason: str, uncertain: bool) -> None:
        try:
            await self._service.fail_processing(
                self._tenant, claim, reason=reason, uncertain=uncertain,
            )
        except Exception:  # noqa: BLE001 - 存储不确定时保留租约，由过期规则转 unknown。
            return

    async def _process(self, claim: KnowledgeClaim) -> None:
        model_started = False
        try:
            await self._service.authorize_processing(self._tenant, claim)
            content = await self._reader.read(claim)
            source = await self._parser.parse(content, claim.source.mime_type)
            await self._service.authorize_processing(self._tenant, claim)
            model_name, extractor = self._model, "codex-cli:enterprise-knowledge-v1"
            expected_rows = None
            if claim.source.mime_type == XLSX_MIME:
                try:
                    if source.images:
                        raise ValueError("表格来源结构无效")
                    expected_rows = organize_xlsx_rows(source.text)
                except ValueError:
                    raise KnowledgeProcessingFailure("source_limit_exceeded") from None
            model_started = True
            result = await self._analyzer.analyze(claim, source)
            if expected_rows is not None:
                if (
                    result.source_text != source.text
                    or result.source_kind != "document_text"
                    or result.image_count or result.image_source_pages
                    or result.analysis.inferences
                    or any(not any(row.value in fact.value for fact in result.analysis.facts)
                           for row in expected_rows.analysis.facts)
                ):
                    raise ValidationError("表格整理未完整保留原文行", context={"knowledge_stage": "source_evidence"})
                result.analysis.validate_evidence(source.text)
            await self._service.authorize_processing(self._tenant, claim)
            await self._service.complete_processing(
                self._tenant, claim, source_text=result.source_text, analysis=result.analysis,
                source_kind=result.source_kind, image_count=result.image_count,
                image_source_pages=result.image_source_pages, parse_warnings=source.warnings,
                model=model_name, extracted_by=extractor,
            )
        except asyncio.CancelledError:
            await self._fail(claim, "model_result_unknown" if model_started else "processing_failed", model_started)
            raise
        except KnowledgeProcessingFailure as error:
            await self._fail(claim, error.reason, error.uncertain)
        except PermissionDenied:
            await self._fail(claim, "authorization_revoked", False)
        except InvalidStateTransition:
            return
        except ValidationError as error:
            stage = error.context.get("knowledge_stage")
            # 只记录受信阶段，不记录异常文本、资料正文或模型返回内容。
            if stage not in _ANALYSIS_STAGES:
                stage = "processing_validation"
            # 生产入口禁用通用日志；仅将固定诊断交给已有白名单 handler。
            # 不重新启用全局日志，不包含原异常或资料正文。
            _LOGGER.handle(_LOGGER.makeRecord(
                _LOGGER.name, logging.WARNING, __file__, 0,
                "enterprise_knowledge_validation_failed stage=%s tenant=%s job=%s",
                (stage, self._tenant, claim.job_id), None,
            ))
            await self._fail(claim, "invalid_analysis", False)
        except Exception:  # noqa: BLE001 - 不猜测提交/Provider异常是否发生在效果之前。
            await self._fail(claim, "model_result_unknown" if model_started else "processing_failed", model_started)

    async def _exports(self) -> int:
        count = 0
        for detail in await self._service.list_pending_exports(self._tenant, limit=5):
            doc, revision = detail.document, detail.revision
            if revision is None or doc.tenant_id != self._tenant:
                raise ValidationError("资料投影绑定无效")
            state: Literal["synced", "failed"] = "synced"
            try:
                await self._exporter.export(detail)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 文件失败只影响投影，不重新调用模型。
                state = "failed"
            try:
                await self._service.mark_export(
                    self._tenant, doc.document_id,
                    revision_id=revision.revision_id,
                    expected_version=doc.version,
                    state=state,
                )
            except InvalidStateTransition:
                continue
            count += 1
        return count

    async def scan_once(self) -> int:
        """旧投影优先恢复，再处理一份资料；无队列时零模型调用。"""
        count = await self._exports()
        claim = await self._service.claim_next(
            self._tenant, lease_owner=self._owner, lease_seconds=self._lease,
        )
        if claim is not None:
            await self._process(claim)
            count += 1
        return count + await self._exports()
