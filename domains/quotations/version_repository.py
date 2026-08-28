"""报价专用窄存储端口；每个方法显式tenant，事务由服务拥有。"""
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from domains.quotations.version_schemas import QuoteDetailView, QuoteSendReceipt, QuoteStateEvent, StoredQuoteIssuer
from domains.quotations.context import QuoteIssuer
from domains.quotations.models import QuoteState
from shared.schemas.identifiers import MessageAttemptId, OpportunityId, QuoteId, TenantId


class QuotationVersionRepository(Protocol):
    """仅报价本域持久操作，不暴露SQL session。"""
    async def lock_opportunity(self, tenant_id: TenantId, opportunity_id: OpportunityId) -> None:
        """取机会粒度报价事务锁，不升级Opportunity行锁。"""
        ...
    async def get(self, tenant_id: TenantId, quote_id: QuoteId, *, for_update: bool = False) -> QuoteDetailView | None:
        """读存储快照并核验hash，可在机会锁后取行锁。"""
        ...
    async def get_by_operation(self, tenant_id: TenantId, operation_id: str) -> QuoteDetailView | None:
        """真实操作查唯一报价，不凭完成标志造receipt。"""
        ...
    async def list_versions(self, tenant_id: TenantId, opportunity_id: OpportunityId) -> tuple[QuoteDetailView, ...]:
        """按版本降序读历史。"""
        ...
    async def add(self, tenant_id: TenantId, quote: QuoteDetailView) -> None:
        """同session原子写draft、行、全部证据和创建事件。"""
        ...
    async def transition(self, tenant_id: TenantId, quote_id: QuoteId, expected: QuoteState, target: QuoteState, event: QuoteStateEvent) -> bool:
        """CAS状态更新与事件同写，业务矩阵由域决定。"""
        ...
    async def overdue_opportunities(self, tenant_id: TenantId, *, now: datetime, limit: int) -> tuple[OpportunityId, ...]:
        """候选读取不先锁报价行。"""
        ...
    async def lock_issuer(self, tenant_id: TenantId) -> None:
        """串行当前租户抬头版本和幂等键。"""
        ...
    async def issuer_by_key(self, tenant_id: TenantId, key: str) -> StoredQuoteIssuer | None:
        """按租户键读完整确认记录。"""
        ...
    async def current_issuer(self, tenant_id: TenantId) -> QuoteIssuer | None:
        """最大version是当前确认，不撤销旧记录。"""
        ...
    async def current_issuer_record(self, tenant_id: TenantId) -> StoredQuoteIssuer | None:
        """同issuer锁内按最大version取完整记录供域分配下一版本。"""
        ...
    async def get_issuer(self, tenant_id: TenantId, issuer_id: str) -> QuoteIssuer | None:
        """按真实不可变ID读选定抬头，不追逐latest。"""
        ...
    async def add_issuer(self, tenant_id: TenantId, record: StoredQuoteIssuer) -> None:
        """只新增完整抬头确认。"""
        ...
    async def send_receipt(self, tenant_id: TenantId, attempt_id: MessageAttemptId) -> QuoteSendReceipt | None:
        """按真实attempt读唯一回执。"""
        ...
    async def add_send_receipt(self, tenant_id: TenantId, receipt: QuoteSendReceipt) -> None:
        """与状态事件同事务新增回执。"""
        ...


class QuotationUnitOfWork(AbstractAsyncContextManager, Protocol):
    """退出未commit事务一律回滚。"""
    quotes: QuotationVersionRepository
    async def commit(self) -> None:
        """明确提交；失败视为未知状态，不能报告未写入。"""
        ...
    async def rollback(self) -> None:
        """失败或取消时回滚并释放锁。"""
        ...


class QuotationUowFactory(Protocol):
    """只为一个租户创建报价专用事务。"""
    def __call__(self, tenant_id: TenantId) -> QuotationUnitOfWork:
        """返回未进入的独立UoW。"""
        ...
