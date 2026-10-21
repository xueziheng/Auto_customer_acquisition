当前 v4 的候选停止、BODYSTRUCTURE 和 MIME 一致性规则见 [v4 准入说明](qq-v4-mime-admission.md)。下文关于 multipart 尚未实现的段落保留为 v2/v3 历史背景；不代表 v4 仍完全阻断标准双文本 alternative。

# QQ 受控回信补丁审阅说明

本文件没有上线或真实运行命令。设计依据见 ADR0103。本轮仅准备可审阅代码和离线证据。

## 可复用接口

- `verify_sent_alias`：纯解析器，强制区分 `sender_sent` 与 `recipient_export`，严格展开合法折行但保留原字节/哈希。`record_verified_alias` 拒绝收件人导出类型，并再核原 attempt 的持久幂等头、成功 Sent 工具调用和原件摘要，不接受主题/正文替代证据。来源限制与 Q02 兼容性见 [最小离线收尾](qq-q02-offline-followup.md)。
- `provision_trial`：仅存储端口；不提供自助启用。须在新游标创建前执行，持原入站 tenant 锁拒绝已有游标。同 attempt/mailbox 无法重复创建预算。
- `SqlQQTrialGate`：固定 scope、DB 时间、当前授权回调、独立预留、原事务提交回调。当前授权应采用 `QQTrialCurrentAccess` 及原 `PostgresProjectReplyScope`，不得在生产传入 `lambda: True`。
- `qq_adapter(..., trial_gate=gate, trial_commit=gate.commit_scan)`：门禁同时传给原 QQ reader 和真实 IMAP transport，且与原页事务成对传入。只有该 transport 的受信 trial 范围能查询已核验 alias；无门禁路径仍只接受 canonical 编号。原 scheduler 的 `InboundDriver` 负责每次扫描领取、间隔和终止，不创建另一扫描循环。
- `TrialModelGenerator`：使用 `TrialModelPorts` 中原全局限制/授权/账本/配额和原 Gateway。Provider 工厂必须返回接入同一 budget 的 `configured_codex_provider(..., round_budget=budget)`；无接线 Provider 在 generate 前被拒绝。模型 scope 与配置版本必须一致，不能为通过试验改低安全条件或更换模型。
- `CurrentEmployeeReplyFactory(..., classifier=BoundReplyClassifier(...), trial_gate=gate)`：分类器必须使用同一 TrialModelGenerator 门禁；原内容读取和分类/工作项 UoW 注入当前检查。不能直接给一个未受限 model_client。

生产入口暂不调用上述组合。由于 `email.qq.sent_alias.read` 没有可靠的真实采集来源且尚未注册，仅打开原 reply_enabled 不能获得这套受控流程。

## 验证方法

在空的独立测试库迁移到 head 后，以显式 TEST_DATABASE_URL、PYTHON_DOTENV_DISABLED=1 运行：

```text
python -m pytest tests/unit/test_qq_bounded_trial.py tests/integration/test_qq_trial_ledger.py tests/integration/test_qq_trial_safety.py tests/integration/test_qq_trial_model.py tests/integration/test_qq_trial_pipeline.py tests/integration/test_qq_trial_migration.py
python scripts/check_boundaries.py
python -m ruff check .
python -m mypy domains shared tool_gateway apps workflows notification_gateway infra
```

测试必须断开外部网络。新测试里的邮件头、成功工具调用、授权引用、模型输出都为合成夹具；它们不能复制进生产用于启用。实际测试清单、通过/失败数、基线静态检查差异和运行依赖以随补丁交付的 evidence 为准。

## 真实验收前的阻断条件

需在原框架证明真实 provider 别名的来源及与唯一发送 attempt 的关联；如 QQ 不保留所需幂等头或返回歧义，停止并报告。需审阅受信生产组装和全新试验的具体范围/起止时间。之后才讨论另行批准“1 封新回信、最多 10 分钟”的验收；不要重复 Q01/Q02，不读取试验起点之前的消息。

本补丁暂未放开 multipart，准确原因是 `mime_structure_unverified`（读取前缺少子部分结构证据），不是认定 multipart 有附件。标准无附件的 multipart/alternative 固定样本已由共享解析器正确解析；该解析器也会跳过附件并返回 CANDIDATE，因此 CANDIDATE 不能证明没有附件。不得要求用户换特殊客户端来掩盖这个缺口。512 token/4 底层请求也可能不足以完成某次真实分类；预算不足应停止，不自动扩额。

## 第二版离线复核（2026-10-16）

### 邮件编号证据来源

原 `QQMailConnector` 复用 `AliyunMailConnector._execute`：provider_ref 是本地地址和确定性编号的 SHA256，不来自 QQ 返回的 Message-ID。SMTP DATA 只检查状态码并丢弃文本；接受后用原 MIME 做 APPEND。原 `_has_sent_message` 同时搜索 canonical Message-ID 和幂等头，所以不能用来取得改写后的编号。既有回信的 In-Reply-To 只表明回信引用，不能单独证明它属于指定的唯一 SENT attempt。

