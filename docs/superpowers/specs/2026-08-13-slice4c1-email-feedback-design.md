# Slice 4C1：Gmail 投递反馈与 one-click 退订闭环设计

> 状态：已批准
> 日期：2026-08-13
> 范围：Phase 1、单租户运行方式、Gmail、PostgreSQL

## 一、背景与目标

Slice 4B 已经具备发件身份保护、触达抑制、单封人工发送和 Gmail 安全恢复，但发送后的投递反馈仍未进入业务闭环。`connectors/gmail/client.py` 中的 `fetch_new_messages` 与 `parse_bounce` 仍是骨架；系统也没有持久化 Gmail 游标、反馈 receipt、隔离记录或可消费的 one-click token。

本切片补齐以下闭环：

1. 经 Tool Gateway 安全轮询 Gmail 投递反馈；
2. 严格解析标准 DSN，并区分 hard bounce 与 soft bounce；
3. hard bounce 立即永久抑制准确的 ContactPoint，并写入发件身份信誉事实；
4. soft bounce 只记录投递结果，不永久抑制，也不自动重试；
5. 提供符合 RFC 8058 的 one-click 退订入口；
6. 通过持久化游标、幂等 receipt、隔离和共享事务证明不丢、不重、不会误封；
7. 用真实 PostgreSQL、进程级测试和安全扫描证明权限、租户与凭证边界。

系统北极星仍是“每消耗一单位成本产生的合格贸易机会数”。反馈闭环的价值在于减少向不可达或已退订联系人继续发送所浪费的成本，并保护发件域名信誉；它不以邮件数量或轮询次数为成功指标。

## 二、明确不做

- 不进入 Slice 5 的 Campaign 自动发送、节奏和自动重试；
- 不进行自然语言回复分类，也不把“退订我”之类正文交给模型判断；该能力属于 Slice 6；
- 不根据 Gmail Feedback Loop 伪造逐封 `ComplaintReceived`。Gmail FBL 是达到隐私阈值后的 campaign 聚合指标，不能作为单封邮件事实；
- 不引入 Gmail watch、Google Pub/Sub 或公网回调；
- 不建设隔离记录管理 UI；该界面随 4C2 的持久化站内通知与运营界面处理；
- 不保存原始 MIME、正文、任意 header 或客户原话到反馈业务表；
- 不让 worker、workflow、域服务或模型接触 Gmail Token；
- 不让 Outreach 与 Sending Identity 互相导入。

## 三、已批准的业务语义

### 3.1 退订作用域

普通 one-click unsubscribe 只能证明该收件地址退订，因此立即对准确 ContactPoint 做跨 Campaign、跨发件身份、跨员工的永久抑制。只有客户明确表示“不要再联系我们公司”，或人工确认时，才升级为 Account 级抑制。

### 3.2 Bounce

- hard bounce：地址永久不可达；永久抑制 ContactPoint，并记录发件身份 hard-bounce 信誉事实；
- soft bounce：邮箱满、临时限流或服务故障等暂时失败；只保存投递 receipt，不永久抑制、不自动重试，也不冒充 hard-bounce 信誉事实；
- 4C1 不决定 soft bounce 的未来重试次数和间隔，留给 Slice 5 Campaign 策略。

### 3.3 无法关联的反馈

只有反馈能够在同一 tenant 下，通过原始确定性 `Message-ID` 或 `X-TradeOS-Idempotency-V1` 唯一关联到 TradeOS MessageAttempt、ContactPoint 和 SendingIdentity 时，才允许改变业务状态。

无法识别、缺少唯一关联或关联冲突的消息只写脱敏隔离记录并告警，不修改抑制或信誉。隔离记录成功提交后允许推进 Gmail 游标，避免确定性毒消息永久阻塞 worker。

### 3.4 One-click

- 出站邮件包含 `List-Unsubscribe` 与 `List-Unsubscribe-Post: List-Unsubscribe=One-Click`；
- 匿名 `POST` 才改变状态；`GET` 只显示固定说明和确认按钮，避免邮件安全扫描器误触退订；
- token 是有时效、单用途、不可枚举的 `key_id + nonce + HMAC`，不包含明文邮箱、tenant 或内部 ID；
- Phase 1 token 固定在对应发送后 90 天过期；
- 重复、未知或过期 token 的 `POST` 都返回相同固定 `204`，不泄露联系人或记录是否存在；
- 重复有效 token 不重复写 suppression、action、outbox 或 allow audit。

## 四、方案选择

### 4.1 采用：整页原子处理

