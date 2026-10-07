# Task5a 实现报告：Typed Gmail正文读取与Gateway内不可变原件

日期：2026-09-05。状态：实现、自审与本批验证完成，待控制器独立审查；未开始5b，未勾整体Task5。

- 工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`
- 分支：`codex/web-core-completion`
- 精确base：`a353ce51251d235319494e1bca5f4cf1c266c015`
- 源码HEAD：`0daa7e2d9d78b82c263b72f84fe03a5825c011a7`；本报告另作文档提交。
- 正式子规格：`docs/superpowers/specs/2026-09-05-email-inbound-5a.md`
- 共享契约决策：`docs/adr/0026-email-inbound-archived-page-contracts.md`

## 已实现接口与边界

| 接口/模块 | 最终行为 |
|---|---|
| `shared.schemas.email_inbound` | frozen/extra forbid DTO、固定disposition与技术预算。Provider bytes/subject/body/guard_body、原值关联header和cursor均repr隐藏且默认序列化排除。Archived只保留route、必要header/时间、provider摘要、Raw ID/hash/size与固定原因，不含正文/地址 |
| `GmailInboundReader(route, transport, resolver, secret_ref).fetch_inbound_page(alias, cursor, page_limit)` | 独立正文入口，不调用feedback parser、不写域事实；fetch才解析凭证。profile空锚定页→固定bootstrap→history；错route/旧gfc1/超长游标拒绝 |
| `initial_inbound_cursor(route, bootstrap_started_at, after_epoch)` / gic1 codec | composition生成一次，5b首次持久化后才profile锚定；after相对固定started最多回看30天，重试/重启不重新生成时间。32KiB，pending最多100 refs，无邮件内容。一次最多一Provider refs页，空页可前进、同页refs去重、重复nextPageToken固定拒绝；pending全部交付前不推进history_start，history404需要人工处理 |
| `GmailInboundApiTransport.get_inbound_message(*, token, message_ref, maximum_bytes)` | 独立typed raw/too_large/message_gone/malformed。固定生产Gmail endpoint，显式controlled loopback+端口，禁环境代理/redirect。HTTP响应/Content-Length/解码均有界；profile/list协议损坏/超限永久，实际网络/5xx临时、401/403需授权、429保留1–3600秒Retry-After |
| `parse_inbound_message(message_ref, GmailInboundRawResult)` | 只提取候选，不验证客户关联；labels排SENT/DRAFT/SPAM/TRASH，顶层DSN/ARF/未知report跳过。唯一dot-atom Message-ID、唯一精确自有In-Reply-To、唯一带timezone且无尾部垃圾Date；不借References/From/Subject/域猜关联，不用internalDate补Date |
| `parse_inbound_content(raw_mime: bytes) -> InboundContent` | 无IO、无tenant/sender/出站关联判断，Raw授权重读可直接复用；Provider包装复用此口。相同MIME/header/parts/depth/解码预算，不把附件或嵌套转发变成客户原话，HTML无网络/脚本执行，产生subject、确定性body及保留原HTML的guard_body |
| `email.inbound.fetch` | 独立LOW/FREE/NONE manifest、InboundTenantCheck和既有PermissionCheck；调用只允许alias/cursor/page_limit。prepare仅验证和HMAC指纹；真实EXECUTING后才factory构造reader并懒解析凭证。route/identity/预算/endpoint/secret不接受caller override，没有改pipeline |
| `InboundPageSlot` / `ToolGatewayEmailInboundReader.fetch` | 容量一task-owned；child不能take/discard父槽。只在真实SUCCEEDED且route/starting cursor一致时领取一次；错handle消耗本task槽，重放/伪handle/DUPLICATE/取消/ledger失败不交付并清槽。wrapper保留受限Retry-After |
| `InboundRawArtifactArchiver(store, bounded).archive(tenant_id, raw_mime, *, maximum_bytes)` | Gateway内Raw.put(EMAIL_RAW)后实际bounded get，核tenant/kind/mime/hash/size/bytes完全等于输入。去重meta不能代替实际读取，任何未知提交/原件不可读整体fetch失败。后续业务回滚不删除已确认原件 |
| `ControlledGmailTransport(path, *, tenant_id)` | 复用Task4持久SQLite外部Provider邮箱，新增receive_inbound/expire_inbound_history及typed profile/list/history/get；逐查询tenant过滤，每次真实读取调用独立call_id持久记录，读BLOB先查长度再bounded substr。没有pytest专用第二套邮箱、没有业务PG读取或预制Message |

硬预算为单MIME4MiB、每页20项/8MiB原bytes、累计header64KiB、parts100/depth20、候选解码4MiB、最终文本256KiB、cursor32KiB。未能完整读取的超大消息raw为None，不宣称已有原件。页bytes不足时当前ref留pending，下页重读；不跳过尚未交付refs。

原件先保存再执行CredentialMarkerGuard：完整subject+guard_body和subject+body分别检查；随后才执行256KiB判定，无裁剪。命中marker固定credential_marker，有Raw但零Message/model；自动回复和普通退订保留candidate，后续分类另做。保守guard不是全面秘密检测或发件人身份认证。

## RED → GREEN 证据

pytest均显式使用下列前缀，不使用已有TEST_DATABASE_URL：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest
```

