# QQ 指定 Attempt 定向收信修正与隔离验收 — 2026-10-15

## 交付结论及授权范围

本补丁仅在 /home/admin/tradeos-qq-reply-20261015 实现并验证，未提交、推送或发布。
基线 HEAD 为 dff351efa1f2ed621aff4aff3fb82a280a49bd6f，即已部署 QQ 私有回信版本的实际提交。
本轮为指定成功发送记录的收信范围修正，不新增数据库迁移、后台、界面或自动发送能力。

用户在协调聊天“自动获客”明确同意：修改收信范围、完成模拟测试。
未执行生产部署、配置变更、重启、真实 QQ 连接/邮件读取/发送、外部模型调用。
没有操作 Simba 或写入老板开发目录；源码未上传外部模型。独立太阳能风扇任务仅存在于隔离 fixture。
此前 qq-private-reply-deployment.md / qq-private-reply-acceptance-20261015.md 是较早阶段历史材料；
本报告说明 dff351e 之后的增量，不能用旧文档的“未部署”或全箱扫描行为解释本轮线上状态。

## 改动和当前权限

生产源文件仅 5 个：

- connectors/qq_mail/inbound.py：只查询明确目标；候选头精确校验、正文前后重验；qic2 游标。
- connectors/qq_mail/inbound_transport.py：只读 IMAP UID SEARCH 定向查询，返回有界候选 UID。
- infra/db/qq_inbound.py：根据指定 Attempt 读取当前 SQL 发送、私有用途、同意和归属事实。
- infra/standalone/settings.py：QQReplySettings.attempt_id，可在准备配置中暂缺，缺失时不能初始化接收或连接邮箱。
- apps/composition_support/email_inbound.py：初始游标与校验器绑定配置中的 Attempt。

复用原入站 Gateway、原件存储、整页事务、Outbox、回复分类、负责人工作项和通知去重；未修改这些核心实现。
测试 7 个文件：新增 test_qq_targeted_search.py、solar_reply_fixture.py，
修改 test_qq_inbound.py、test_qq_inbound_scope.py、qq_reply_chain.py、
test_private_reply_send.py 和 test_manual_email_send_api.py。
既有 campaign 夹具增加有默认值的边界参数，太阳能风扇夹具使用独立名称、solar_fan 类别、单联系人/单封限额。
审批、额度、发件可用性由受控夹具提供，不代表真实审批已经完成。

目标必须为当前租户下、指定发件身份、manual 入组、私有用途记录一致、
sent 且有非空服务商回执的唯一 Attempt；当前销售归属锁、员工/用户有效性、
QQ 本人绑定及 consent/private_reply_test_confirmation 必须仍有效。
仅限 1249583590@qq.com 发往 986647123@qq.com 的本次私有测试。
另一条同邮箱对的历史成功 Attempt 不获得接收授权。

## 查询与停止语义

命令形状为：

```text
UID SEARCH UID <last+1>:<fixed-horizon>
  HEADER In-Reply-To "<指定 Attempt 的原始 Message-ID>"
  HEADER From "986647123@qq.com"
  HEADER To "1249583590@qq.com"
```

只有返回的候选 UID 才会下载有限邮件头；检查唯一且精确的 In-Reply-To、单一收发地址、无 Cc，
再核对当前授权后读取正文。正文头部与先前候选头必须一致；返回页前再次检查授权和 UIDVALIDITY。
单次查询最多 100 个结果，页数量、头部/正文/页字节数继续受原有边界限制。
初始游标就含 Attempt ID，首次查询后还固定原始 Message-ID，
防止换目标后复用相同 Gateway 参数；UIDVALIDITY 和分页固定 horizon 同样被保留。

- 无目标、目标无效/撤权：连接邮箱前拒绝。
- 查询不支持、网络异常、格式异常、超过结果限额、UIDVALIDITY 改变：停止，不回退逐 UID 全箱扫描。
- 原 Gateway 将这些失败持久记为 failed_permanent，原 InboundDriver 将游标记为 provider_permanent，
  next_retry_at=null；后续轮询和重新构造 Store/Driver 均不会自动重试。
