# Phase2成本与报价批次验收

实施日期2026-08-29，文件名沿用计划日期。
T10验收任务BASE `ce2e76a71d10ce855a47f6b93fa7b242485883fe`；
成本报价本批全分支BASE `809d7b6eee743a10a0e7baced5c98b0bf1c7a336`。
本文件随T10实际验收补全；未完成的门不视为通过。没有push、merge、部署或生产启用。

## 证据边界

| 层次 | 当前实际证据 |
|---|---|
| 工程定向 | 四文件mypy36项本分支问题RED→GREEN；三个旧测试3failed→29passed；成本unit/PG230passed |
| 公开前置与真实工厂 | Linux真实API lifespan、独立worker runtime/engine/outbox、真实PG/parser、受控boto SDK；真实客户回复Verifier→Need晋升→Opportunity分配→成本→报价→独立审批→PDF，1passed |
| 并发与恢复 | 真实多连接锁/报价/审批/文件恢复112passed；成本冻结另已在230项中覆盖，不累加重叠场景；对象重建不是独立OS崩溃隔离 |
| Browser | initial 6e时点完整E2E 9passed；真实Uvicorn/三Vite/Chromium完成单位/政策/证据/成本/修订/独立审批/403/PDF下载；controller另实际检查1280/390和PDF全部1页；Fix1当前状态见下节 |
| 真实供应商/对象存储 | not_run；PDF输入是受控文本，SDK无真实网络 |
| 真实客户回复/可达性/历史投递 | not_run；fixture公开记录模拟历史，真实Verifier仍要求持久SENT关联 |
| 本次报价真实发送 | not_run；受控链也未发送，quote approved、机会未won、需求未fulfilled |
| 前端门 | initial 6e为247passed；Fix1裁定127修后21文件252passed，typecheck/build/生成类型零漂移通过，lint 0errors、133既有warnings |
| 后端全量 | initial 6e为6785passed；Fix1保留完整命令6819passed/11deselected、1105.00s，新增诊断过滤器另由73定向覆盖；源码时点差异见下节 |
| 独立整项审查 | 首审3个Important、Fix1新增N1及Fix2残余均已处理；Fix3核心复审通过，Fix4/Fix5补齐测试失败路径自有进程回收，最终Fix5复审clean |

### Fix轮次状态

Fix BASE `6f7ddf2614834336dad6bbb327a61c1ecd2001cc`。首审发现：容器退出失败/缺回执未传回宿主，
半开HTTP连接未计入8并发且15秒只是空闲超时，准备/同步Docker/清理未受300/900秒全栈watchdog约束。
初始6785+9是真实历史通过，但不能证明这三条边界。

Fix1针对退出验收、接入前容量/绝对时钟、独立子进程监督和本次资源所有权增加保护。
当前73项定向通过，含新增诊断过滤器、真实Chromium独立进程组/哨兵、取消与清理未知；真实公开闭环与原A宿主8项通过，
正常DOM、真实API137拒绝、初始化后宿主卡住的fallback清理、visual自动DOM后父SIGTERM短路径均取得定向证据。
完整E2E本轮实际`1 failed, 10 passed, 6819 deselected in 112.11s`：客户单位确认点击超时，
清理和worker退出通过；此旧失败不能回溯认定与后续探针同因。真实早输入探针独立证明初始单位GET会被
source/unit编辑取消。裁定127仅修改单位表单读取scope及Need切换同步清旧hash/receipt，保留所有身份/确认/原文门。
新增deferred测试2failed→完整55passed；修后真实早输入GET返回、摘要出现并按实际hash确认，随后正常DOM通过，
合计2passed53.76s。完整not-e2e沿原进程最终6819passed/11deselected、1105.00s、exit0；
修后完整E2E（含保留探针共12项）实际12passed/6820deselected、139.97s、exit0。
后端/监督算法及Linux镜像源码未在not-e2e启动后变化，新增诊断测试另验；不得把不同快照说成同一全仓冻结。
当前测试镜像为`sha256:58270a3f8a6ee6e97603cd61e8fd1c482f65d037be596a39a6f3130e48fc5cb2`，
后端/renderer未修改，前端仅上述单位读取UX修复。旧五件批准样例保留原字节；Fix1新产物不冒充controller复核样例。
最终E2E本地产物`output/playwright/t10-f17de10f2da0466a9ffd0ff383a46d50/`，PDF1页42048bytes，
SHA256 `ddba1899167ff0d0db3a243022abf6b8ecc6bb7530212d533146f2bf25e02050`；
自动实际下载/API/result hash一致。worker cycles20、禁用工具0、child/cleanup verified；
本次159个历史观测PID均退出、四端口关闭、t10容器/network空。新产物不纳入本轮提交。

