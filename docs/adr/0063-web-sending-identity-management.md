# ADR：Web 发件身份冷启动管理读取与窄命令

日期：2026-09-06。状态：接受（Task 8 已授权子规格）。

当前 Campaign 可用列表排除 created/auth_pending/受限身份，无法支持刷新后的冷启动管理。
新增公开 `list_for_management(tenant_id, limit, actor)`，仅 boss/TENANT，使用原 IDENTITY_LIST
审计和逐行授权，有界 SQL 读取全部状态，返回原 IdentityView，不暴露 connector reference。
原 list_available_for_campaign 与状态机不变，不引入跨域依赖或新命令账本。

HTTP 登记与启动预热分别调用原 register/start_warmup；严格 body 必须声明 confirmed=true，
声明只表示本次真人确认意图，不取代 RequestIdentity 或域授权。不得接受认证结果、开始日期、
强制状态。登记未知时冻结原 payload，复用原耐久地址唯一性；预热未知时只读精确 ID 当前事实，
不从 warming/target 推断本次调用成功，不重置开始日期。

入站绑定仅消费既有受信 binding；active 只表示绑定事实，未取得 scheduler 能力不能声称同步。
