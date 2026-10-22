# 服务器 QQ 基线与 Agent 升级整合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在服务器 QQ 精确备份上合并昨日升级并验证功能保留。

**Architecture:** QQ 快照是基线，昨日分支是增量输入。合并冲突按真实入口与接口逐项解决，测试证明两边可共存。按用户后续明确要求，在服务器QQ独立checkout完成修复和验证，冷备份后切换QQ；原主环境保持运行。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、Postgres、Vue 3、TypeScript、Vitest。

**Spec:** `docs/superpowers/specs/2026-10-22-server-qq-agent-integration.md`

## Global Constraints

- 不使用 T7 为源码基线，不改两个已推送备份分支。
- 真实凭证仅由受信宿主解析，不输出给模型/Git；允许QQ备份/配置保留/迁移/切换及有界免费来源诊断。禁止真实邮件测试；真实模型未作为本轮测试内容。
- 保留 QQ UI、知识、平台配置引用、获客源注册与执行，九条硬边界不放宽。
- 不用登记数或合成测试声称真实来源全部可用。

## Review Focus

- QQ business inquiry/reply 模块存在但未装配或被普通对话抢占：组合入口测试。
- 0086 与新增迁移语义冲突、重复 head：迁移升级与回归测试。
- 已配置来源仍出现在 UI 却不能走原路径：source manifest/connection/dispatch 对照与执行测试。
- QQ 跟进 UI 字段/动作被旧 UI 覆盖：保留业务邮件与跟进页面用例。
- 旧服务套餐默认改变现有额度/调度、跨租户引用：关闭态与当前授权边界回归。

### Task 1: 语义合并与服务器功能保留

**Files:**
- Modify: `apps/composition_support/model.py`, `shared/schemas/model_invocation.py`, `apps/api/routers/business_mail_followup.py`, `apps/web/src/views/inbox/BusinessMail.vue`, `apps/web/src/views/inbox/BusinessMailFollowup.vue`, `migrations/versions/0086_business_mail_followup.py`（冲突先保留 QQ 语义，再兼容升级）
- Preserve: `agent_runtime/business_inquiry.py`, `apps/scheduler_worker/business_inquiry.py`, `apps/scheduler_worker/business_reply.py`, `connectors/qq_mail/business_reply.py`, `domains/conversations/business_inquiry.py`, `domains/conversations/business_reply.py`, `infra/db/business_inquiry.py`, `infra/db/business_reply.py`, `tool_gateway/handlers/business_inquiry.py`, `tool_gateway/handlers/business_reply.py`, `tool_gateway/handlers/business_reply_evidence.py`, `workflows/business_inquiry/flow.py`, `workflows/business_reply/flow.py`
- Merge: 剩余实际改动以任务外证据 `../semantic-differences.json` 及两份 Git tree 为精确清单，禁止机械全选 ours/theirs。
- Test: `tests/unit/test_business_inquiry*.py`, `tests/unit/test_business_reply.py`, `tests/integration/test_business_inquiry.py`, `tests/integration/test_business_reply.py`, `tests/integration/test_business_mail_followup*.py`, `apps/web/tests/business-mail-followup.test.ts` 及升级已有搜索/普通对话/库存恢复测试。
- Create: `docs/operations/server-qq-agent-integration.md`（保留矩阵、冲突决策、测试命令与限制）。

**Interfaces:**
- Consumes: QQ `5b6474a`、升级 `682c541`、共同基础 `22ece5a`、冻结的两个服务器 manifest。
- Produces: QQ 为第一父提交的合并结果、保持原始 SourceDefinition/配置协议的获客路径、兼容的询盘/回复/普通对话装配、单 head 迁移、UI 与后端组合回归证据。

- [x] Step 1: 比较两边实际 tree 与入口，记录归并方案；读取所有适用 AGENTS/HANDBOOK。先运行 QQ 独有/迁移相关测试，记录既有失败，不静默修改原始备份。
- [x] Step 2: 在新分支开始合并 `origin/codex/tradeos-agent-upgrade`，按来源逐项解决；合并既有实现无需伪造新 TDD 失败，任何新修复先补/运行确实失败的回归。Expected: 两边已授权能力都保留，无未解决冲突。
- [x] Step 3: 跑上列组合测试与获客源配置、调度、执行测试。Expected: PASS；具体未覆盖真实来源逐项记为未实测。
- [x] Step 4: 跑结构、ruff、mypy、完整 unit/evals、相关隔离 PG 集成、Web test/lint/typecheck/build 与 API 生成一致性。Expected: PASS 或明确列出确切环境阻塞，不能隐藏失败。
- [x] Step 5: 生成保留矩阵与限制，结构自检后提交合并及必要修复。使用临时 git identity `Codex <codex@localhost>`；实现者不自行 push；控制者审查、服务器验收及文档完成后按用户授权推送。Expected: clean branch，QQ 和升级都是祖先，无凭证。


### 最终服务器验收与切换

- [x] QQ跟进安全轮询及968文件类型检查修复。
- [x] Linux浏览器/生命周期/MIME兼容问题修复，保留原失败日志并运行208项受影响回归。
- [x] 0080历史角色测试修复，0086→0102现有租户角色验证通过；并行临时库teardown干扰由独占重试闭合。
- [x] 33来源逐项审计，88项受控来源测试；免费TED/Hunter诊断及DeepSeek/Tavily门禁结果逐项记录。
- [x] QQ冷备份、原配置保留、4组来源设置与23条备注迁入、迁移0102、源码切换与健康检查。
- [x] 自动收发关闭，任务临时PostgreSQL清理，原主环境未切换。
- [x] 最终报告与回退说明完成；控制者在文档提交后推送并单独核对GitHub SHA。

完整结果见[最终验收报告](../../operations/server-qq-acceptance-20261023.md)。18项待接入来源、缺少真实搜索配置及尚未执行的真实邮箱验收是明确保留的限制，不记为已完成的生产功能。
