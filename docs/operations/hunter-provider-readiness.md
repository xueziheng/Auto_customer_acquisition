# Hunter Provider 就绪运维手册

适用范围：Phase 1 的 Hunter `contact.enrich` / `contact.verify` 生产组合。本文档只描述如何
把已交付的代码门禁变成可审计的部署事实；它不证明真实 Hunter 已验证，也不证明 Phase 1
运营验收完成。

仓库自动化始终使用受控 transport。本次仓库验收的真实 Hunter validation、真实 smoke 和
Phase 1 operating acceptance 均为 `not_run`。任何外部步骤未实际执行时必须继续写
`not_run`，不得用单元测试、E2E 或 Settings 截图替代。

## 1. 前置条件与身份

先确认：迁移精确到单一 head `0033`；目标 tenant 已确定；配置声明操作者具备
`provider:configure`，验证操作者具备 `provider:validate`；scheduler 只允许一个取得
PostgreSQL advisory lock 的副本运行。操作者 ID、tenant ID、配置版本、key 轮换版本和每次
验证 key 都必须是安全、非秘密标识。

Hunter API key 由密钥服务或受限进程环境在 Git 之外注入。只有 Connector 在真实验证或业务
调用时解析它。不要把值粘贴进命令、聊天、工单、数据库、截图或证据文件；不要运行
`echo`、`env`、`printenv` 或 `set -x` 检查它。`TRADEOS_HUNTER_API_KEY_SECRET_REF` 的值只是
引用名而不是 key，但仍属于不应外显的部署信息。

所有运维命令必须从仓库根目录使用受控 Python 3.12。部署可预先把项目专用
`TRADEOS_PYTHON_BIN` 指向批准的虚拟环境解释器；未预设时只从当前 `PATH` 解析，再显式核对
版本。不要在文档或脚本里假设某台机器的 conda 绝对路径：

```bash
set -euo pipefail

if [ -z "${TRADEOS_PYTHON_BIN:-}" ]; then
  TRADEOS_PYTHON_BIN="$(command -v python3)"
fi
export TRADEOS_PYTHON_BIN
test -x "$TRADEOS_PYTHON_BIN"
"$TRADEOS_PYTHON_BIN" -c \
  'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else "需要 Python 3.12")'
"$TRADEOS_PYTHON_BIN" scripts/run_alembic.py heads
"$TRADEOS_PYTHON_BIN" scripts/check_boundaries.py
"$TRADEOS_PYTHON_BIN" scripts/scan_sensitive.py
```

预期第一条只输出 `0033 (head)`，后两条零违规。真实环境还必须由部署平台以不出现在命令行
参数、日志或进程列表中的方式注入数据库、Gateway fingerprint 与 Hunter 凭证。

## 2. 不解析凭证地声明配置

先生成全新的安全配置世代和非秘密 key 轮换版本。密钥值改变时必须同时创建新的 key 版本和
配置版本；回滚也必须创建新配置版本，不能复用历史验证。

以下命令只传操作者 ID；四项 Hunter 配置由受限进程环境提供，命令不会解析 Hunter key、
不会创建 Connector、不会联网：

```bash
"$TRADEOS_PYTHON_BIN" scripts/configure_hunter_provider.py \
  --actor-id "$TRADEOS_OPERATOR_ID"
```

安全成功输出只包含 `provider`、`configuration_version`、`state`，其中 `state` 应为
`validation_not_run`。不要把 secret ref 名复制到输出或证据中。输入错误固定 exit 2 与
`Hunter Provider 配置输入无效`；声明/存储错误固定 exit 3 与
`Hunter Provider 配置声明失败`，两者均不得打印异常原文。

## 3. 观察 validation_pending

配置声明成功后，在 Settings 的 Playbook 与国家政策两个位置观察：当对应国家政策已允许
联系人补全时，原因应为 `CONTACT_ENRICHMENT_PROVIDER_VALIDATION_PENDING`，中文提示为
“Hunter 配置已声明，等待人工 Provider 验证”。此时两个 Hunter 联系人工具仍关闭；观察页面
不解析凭证、不调用 Hunter。

若显示 Provider 未配置，先核对目标 tenant 和配置声明是否一致；若国家政策未配置或禁止，
先完成独立的政策流程。不要为了看到 pending 绕过政策、手改 readiness 行或伪造事件。

## 4. 显式执行一次 provider.hunter.validate

每次真人决定的验证必须使用新的安全 validation key。命令只把该 key 用作 Tool Gateway
幂等键，并固定访问 Hunter `/account`；不得传邮箱、姓名、企业、国家、URL 或 API key：