| 命令尾部 | 真实RED及意义 | 对应GREEN |
|---|---|---|
| `tests/unit/test_email_inbound.py -q --tb=short`（最初单个契约用例） | exit1，ModuleNotFoundError: connectors.gmail.inbound；独立能力不存在 | exit0，1 passed in0.16s |
| 同文件 `-k html_is -q --tb=short` | exit1，body包含HTML标签，不是确定性文本 | 修正文本化+原HTML guard_body后被后续36/44/46项单测与本批整组覆盖 |
| `tests/integration/test_gmail_inbound_http.py -k malformed_json -q --tb=short` | exit1，损坏JSON抛GmailNetworkError而非永久typed disposition；contextmanager还显露旧frozen异常的traceback赋值局限，后者不是新产品RED | 修正后在完整本批及HTTP组通过 |
| `tests/integration/test_email_inbound_gateway.py -k html_tags -q --tb=short` | exit1，标签拆开的marker原为candidate；真实PG/Gateway测试，不是假模型判断 | 与dot-atom/Date用例合跑：6 passed /54 deselected in6.54s；后续本批总跑再次通过 |
| `tests/unit/test_email_inbound.py -k 'dot_atom or garbage' -q --tb=short` | exit1，5 failed /38 deselected，畸形dot-atom ID/Date尾部垃圾被错误接受 | 上述6项合跑GREEN；后续单元46项再次通过 |
| 同文件 `-k attached_multipart -q --tb=short` | exit1，附加multipart子text被当客户原话 | 传播附件禁止文本化标志，单元44项/最终46项通过 |

未把测试装配错误当产品RED：首次真实归档组3失败因fixture误把旧S3ObjectBlobTransport当bounded adapter；改用真实S3BoundedObjectBlobTransport后5 passed。HTTP envelope helper参数重名产生2个TypeError；伪返回值单元fixture还曾缺tool_id/tcl前缀，均修正为合法ToolCallResult。它们不证明生产规则失败。

## 实际测试与最终门禁

1. 本批完整聚焦命令：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/unit/test_email_inbound.py tests/integration/test_email_inbound_gateway.py tests/integration/test_gmail_inbound_http.py -q --tb=short
```

最后一次该命令：exit0，**80 passed in17.34s**。此后仅补5个HTTP协议损坏/超限/真实连接失败用例并收紧HTTP分类，按直接影响范围运行下列59项组；没有把两轮数字相加声称85项一次总跑。

2. 最后HTTP兼容命令：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_gmail_inbound_http.py tests/integration/test_gmail_connector_http.py tests/integration/test_gmail_feedback_http.py -q --tb=short
```

最终exit0，**59 passed in26.17s**；涵盖新增19项入站HTTP与原发送/feedback HTTP兼容。