Fix1复审确认原I1/I2/I3和单位读取修复已解决，但指出周期PPID快照会漏两次观察间生成并
孤儿化的独立session子孙。Fix2真实进程RED证实run已code2仍误报cleanup_verified；
正常DOM存在这种泄漏尚未实证。修正后PPID快照只用于尽力定位/清理已知PID；只有固定入口
exit0、唯一正常终态回执、无父强杀/主异常、正常结束（或visual正常请求停止），再与已知PID、
精确owner Docker资源及端口清理合取才可verified。任意异常/非零/重复或缺回执/强杀/取消均
unknown且非零，即使已知资源另已证清理；这不宣称任意OS子孙树封闭。Fix2定向77passed，
真实runtime137单目标取得固定owned/exit137回执并按预期unknown；
完整E2E实际12passed/6824deselected、116.42s，正常fixed入口verified，两个故障入口unknown且
分别核已知PID、四端口与精确owner Docker资源已清。首次后端完整门因既有bridge关闭测试只接受
EOF、不接受本机合法RST而1failed/6823passed；仅收口该测试契约后最终6824passed/12deselected、
1056.94s，Ruff与结构七项通过。Fix2候选已以
`c08885d0a35aeaaf47bc191230d1598435420da4`提交。独立增量复审随后确认N1仍有一处残余：
已观察PID虽然以birth防复用，旧实现仍要求PGID不变；child改变进程组并在父退出后被重新托管时会从
`live`消失，仍可能误报verified。Fix3仅修这一身份条件、补两个真实故障入口的独立sentinel保护并
更正文档。真实RED为同一PID/birth的child改变PGID、reparent后仍活，而监督器误报
`cleanup_verified=true/code=0`；移除PGID身份等值、继续以PID+birth防复用后，同例回收该child并
返回unknown/code2，生命周期25passed。runtime137使用test-only四阶段固定回执连续3次到`exit137`，
host_stall到`owner_verified`；两者独立sentinel均存活且finally只回收自身。两次历史pre-final回执缺失原因
仍未明，阶段文件不是公共协议，也不能由后续通过倒推历史根因已解决。

Fix3冻结源码的完整not-e2e为`6825 passed, 12 deselected in 1064.45s`；完整E2E为
`12 passed, 6825 deselected in 115.75s`。正常fixed入口code0/verified，两个真实故障入口均
unknown/nonzero；最终四个生命周期共观察192个互异PID且均已退出，所有记录loopback端口关闭，T10容器/network为空。
正常自动链产物在`output/playwright/t10-2377b2119bc04413873cd22be3a4807f/`，PDF一页42047bytes、
SHA256 `0346cfb7896bbbb65343ea3b8c6c3dba362b7472121d053916c806c55f23d17a`；不覆盖或纳入旧五件批准样例。
Fix3独立增量复审确认N1功能残余、两个真实故障sentinel和文档矛盾均已解决；仅发现新增真实进程
测试的失败路径没有确认自有child最终消失。Fix4增加精确PID有界确认后，复审又发现child超时会先抛出而
跳过sentinel回收。最终Fix5把两类清理失败延后汇总：始终先精确terminate/wait sentinel，必要时只对
本测试持有的subprocess执行kill兜底并再次有界等待，确认两个PID后再用`ExceptionGroup`报告全部错误。
单项1passed、生命周期单元文件25passed、Ruff通过；最终独立增量复审无Critical/Important/Minor。
这些Fix4/Fix5只改测试失败路径，不改变Fix3冻结产品/监督算法或完整回归结论。T10整项审查完成；
成本报价全分支仍须另做从`809d7b6`起点的完整审查，不能据T10通过宣称整个Phase2完成。

### 全分支完整审查与统一修复

全分支独立审查从`809d7b6eee743a10a0e7baced5c98b0bf1c7a336`连续读取至
`a667938d7182feb5ab38a5381607fb2e944c1f3b`的92个提交、89658行/4020243bytes完整diff。
无Critical；唯一Important为`SqlAlchemyNeedUnitUnitOfWork.__aenter__`在两个事务级`set_config`
await被取消时会越过rollback/close，可能遗留连接、事务或锁。另9项均裁定为非阻断测试覆盖、失败路径
资源卫生或结构维护债，继续显式保留，不用全绿结果将其抹除。

统一修复提交`969a915db94b536a1762d2f6a0d8c1a33d455723`只改NeedUnit UoW进入失败清理并新增
单元测试：捕获`BaseException`后依次尽力rollback、close，二次SQLAlchemy/Runtime/CancelledError
均不覆盖首个取消或其他终止异常；普通异常仍按首因固定映射，清理日志无参数、无exc_info和异常正文。
TDD旧实现21failed，修后21passed；真实PG `test_need_units` 26passed、`test_need_unit_access`
6passed，Ruff和单文件mypy通过。唯一scoped独立复审判定原Important ADDRESSED、无新增
Critical/Important/Minor，9项非阻断债未被直接恶化。