worker 先经 Tool Gateway 拉取一页 Gmail feedback，再在一个数据库事务中完成 receipt 去重、业务状态变更、隔离记录、游标推进和待提交的业务事件。可重试错误使整页回滚；已识别的确定性坏消息进入隔离后不阻塞。

该方案适合 Phase 1 的单租户和低反馈量，事务语义最清晰，也最容易证明不丢、不重和状态一致。

### 4.2 未采用：逐消息提交与连续游标

逐消息事务能降低单条失败的影响，但需要维护游标缺口集合，只能在连续区间全部成功后推进 checkpoint。Phase 1 不承担这套恢复状态机。

### 4.3 未采用：durable inbox 后异步消费

先持久化 inbox 再推进 Gmail 游标，之后异步更新抑制与信誉，扩展性更强，但会产生“反馈已接收、保护措施尚未生效”的时间窗口，还要新增第二套消费者。4C1 不接受这项最终一致性。

## 五、架构与依赖边界

```text
apps/email_feedback_worker
        │
        ▼
Tool Gateway（租户、权限、凭证与错误分类）
        │
        ▼
Gmail Connector（Gmail API + 严格 DSN 解析）
        │ typed feedback page
        ▼
workflows/email_feedback（整页事务协调）
       ┌┴────────────────────────┐
       ▼                         ▼
Outreach 公共事务参与者       Sending Identity 公共事务参与者
抑制与停止活跃触达           投递信誉事实与状态熔断
```

### 5.1 运行进程

新增独立 `apps/email_feedback_worker`。它只负责配置、生命周期、advisory lock、轮询节奏、信号和生产装配，不解析 MIME，也不直接操作业务表。

### 5.2 外部访问

所有 Gmail API 调用都经过 Tool Gateway 的新 typed read operation。Gateway handler 调用 Gmail Connector；OAuth Token 只存在于 Connector 的 secret wrapper 和 HTTP transport 调用栈。任何上层 DTO、数据库、日志、指标和异常都不得出现 Token。

Connector 将旧的自由 `dict` 骨架替换为严格类型：页面包含 opaque next cursor 和 feedback items；每项只允许 provider event ID、安全关联键、标准 DSN 状态、反馈类型和 UTC 时间，不允许原始 MIME、正文或任意 header 穿过边界。

一封 DSN 含多个 recipient block 时，Connector 为每个 block 生成独立 provider event ID。该 ID 由 Gmail message reference 与 block ordinal 的规范字节串做 SHA-256 得到，不包含收件地址或诊断原文；同一 Gmail 原文重放必须生成完全相同的 ID。

### 5.3 跨域事务

`workflows/email_feedback` 可以依赖两个域的公共 `service.py` contract，但域与域之间保持零导入。

为实现已批准的整页原子性，新增 `FeedbackPageUnitOfWork` Protocol。外层 UoW 拥有一个 SQLAlchemy session，并向两个域提供由域公开 contract 定义的 transaction-bound participant。participant 复用本域授权、不变量、action 与 outbox 逻辑，但不自行 commit；只有外层 UoW 能提交整页。

禁止 workflow 或 infra 复制 suppression、信誉阈值或状态机算法，也禁止直接绕过域 contract 写 Outreach/Sending Identity 表。

### 5.4 权限

- worker actor 由 composition root 创建，只能操作一个 tenant、一个 mailbox 和相应 SendingIdentity；
- Tool Gateway 仍执行 tenant 与 permission checks；
- transaction-bound participant 仍执行各域二次授权；
- one-click token 是 capability，但只能映射到服务器持有的准确 tenant、ContactPoint 和 MessageAttempt；API 不接受调用者提供这些 ID 或 suppression reason；
- token 有效后，composition root 才创建精确 ContactPoint scope 的最小 SYSTEM actor；
- authorizer-first，失败固定 deny audit；成功事务内只写一条唯一 durable action，事务提交后正常执行路径只调用一次 allow audit。durable action 是 crash-safe 的审计事实，不能把进程日志冒充 exactly-once 持久化证据。

## 六、持久化模型

所有表都带 `tenant_id`，所有唯一约束和查询都包含 tenant 边界。

### 6.1 `email_feedback_cursors`

- 主键：tenant + mailbox；
- 字段：opaque provider cursor、version、首次 bootstrap 时间、最后成功时间；
- 使用行锁或 compare-and-set 防止游标被旧 worker 覆盖；
- cursor 只能随成功整页单调推进，不允许任意回退或删除。

### 6.2 `email_feedback_receipts`