3. 此前直接旧Gmail/feedback及Task4受控Provider兼容组合：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/unit/test_email_inbound.py tests/integration/test_email_inbound_gateway.py tests/integration/test_gmail_inbound_http.py tests/unit/test_gmail_connector.py tests/unit/test_gmail_feedback_parser.py tests/unit/test_gmail_arf.py tests/unit/test_email_feedback_handler.py tests/integration/test_gmail_connector_http.py tests/integration/test_gmail_feedback_http.py tests/integration/test_web_core_launcher.py -k 'not (occupied_port or missing_node or leader or anchor or short_lived or handshake or migration or dead_process or process_birth or hup or term or startup or stage or real_controlled or closes or network_boundary or provider_constructors or external_listener)' -q --tb=short
```

exit0，**216 passed /20 deselected in48.63s**。包含旧发送/恢复搜索、DSN/ARF解析/旧反馈handler/HTTP和Task4 Provider持久调用兼容；选择器排除主要未改的生命周期组。该组合先于纯Raw解析口提取和最终HTTP收紧，后续分别由80项本批和59项HTTP验证覆盖；不称最终全仓测试。没有重跑337项Web。

4. 静态/安全：

- ruff check本批全部Python文件（含tests）exit0，All checks passed。
- mypy：shared/email_inbound.py、shared/schemas/email_inbound.py、4个connectors/gmail/inbound*.py、原transport.py、两个handlers/email_inbound*.py、checks/email_inbound.py、infra/email_inbound_artifacts.py、infra/controlled/providers.py；exit0，12 source files无问题。最后transport收紧后另对transport/inbound_transport跑2 files无问题。
- `.venv/bin/python scripts/check_boundaries.py`：exit0，7项全部通过。
- `.venv/bin/python scripts/scan_sensitive.py --staged`：exit0；包含21个源码/测试/文档文件；另对本批新文件与直接改动执行增量扫描exit0。没有改scanner规则。
- `git diff --cached --check`：exit0；git均由Python subprocess捕获stderr，只输出安全stdout/退出码。未删._pack、repair/repack、改全局身份。
- 不重复全仓敏感扫描。Task4记录的4处旧fixture基线仍归Task12，此报告不称全仓扫描通过。

真实集成故障注入只作用于存储故障或外部Provider响应：PG trigger拒绝EXECUTING/完成ledger、PG表锁期间取消、真实Raw commit后抛未知并观察meta存而bytes失、真实MinIO缺失/损坏字节、Provider空/重复页和过期history。域、Gateway、Repository、业务规则没有替身。槽伪造返回值测试是单独的纯领取边界测试，不被当成真实Gateway成功证明。

## 资源与自审

真实资源由Task4 `Supervisor.start_infrastructure`/OwnedContainers/OwnedProcess创建并管理：独占PG/MinIO、本owner原迁移短进程和最小既有身份初始化，无第二套启动框架。测试没有启动新正文worker或任何新app进程，没有读取.env/既有DSN/token/cookie/生产配置；生成秘密只进入受信进程，不回显。

每fixture finally关闭对象client/engine，Supervisor.close核验owner后删除容器、关闭监听、删除私有配置；Provider SQLite本fixture文件随后删除。独立复核保留的8次本任务owner目录：owned_container_residuals=0、owned_private_config_residuals=0；没有pkill/prune/清理外来资源。短迁移/初始化子进程沿Task4 exec前anchor机制结束，未改其实现。HTTP测试有界join并断言本server线程停止。

自审修正：HTML两视图guard、Raw纯解析重用、严格dot-atom/Date、附加multipart继承限制、候选Unicode解码扩张预算、Caller配置override拒绝、Wrapper Retry-After保留、HTTP永久协议错误与真实网络失败区分、敏感字段默认序列化排除。没有改变根九条、金额/租户/审批/依赖方向、旧feedback worker或核心pipeline。没有新增依赖或迁移。

## 5b/Task6消费与未实现事项

```python
async def fetch(self, tenant_id: TenantId, mailbox_alias: str,
                cursor: str, page_limit: int) -> ArchivedInboundPage: ...
