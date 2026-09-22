# Task13 限定修复报告 · round 1/5

状态：**DONE_WITH_CONCERNS**，I1/P2与M1/M2的实施修复及局部验证完成，等待原review_task13限定复审。
本报告不提前宣布Task13或最终全分支审查通过。实际日期：2026-09-06。
FIX BASE `fe6f7dc2622391c9359866dd90fafbf3573ebdb5`；FIX SOURCE `041bc741e0cd57dc9ed942d3a53f116b4fdffcbd`。

## 修复内容

- I1：本批验收helper使用ExitStack/AsyncExitStack，每创建engine/transport/S3 client立即注册关闭。
  关闭回调各自捕获普通关闭异常并仅追加固定类别，其他已注册资源仍被尝试关闭；存在主失败时保留原主失败，
  无主失败但有关闭错误则固定`backup_resource_cleanup_failed`，不能报告通过。
  `_artifact`在settings/transport构造前即登记engine，第二client构造前已登记first；正常证据新增
  `resource_cleanup_errors`。不修改生产Connector、Supervisor或业务规则。
- M1：原logging.disable状态由测试最外层finally恢复，覆盖运行、资源清理和证据写入。
  证据写失败不会覆盖已有主失败；无主失败时固定`backup_evidence_write_failed`，不回显OSError原文。
- M2：仅移除正式索引历史表格中间空行，32条历史导航均属于连续的同一GFM表格；历史文件/hash不变。

本轮7个源码/证据文件见`fix1-source-commit.json`；测试源码hash见`task-13-fix1-gates.json`和
新`backup-restore.json`。未新增业务backup/restore接口、launcher、公共协议、字段或迁移。

## 真实RED/GREEN与正常恢复复验

新增`tests/unit/test_backup_restore_cleanup.py`全部使用无DB/无Docker/无网络替身，初始4 failed/0.48s
分别命中engine未dispose、第二client构造失败first未close、first.close失败second未close、
证据写失败后全局日志disable仍50（原0），不是fixture setup失败。关闭失败替身只用合成错误。

修复后首轮4 passed/0.41s；补精确安全关闭类别断言后4 passed/0.43s；该轮ruff发现1个PIE807，
将空dict lambda改为dict后最终unit4 passed/0.44s、ruff通过。每轮结果独立，不相加。

| 最终命令/检查 | 结果 | 耗时 |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/unit/test_backup_restore_cleanup.py -q --tb=short` | exit0 / 4 passed | 0.44s |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_web_core_backup_restore.py -q --tb=short` | exit0 / 1 passed，新owner正常复验一次 | 9.67s |
| `.venv/bin/python scripts/check_boundaries.py` | exit0 / 7项PASS | 4.868s |
| `.venv/bin/python -m ruff check tests/integration/test_web_core_backup_restore.py tests/unit/test_backup_restore_cleanup.py --output-format concise` | exit0 | 0.083s |
| 显式本轮文件敏感扫描 | exit0 | 0.028s |
| `.venv/bin/python scripts/scan_sensitive.py --staged` | exit0 | 0.155s |
| `git diff --cached --check` | exit0 | 未单独计时 |
| 历史表格连续行/索引链接 | 32行 / 66链接 / 缺失0 | 未单独计时 |

关闭失败时“其余资源仍被尝试释放”由替身回归证明，不宣称真实SDK关闭失败必然成功释放。
主失败保留和固定安全类别已断言；证据写失败测试也断言原主失败及日志状态恢复。

新正常复验 source owner `25cfa8c8ff3445cfa969c9e5474df64f`，target owner `f9e358faf5424a4cb2e6e619d36c225e`；
两者metadata hash `c1df0afa630f47c29dee24c67ea62822dc3939c96cf85e44021be51ddd345378`一致，
原件SHA256 `fa9a230d284f919df4a6d374649935e4f4ad53ae95ade1a56a903f00b1d32de9`一致，
源复读不变，same-owner/nonempty目标拒绝。resource_cleanup_errors=[]；两组精确owner清理errors=[]、
private_config_removed=true、processes=[]、四listener fd=-1。所有DB操作串行，没有与其他DB测试重叠。