因此本成本报价批次工程分支达到审查可合并状态；这不是实际合并授权，也不改变真实供应商、真实对象存储
网络、真实可达性/投递/客户回复、本次报价发送、生产迁移/部署/启用均为`not_run`的边界。

首个闭环测试缺fixture，后续DTO/CORS/来源权限/选区手势/审批视图问题均为夹具错误，未改生产校验。
正确夹具后业务链直接GREEN，属于新增保护。完整命令、原始失败与实际输出保留于T10报告。
真实DOM随后发现两项前端缺口，先RED后最小修复：重复原生同选区事件取消locator请求，
以及客户单位裸msg_ID与message:来源映射不一致。保留真正来源/选区/身份变化的失效门，
拒绝upload/双前缀/错消息，未改后端ACL/DTO/单位词法；相关50项前端测试通过。

## 复现

使用项目Python3.12、现有Node/npm、Docker/Chromium依赖，显式PYTHONPATH为当前checkout，
`PYTHON_DOTENV_DISABLED=1`并解除`TEST_DATABASE_URL`，不读取生产`.env`或自行替换已验base。
固定Linux builder只覆盖具名白名单；原A/B入口指向和预算保留，A另显式固定容器command以免继承T10的CMD。

```sh
env -u TEST_DATABASE_URL python3 -m pytest tests/integration/test_costing_quote_closed_loop.py -q --tb=short -s
env -u TEST_DATABASE_URL python3 -m pytest tests/e2e/test_costing_quote_browser.py -q --tb=short -s
```

旧A/B完整套件另须显式`QUOTE_EVIDENCE_IMAGE_ID`。controller视觉时源码产物为ad004566…；
自审修正历史类别投影后，保留样例对应源码产物为
`sha256:80727840105266cdf3236a1b6a31cdbd9a6f062d7c9ecc99748f91a7699dc189`。
第一轮全量漏该变量，明确中断（1failed/699passed/9deselected、exit2），不是最终通过；
补配置后原Linux资源目标1passed108.15s，原入口/预算未改，再执行完整门。
补配置的第二轮在自审发现helper类别硬编码后由controller裁定中断（696passed、exit2），
不是最终全量通过。新增6项投影契约先全RED再GREEN，helper只从同租户公开get_hypothesis
读取并核对account、inferred/contacting状态后投影实际category；缺失/错企业/拒绝或已验证态均拒绝。
假设可用于历史触达不等于已验证需求，真实回复Verifier与公开晋升仍执行。
新image真实闭环7passed（6契约+1业务链）、整DOM1passed38.66s，之后再执行最终完整门。

该完整后端轮实际44failed/6741passed/9deselected，耗时1141.69秒，未通过。
失败为Linux用例宿主归属、旧迁移/ORM/事件快照和旧demo测试污染全局日志级别。
仅具名测试修正，未修改生产schema/日志或删断言；日志原顺序59passed，显式快照/事件46passed，
quota/单位/审批/旧新artifact/demo真实PG103passed。另发现A包装器继承T10 image默认CMD，
此前wrapper的passed不是有效A证据；已固定恢复A入口并保留RED，新镜像实际A内264passed36.08s、
host 1passed38.22s，首来源测试1次和两个裁剪反例2次均经固定身份记录及host断言核实。
原parser资源runner始终显式命令，其既有结果不受影响。

initial T10回归时点镜像为`sha256:6e122e8f484be2d31fbb5e6fb3ef63991a319a6e775dbd62a7f1b316bd1b1ddf`。
它相对807278只更新测试入口、Linux归属与执行证据；生产UI/后端/renderer及公开闭环helper未变。
本轮完整not-e2e/e2e运行期间源码冻结，仅完善文档。下列已批准五件807278样例保留原字节，
不称为6e122e8f镜像产物，新回归输出另记。

initial完整not-e2e实际`6785 passed, 9 deselected in 1052.59s (0:17:32)`、exit0；
这9项是另一个完整E2E命令覆盖的marker互斥选择，无skip。完整原始记录由T10实施报告索引保留。
initial时点全库Ruff与结构七项通过；四文件mypy、前端命令之后对应代码在initial验收收尾未再变化。

initial 6e122e8f完整E2E实际`9 passed, 6785 deselected in 72.50s`。其本地新产物在
`output/playwright/t10-5bf703567e414666891b3b16a5d1e58d/`，1页42047bytes、
SHA256 `7557d8839ce85094c21a904852c89eda180c4d1617cbf2e4dc66c83ada3c7838`，
自动实际下载与API/result hash一致；此目录不纳入提交，也不冒称另有controller视觉复验。