# page.route: InboundRoute(tenant_id, mailbox_alias, configured_identity_id, route_id, config_version)
# page.starting_cursor / page.next_cursor: opaque gic1，repr/默认序列化排除
# page.items: tuple[ArchivedInboundItem, ...]
# item: provider_ref_digest, disposition, external_message_id, in_reply_to,
#       sent_at, parser_version, raw: ArchivedInboundRaw | None
```

5b必须先将composition给出的initial cursor/固定started_at/after_epoch持久化，再调用fetch。只读opaque pair，不导入Gmail codec；CAS与整页transaction提交成功后才能采用next_cursor。receipt fingerprint必须显式规范化覆盖**完整route绑定/版本、原值Message-ID/In-Reply-To、时间、Raw ID/hash/size、parser_version与固定disposition**，不能对脱敏model_dump求hash漏掉敏感事实。

Task6在Gateway授权读Raw后使用 `parse_inbound_content(raw_mime) -> InboundContent`，不得伪造labels/internalDate。解析与已过guard分离，必须分别检查完整subject+guard_body和subject+body，再按256KiB规则处理；不能另用不同HTML文本化方法绕过标签拆分/隐藏marker测试。ArchivedItem没有任何模型候选文本，不能直接当模型输入。

5b尚未实现：三张技术表、tenant锁/CAS、真实Outreach已SENT关联、整页Message/event/receipt/review/cursor原子入库、同ID异内容预检、待核对权限/原件下载API、scheduler入站driver装配。Task6正文分类/Need/Opportunity和完整回复闭环未实现。本批candidate只表示技术上可供后续判断，不能称关联已验证、客户明确需求或商业成功。

Raw旧未知提交补偿缺陷没有修复；本批如实检测“metadata存在但bytes不可读”，fetch失败而非假成功。业务事务回滚允许保留孤立不可变Raw，未提供清扫器。普通自动回复/退订后续语义依赖5b/Task6，不在入站插件自行做抑制或停序列。

真实Gmail/Google SDK、OpenAI、Tavily、客户收发与生产启用：**not_run**。仅核实官方只读文档：
Gmail get的固定URL/format=RAW参见[users.messages.get](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get)；
history/pageToken与过期404参见[users.history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)和[同步指南](https://developers.google.com/workspace/gmail/api/guides/sync)。Provider合成fixture不等于真实邮箱来源验证。

## Fix1：拒绝非文本codec与完整Date语法（review I1/I2）

审查基点：`0da81d16ba31daa43add8a2e6aa51373c15f077e`。
修复源码：`904a29475b5359680a9eb0fdfb911779044e4994`；本节另作文档提交。
只修改 `connectors/gmail/inbound_mime.py`、两个直接覆盖测试文件及正式5a子规格；没有修改
controller ledger/plan/5b brief、旧feedback/HTTP传输、共享DTO、Raw/Gateway规则或其他任务。

### 修复与兼容策略

I1：`_Budget.text`在任何decoder查询/构造前，先把charset与显式文本字符集/别名映射比较。
只将映射内受信的标准库文本codec名交给`codecs.getincrementaldecoder`；不将任意邮件charset
交给codec registry。未知charset、zlib/bz2等压缩、base64/hex/Unicode escape等转换固定
`malformed`，零decoder查询；没有以“返回bytes后捕获类型错误”代替准入判断。
现有逐8192bytes增量解码和UTF-8累计4MiB预算仍执行，HTML原始/文本两视图guard语义不变。

正式子规格列出支持范围：ASCII；UTF-8/16/32及显式LE/BE；ISO-8859-1至11、13至16；
Windows-1250至1258；KOI8-R/U；GB2312/GBK/GB18030、Big5/HKSCS、Shift-JIS/CP932、
EUC-JP/ISO-2022-JP、EUC-KR/CP949/ISO-2022-KR和明确列出的常用别名。未声明charset仍ASCII；
名字大小写不敏感、下划线归为连字符，只在有限映射内匹配。UTF-7及其余未列出编码固定隔离，
这是显式兼容范围限制；不能用任意codec fallback绕过预算。

I2：替换末尾`_ZONE.search`为完整字段`_DATE.fullmatch`，允许可选英文weekday、日/month、
1900–9999四位year、HH:MM[:SS]与恰好一个数字或列出的英文时区。随后才调用
`parsedate_to_datetime`做日期/偏移语义校验及UTC转换。尾部额外token/第二时区不可能通过匹配；
无timezone、-0000、重复Date header、非法日期仍固定`invalid_sent_at`且`sent_at=None`。
自审另发现标准库会把四位`0000/0001/0099`按旧年份规则解释为2000/2001/1999，已以同一完整
语法限定年份，避免字面日期被重解释；没有用尾部黑名单修补。

### 真实RED与针对性GREEN

全部pytest前缀仍为`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest`。

| 精确命令尾部 | 实际结果与含义 |
|---|---|
| `tests/unit/test_email_inbound.py -k 'compression_charset or single_timezone_grammar' -q --tb=short`，修复前 | exit1；5 failed /46 deselected in0.18s。zlib/bz2两个用例观察到真实标准库decoder被查询；`+0000 garbage GMT`、`+0000 -1200`、`GMT GMT`三个用例原为candidate |
| `tests/unit/test_email_inbound.py -q --tb=short`，两项修复及兼容矩阵后 | exit0；94 passed in0.29s |
| `tests/unit/test_email_inbound.py -k zero_padded_legacy_year -q --tb=short`，自审年份约束前 | exit1；3 failed /94 deselected in0.16s，实际观察到0000/0001/0099被改作2000/2001/1999 |
| `tests/unit/test_email_inbound.py -q --tb=short`，最终 | exit0；97 passed in0.28s |
| `tests/integration/test_email_inbound_gateway.py -k 'codec_and_date_rejections or real_archive_matrix_replay_and_sensitive_ledger or html_tags' -q --tb=short` | exit0；6 passed /18 deselected in7.31s。新增4个压缩charset/歧义Date输入，经真实ControlledGmailTransport→Connector→Gateway→PG/MinIO保存原件后固定隔离；逐个真实bounded读回原bytes、sent_at=None、槽为空并保留正确next cursor。另覆盖原归档矩阵与HTML拆分marker |

集成组先于最后三个年份自审用例，年份收紧后重跑最终97项单元；没有把这些分轮数字相加声称
一次103项总跑。两个压缩用例的未压缩输入只有1024 bytes，压缩后小样本直接使用真实zlib/bz2；
observer只观察decoder查询并委托原实现，最终断言**未查询**。没有大内存压缩bomb测试。
另8个非文本/未知charset用例验证零decoder查询；28种常用文本编码/别名检查原文准确解码，
7种合法Date（数字偏移、GMT/UT/EST、无weekday、无秒、大小写、Tab）保持兼容。原无timezone、
重复Date、-0000、UTF-8解码预算、纯Raw/Provider同解析语义等用例随完整单元97项通过。
没有运行旧216组、全Web、真实Gmail/模型/客户动作。

### 门禁、清理与自审

- `.venv/bin/python -m ruff check connectors/gmail/inbound_mime.py tests/unit/test_email_inbound.py tests/integration/test_email_inbound_gateway.py`：exit0，All checks passed。
- `.venv/bin/python -m mypy connectors/gmail/inbound_mime.py`：exit0，1 source file无问题。
- `.venv/bin/python scripts/check_boundaries.py`：exit0，7项通过。
- `.venv/bin/python scripts/scan_sensitive.py connectors/gmail/inbound_mime.py tests/unit/test_email_inbound.py tests/integration/test_email_inbound_gateway.py docs/superpowers/specs/2026-09-05-email-inbound-5a.md`：exit0；源码暂存后`scan_sensitive.py --staged`也exit0，未改scanner/旧基线。
- 源码提交前`git diff --cached --check`：exit0。git仍经Python subprocess捕获stderr，不动共享.git噪声或全局身份。
- 真实集成继续复用Task4同一owner资源组件。fixture finally核验清理成功，关闭client/engine/监听并删除本次Provider SQLite；独立核验本次最新owner的容器残留0、私有config残留0。纯单元没有新外部资源，没有操作其他owner。
- 自审核对charset只映射固定文本decoder、映射发生于解码前、预算没有弱化、Date全字段匹配且无第二时区/旧年份重解释；本批修复不改变5b/Task6消费签名与“解析不等于已过guard”的约束。