- 旧 qic1 游标直接拒绝；不转换、清空或自动重置。未来发布前须重新核对实际已有游标。
- Campaign 暂停停止新发送，不等于停止接收。
- 本轮未实现“收到一封立即自动关闭”。有限运行窗口由后续获批的运维启停控制。

IMAP SEARCH HEADER 是包含匹配而非精确等于，仍可能返回子串或大小写误匹配候选；
本地精确校验拒绝不匹配正文。服务商仍会在邮箱内部搜索索引/邮件头，
不能称为“完全不涉及其他邮箱数据”。References-only、缺少/畸形 In-Reply-To、
客户端改写头部或不满足精确规则的回复可能漏收，不采用宽泛 References 回退。
没有真实连接 QQ，服务器对此命令的支持、索引行为和真实送达尚待限定范围的实测。

## 本轮验证证据

证据根：/home/admin/tradeos-qq-reply-20261015/.test-results/。
下列批次可能重叠，不能相加。

| 检查 | 最终结果 | 日志 |
| --- | --- | --- |
| 最终相关后端回归 | 661 passed，0 failed，无 skip/deselect，123.08 秒，EXIT 0 | check-1792045127227648410.txt |
| 从空 fixture 数据库迁移到现有 0077 | EXIT 0；未新增 migration | check-1792044990645232736.txt |
| 全部变更 Python 的 Ruff | All checks passed，EXIT 0 | check-1792044979222135725.txt |
| scripts/check_boundaries.py | 所有结构自检通过，EXIT 0 | check-1792045014014797313.txt |
| git diff --check | EXIT 0 | targeted-query-evidence.json |
| 后端 mypy | 未运行，测试环境未安装；未安装依赖 | check-1792044533101975429.txt |
| 生产与老板目录保全 | 比对通过 | targeted-query-preservation-before.json / targeted-query-preservation-after.json |

完整 pytest 参数在 targeted-final-command.json 和最终日志的 COMMAND 中。
最终测试数据库为 qq_targeted_accept_20261015；早期独占测试库保留。
测试入口使用以下隔离执行方式（数据库为本工程 Unix socket fixture，非生产）：

```text
unshare --net -- runuser -u admin -- env -i
  HOME=/home/admin PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1
  PYTHONPATH=/home/admin/tradeos-qq-reply-20261015
  DATABASE_URL=<fixture socket URL> TEST_DATABASE_URL=<同一 fixture URL>
  /home/admin/tradeos-qq-reply-20261015/.test-venv/bin/python
  -m pytest -q -rs <targeted-final-command.json 中的精确文件集合>
```

容器 tradeos-qq-reply-fixture-20261015 使用 network=none，
只有本隔离工程 .test-pg-data、.test-pg-socket 两个挂载。
真实 SQL/Gateway/工作流/通知实现参与验证，邮箱、模型和对象传输为内存端口，
没有真实 SMTP、IMAP、公网模型或外部源码上传。最终容器停止状态在 targeted-query-evidence.json 中记录。
测试数据库、日志和容器保留。

覆盖正确直接回复三类结果（明确兴趣、愿意继续/WhatsApp、无当前需求）；
原手动发送 API → 成功 Attempt → 定向接收 → 原件/Outbox → 模型 Gateway →
分类/原负责人工作项/HTTP 处理 → 老板站内通知，以及重复轮询/重放不重复处理。
正向回复不冒充正式 Need、Opportunity 或 Handoff。
覆盖无目标、其他成功历史 Attempt、未知发送、撤销同意、转派负责人、
子串误匹配、References-only、抄送/多值头、查询失败/超限和 UIDVALIDITY 变化。
新增四种失败集成用例确认：正文/候选头/原件/入站消息/分类模型/工作项均未处理，
仅一次查询，持久永久失败，后续轮询和时钟前进一天后的 Driver 重建仍不重试。
自动发送独立关闭及原流程相关回归通过。

