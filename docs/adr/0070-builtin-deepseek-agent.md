# ADR 0070：内置 DeepSeek Agent 的受信调用与持久交互

日期：2026-09-22。状态：采纳，按首批实施计划逐步落地。

## 背景

员工应在 TradeOS 登录后使用模型能力，运行期不依赖 Codex。现有窄模型端口缺少实际
用量和持久调用身份，单次提案 UI 也不能保留多轮澄清。新增能力沿用根九条硬边界。

## 决策

- shared 新增模型调用身份、限额、usage 契约及 AgentSessionId/AgentTurnId/ModelInvocationId。
  身份来自当前员工和持久 Run，凭证引用不进入 Agent；正文默认不序列化、不进入 repr。
- 新增 DeepSeek Connector，固定官方 HTTPS、无 SDK 自动重试、无 Provider/模型回退。
  根目录已经存在 OpenAI Connector，但新部署只显式装配 DeepSeek。
- 新模型工具用 manifest/handler/checks 扩展原 Gateway，不改核心管线。授权通过后才解析凭证。
  原文经请求私有的一次性 slot 交付，通用 ledger/outbox 只保存 HMAC 与安全引用。
- 持久额度预留用独立事务和独立锁，不锁 workflow 已持有的 Run。租户与员工上限同时执行；
  配置变更不重置用量。发送后断连/崩溃/结果丢失按 unknown 保留预留，不自动重试。
  未知 usage 不补零，金额只用有版本来源的 Decimal 费率计算。
- 新增 domains/assistant 管理员工私有 Session/Turn、执行意图与安全结果引用，不复用客户对话域。
  跨域组合由上层消费公开服务；每次历史查询重核对象权限，摘要保留依赖闭包。
- 接纳 turn 与执行意图原子保存，预分配 canonical Run；scheduler 按唯一身份 start/bind/recover。
  不新增通用 Agent 队列，现有通用 Agent Worker/Browser Worker 不因此启用。
- 研究提案用 tenant+来源轮次+候选版本唯一键在原 directives UoW 内去重。聊天肯定词不批准业务；
  精确确认沿原接口，确认后的 research Run 独立于对话轮次。
- composition_support/model.py 仅做无 IO 的机械装配，各进程独立对象、显式资源归属；
  禁止读取环境、启动循环、互导进程模块、复制业务权限规则。
- 本批新增独立本机 profile，保留 pilot 网络约束；同源 HTTPS/共享部署和其他真实业务能力分批验收。

## 后果

用量安全需要持久配额与恢复测试，模型能回答并不等于可发信或客户需求已验证。
保守的 unknown 可能留下已付费但无可用结果的一次调用；用户可显式新建 attempt，原历史保留。
本 ADR 不授权真实模型消费、生产迁移或公网部署，真实配置由管理员提供且限额明确。