审查前9.31s正常路径证据精确复制为`backup-restore-pre-review.json`；旧证据仍有效，但没有当时
I1/M1关闭失败覆盖，不能把旧正常通过升级成异常路径已验。新结果写`backup-restore.json`，不覆盖历史数字。

## 范围与保留限制

没有重跑Task12全仓、Web或旧archive审计，没有改生产代码。Task12 48e4465全量9318与
7e10383主链1的既有版本分栏保持，不与本轮unit/恢复数量合计。
本轮仅静止owned PG/原件恢复；不证明PITR、运行中快照、生产灾备或受控邮箱/模型/游标恢复。
Mac完整报价/自动寻源准入未配置，Linux不同owner/Need；真实Provider、共享认证/部署、桌面和
通用Agent/Browser/Catalog后续消费者限制全部保留。没有push/merge/部署/外发/真实凭证读取。

source提交仅明确7路径；root的progress/review-context/final-context未stage。Git stderr仅计数：
stage7、staged列表11、diff-check15、source commit391行；不回显AppleDouble或维修共享.git。
后续只由同reviewer读取fixdiff限定复审；以下首批报告保留历史交付、正常路径和失败记录，不代替本节。

---

# 首次交付历史报告（SOURCE 0bfb4d1）

状态：**DONE_WITH_CONCERNS**。本批实现/局部自检完成，待控制者派独立Task13审查及最终全分支审查；不预写通过。
实际日期：2026-09-06。BASE `aa24508dc77fac1e6cc2e21225cb3dcd21dd240b`。
SOURCE `0bfb4d11a8e3e8eb7678d48be091736bd399870b`；本机 `codex/web-core-completion` 工作树提交，未push/merge/部署/真实外发。
本批唯一实现者，无子代理或审查代理。

## 交付文件与接口

- `docs/superpowers/specs/2026-09-06-web-core-delivery.md`先行记录详细规格、既定边界和最小恢复方案。
- README/ROADMAP/HANDBOOK逐条同步本机受控Web闭环、四应用、已验Phase2切片和真实运营未验收；
  HANDBOOK七个历史切片逐项注明当前状态，未取消审批/根九条硬边界。
- `docs/operations/web-core-local.md`补Python3.12+/Node24依赖来源、原启动/停止/HUP、精确owned清理、
  typed研究/单Provider触达/完整回复、预算unknown、Mac/Linux分环境命令和恢复演练界限。
- `docs/operations/web-core-capability-matrix.md`主表从Task0 ec801a8/0058更新为Task12实际版本/0059；
  页面/API/唯一执行者/disabled/not_run分栏。旧表独立历史归档，不混用“需补组合”与“已完成”。
- `docs/architecture/12-client-capability-boundaries.md`逐项给文件选择/监听、引用解析、通知、浏览器、
  能力发现的owner、输入/输出、权限与失败，引用真实Work Uploads、Artifact、NotificationChannel、
  BrowserJob/Repository/Reader、RuntimeCapability和当前identity接口。无Tauri、空目录、假IPC、客户端自报授权。
- `apps/AGENTS.md`仅同步Web浅骨架与Agent/Browser任务来源的过期事实；根AGENTS/GLOSSARY未改。
- `domains/conversations/service_impl.py`及`tests/integration/test_conversations_correction.py`仅修两处Task7
  docstring；去docstring AST与BASE均完全相同，业务未变。PermissionDenied与已授权未分类ValidationError准确分开。
- `tests/integration/test_web_core_backup_restore.py`仅为本批静止owned数据恢复验收，不增加业务产品入口。
- `docs/acceptance/web-core-delivery/`正式保存安全报告/审查、Task9完整spec、ledger原文字节快照、
  safe JSON、截图和索引；Task12已tracked证据与四张代表图直接复用。

