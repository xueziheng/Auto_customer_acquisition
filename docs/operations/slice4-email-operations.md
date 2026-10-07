# Slice 4 邮件运维手册（Slice 4 Email Operations Runbook）

适用能力：发件身份（认证/预热/信誉熔断）、单封手动发送、投递反馈（hard
bounce/complaint）、事务通知（站内 + 邮件）。配套演示与验收见
`scripts/demo_slice4_manual_send.py` 与
`tests/integration/test_demo_slice4_manual_send.py`。

> 本手册区分两类验收：**容器验收**（本地受控 transport，可随时重跑）与
> **真实域名验收**（真实冷开发域名 + Google Workspace + ARF 反馈通道，
> 需要 Git 之外的独立配置）。真实域名验收未完成前，不得宣称 Slice 4 完成。

---

## 1. 必需环境变量与进程命令

### 应用进程

| 变量 | 用途 |
|---|---|
| `DATABASE_URL` | PostgreSQL async URL（唯一必填） |
| `TRADEOS_TENANT_ID` | Phase 1 单租户固定租户 ID |
| `TRADEOS_DEV_MODE=true` | 本地受控 transport 开关（生产必须为 false） |
| `TRADEOS_CORS_ALLOWED_ORIGINS` | Web 来源精确白名单 |
| `TRADEOS_API_RETRY_AFTER_SECONDS` | 手动发送被限流时的建议等待 |
| `TRADEOS_HANDOFF_POLICY` | 接管 SLA/积压阈值 |
| `TRADEOS_SCORING_POLICY` | 打分桶边界 |
| `TRADEOS_OUTBOX_MAX_ATTEMPTS` | outbox 投递重试上限 |
| `GMAIL_OAUTH_TOKEN_REF` / 其引用的环境变量 | Gmail OAuth 凭证引用 |
| `TOOL_CALL_FINGERPRINT_KEY_REF` / `_VERSION` | 工具调用幂等指纹 |
| `TRADEOS_UNSUBSCRIBE_*` | 退订链接与 one-click HMAC |
| `TRADEOS_TOOL_LEASE_SECONDS` | 工具调用租约 |

### Worker 进程

| 变量 | 用途 |
|---|---|
| `TRADEOS_SCHEDULER_INTERVAL_SECONDS` / `BATCH_LIMIT` / `LOCK_KEY` | scheduler 周期/批/单副本锁 |
| `TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS` | scheduler outbox 上限 |
| `TRADEOS_HANDOFF_T1_SECONDS` / `T2_SECONDS` | 接管升级时点 |
| `TRADEOS_DKIM_SELECTOR` | DNS 认证使用的 DKIM 选择器 |
| `TRADEOS_SCHEDULER_HEALTH_PORT` | scheduler health |
| `TRADEOS_NOTIFICATION_*` | 通知 worker（poll/batch/health/lease/收件人目录/事务身份/Gmail base URL） |
| `TRADEOS_EMAIL_FEEDBACK_*` | 投递反馈 worker（bootstrap 30 天固定，不提供覆盖） |

进程命令（迁移后）：

```bash
python3 scripts/run_alembic.py upgrade head
python -m apps.api.main            # API（多副本）
python -m apps.scheduler_worker.main   # 单副本或带分布式锁
python -m apps.notification_worker.main
python -m apps.email_feedback_worker.main
```

完整占位清单见 `infra/.env.example`；填好的 `.env` 永不提交。

## 2. Alembic 与 readiness

- 启动前：`python3 scripts/run_alembic.py upgrade head`；所有 worker 启动时执行
  `assert_database_schema_current`，schema 不匹配即拒绝启动（不自动升级）。
- 就绪探针：API `/health/ready`；scheduler/notification 各自 health 端口；
  容器验收脚本会等待全部就绪后才开始旅程。

## 3. SPF / DKIM / DMARC 解读

认证事实只存 typed 记录（`sending_auth_checks`），原始 DNS 不落库。
`dns.auth.check` 工具只读查询三个固定 TXT 名称：

| 检查 | 名称 | 通过条件 |
|---|---|---|
| SPF | `<domain>` | 恰一条 `v=spf1` 记录、语法合法、无 `+all`/`?all` 弱策略 |
| DKIM | `<selector>._domainkey.<domain>` | 恰一条、`v=DKIM1`（可选）、`k=rsa`、`p` 为合法公钥 |
| DMARC | `_dmarc.<domain>` | 恰一条、`v=DMARC1`、`p` ∈ none/quarantine/reject 且非 none |

任一失败 → 认证请求 `failed`；全部通过 → 认证请求 `succeeded` 且身份
`auth_pending → warming`（必须显式 `start_warmup`，目标日量 5–100）。
`reserve_send_slot` 在每次发送前重查最新认证——认证回退或缺失即拒绝发送。

## 4. Gmail OAuth 轮换与对账