API/PG仅internal网络；本机Docker实际Ports为空，Mac无法直连，使用已批准的有限测试HTTP桥：
host仅loopback、固定容器relay→127.0.0.1:8000真实Uvicorn、每请求15秒/请求响应各1MiB/最多8并发。
不开放任意URL、目标、命令或重定向，不证明原生Docker发布或生产入口。
当前自动总生命周期为 integration 300 秒、browser 360 秒、协调视觉900秒；父层从准备前计时，browser 最多315秒工作并保留末45秒清理。360 秒是冷源码层与已有 session E2E 栈下仍能完成真实表单闭环的受测预算，不改变 fail-closed 清理语义。
独立子进程承载同步Docker与真实Chromium；只按本次实际子孙身份及network/PG/API名字+owner+ID核对后清理。
daemon无响应时不能证明服务端build已取消/资源已删；必须有界非零并记cleanup_unknown，不计verified。
API/worker同一测试OS进程但独立runtime，跨进程是API与Vite/Chromium；未验worker独立OS崩溃隔离。

## 实际样例与视觉边界

此前实际视觉样例保留在当前worktree的
`output/playwright/t10-visual-758946ea6f444905bafe30adfa6e0281/`，
包含approved-quote.pdf、quote-1280.png、quote-390.png、approval-1280.png及result.json。
PDF1页42048字节，SHA256 `d980e44eccb41c002044c4fc6e289918e7707180f9e8822702ea4c0f47233638`；
实际factory金额200.00 USD与域结果一致，无内部成本/利润/供应商原话。
controller实际渲染全部1页，无裁切/黑块/JS/form/附件；页面1280/390无横向溢出、V1刷新不跳版本，
产品员工客户文件操作被拒，老板blob预览成功。controller的download事件等待超时不记通过；
下载成功证据是自动Chromium实际保存bytes/hash，不将两种浏览器观察混为一项。
受控视觉栈收到SIGTERM后exit0，worker STARTED且518cycles，禁用Gateway调用0；
finally核实本次Vite/桥exec/APIworker/PG/network与端口全部清理，不影响用户其他资源。
该视觉检查对应initial验收的生产前端/后端/renderer相同代码；之后Fix1另修改单位读取UX，
其真实早输入/确认行为由Fix1新测试覆盖，不宣称旧视觉检查与最终Vue源码完全相同。
新image自动PDF另行核对，不用旧视觉文件冒充最终夹具产物。

保留样例对应的完整E2E在807278…image上为`9 passed, 6785 deselected in 72.78s`。
保留的受控产物目录为`output/playwright/t10-1c6ab00109e941cca22b87cc795b56c3/`，
原字节交付副本为[受控已批准报价PDF](../../output/pdf/t10-controlled-approved-quote.pdf)。
PDF1页42047字节，SHA256 `24ac8ffa80fb8340b6b68dfcba9c036543f20a599b854fc1a7e5079e22da7ef2`，
V2 `quo_01M156NT1Y3Y0KJCBY1Y48P86D`，50pieces、4.00/200.00USD。
controller独立核对原件/副本hash、文本/metadata并渲染全部1页，无裁切/重叠/乱码，
无JS/附件/表单或内部成本/利润/供应商原话。此前页面交互证据与此保留PDF证据分时点保留。
已纳入版本管理的受控截图：[桌面1280](../../output/playwright/t10-1c6ab00109e941cca22b87cc795b56c3/quote-1280.png)、
[窄屏390](../../output/playwright/t10-1c6ab00109e941cca22b87cc795b56c3/quote-390.png)、
[独立审批](../../output/playwright/t10-1c6ab00109e941cca22b87cc795b56c3/approval-1280.png)，
以及[受控结果清单](../../output/playwright/t10-1c6ab00109e941cca22b87cc795b56c3/result.json)。
其loopback端口/ID只是已清理栈的历史上下文，不是可用服务地址；二进制快照会过期且增加仓库体积。

## 范围与成本

本批是确定性成本、版本化报价/审批、来源确认及客户PDF交付，不完成整个Phase2。
寻源自动化、需求簇排序、联系人瀑布、70/30分配和反压仍为后续；Phase1运营状态不改。
用户配置利润/费用分类/精度/汇率/期限，测试值不得成为默认政策。
代价是22项人工确认、每runtime独立受限解析器、PG锁/限流和测试传输桥维护；免费来源不意味着模型/基础设施免费。
既有结构concerns保留给整批审查：quote_files.py大模块、CostingQuotes.vue聚合及22项标签重复；
另有T8B1 scoped review记录的file_access._policy_lease取消时cleanup BaseException处理，未在本项静默改动。
详见[运维说明](../operations/costing-quotation.md)。
