"""需求域服务 —— **本域的公共 API**。

其他域和上层只能通过这里访问需求域，不得导入 ``models.py`` 或
``repository.py``。

改这个文件的方法签名等于改公共契约，需要评估所有调用方。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.demand.schemas import (
    DemandSignalView,
    HypothesisDiscoveryView,
    HypothesisView,
    NeedClusterView,
    SignalCaptureRequest,
    ValidatedNeedView,
)
from shared.schemas.evidence import ConfidenceResult
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    NeedClusterId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)

_PROMOTABLE_NEED_FIELDS = (
    "product_category",
    "application",
    "material",
    "size_spec",
    "quantity",
    "packaging",
    "destination",
    "required_by",
    "target_price",
    "current_supply_issue",
    "certification_required",
)


def promotable_need_field_names() -> tuple[str, ...]:
    """返回客户原话可验证的需求字段词表。"""
    return _PROMOTABLE_NEED_FIELDS


def mutable_need_field_names() -> tuple[str, ...]:
    """返回已验证需求可追加历史的字段词表，不含不可变产品类别。"""
    return _PROMOTABLE_NEED_FIELDS[1:]


@runtime_checkable
class DemandAccountNameReader(Protocol):
    """由上层适配 Prospecting 的最小展示名端口；demand 不跨域导入。"""

    async def names_for(
        self,
        tenant_id: TenantId,
        account_ids: tuple[ProspectAccountId, ...],
    ) -> dict[ProspectAccountId, str]: ...

    async def countries_for(
        self,
        tenant_id: TenantId,
        account_ids: tuple[ProspectAccountId, ...],
    ) -> dict[ProspectAccountId, str]: ...


@runtime_checkable
class DemandService(Protocol):
    """需求域服务。"""

    # --- 信号 -----------------------------------------------------------

    async def capture_signal(
        self, tenant_id: TenantId, request: SignalCaptureRequest
    ) -> str:
        """记录一条需求信号。

        实现要求：
        - 输入（SignalCaptureRequest）由调用方提供 source_id/extracted_by；
          WEB_PAGE 时 source_id == page_hash、page_hash 是小写 SHA-256，且
          URL/不可变快照引用必填；快照必须在同租户存在、kind 为
          web_snapshot 且 content_hash 一致，否则抛 MissingWebEvidenceError
        - 去重 key = (tenant_id, entity_name, signal_type, source_type,
          source_id)（全非空 5 列）：同一来源身份视为同一信号，返回已有
          ID，不重复计费也不重复计数、不重复发布事件
        - 发布 ``DemandSignalCaptured``（仅新插入时；metadata-only）
        """
        ...

    async def discard_signal(
        self, tenant_id: TenantId, signal_id: str, reason: str
    ) -> None:
        """丢弃信号。原因必填，用于评估各信号源的信噪比。

        - 不存在/跨租户不可见 → ValidationError("需求信号不存在")
        - LINKED_TO_HYPOTHESIS → InvalidStateTransition（保护证据链）
        - 已 DISCARDED：同 reason 幂等 no-op；不同 reason → InvalidStateTransition
          （拒绝覆盖 first reason）
        - 不发布事件
        """
        ...

    # --- 假设 -----------------------------------------------------------

    async def create_hypothesis(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        category: str,
        signal_ids: list[str],
        reasoning: str,
        inferred_by: str,
    ) -> NeedHypothesisId:
        """创建需求假设。

        实现要求：
        - ``signal_ids`` 不得为空——无证据的假设由 guardrails 拦下，
          但这里也要拦一道（防止绕过 Agent 直接调服务）
        - ``reasoning`` 存成 ``InferredField``，``based_on`` 从
          ``signal_ids`` 展开成 ``EvidenceItem`` 列表
        - 同一 ``(account_id, category)`` 已有活跃假设时不重复创建，
          而是把新信号并入既有假设的证据——否则同一家公司会被不同
          批次的探索反复生成假设，然后被联系多次
        - 发布 ``NeedHypothesisCreated``
        """
        ...

    async def get_confidence(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> ConfidenceResult:
        """现算置信度。

        **不读数据库里的 confidence 列——没有这一列**（硬边界 3）。
        每次依据当前证据集用 ``derive_confidence`` 推导，返回带
        ``explanation`` 的结果，界面上要能展示"为什么是这一档"。
        """
        ...

    async def reject_hypothesis(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        loss_reason: str,
        rejected_by: EmployeeId | None = None,
    ) -> None:
        """否决假设。

        ``loss_reason`` 用 ``domains.opportunities`` 的 ``LossReason``
        枚举值（**以字符串传入，不 import 那个域**）。

        这是反馈闭环的输入：哪类假设最容易被证伪，直接决定下一轮
        探索策略往哪调。

        发布 ``NeedHypothesisRejected``。
        """
        ...

    # --- 验证（本域最关键的操作） ---------------------------------------

    async def promote_to_validated(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        source_message_id: MessageId,
        extracted_fields: dict,
        confirmed_by: EmployeeId | None = None,
    ) -> ValidatedNeedId:
        """把假设晋升为已验证需求。

        **门槛不可降低。** 实现必须先调
        ``NeedHypothesis.can_promote_to_validated()``，不通过就抛
        ``InsufficientEvidenceError``。

        不要提供 ``force=True`` 参数。一旦有后门，它就会在赶进度时
        被用上，然后"已验证需求"这个数字失去意义——而这个数字是
        整个系统商业价值的度量。

        实现要求：
        - 每个 ``extracted_fields`` 的值都要包成 ``FactualField``，
          ``provenance.source_id`` 指向 ``source_message_id``
        - ``extracted_fields`` 键 ⊆ {product_category} ∪ 10 个可变更业务字段
          （11 键白名单）；product_category 必填且创建后不可变
        - 完整度由字段推导，不接受传入
        - 假设状态转为 ``validated``
        - 发布 ``NeedValidated``
        """
        ...

    async def update_need_fields(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        fields: dict,
        source_message_id: MessageId,
        updated_by: EmployeeId | None = None,
    ) -> None:
        """补全需求字段（客户在后续对话里给了更多信息）。

        实现要求：
        - 每个新字段都要有自己的 provenance，指向说这句话的消息
        - **字段值变更要保留历史**，不能直接覆盖。客户把数量从 5000
          改成 3000 是重要的商业信息（可能预算收紧），覆盖掉就丢了。
        - 跨过 3 级门槛自动置 sourcing_ready，**不发事件**（catalog 无匹配
          schema，最小语义）
        """
        ...

    async def mark_sourcing_ready(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> None:
        """标记为可寻源。完整度不足 3 抛 ``SourcingThresholdNotMetError``
        （既有域错误，消息含 missing_fields）。"""
        ...

    # --- 查询 -----------------------------------------------------------

    async def list_signals(
        self,
        tenant_id: TenantId,
        *,
        signal_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[DemandSignalView]: ...

    async def list_hypotheses(
        self,
        tenant_id: TenantId,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[HypothesisView]: ...

    async def get_hypothesis(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> HypothesisView: ...

    async def list_needs(
        self,
        tenant_id: TenantId,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[ValidatedNeedView]: ...

    async def get_hypothesis_for_discovery(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> HypothesisDiscoveryView:
        """读取账户发现所需的最小安全投影。

        只返回仍处于 inferred/contacting 的假设、tenant-bound 企业名/国家、
        typed category 与内部 signal ID。推断正文、观察摘要、来源 URL 均不跨越
        此边界，避免其中的联系人姓名进入账户发现模型。
        """
        ...

    async def get_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> ValidatedNeedView:
        """读取已验证需求。

        返回的 View 必须包含 provenance 摘要——调用方（尤其是 API）
        需要把"这个数量从哪来"传给前端。
        """
        ...

    async def list_hypotheses_for_outreach(
        self,
        tenant_id: TenantId,
        *,
        countries: list[str] | None = None,
        min_confidence_tier: str | None = None,
        limit: int = 50,
    ) -> list[HypothesisView]:
        """列出可进入触达的假设。

        实现要求：
        - 按 ABAC 范围过滤（调用方传入允许的国家）
        - 排除已在抑制名单里的企业——**在这里就要排掉**，不要等到
          发送前才拦；否则会白算一遍打分和邮件草稿的成本
        - 排除已有活跃序列的企业（Lead Ownership Lock）
        """
        ...

    async def get_cluster(
        self, tenant_id: TenantId, cluster_id: NeedClusterId
    ) -> NeedClusterView:
        """读取需求簇。"""
        ...

    async def list_clusters(
        self, tenant_id: TenantId, *, limit: int = 50
    ) -> list[NeedClusterView]: ...

    async def try_assign_cluster(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> str | None:
        """尝试把需求归入簇，返回簇 ID。

        Phase 1 实现建议：类别精确匹配 + 材质/规格关键词重叠即可。
        **不要一上来就用向量相似度**——需求量小的时候，简单规则的
        结果更可预测，也更容易调试。等 pgvector 里有足够语料再换。
        """
        ...