没有公共业务API/Protocol/字段/事件/迁移变更，未引入新的长期规则入口。
58个源码/文档/证据文件在SOURCE提交；大部分新增行是安全历史快照，可按hash证明复制，无需重审旧实现。
正式导航入口为`docs/acceptance/web-core-delivery/README.md`；当前报告也在该目录持久保存。

## 静止owned备份恢复实际验收

原入口没有可复用backup脚手架；已向root报告具名最小方案并获准。复用原Supervisor源基础设施、
OwnedContainers、ControlledConfig与RawArtifactStore；不构建新launcher，也不接受用户运行库/外部dump参数。

- source owner `33b9119b832e48a5af47054d47a6fa22`；target owner `d116ead76b1643ea80d35ce3cfe7dea2`，不同owner，各有明确两个容器ID。
- source由原入口迁移至0059并初始化合成员工，原RawArtifactStore写一份合成email_raw；没有应用写入者。
- target为另一全新空PG/MinIO，不先迁移/初始化、不含业务数据。PG恢复前public关系为空，原件复制前bucket为空；
  同owner和恢复后的非空目标均拒绝，不关不可变约束、不覆盖运行库。
- source pg_dump→内存tar→target owned容器0600临时dump→pg_restore；对象由本source bucket复制到target bucket。
  dump不回显、不保留。ControlledConfig只在测试内存解析本轮私有配置，未读取真实凭证/环境/SQLite。
- 恢复后通过原Store读取：metadata hash `ab168a0774f8ca60d641bd158c68e59f1c0ef3c92ca9908d701b9b9ff75cfac9`、
  原件SHA256 `fa9a230d284f919df4a6d374649935e4f4ad53ae95ade1a56a903f00b1d32de9`，源/目标一致，tenant raw row_count=1，head0059，源复读不变。
- 最终先关闭Store transport、engine及S3 clients，再按owner+精确容器ID由原生命周期删除；两组errors=[]、
  私有config删除、进程列表空、四个listener fd均-1。迁移/身份初始化子进程由原OwnedProcess验证PID出生时间。
  安全证据 `docs/acceptance/web-core-delivery/backup-restore.json`，含精确ID和测试源码SHA256。

仅证明静止owned PG+一份原件到另一空目标的元数据/原件一致；不证明运行中跨PG/S3快照、PITR、
保留/轮换、生产恢复、受控邮箱/模型/游标灾备或自动恢复既有launcher。完整停止后新启动是新空环境。

## 测试与局部门禁

本批仅新增验收测试；没有待修生产backup实现，因此没有伪造“业务RED”。同owner/非空目标反例在实际演练中执行。
数据库测试始终单进程串行，source/target依次创建和操作，未与其它DB测试重叠。