- 唯一键：tenant + mailbox + provider event ID；
- 不可变事实：kind、occurred_at、安全的关联 ID、处理结果、创建时间；
- kind 仅允许 hard bounce、soft bounce、unparseable；
- result 仅允许 applied、recorded、quarantined；
- 不存地址、正文、MIME、诊断原文、任意 header 或 provider payload；
- UPDATE/DELETE 由数据库 trigger 禁止。

### 6.3 `email_feedback_quarantines`

- 一对一关联 receipt；
- 保存固定原因码、provider reference 的不可逆摘要和时间；
- 原因词表包括 malformed、unsupported、missing-correlation、ambiguous-correlation、cross-tenant-correlation；
- 不保存异常字符串或客户内容；
- UPDATE/DELETE 由数据库 trigger 禁止。

### 6.4 `unsubscribe_tokens`

- 唯一存 32-byte 随机 token nonce 的 SHA-256，不存 URL token 本身；
- 保存 tenant、ContactPoint、MessageAttempt、key ID、expires_at、consumed_at；
- consumed_at 只能从 NULL 单向写入，行不可删除；
- URL token 为 `key_id.base64url(nonce).base64url(HMAC-SHA256)`，不编码业务 ID；
- active signing key 和 verification-only 历史 keys 只来自 strict runtime config，不进入数据库。

## 七、整页轮询数据流

1. worker 取得 `(tenant, mailbox)` PostgreSQL advisory lock；未取得即本周期不处理。
2. 读取 cursor，经 Tool Gateway 拉取最多 100 项。外部网络调用期间不持有数据库事务。
3. Connector 对 `multipart/report; report-type=delivery-status` 做确定性解析：标准状态 `5.x.x` 为 hard，`4.x.x` 为 soft；缺失或矛盾时返回 unparseable，不从自然语言猜测。
4. 开启 `FeedbackPageUnitOfWork`，锁 cursor 行并验证起始 cursor 仍与拉取时一致。若已变化，回滚并重新拉取。
5. 依 provider page 顺序处理每项：
   - receipt 已存在：幂等跳过业务效果；
   - hard bounce：唯一关联 MessageAttempt；写 ContactPoint `HARD_BOUNCE` suppression、停止所有匹配活跃 Enrollment、记录 SendingIdentity hard-bounce 信誉事实；
   - soft bounce：只写 receipt；
   - unparseable、缺失关联、歧义或跨租户关联：写 quarantine；跨租户同时产生固定 CRITICAL 安全告警；
   - 任何事件都不能以收件地址和时间的近似匹配替代确定性发送键。
6. 处理完整页后更新 cursor；提交 receipt、quarantine、domain effects、actions、outbox 和 cursor。
7. 每项新业务效果在事务内只有一条唯一 durable action。UoW 成功退出后，正常执行路径为每项新效果调用一次 allow audit；duplicate 不重复写 action 或 allow。进程在 commit 后、日志调用前崩溃时，以 durable action 为权威审计证据。

hard bounce 总会产生 delivery reputation fact；只有该事实使信誉窗口跨越阈值时，Sending Identity 才产生状态 action 和相应 outbox。不得因为一条 hard bounce 就伪造未触发的 suspension。

## 八、One-click 数据流

### 8.1 出站

在发送协调器获得准确 MessageAttempt、ContactPoint 和 tenant 后，先创建 token mapping，再生成严格 HTTPS unsubscribe URL。未成功发送的 token 可以保留到过期，但不得被复用给其他发送记录。

Gmail Connector 继续负责生成：