```bash
"$TRADEOS_PYTHON_BIN" scripts/validate_hunter_provider.py \
  --actor-id "$TRADEOS_OPERATOR_ID" \
  --idempotency-key "$TRADEOS_VALIDATION_KEY"
```

Tool Gateway 固定执行 `tenant → permission → idempotency → rate_limit`，提交 canonical
`EXECUTING` 后才写 `validation_started`，再由 Connector 解析精确引用并访问固定 host。
成功 stdout 只包含 `tool_id`、`status=validation_passed`、`tool_call_id` 和安全配置版本。
失败只在 stderr 返回同一组安全 ID/版本与 **CLI 安全 category**；原始 `/account` JSON、账户
信息、Provider error body、凭证引用和值全部丢弃。

CLI category 是 Tool Gateway 给操作者的粗粒度处置分类，不等于 `durable readiness outcome`。
例如 CLI 可见 `provider_auth_required`，readiness event 保存 `auth_required`；
CLI 的 `provider_permanent` 可能对应 durable `provider_permanent`，也可能对应更精确的
`response_invalid`。CLI 不会直接输出 `auth_required` 或 `response_invalid`。必须在受控只读
数据库控制台以 `tenant_id` 和原 validation key 作为 bind parameter 查询，不得字符串拼接：

```sql
SELECT event_type, outcome_code, occurred_at
FROM provider_readiness_events
WHERE tenant_id = :tenant_id
  AND validation_key = :validation_key
  AND event_type IN (
    'validation_started', 'validation_passed', 'validation_failed'
  )
ORDER BY sequence;
```

只有 `validation_failed.outcome_code` 是 durable 固定失败结果；`validation_passed` 的
`outcome_code` 为 null，只有 `validation_started` 则推导为 inconclusive。再查同一个 canonical
Tool Gateway 调用，取得 CLI category 和 rate-limit 时间：

```sql
SELECT tool_call_id, status, error_category, retry_after_at,
       created_at, updated_at, completed_at
FROM tool_calls
WHERE tenant_id = :tenant_id
  AND tool_id = 'provider.hunter.validate'
  AND idempotency_key = :validation_key
LIMIT 1;
```

这两条查询与输出只含安全 ID、固定枚举和 UTC 时间；禁止扩大列清单，尤其不得查询请求内容、
fingerprint、provider payload、secret ref 或 key。`retry_after_at` 只来自 canonical ledger；
不要从 CLI category 猜等待时间。

同一当前配置同时只允许一个 active validation。不要并发运行；任何结果都不自动重试。

## 5. 处理固定结果

| CLI 可见状态/category | 必须确认的 durable readiness 事实 | 人工处置 | 是否直接重跑 |
|---|---|---|---|
| `validation_passed` | `validation_passed` event | 保存安全证据，进入 singleton scheduler 重启 | 否 |
| `provider_auth_required` | `validation_failed.outcome_code=auth_required` | 停止；核对密钥服务绑定。若 key 有变，创建新 key 版本和新配置版本 | 否 |
| `rate_limited` | Provider 失败时为 `outcome_code=rate_limited`；同时读取 canonical `retry_after_at` | 停止；按持久时间和 Provider 账户状态人工判断 | 否；人工决定后用新 validation key |
| `provider_transient` | `validation_failed.outcome_code=provider_transient` | 停止；确认请求确定失败并检查网络/Provider 状态 | 否；人工决定后用新 validation key |
| `provider_permanent` | 查询区分 `outcome_code=provider_permanent` 或 `response_invalid` | 停止；按持久 outcome 检查 transport、部署版本或响应契约 | 否 |
| `reconciliation_required` | 通常只有 `validation_started`，推导为 inconclusive | 核对 Gateway ledger，不得猜测成功 | 否；如需重新验证，创建新配置版本 |

CLI 若显示 `validation`、`permission_denied`、`in_progress`、`unexpected` 等其他安全 category，
也不能推断 Provider 已得到结果；先执行上述两条 tenant-scoped 查询。没有
`validation_started` 表示尚未进入 Provider 验证，只有 started 表示结果不确定。

失败后的再次验证仍必须是新的真人动作和新的 validation key。认证修复、配置变更、key 轮换
或回滚必须先走第 8 节的新配置世代；禁止覆盖、删除或修改 append-only readiness 事实。

## 6. 重启 singleton scheduler

只有 `validation_passed` 后才能重启。部署命令因平台而异，必须等价于以下非可运行占位流程：

```text
<deployment-control> ensure scheduler-worker replicas=1
<deployment-control> restart scheduler-worker
<deployment-control> verify scheduler-worker advisory-lock-owner=1
```