公开协议支持以上边界，但不证明 QQ 的具体行为：[SMTP RFC5321 §4.2.5](https://www.rfc-editor.org/rfc/rfc5321#section-4.2.5) 的成功响应表示接受投递责任；[IMAP RFC3501 §6.3.11](https://www.rfc-editor.org/rfc/rfc3501#section-6.3.11) 的 APPEND 是保存客户端提供的消息；[UIDPLUS RFC4315 §3](https://www.rfc-editor.org/rfc/rfc4315#section-3) 的 APPENDUID 是目标文件夹中的 UID，不是收件方 Message-ID。这里引用 RFC3501 对应现有 IMAP4rev1 实现，并不表示它是最新 IMAP 标准。

[荣耀官方客户端支持说明](https://www.honor.com/cn/support/content/zh-cn00406697/) 提到 QQ 可能同时保存客户端与服务器副本，只能支持“副本来源不能混同”，不能证明本账号当前设置、幂等头保留或服务端副本与实际投递编号一致。本轮未找到可据此保证这些属性的 QQ 官方协议契约。

可能验证的来源是：指定新发送的收件方实际原始头，或经同次实际投递头独立核对过的 QQ 服务端 Sent 头。两者都要保留精确幂等头、双方地址并唯一关联原 attempt；若头缺失或不一致，停止。仅凭非 canonical 编号也不能认定来源，因为协议并未保证 APPEND 副本永不被服务端改变。

因此当前不注册 `email.qq.sent_alias.read`、不制作成功账本、不接入生产启动开关。现有 Connector/Gateway 可以承载将来的固定文件夹、UIDVALIDITY/精确 UID、头大小/数量/时长预算、原件归档和审计；但在来源未知时编写并启用采集器会把假设固化为授权，不能算完成修复。

将来最小实测需单独批准一个全新 attempt：先确定可读来源和具体 UID；限定一次只读会话、单个 UID、只取必需原始头（最多 64 KiB）、不 SEARCH、不读正文、不重试，验证实际投递 Message-ID 与原幂等头。若来源在收件方邮箱，需要该邮箱另行授权或由用户提供原始头导出；不能借用发件方凭证。无法精确定位时停止并重新审阅范围。此处只是待批准的证据实验说明，不是本轮执行授权，也不包含新发信的授权。

### MIME 与原始传输接线

第二版修复原真实 transport 的两个缺口：受控 alias 被旧 canonical 正则拒绝；头部 FETCH 未请求 MIME/Bcc，导致替身返回全头时才有效的附件预检。现在 trial 使用同一门禁核对 route/attempt/员工/双方/alias，拒绝引号、反斜线和控制字符进入 IMAP 参数。原头部 FETCH 中增加 Bcc 与 MIME 元数据，仍为一次候选头、原 64 KiB 限制，不增加 SEARCH/正文预算；非 trial 查询保持原字段。

固定 socketpair IMAP 服务现在按 HEADER.FIELDS 真正裁剪头。它验证 alias 可以到达原 imaplib 解析器，也验证 multipart、Bcc、附件、重复 MIME 头、非文本类型均在正文 FETCH 前停止。固定原因持久化至原 trial 事件/状态；后续通用失败不会覆盖首次原因，未知文本仍投影为 failed_or_unknown。

最小 multipart 方案需要在原候选头 FETCH 中同时取得有界 BODYSTRUCTURE，严格核对子部分数量/深度、类型、disposition、filename、完整性和总字节，再取原单封完整 MIME 并交叉核对。它可保留一头一正文、256 KiB 的次数/字节上限，但**扩展了读取的结构元数据和协议解析面**；当前仅顶层头不能证明子部分无附件。直接先读完整 MIME 再拒绝附件会改变现有“正文读取前拦截”边界。本轮未实现未经验证的 BODYSTRUCTURE 解析器，保留兼容性阻断，不把跳过附件当作无附件证明。

### 36 项完整环境回归的具体阻断

本机已有 Linux Node 24.21.0 / Vite 8.2.1，Node 可用任务 PATH 复用；现有 node_modules 的 package.json 和 package-lock.json 均与本隔离源码不同，不能直接视为本版本完整依赖。原 Supervisor 硬性要求 Node24、Vite 文件、`/var/run/docker.sock`，并用固定镜像创建带 owner 标签的 PG/MinIO，映射到 Docker 主机 loopback 的随机端口。

本次只读检查：WSL Docker socket 不存在，Windows Docker Desktop 引擎管道不存在、没有运行中的 Docker 进程。未启动全局 Docker，未改 WSL 集成或系统 PATH，未复制不匹配依赖假装完整安装。故 36 项原完整环境测试仍不具备运行条件，断言不改、不 skip、不计为通过。

此外当前 `unshare --net` 使测试看不到宿主 loopback 映射端口；只挂 Docker socket不能解决 PG/MinIO 连通性，也不能证明容器无外网。完整验收应先准备独立、无公网路由的测试运行时，使原 Docker 与测试客户端共享受控 loopback，并预置精确依赖和镜像。或者经审阅建立仅到本 owner PG/MinIO 端口的连接通道及容器出站限制；不能直接恢复宿主网络。当前没有这样已运行的测试运行时，本轮没有擅自启动新 daemon、配置防火墙或改 supervisor 为 fake。