这不是全仓测试。本次集合不含 test_conversations_correction.py、
test_notification_dedup.py 和 test_private_reply_migration.py；
它们此前版本的补验不能算作本轮结果。本补丁未修改迁移和前端，因此本轮未重跑前端构建或浏览器交互。
中间 Ruff 排序和新夹具私有域导入检查失败已修复，未增加检查豁免或私有域动态导入。
初轮 657 项通过后增加上述四种失败用例，最终为同一新库一次 661 项通过。

## 线上保护核对

本轮结束核对时：

- current 仍为 /srv/tradeos/releases/20261015-qq-private-3521dcf，实际提交 dff351efa1f2ed621aff4aff3fb82a280a49bd6f。
- tradeos active，MainPID=480861，启动时间仍为 2026-10-15 13:20:24 CST；本轮未重启。
- 发布目录 2,936 个文件（排除 __pycache__）与本轮基线逐个匹配，无新增、缺失或改变。
- 模型配置 SHA256 与基线一致；reply_enabled=false，automatic_send_enabled 缺省且默认 false，qq_reply 未配置。
- 老板目录 /srv/tradeos/development/TradeOS 的 HEAD、Git 状态及已有脏文件/未跟踪文件哈希与基线一致。
- 本轮保全不冒称完整生产数据库快照；测试只使用独占 fixture 数据库。

## 下一阶段发布与有限窗口计划（未执行）

1. 取得具体发布授权后，重新核对线上 current/提交、源码哈希、0077 schema、配置、入站游标、
   老板目录及当前业务写入。如果期间版本改变，先在新的隔离版本整合并验证。
   审查本补丁与完整 SHA256 清单，构建独立 release；不能在老板目录发布。
2. 按现有运维机制准备可恢复的当时备份和兼容 0077 的回退版本。
   本补丁没有新迁移，不执行 0077→0076 降级，也不恢复较早部署快照覆盖后续业务写入。
   在批准的短维护窗口切换新 release、重启 TradeOS、验证 API/scheduler/notification，
   初始保持接收及自动发送关闭。
3. 使用现有任务与审批机制新建独立太阳能风扇单封任务：
   明确产品、收件邮箱、负责人、唯一发件身份、单步/单封额度与私有用途同意，
   审批边界覆盖本产品与本次试验。不得复用三轮车审批。本轮仅有 fixture，没有创建线上任务。
4. 真实收信、发信和测试正文模型处理需要明确授权范围。
   先确认邮件内容及接收时间范围，按原手动流程发送一次；
   仅在本次 Attempt 为 sent 且有服务商回执时填入 qq_reply.attempt_id。
   unknown 不重发，不能改 key 或删账本制造第二次机会。
   缺失目标的准备配置不能作为已可收信证据。
5. 启用前先落实与当前交互/调度循环独立的超时收尾机制：
   用既有主机运维调度能力预置到期处理，记录负责人、截止时间、验证路径和人工兜底；
   到期将 reply_enabled=false、移除 qq_reply 配置，并保持 automatic_send_enabled=false，
   按现有服务方式只重启 TradeOS 使配置生效。没有在本轮创建 timer、启停脚本或修改生产配置。
   正常结束也执行同一关闭流程，再检查配置、进程启动时间及不再发生新接收/分类。
6. 运维截止是停止新工作和请求退出的时间，不保证到点瞬间中断。
   优雅退出可能等待当前阶段；已提交到模型/邮件服务商的请求不能撤回。
   若必须硬性中断，需先单独审定超时/强制停止与未知结果处理边界，不承诺靠 Campaign 暂停实现。
   记录实际退出及关闭完成时间，保留 Attempt、游标、模型和通知账本，不删除或自动重试。
7. 首次受限实测必须核对 QQ SEARCH 实际支持、SMTP 接受与真实送达、
   唯一回复关联、模型真实分类、负责人工作项和老板通知，以及无重复处理。
   不支持或异常即按永久阻断停止，不能临时打开全箱扫描。
   发现 qic1 或不同目标游标时停止评审，不能自动换版本/清空游标绕过绑定。

补丁与文件清单：.test-results/targeted-query.patch、
.test-results/targeted-query-manifest.json；证据索引：.test-results/targeted-query-evidence.json。
