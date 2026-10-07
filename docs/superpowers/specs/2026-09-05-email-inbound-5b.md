# Task5b：耐久整页入站与待核对接入

日期：2026-09-05。实施基点 a55e44a5db7bc2393ff948f827df0df26d0d772e，迁移当前单head 0058。
沿Task5、Task5b brief及ADR0026；5a最终接口不变，Task6正文分类和Task8页面不在本批。

## 整页提交

composition首次生成固定bootstrap_started_at、最多30天after_epoch和initial opaque cursor，
先耐久保存，再允许Gateway读取profile空锚定页。workflow不导入Gmail codec，所有重试复用旧起点。
短事务读cursor结束后调5a ToolEmailInboundReader。取得页后唯一外层PG事务依次获取入站专用
tenant advisory transaction lock、cursor行锁，核完整route和cursor/version CAS，再按provider digest
排序预检整页receipt。Conversations和Outreach bound UoW共用此外层session，不commit/close。
所有Message、InboundMessageStored/outbox、receipt、review和新cursor只commit一次。

同provider digest同fingerprint幂等；异fingerprint在任何域写之前整页拒绝，保留winner。
显式canonical fingerprint版本inbound-receipt-v1覆盖完整route、tenant、identity、provider digest、
原值Message-ID/In-Reply-To、UTC时间、Raw tenant/ID/hash/size、parser版本、guard版本及disposition。
禁止hash已脱敏model_dump；不得保存原header、主题、正文、地址、异常、对象key、凭证引用列。
不同provider但同RFC Message-ID继续由原Conversations reingest核Raw/时间/出站关联一致性；冲突全页回滚。

取消、提交前域或审计故障回滚整页。allow审计先缓冲，成功commit后刷新；刷新失败不能使已提交页重做。
commit/close未知使用新session取得同tenant事务锁，重读真实cursor和完整receipt fingerprint，
只有新cursor和完整页证据均精确一致才确认提交；缺失或不一致固定未确认，不能凭内存报成功。
取消仍向调用方传播，恢复只读耐久事实。Gateway已确认的不可变Raw不属于PG事务，回滚不删。

## 真实关联

仅唯一精确In-Reply-To，先核其中自有route与受信route一致，再调用原
Outreach.resolve_delivery_feedback(DeliveryCorrelationLookup(deterministic_message_id=原值))；
SYSTEM actor只当前identity。原域核实际Attempt SENT、Enrollment与identity，返回完整tenant/
account/contact/enrollment/attempt。无关联及跨tenant固定待核对；不得跨tenant查找、猜From/
References/Subject或使用入站自带幂等header。受信页tenant/route错配是整页完整性失败。
关联只证明引用了自有已发消息，绝非发件人认证或Validated Need。
AUTO/普通退订candidate正常入Message；DSN/ARF/SENT/DRAFT技术skip零Message和反馈业务效果。
触发既有分类的事件是InboundMessageStored，ReplyReceived为分类结果，事件字段不变。

## 技术持久化

0059新增且只新增email_inbound_cursors、email_inbound_receipts、email_inbound_reviews。
三表显式tenant，所有FK含tenant；Raw和Message FK均指同租户真实事实。
cursor主键tenant/mailbox，含identity、route/version、固定起点、opaque cursor、CAS version、
last_succeeded_at、固定blocked_reason、next_retry_at。receipt主键tenant/mailbox/provider digest，
含完整fingerprint、parser/guard版本、disposition、nullable Raw元数据和Message ID、created_at。
review主键tenant/review ID，唯一receipt复合FK、固定reason、nullable Raw ID、created_at。
receipt/review只增；迁移downgrade拒绝任何非空新表，不削弱旧迁移保护。

## 人工绑定、阻断恢复与权限

控制器已采纳：POST /email-inbound/binding仅identity_id；SendingIdentity窄boss权限核真实身份，
记录实际确认员工及时间，Task8必须显式人工绑定，register后不能静默绑定。只接受真人明确选择的
真实SendingIdentity，受信单mailbox/route/secret不受请求覆盖。未绑定准确disabled，同owner重启
复用耐久绑定，同身份重复幂等，禁止暗换identity、编辑cursor、跳到最新或重关联。
永久协议错误/history过期/poison页耐久blocked，暂停自动获取；人工只能POST /email-inbound/retry带expected_version请求同位置重试。
旧页面请求不得清除新版本阻断；next_retry_at未到时人工也不得越过Retry-After，返回waiting。
暂态/429使用1–86400秒（沿5a公开有界Retry-After，不截短Provider等待期限）有界next_retry_at，所有失败状态写入做原version/cursor CAS，
并发旧失败不能覆盖新成功。过期history原位重试仍失败时继续blocked，无reset旁路。
待核对列表仅当前active boss；域公共窄权限端口判定，infra做tenant只读查询，API不自造角色规则。
只展示review ID、固定reason、UTC时间、archived布尔及有限分页；无Raw如实不可下载。
原件只经独立Gateway受权读取，前后重核当前员工、精确review到Raw、有限完整性和bytes预算。
返回attachment/octet-stream与nosniff，禁止HTML执行与模型消费，不为权限伪造Message。

## 生命周期及验收

独立inbound driver装入原scheduler singleton，阶段前后确认同backend；stop和资源释放沿原生命周期。
没有第二正文worker、API/scheduler互导或额外业务系统。受控composition只访问本owner PG/MinIO/
持久ControlledGmailTransport，真实Provider和客户操作not_run。
真实测试只合成Provider响应；出站前置经登记、认证、预热、审批等原公开流程，禁止直接插SENT/
Message/Need/Opportunity。验证页第二项失败全回滚、两session并发、replay/CAS、同ID冲突、
commit未知/取消/close故障、review当前权限/缺Raw、独立scheduler重启/失锁及独占迁移roundtrip。
pytest显式env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1；复用Task4 Supervisor及owned资源，
清理不接触其他owner。按改动跑ruff/mypy/boundaries/增量敏感扫描和diffcheck；API schema原流程生成。

### 5b最终消费者与日志口径

原StandardAuditLogger为独立运行日志，不是持久业务表；保持原TransactionAwareAudit的allow缓冲与提交后flush。
提交前缓冲/deny sink异常、Message、事件/outbox、receipt/review写失败均整页回滚。提交后日志sink失败记录固定错误，
已提交业务不得伪称回滚或重做。

自动入站driver以前述原InboundMessageStored消费者已在同进程完整注册为前提；reply_factory=None的
受控环境仅可人工绑定/状态/待核对查询，scheduler inbound_body能力明确disabled/required_ports_missing。
绑定active仅是绑定技术状态，运行能力必须读取原scheduler健康投影；Task8不得把两者合并为已运行。
无消费者时零fetch、零cursor推进、零新入站事件；Task6补齐原回复组合后自然启用，不造临时队列或空ack。
