# 部署

Phase 1 只需要单租户跑通。但**部署形态从一开始就要支持后续两种模式共用同一套代码**——如果 Managed Dedicated 需要单独的代码分支，维护成本会失控。

---

## Phase 1 拓扑

```text
                    ┌──────────────┐
                    │  apps/web    │  静态产物，CDN 或 Nginx
                    └──────┬───────┘
                           │ HTTPS
                    ┌──────▼───────┐
                    │  apps/api    │  FastAPI，无状态，可横向扩
                    └──────┬───────┘
                           │
     ┌─────────────────────┼─────────────────────┐
     │                     │                     │
┌────▼─────┐    ┌──────────▼────────┐   ┌───────▼────────┐
│PostgreSQL│    │      Redis        │   │  S3 / MinIO    │
│+pgvector │    │ 缓存/限流/幂等占位  │   │  原始资料       │
└────┬─────┘    └──────────┬────────┘   └────────────────┘
     │                     │
     └─────────┬───────────┘
               │
   ┌───────────┼───────────┬──────────────┬─────────────────┐
   │           │           │              │                 │
┌──▼────────┐ ┌▼─────────┐ ┌▼───────────┐ ┌▼──────────────┐
│agent-     │ │scheduler-│ │browser-    │ │notification-  │
│worker     │ │worker    │ │worker      │ │worker         │
└───────────┘ └──────────┘ └────────────┘ └───────────────┘
```

**Worker 之间不互相调 HTTP**，靠数据库与事件总线协作。

---

## 各进程的伸缩与约束

| 进程 | 副本 | 关键约束 |
|---|---|---|
| `api` | 多副本 | 无状态。不执行长任务 |
| `scheduler-worker` | **单副本或带分布式锁** | 重复扫描会导致重复推进状态机 |
| `agent-worker` | 多副本 | 按成本与并发限流；模型调用要有超时与重试上限 |
| `browser-worker` | 多副本，资源限额 | 每租户/账号/Run 独立 BrowserContext；内存易涨，需定期回收 |
| `notification-worker` | 多副本 | 投递失败重试，不阻塞主流程 |

`scheduler-worker` 是唯一有单例要求的进程。Phase 1 可以直接单副本；上多副本前必须先加分布式锁。

---

## 本地开发

```bash
docker compose -f infra/docker-compose.yml up -d
```

起 PostgreSQL + pgvector、Redis、MinIO 三个依赖（**不含 Temporal**，Phase 1 不用）。

应用进程在宿主机跑，便于调试。环境变量从 `infra/.env.example` 复制后填值，**填好的 `.env` 不提交**。

---

## Hunter Provider 配置、验证与重启顺序

Hunter 联系人工具的生产 composition 和持久 activation gate 已实现，但仓库自动化没有真实
Hunter Key、不会访问真实 Hunter 网络；真实 validation/smoke 当前均为 `not_run`，也没有
任何外部生产部署已激活的事实。

真实部署必须按以下顺序执行：

1. 在密钥服务中注入 key；值不得进入 Git、命令行、日志、工单或模型上下文。
2. 分配新的安全配置版本和非秘密 key 轮换版本，通过
   `scripts/configure_hunter_provider.py` 声明 tenant-scoped 配置；该命令不解析 key、不联网。
3. 确认 Settings 显示 validation pending；缺配置或验证时两个 Hunter 工具同时关闭。
4. 授权真人以新的幂等键运行一次 `scripts/validate_hunter_provider.py`；它只经 Tool Gateway
   访问固定 `/account`。失败或不确定时停止，禁止自动重试。
5. 仅在 validation passed 后重启 scheduler。部署入口必须注入完整
   `SchedulerRuntimeFactory`，并维持单副本/单 advisory-lock owner。
6. 锁 owner 复核 dedicated backend connection 后，在第一轮 cycle 前写 matching
   `runtime_composed`；失败则释放锁、零 cycle 退出。
7. Settings ready 后仍逐次执行国家政策、Playbook、suppression 与 quota；scheduler 当前
   存活由独立 health/readiness 监控，而不是由历史 runtime fact 猜测。

key 轮换、修复认证或回滚都必须创建新的配置版本并重走全流程；新配置立即使旧
validation/runtime 失效。精确命令、固定失败分类、安全证据 allowlist 与 `not_run` 记录见
`docs/operations/hunter-provider-readiness.md`。