```text
List-Unsubscribe: <https://.../unsubscribe/{opaque-token}>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

生产配置拒绝非 HTTPS base URL；本地与测试通过显式 dev 配置使用 loopback HTTP。

### 8.2 入站

- `GET /unsubscribe/{token}` 返回相同的固定页面和 POST 确认按钮，不改变状态；
- `POST /unsubscribe/{token}` 只接受 `application/x-www-form-urlencoded`，body 必须精确为 `List-Unsubscribe=One-Click`；人工确认按钮提交相同 body；
- 先验证格式、HMAC key ID、签名、hash mapping、expiry 和 tenant；
- 有效且未使用 token 在单一事务中新增 ContactPoint suppression、停止活跃 Enrollment 并设置 consumed_at；
- 有效但已使用、未知、格式错误或已过期 token 均返回无内容 `204`，不说明原因；
- token path、header 和 body 都有固定长度上限；超限请求不进入数据库，并返回相同固定 `204`；
- GET 固定页面设置 `Cache-Control: no-store`、`Referrer-Policy: no-referrer` 和禁止外部资源的 CSP；
- handler 不使用 cookie 或登录态，也不把 token、ContactPoint、邮箱或客户信息写入日志、指标、响应或异常。

## 九、错误与恢复语义

### 9.1 整页可重试

以下错误使整页回滚且 cursor 不推进：

- Gmail provider auth 暂时不可用、网络错误、429 或 5xx；
- PostgreSQL deadlock、serialization、连接或 commit failure；
- cursor compare-and-set 失败或 advisory lock 丢失；
- Connector 连 provider event ID 都无法安全取得，因而不能建立幂等隔离事实；
- handler registry 或 runtime config 损坏。

429 尊重安全解析后的 `Retry-After`；其余使用有上限的指数退避。退避、clock 与 wait 均可注入测试，不 real sleep。

默认退避序列为 5、10、20、40、80、160、300 秒，成功提交一页后归零。合法 `Retry-After` 限定为 1..3600 秒；缺失、非整数或越界值按默认退避处理。单次 Gmail HTTP 请求硬超时 30 秒。

### 9.2 单消息确定性失败

能取得 provider event ID 的 malformed、unsupported、missing/ambiguous/cross-tenant correlation 转为 quarantine，允许同页继续。它们不产生 suppression、信誉事件或 allow audit。

### 9.3 信号与锁

- 每个 tenant/mailbox 最多一个 poller；
- advisory lock 使用独立 PostgreSQL connection；每次 fetch 前和 fetch 返回后都验证 backend PID 与连接仍相同，失联即视为失锁；
- SIGINT/SIGTERM 只设置 stop event，不取消正在提交的页面；
- 当前页成功提交或回滚后退出；
- `CancelledError` 传播，但 finally 必须释放 advisory lock、关闭 session、HTTP client 和 engine；
- 失锁后不再 fetch 或处理下一页。

## 十、安全、日志与可观测性

- feedback worker strict config 只要求 database URL secret、Gmail credential ref、tenant、mailbox alias、SendingIdentity、轮询间隔和页上限；它不得获得 one-click HMAC keys；
- API/发送 runtime 的独立 strict config 才包含 HTTPS unsubscribe base URL 和 HMAC key ring；它不得获得 Gmail OAuth Token；
- active HMAC key 负责签名；历史 key 仅验证，至少保留到其最后 token 过期；
- HMAC key 至少 32 bytes；key ID 只允许 1..32 位小写 ASCII 字母、数字和连字符；nonce 必须由 CSPRNG 产生；
- poll interval 使用 5..3600 的严格整数秒，默认 30；page limit 使用 1..100 的严格整数，默认 100；首次回扫策略固定为 30 天，不接受运行时扩大；
- 配置拒绝空值、重复 key ID、未知字段和错误类型；
- 固定中文日志只带 phase、category、tenant、mailbox alias 和计数；不带 `exc_info`、异常文本、高基数 provider ID 或任何客户/凭证数据；
- 指标标签只允许 tenant、mailbox alias、feedback kind 和 result category；
- 关键指标：cursor lag、processed、duplicate、quarantined、hard/soft bounce、token valid/expired/invalid、page rollback、连续失败周期和 provider degraded 状态；
- readiness 证明 strict config、migration head、数据库和 handler registry；实时 Gmail 不可用只使 provider degraded，不使进程反复重启；
- 所有跨租户关联写固定 CRITICAL 安全告警，且不得在完整 LogRecord 或异常链泄露原值。

## 十一、测试设计

### 11.1 TDD 规则

每个小任务先写行为测试并运行 genuine RED；只把缺失行为导致的失败算 RED。fixture、Docker、PATH、warning、AppleDouble、import、event-loop 或测试自身错误必须先排除。实现只做到目标 GREEN，再运行相关回归与全门禁。

所有 Python 命令使用：

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH
```

### 11.2 单元与契约

- feedback DTO 的类型、UTC、枚举、长度、control char 和不可回显错误；
- RFC 3464 hard/soft 判定、malformed/contradictory DSN、重复 header、MIME 大小边界；
- 不从正文、subject 或模型输出猜测反馈；
- suppression/reputation 决策表和 tenant/actor/action/scope 权限矩阵；
- preauthorize → resource load → full require → write唯一 durable action → commit → 正常路径一次 allow；
- 失败路径零 allow；
- token 生成、hash、HMAC、90 天含边界、key rotation、GET 无副作用、POST 幂等和固定响应；
- 原始邮箱、token、credential-like、DSN 和客户内容的恶意 payload 拒绝矩阵。

### 11.3 真实 PostgreSQL

