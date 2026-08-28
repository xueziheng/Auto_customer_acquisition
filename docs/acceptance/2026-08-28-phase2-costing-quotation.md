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
| Browser | 完整E2E 9passed；本项真实Uvicorn/三Vite/Chromium完成单位/政策/证据/成本/修订/独立审批/403/PDF下载；controller另实际检查1280/390和PDF全部1页 |
| 真实供应商/对象存储 | not_run；PDF输入是受控文本，SDK无真实网络 |
| 真实客户回复/可达性/历史投递 | not_run；fixture公开记录模拟历史，真实Verifier仍要求持久SENT关联 |
| 本次报价真实发送 | not_run；受控链也未发送，quote approved、机会未won、需求未fulfilled |
| 前端最终门 | 21文件247passed；typecheck/build/生成类型漂移通过；lint 0errors、133既有warnings |
| 后端全量最终门 | 当前源码6785passed、9deselected、1052.59s；上一轮44失败及修正完整保留 |
| 独立整项审查 | 实现者已自审，controller独立审查待执行，不以测试通过代替 |

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

最终回归时点镜像为`sha256:6e122e8f484be2d31fbb5e6fb3ef63991a319a6e775dbd62a7f1b316bd1b1ddf`。
它相对807278只更新测试入口、Linux归属与执行证据；生产UI/后端/renderer及公开闭环helper未变。
本轮完整not-e2e/e2e运行期间源码冻结，仅完善文档。下列已批准五件807278样例保留原字节，
不称为6e122e8f镜像产物，新回归输出另记。

最终完整not-e2e实际`6785 passed, 9 deselected in 1052.59s (0:17:32)`、exit0；
这9项是另一个完整E2E命令覆盖的marker互斥选择，无skip。完整原始记录由T10实施报告索引保留。
最终源码全库Ruff与结构七项通过；四文件mypy、前端最终命令之后对应代码未再变化。

当前6e122e8f完整E2E实际`9 passed, 6785 deselected in 72.50s`。其本地新产物在
`output/playwright/t10-5bf703567e414666891b3b16a5d1e58d/`，1页42047bytes、
SHA256 `7557d8839ce85094c21a904852c89eda180c4d1617cbf2e4dc66c83ada3c7838`，
自动实际下载与API/result hash一致；此目录不纳入提交，也不冒称另有controller视觉复验。

API/PG仅internal网络；本机Docker实际Ports为空，Mac无法直连，使用已批准的有限测试HTTP桥：
host仅loopback、固定容器relay→127.0.0.1:8000真实Uvicorn、每请求15秒/请求响应各1MiB/最多8并发。
不开放任意URL、目标、命令或重定向，不证明原生Docker发布或生产入口。
自动总生命周期300秒、协调视觉900秒；只清理本次Vite、桥/exec、API/worker、PG和network。
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
该视觉检查对应生产前端/后端/renderer最终相同代码；后续仅测试前置helper及测试入口/旧契约有改动。
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