---

## Slice 4 演示、验收与运维

发件身份 / 手动发送 / 投递反馈 / 事务通知的可重复演示与运维手册：

- 进程级演示：`scripts/demo_slice4_manual_send.py`（受控模式
  `TRADEOS_SLICE4_DEMO_MODE=controlled`），真实 services/UoWs/Tool
  Gateway/scheduler worker/notification worker + 本地受控 Gmail HTTP 与
  fake DNS；只直插 employee 与受控连接器配置前置，不直插业务行。
- 容器验收：`tests/integration/test_demo_slice4_manual_send.py`（同一
  migrated PG 跑两次，验证租户隔离、恰一次冷发、认证事实、熔断与阻断、
  站内通知与双渠道投递状态、无效 DSN/OAuth/DNS 配置的固定脱敏失败）。
- 运维手册：`docs/operations/slice4-email-operations.md`（env 清单、
  Alembic/readiness、SPF/DKIM/DMARC 解读、OAuth 轮换与对账、通知积压与
  死信、熔断恢复清单、安全日志字段、容器与真实域名验收）。

真实域名验收需要 Git 之外的独立配置（冷开发域名、Google Workspace、
ARF 反馈通道）；前提不可用时外部验收记为 `not_run`，不得宣称 Slice 4
完成。演示与验收文档不构成新的能力声明——发送仍只经 Tool Gateway，
DNS 认证仍只由 `connectors/dns_auth` 提供。

---

## 密钥

凭证只存在于三处，模型与 Agent 都碰不到（硬边界 1）：

```text
Vault / KMS        服务端密钥。生产环境唯一来源
macOS Keychain     桌面端本地凭证（Phase 3）
Connector 内部     运行期从密钥服务取，不落日志、不进上下文
```

本地开发用 `.env`，但**变量名与生产保持一致**，避免「本地能跑、线上找不到 key」。

日志与审计对凭证类字段一律脱敏，不打印完整请求头。

---

## 数据库迁移

- 迁移脚本与代码同仓、同版本发布
- 迁移必须可前滚；破坏性变更（删列、改类型）分两次发布：先兼容双写，再清理
- 审计与 Provenance 类表**不接受破坏性迁移**

---

## 可观测性

| 用途 | 工具 |
|---|---|
| 分布式追踪 | OpenTelemetry |
| 错误追踪 | Sentry |
| 指标 | Prometheus |

必须有告警的项：

```text
发件身份信誉降级或熔断        退信率/投诉率超阈值
scheduler-worker 停止推进      待接管队列积压
模型调用失败率异常             browser-worker 内存或崩溃率异常
数据库连接池耗尽               审计写入失败
```

**审计写入失败必须告警且阻断相关业务动作**——审计不完整时继续发送客户邮件，是合规上的裸奔。

业务指标见 [09-scoring-and-feedback.md](09-scoring-and-feedback.md#五必须监控的指标)。

---

## Phase 3 的两种部署

同一套代码，靠配置区分。

### Managed Shared（普通会员）

```text
平台统一提供模型与外部 API      共享计算资源
共享数据库，靠 tenant_id 严格隔离
按会员 + 席位 + 积分收费
```

风险点是隔离：Phase 1 就要求所有表带 `tenant_id`、所有查询强制过滤（硬边界 8），正是为这一步做准备。**开启多租户前必须做一次隔离专项测试**，包括故意构造缺失租户条件的查询看能否被拦下。

### Managed Dedicated（大型企业）

```text
独立数据库        独立对象存储      独立模型项目
独立浏览器 Worker  独立日志         独立密钥
独立备份          可选固定出口 IP
```

固定出口 IP 对邮件送达率有实际帮助（IP 信誉可独立积累），是这个档位的一个真实卖点。

**不要为 Dedicated 建代码分支。** 差异只体现在部署配置与资源归属，业务代码完全一致。

---

## 备份与恢复

- 数据库每日全量 + 持续 WAL 归档
- 对象存储开版本控制（原始资料不可变，误删是主要风险）
- **定期演练恢复。** 没演练过的备份等于没有备份
- 恢复目标先写下来（RPO / RTO），再据此选方案