| 命令/检查 | exit / 实际结果 | 耗时 |
| --- | --- | --- |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_web_core_backup_restore.py -q --tb=short` 首轮 | 0 / 1 passed | 10.64s |
| 同命令，补源复读不变与安全cleanup投影后最终 | 0 / 1 passed | 9.31s |
| `.venv/bin/python scripts/check_boundaries.py` | 0 / 七项PASS | 4.857s |
| `.venv/bin/python -m ruff check tests/integration/test_web_core_backup_restore.py domains/conversations/service_impl.py tests/integration/test_conversations_correction.py --output-format concise` | 0 | 0.091s |
| `.venv/bin/python scripts/scan_sensitive.py` 显式本批文件清单 | 0 / 无命中 | 0.074s |
| `.venv/bin/python scripts/scan_sensitive.py --staged` | 0 / 无命中 | 0.532s |
| `git diff --check` | 0 | 0.037s |
| `git diff --cached --check` | 0 | 未单独计时 |
| 当前修改文档相对Markdown链接逐项exists检查 | 0 / 189链接，缺失0 | 0.002s |
| 两Task7文件去docstring AST与BASE比较 | 完全相同 | 未单独计时 |
| 32份历史source/archive SHA256比对 | 一致，无差异 | 未单独计时 |

完整命令参数/显式文件列表与安全输出见`task-13-gates.json`；恢复测试源码SHA与`backup-restore.json`一致。
结构检查初次误用系统`python3`3.9.6，exit1/5.195s，无法解析本仓Python3.12语法；
未改业务源码，切换已声明的Python3.12.14通过。HANDBOOK已明确激活venv或使用`.venv/bin/python`。

未重跑Task12未变全量；48e4465完整9318与7e10383仅测试I1修复后主链1仍分栏，绝不合计。
Web源码未改，因此不重复411项/构建。备份测试两轮是不同断言版本，不合计为2项完整结果。

## 失败历史与文档校正

1. 新验收文件ruff初次1个ISC004隐式字符串连接格式问题，补括号后exit0；无数据库已启动。
2. 临时归档helper从/tmp运行未给PYTHONPATH，ModuleNotFoundError，发生在任何复制前；显式PYTHONPATH后完成。
3. 默认系统Python3.9结构parse失败如上，不误报业务边界缺陷，也不隐藏失败。
4. 文档链接自检定位README历史不存在的0005-modular-monolith ADR，改指真实总体架构；最终189链接均存在。
5. Task12已记录的首轮98失败、I1旧独立Campaign审批声明撤回、DB stop/start随机端口失败、
   Linux独立报价与Mac限制、旧cleanup_unknown及后续核验等全部按原文保留，不倒写历史结果。

## 持久证据、截图与历史复制

`archive-manifest.json`记录完整progress快照截止`2026-09-06T11:53:12.025277+00:00`、每份source/archive SHA256及大小；
Ruling所在行87（不是问题数）。分批Task3a/3b/5a/5b额外报告也按精确hash保存，共32份。
历史原文内的“等待/未通过”等状态属于对应时点；当前状态由正式主矩阵、Task12最终review和后续root裁定解释。
所有Ruling理由和代价保留；Task13和最终review尚未发生的裁定留root追加，没有提前填通过。

本批实际直接view：最终Mac Need1440/Handoff390、最终Linux Quote390/精确成本1440，复用原tracked路径。
Handoff图是待接管；accepted只由真实HTTP/proof证明。报价图不是PDF内容证明。Mac/Linux不同owner/Need。
另直接view并归档Task8 Inbox原件390、Task9 Settings未知390、Task11成本缺项390三张历史代表图；
分别标明历史源码/视觉边界，Task9受控HTTP响应图不充当Task12真实DB故障证明。

六张已tracked Catalog截图逐张view后安全归档：policy pending、审批深链、proposal desktop/390、
queued desktop、stale390；原六路径仅恢复至Task13 BASE，archive/base SHA均记manifest。
未操作其他Task10/12历史output、未git clean/prune、未维修共享.git。Git AppleDouble只记stderr行数：
源stage58、staged列表13、staged diff-check22、source commit337；不回显或维修该噪声。

报告包首次普通git add因本plan目录被ignore拒绝（exit1，stderr仅计7行）；按任务明确要求仅对这份
具名report使用git add -f，不扩展到其他scratch文件。报告包64个链接0缺失，显式敏感扫描exit0/0.059s。

## 保留限制与后续责任

- 当前只有本机受控Web闭环完成；dev角色不是真实登录。多人后端会话/撤销/CSRF/TLS/共享部署未验收。
- Mac报价/自动寻源准入未配置；Linux独立原报价/PDF链不同owner/Need，不能冒称Mac统一入口完整报价。
- 真实Tavily/Hunter/Gmail/模型/供应商/生产账户未运行；typed合成研究/单Provider验证不是真实readiness。
- Agent通用任务源、policy/descriptor/模型消费者、Browser任务来源、Catalog培养消费者与桌面未实现或disabled。
- HUP只覆盖四应用；PG单独stop/start随机公开端口可能变，原配置不自动更新，A5只证固定端点短断。
- 联系人秒/分钟内存限流不保证跨重启额度；总模型token、费率、人工工时和单位合格贸易机会成本未知，不写0。
- 恢复演练不是备份产品/PITR/运营中灾备；本批只做用户已授权的静止owned目标测试。
- 待root完成独立Task13 review、最终全分支review、追加裁定及本plan scratch收口；本报告不代替这些门禁。