- 凭证只经密钥服务按 `GMAIL_OAUTH_TOKEN_REF` 解析；不落日志、不进返回值、
  不进异常消息。轮换：写入新 token → 更新密钥服务 → 重启消费进程。
- 401/403 视为授权失效：告警 + 人工重新授权，不可自动重试。
- 不确定结果（可能已写入）：进入 `reconciliation_required`，只按确定性
  `Message-ID` 与 `X-TradeOS-Idempotency-V1` 搜索，**禁止自动重发**；
  搜索未命中继续保留人工对账，命中则按原 provider ref 完成 Attempt。
- 429 的 `Retry-After` 只接受 1–3600 秒；worker 保持 ready 并标记
  provider degraded，不回退 cursor、不伪造成功。

## 5. 通知积压与死信处理

- 通知 job（`notification_jobs`）与逐渠道投递（`notification_deliveries`）
  持久化；渠道失败可重试，不阻塞业务主流程。
- scheduler 投递 outbox 事件；无订阅方的事件按生产 dead-letter 行为落库
  （固定 `NO_REGISTERED_HANDLER` 标记），不影响同批其他事件——排障时先查
  `outbox_events.last_error`（只含固定标记与 event_type，无 payload）。
- 积压：`notification_jobs` 中长期 `pending/processing` 且
  `lease_expires_at` 过期的行说明 worker 未消费；检查 worker 进程与
  health，然后等待租约到期自动重领，或人工确认 worker 无重复副本。
- 投递失败重试不阻塞主流程：先修渠道（如 OAuth），再让 worker 重试。

## 6. 发件身份熔断恢复清单

身份 `suspended` 触发源：信誉速率超阈值（需 ≥50 样本窗口）、spam trap /
blocklist 命中（即时）、认证回退。恢复步骤：

1. 读取 `sending_auth_checks` / `sending_reputation_events` 定位触发源；
2. 修复根因（DNS 记录、发送内容、数据源质量）；
3. 重新发起认证检查并等待真实 worker 完成（SPF/DKIM/DMARC 全过）；
4. boss/TENANT 调用恢复（带 1–1000 字符调查记录）；恢复只回到
   `sendable_state_before_restriction`（warming/active），禁止跳过预热；
5. 恢复前最新认证必须仍全部通过；恢复后先观察一天窗口再提量。

`resume_from_throttle` 是 SYSTEM 单例动作，阈值严格（hard bounce
`< .024`、complaint `< .0008`，无 spam trap/blocklist）；读取不隐式恢复。

## 7. 安全日志字段与禁止数据

允许持久化/记录的字段：租户 ID、typed ID、固定状态/分类、provider
reference、HMAC 指纹摘要、时间戳、固定脱敏错误标记。

禁止持久化/记录：邮箱地址、主题、正文、OAuth token、完整请求、
原始 DNS/MIME、退订链接、客户原话、异常原文、DSN/凭证。

演示脚本与验收测试以 marker 断言（`demo-subject-marker` 等）确保任何
敏感输入都不进入 stdout/stderr/数据库。

## 8. 容器验收命令

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH make check
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/integration -q -W error
cd apps/web && npm run typecheck && npm run lint && npm run test && npm run build && cd ../..
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
  TRADEOS_REQUIRE_E2E=1 pytest tests/e2e -q -W error
python3 scripts/check_boundaries.py && python3 scripts/scan_sensitive.py && git diff --check
```

演示专项（同一 migrated PG 上跑两次，验证租户隔离与全程不变量）：

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
  pytest tests/integration/test_demo_slice4_manual_send.py -q -W error
```

手工跑演示（受控模式；`infra/.env.example` 不启用演示模式，必须在命令行
显式设置）：

```bash
DATABASE_URL=<url> TRADEOS_SLICE4_DEMO_MODE=controlled \
  python scripts/demo_slice4_manual_send.py
```

## 9. 真实域名验收清单与证据采集

前提（Git 之外独立配置）：独立冷开发域名（与主业务域名物理隔离，ADR
0004）、Google Workspace 邮箱 + OAuth、支持 ARF 的反馈通道。验收步骤：

1. 真实 SPF/DKIM/DMARC 事实经 `dns.auth.check` 验证并落库；
2. 预热合规（或已合规）的发件身份；
3. 经 UI 发送一封已批准测试消息；
4. 生成真实 hard bounce，确认 cursor/信誉/抑制联动；
5. 仅在受控邮箱/支持 ARF 的 provider 上生成真实 complaint，确认
   投诉信誉与熔断；
6. 证明后续发送被阻断；
7. 证明站内与事务邮件告警送达；
8. 验收报告只保存安全 ID、时间戳与脱敏地址的截图。

完成标准（四者同时）：真实 SPF/DKIM/DMARC 通过、预热合规身份、一次真实
发送成功、一次真实 hard bounce + 一次真实 complaint 均回流并触发正确
联动。任一前提不可用：把外部验收记为 `not_run`，**不宣称 Slice 4 完成**。