- migration up/down/up、表/索引/constraint parity；
- tenant isolation、immutable receipt/quarantine、cursor CAS 与 token 单向 consumed_at；
- hard bounce 的 receipt、ContactPoint suppression、Enrollment stop、信誉 fact 和条件状态 outbox；
- soft bounce 仅 receipt；
- quarantine 对业务表零写；
- 任意 repo、domain participant 或 commit failure 时，两域效果和 cursor 全回滚；
- replay、20 路并发重复 feedback、两个 poller 竞争、失锁和 crash-restart；
- one-click 20 路并发恰好一次 suppression，重复、未知和过期 token 固定 204；
- 跨 tenant 相同 provider ID、相同 nonce hash 和相同 Message-ID 不互相可见。

### 11.4 Tool Gateway 与 Connector

使用本地 fake Gmail HTTP server 加真实 Tool Gateway pipeline，覆盖：

- 分页、opaque cursor、首次 bootstrap、provider duplicate；
- 401、403、429、Retry-After、5xx、网络断开和 malformed response；
- Authorization 只到 Connector transport；
- 捕获完整日志、异常链、数据库和 HTTP 响应，证明凭证、邮箱、正文、token、DSN 与 provider payload 不泄露。

### 11.5 进程级

使用真实 PostgreSQL、真实 migration、真实 worker 子进程和 fake Gmail server：

- 首次运行回扫 30 天，且只处理精确关联 TradeOS MessageAttempt 的反馈；
- 首次成功后只用持久化 cursor；
- SIGTERM 在当前页面结束后退出；
- crash/restart 从最后提交 cursor 恢复；
- invalid DSN、invalid secret/config 和 provider auth failure 固定脱敏；
- readiness 与 degraded metrics 符合设计；
- teardown 后无进程、端口、临时目录、AppleDouble 或凭证残留。

### 11.6 强制门禁

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH make check
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
  pytest tests/integration -q -W error
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

新 migration 还必须执行真实 PostgreSQL `head → previous → head`。新进程和 one-click API 必须有 subprocess/HTTP 行为测试，不以 mock-only 测试代替。

## 十二、验收标准

1. hard bounce 只在精确同租户关联时产生一次 ContactPoint suppression 和一次 delivery fact；
2. soft bounce 永不产生永久 suppression 或 hard-bounce fact；
3. duplicate、重启和并发不会重复业务效果；
4. 可重试整页错误不会推进 cursor，也不会留下任一域的部分写入；
5. 确定性坏消息能被隔离并允许后续消息处理；
6. one-click GET 无副作用，POST 并发幂等，不泄露记录存在性；
7. 所有业务查询和约束 tenant-scoped，跨租户 mutation 为零；
8. Gmail Token、地址、正文、token、DSN 和异常原文不出现在数据库非授权字段、日志、指标或响应；
9. worker 丢锁、停止、取消和 crash 后都能安全恢复；
10. 全库门禁、真实 PostgreSQL 集成和精确提交的 GitHub Actions 全绿。

## 十三、发布顺序

1. 先发布 migration、domain transaction participant 与基础 repository；
2. 发布 token issuer、one-click API 和 outbound unsubscribe URL 集成；
3. 部署 feedback worker，但 strict config 设为 disabled；
4. 在测试 tenant 做受限 30 天回扫演练，核对 receipt、quarantine、suppression、信誉与日志；
5. 启用正式轮询；
6. 观察 cursor lag、quarantine、rollback、provider degraded 和 hard/soft bounce 指标；
7. 指标稳定后进入 4C2；不得把 4C1 的隔离 UI 或投诉聚合顺手扩入本切片。

每个小任务独立 commit、push，并核对该精确 SHA 的 GitHub Actions 成功后才进入下一任务。

## 十四、参考标准与官方资料

- [RFC 3464 — An Extensible Message Format for Delivery Status Notifications](https://www.rfc-editor.org/rfc/rfc3464)
- [RFC 8058 — Signaling One-Click Functionality for List Email Headers](https://www.rfc-editor.org/rfc/rfc8058)
- [Gmail Feedback Loop](https://support.google.com/mail/answer/6254652?hl=en&rd=1)
- [Gmail Postmaster Tools API MetricDefinition](https://developers.google.com/workspace/gmail/postmaster/reference/rest/v2/MetricDefinition?hl=en)
- [Gmail Postmaster dashboards](https://support.google.com/mail/answer/14668346?hl=en)

这些资料只用于确定协议和 Gmail 能力边界。业务作用域、租户隔离、权限、审计与数据最小化仍以 TradeOS 全库硬边界为准。