不要直接把仓库的零参数 `apps.scheduler_worker.main` 当成完整 production composition；部署
入口必须注入 `SchedulerRuntimeFactory` 及全部 typed dependencies。候选进程启动时只核对安全
配置哈希与 durable 当前配置，不解析 Hunter key、不访问 Provider。只有复核同一 dedicated
backend connection 仍持有 advisory lock 的副本，才可在第一轮 cycle 前同时核对两个 manifest
并追加 `runtime_composed`。激活失败必须释放锁、零 cycle 退出。

## 7. 验证 runtime_composed 与 Settings ready

在受控只读数据库控制台以 bind parameter（不是字符串拼接）读取当前配置的 runtime 事实：

```sql
SELECT provider_readiness_event_id, occurred_at
FROM provider_readiness_events
WHERE tenant_id = :tenant_id
  AND configuration_version = :configuration_version
  AND event_type = 'runtime_composed'
ORDER BY sequence DESC
LIMIT 1;
```

保存该 `provider_readiness_event_id` 作为 scheduler runtime fact ID。随后刷新 Settings；在国家
政策已允许的前提下，必须精确显示
`联系人补全生产组合已就绪；每个目标国家仍会逐次检查国家政策。`。`ready` 只证明同一
tenant、同一精确配置已验证并曾由锁 owner 完整注册；scheduler 当前存活仍由独立
health/readiness 监控证明。

如果没有 runtime 事实或 Settings 显示 `CONTACT_ENRICHMENT_RUNTIME_NOT_COMPOSED`，检查
scheduler 锁、完整双工具注册和激活提交；不得手工插入事实。若 Settings/readiness reader
失败，系统必须 503/关闭，不能猜 ready。

## 8. 轮换或回滚必须创建新配置版本

轮换顺序固定：在密钥服务写入新值 → 分配新的非秘密 key 版本 → 分配从未使用过的新配置
版本 → 更新受限部署环境 → 重新执行第 2～7 节。新 `configured` 事实立即使旧 validation 与
runtime 事实失效，旧进程的 live guard 会在 Connector 构造和凭证解析前关闭。

回滚不能复用旧 `configuration_version`，也不能依赖旧 `validation_passed`。用新的回滚配置
世代指向经批准的密钥版本，再重新声明、真人验证和重启 singleton scheduler。任何一步失败
都保持两个 Hunter 工具关闭。

## 9. 记录真实 smoke 状态

真实 smoke 只能取以下值：`not_run`、`passed`、`failed`、`inconclusive`。仓库自动化没有
Hunter key、没有请求真实 Hunter 网络，因此当前记录必须是：

```text
real_hunter_validation_status: not_run
real_hunter_smoke_status: not_run
production_contact_tools: no_external_deployment_fact
```

真实 smoke 由授权人员在批准的合成/内部测试目标上、经过所有现有 Gateway 门禁执行；不能用
`/account` 验证替代联系人业务调用，也不能用受控 transport 测试写 `passed`。失败或结果不
确定时分别写 `failed` / `inconclusive`，禁止自动重复付费调用。

## 10. Provider readiness 与 Phase 1 运营验收分离

Provider readiness 只回答：当前安全配置是否经真人验证，以及锁 owner 是否为精确配置完整
注册两个 Hunter 工具。它不替代每次调用的 tenant、permission、Playbook、国家政策、抑制、
可达性和 quota 检查，也不产生任何国家法律结论。

Phase 1 仍必须同时完成：从零启动真实 Campaign 并产出已验证需求；每条需求能打开客户原话
与 Provenance 证据链；真实发件域名信誉健康；人工接管等待时长已测量且无超时流失。本次
仓库验收没有执行这些外部运营事实，状态必须保持：

```text
phase1_operating_acceptance: not_run
```

### 安全证据记录模板

证据记录只允许以下字段；不添加配置哈希、secret ref、API key、Provider payload、异常文本、
联系人 PII、国家法律判断或任意自由文本：

```yaml
tenant_id: <tenant-id>
configuration_version: <safe-configuration-version>
validation_key: <safe-validation-key>
tool_call_id: <tool-call-id>
fixed_outcome: <durable-validation-passed-or-readiness-outcome-code>
validation_started_at_utc: <utc-timestamp>
validation_finished_at_utc: <utc-timestamp-or-null>
scheduler_runtime_fact_id: <provider-readiness-event-id-or-null>
operator_id: <operator-id>
```

`fixed_outcome` 从 readiness event 得出，不能复制或改写 CLI category；rate-limit 等待时间只
留在 canonical ledger，不扩充本证据模板。`not_run` 是真实状态，不是缺失值。只有外部事实
确实发生并留下上述安全证据后，才能更新对应验收状态。
