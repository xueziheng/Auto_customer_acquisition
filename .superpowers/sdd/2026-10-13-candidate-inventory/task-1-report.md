# Task 1 交付报告

状态：实现完成，基线 a3e418b17d530604a9e2859fbb79ca9e337884c1（brief旧基线10ef5fd未用于重置）。不访问生产、不读取凭证、不实施外部采集，不修改API/UI/CLI。父任务并行修改API/UI，因此本任务只提交列出的自身文件。

## 已实现

- ADR0091与GLOSSARY定义公共候选、公司独立分配、durable采集任务与受信公共投影。
- 严格白名单PublicCandidate：注册公开来源、WEB_PAGE/EXTERNAL_API、URL、原件ID、提取者、时区时间、64位内容hash必填；拒绝上传、会话、推断、未知来源、未批准共享的商业源及额外私有字段。统一域名与邮箱身份。采集器负责原件真实性核验，HTTP不得直接暴露公共写端口。
- InventoryQuery支持country、region='africa'与product_tags。标签规范后精确相交；非洲采用[UN M49](https://unstats.un.org/unsd/methodology/m49/)区域代码（2026-10-13核实），不是商品标签。
- 绑定control_tenant的独立公共仓储与business_tenant的公司服务。公共快照不会带另一企业员工或沟通事实。
- 公司事务统一锁额度行，域名unique与邮箱主键双重兜底；跨员工、跨查询不重复分配，其他公司独立分配。幂等键在公司范围内绑定员工、规范查询与数量，冲突失败。
- 日额度默认50，单请求1..100；公司时区默认Asia/Shanghai且构造可配置，status返回reset_at。按实际交付计数，缺口不预扣。首次分配与缺口任务同事务。
- 任务durable排队、领取、租约过期恢复与fencing；领取每次更新令牌。旧worker不能交付或记录来源尝试；等待行锁后再次检查到期时间，不能以等待前的时间通过校验。交付仍归原员工并受当天公司额度限制。
- 实际来源尝试记录、聚合历史、按真实availability/已核实cost_class/相关性/历史收益选择来源。至少保留一个按尝试次数和最久未用轮换的探索槽位；计划执行者不得命中目标后跳过探索。静态selected不代表可用，不编造货币数字或置信度。
- public save返回原子新增新鲜域名数，刷新、去重和过期资料不计新增，便于可信来源收益记录。公共刷新保留稳定candidate ID，公司历史分配快照不变。
- 0073新增七张表、全表tenant_id、固定tenant角色的PERMISSIVE+RESTRICTIVE双策略RLS与FORCE RLS。表注册进统一Base metadata，迁移DDL固定。

## RED / GREEN 真实证据

所有命令在工作树运行；解释器 `/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python`；测试临时目录 `--basetemp=/tmp/tradeos-inventory-task1`。未输出数据库连接串。

1. 先写单位行为契约：10 failed，原因 `RED：公共候选库存领域接口尚未实现`；补模型与确定性规则后10 passed。
2. 先写真实Postgres仓储测试：3 fixture errors，均为 `RED：原子库存仓储尚未实现` 的显式pytest.fail；并非数据库/依赖故障。实现后3 passed。
3. 新增region与job刷新/失败需求：单位1 failed（region额外字段被旧模型拒绝）；集成1 failed、3 passed（同原因）。实现后合计15 passed。
4. 新增来源历史：1 failed、8 passed，缺少source_history；实现后20项本任务测试passed。
5. 新增公共保存可信计数：1 failed、10 passed，save_public返回None；实现原子新增计数后passed。
6. 等待行锁导致租约到期回归：1 failed、11 passed，`DID NOT RAISE ValueError`；改为取得锁后检查当前时间，最终passed。

最新相关回归命令：

```text
python -m pytest tests/unit/test_prospect_inventory.py tests/integration/test_prospect_inventory.py tests/unit/test_prospecting_contracts.py tests/unit/test_prospecting_models.py tests/unit/test_prospecting_service.py tests/integration/test_prospecting_repositories.py -q --basetemp=/tmp/tradeos-inventory-task1
```

最终结果：91 passed；1条既有Alembic path_separator弃用warning，无skip。本任务包含12单位测试、12真实Postgres集成测试，其余为潜客领域回归。真实Postgres来自隔离testcontainers pgvector/pgvector:pg16，独立factory连接覆盖跨员工并发额度、同键8并发、任务锁等待及租约恢复。RLS以NOBYPASSRLS低权限角色实际验证控制租户不可见、GUC伪造无效。迁移在额外隔离随机测试库执行upgrade head→downgrade0072→upgrade head，并验证列集合/nullability、RLS开启与强制。

静态验证：`python -m mypy domains/prospecting/inventory.py infra/db/prospect_inventory.py` 通过；自身Python文件ruff check通过；`python scripts/check_boundaries.py` 所有七项通过。

全库pytest由父任务在API/UI完成集成后统一运行（父任务明确指示避免并行半成品干扰）；本报告不宣称全库通过。未运行真实外部来源、真实客户收发或生产访问。

7. SAM.gov公开API带key准入：1 failed、11 passed，旧规则把认证方式误当共享许可；改为明确PUBLIC_SOURCE_IDS白名单后passed。

## 准确领域接口

所有公共DTO/Protocol通过 `domains.prospecting.service` 重导出（显式__all__防止ruff删除导出）。

```python
class InventoryQuery:
    country: str | None = None
    region: Literal['africa'] | None = None
    product_tags: tuple[str, ...]  # 至少一个，规范精确标签

class InventoryResult:
    allocations: tuple[CandidateAllocation, ...]
    job: AcquisitionJob | None

class InventoryStatus:
    tenant_id: TenantId
    daily_limit: int
    used_today: int
    remaining_today: int
    reset_at: AwareDatetime
    fresh_candidates: int

class CandidateAllocation:
    tenant_id: TenantId
    id: CandidateAllocationId
    employee_id: EmployeeId
    candidate: PublicCandidate
    allocated_at: AwareDatetime
    request_key: str  # 实际为SHA256摘要，HTTP投影需省略

class AcquisitionJob:
    tenant_id: TenantId
    id: AcquisitionJobId
    employee_id: EmployeeId
    query: InventoryQuery
    requested_count: int
    remaining_count: int
    status: Literal['queued','processing','complete','failed']
    attempts: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    failure_reason: Literal['sources_unavailable','budget_exhausted','collector_failed'] | None
    lease_until: AwareDatetime | None
    lease_token: str | None  # 并发令牌，HTTP投影必须省略

async def status(tenant_id: TenantId) -> InventoryStatus: ...
async def request(tenant_id: TenantId, employee_id: EmployeeId, query: InventoryQuery,
                  count: int, request_key: str) -> InventoryResult: ...
async def list_allocations(tenant_id: TenantId, allowed_owner_ids: tuple[EmployeeId,...] | None,
                           limit: int) -> tuple[CandidateAllocation,...]: ...
async def list_jobs(tenant_id: TenantId, allowed_owner_ids: tuple[EmployeeId,...] | None,
                    limit: int) -> tuple[AcquisitionJob,...]: ...
async def get_job(tenant_id: TenantId, job_id: AcquisitionJobId) -> AcquisitionJob | None: ...
async def set_daily_limit(tenant_id: TenantId, daily_limit: int) -> None: ...

# AcquisitionWorker后台端口
async def claim_job(tenant_id: TenantId, lease_duration: timedelta) -> AcquisitionJob | None: ...
async def record_source_attempt(tenant_id: TenantId, job_id: AcquisitionJobId,
                                lease_token: str, source_id: str, outcome: str,
                                valid_new_count: int) -> None: ...
# outcome仅允许complete/unavailable/failed/quota_exhausted
async def source_history(tenant_id: TenantId) -> tuple[SourceHistory,...]: ...
# SourceHistory={source_id,attempts,valid_new_count,last_used_at}，不推导可用或成本
async def deliver_job(tenant_id: TenantId, job_id: AcquisitionJobId,
                       lease_token: str) -> InventoryResult: ...
async def fail_job(tenant_id: TenantId, job_id: AcquisitionJobId,
                   lease_token: str, reason: str) -> None: ...
# reason仅允许sources_unavailable/budget_exhausted/collector_failed

# PublicCatalog受信端口
async def fresh(query: InventoryQuery | None, now: datetime) -> tuple[PublicCandidate,...]: ...
async def save_public(candidates: tuple[PublicCandidate,...]) -> int: ...
# 返回本次真正新插入且新鲜的企业域名数；不等于已验证需求/合格贸易机会

def select_sources(states: list[SourceState], request_budget: int) -> tuple[str,...]: ...
# request_budget为调用槽位数量；unknown成本类别排除，availability必须available
```

实现装配：`PostgresPublicCatalog(control_factory, control_tenant, *, now=...)` 与 `PostgresInventoryService(business_factory, business_tenant, catalog, *, now=..., timezone='Asia/Shanghai')`。请勿以同一个具有跨租户权力的生产凭证替代这两个绑定低权限会话。当前InventoryService只实现绑定租户/参数scope，不独立解析当前员工角色。父任务依据API AGENTS双重判权要求，将组合带当前员工授权的领域门面；本底层服务不能作为工人可自由调用的授权接口。上层授权None全公司、get_job员工范围、set_daily_limit平台权限；仓储只接受当前绑定tenant，不能从请求动态切换连接角色。

## 文件范围

GLOSSARY.md；docs/adr/0091-candidate-inventory.md；domains/prospecting/inventory.py；domains/prospecting/service.py；shared/schemas/identifiers.py；infra/db/prospect_inventory.py；infra/db/prospect_inventory_tables.py；infra/db/tables.py；migrations/versions/0073_candidate_inventory.py；tests/unit/test_prospect_inventory.py；tests/integration/test_prospect_inventory.py；本报告。

## Concerns与后续接线

- schema验证不能独立证明网页真实性/许可。必须仅让受信collector调用save_public，核验原件与白名单；不允许HTTP提供public=True或来源metadata自我认证。商业API需另做共享许可准入，此轮明确不默认入池。
- 公开结果入池与公司交付使用不同角色/事务；不能声称跨控制面和业务面单事务。崩溃后公共候选保留，任务重启再从池交付，域名/邮箱/公司幂等约束兜底。
- 当前fresh在控制租户读取新鲜投影后精确匹配，适合本轮规模；超大公共目录后需SQL索引和分页，不能将跨租户读取“优化”为公司访问公共表。
- 无命中的任务deliver后重新queued；后台必须安排退避、下一日额度等待和明确失败策略，避免高速空转。来源计划中的探索需要执行者保证不短路。
- 来源新增计数是新鲜企业域名，不是可达联系人或合格机会；邮箱仍未验证，不能进入发送序列。新域名之间共享邮箱可能使某公司交付量低于公共新增数。
- 使用显式公司时区；上层应持久维护该配置并保持同一公司所有worker一致。
- lease_token、request_key摘要为内部字段，请用HTTP白名单投影，不直接转储完整库存DTO。

## 审查修复（Task 1 review两项P2及幂等异常接线）

此次生产修改仅限 `infra/db/prospect_inventory.py`，另补本任务集成测试和更新本报告。父任务的API/UI/权限门面文件未修改或提交。

- 时间文本比较：冲突更新把JSON内observed_at两侧显式CAST为PostgreSQL带时区时间再比较；无需新增列或迁移。已有整秒观察后同秒100000微秒的新观察能够刷新，反向旧整秒重放不会回退名称、邮箱或过期时间。
- 跨午夜额度：在持有公司额度锁、查询已分配身份之后，取得单个交付时间样本，使用同一时间样本对齐quota_day并必要时归零used_today，再计算交付容量与填写allocated_at。覆盖昨日有余量和昨日已满额两种情况；新日后续请求仍只能用剩余额度。
- 幂等参数冲突：只将该处ValueError改为shared.errors.IdempotencyConflict，以使用已有HTTP领域异常映射；其他ValueError未泛改。本集成测试改为明确断言领域异常。HTTP映射验证由父任务集成执行，本任务不宣称已测HTTP行为。

针对性TDD证据：新增4例真实Postgres测试先得到 `4 failed, 12 deselected`。两例时间排序均显示旧整秒观察覆盖/阻止新微秒观察；跨午夜余量例持久quota_day仍为2026-10-13，昨日满额例新日交付量为0。测试以独立连接锁住candidate_allocations表，第三连接对公司额度行FOR UPDATE NOWAIT得到SQLSTATE55P03，确认业务已取得额度锁且确在分配查询前等待，再推进可控时钟到上海午夜并释放表锁；没有替换仓储或_mock额度逻辑。

幂等异常测试单独先得到 `1 failed, 15 deselected`，原因旧代码仍抛ValueError。生产修复后针对性组合 `-k 'timestamp_order or crossing_midnight or idempotence_conflict'` 得到 `5 passed, 11 deselected`。

最终重新运行上述完整本任务及prospecting回归命令：`95 passed, 1 warning`（既有Alembic path_separator弃用warning）；同时mypy两领域/仓储文件、ruff本次修改Python文件及结构自检全部通过。未访问凭证、生产或真实客户渠道。本次不改变既有API签名、DTO或迁移版本。
