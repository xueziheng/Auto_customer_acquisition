# 本机受控 Web：启动、配置、停止

这个入口启动本机合成演练环境。它不连接既有业务库，不解析真实 Provider 密钥，
不部署、不发送真实邮件。2026-09-06 的 A1–A10 受控验收已完成，精确版本及 Mac/Linux
边界见[正式验收](../acceptance/2026-09-05-web-core-completion.md)。多人共享部署未验收。
2026-09-07 最终收口修复与独立复审已完成，新增回归及全部裁定见[交付索引](../acceptance/web-core-delivery/README.md)。

## 首次准备

需要 Python 3.12+、Node 24、运行中的本机 Docker（`/var/run/docker.sock`）。在当前仓库
建立独立环境；不需要旧 Catalog 工作树，也不要复制 `.env`。Python 从 Python 官方发行版或
本机已声明的 Python 3.12+ 解释器提供，Node 从 Node.js 官方 24.x 发行版提供（package.json约束24.x）。
Task12 实际版本为 Python3.12.14/Node24.15.0，已分别在独立依赖环境验证；路径与安全记录见
[installation-isolation.json](../../output/acceptance/task12/installation-isolation.json)及
[node-isolated-final.json](../../output/acceptance/task12/node-isolated-final.json)。下列命令假定
python3.12、node、npm已在PATH，Docker/指定镜像已具备；不依赖旧工作树环境。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
node --version  # 应为 v24.x
npm --prefix apps/web ci
```

依赖准备允许访问公开包仓库。启动器自身不会拉镜像；请预先准备
`pgvector/pgvector:pg16` 和
`bitnamilegacy/minio@sha256:50cec18ac4184af4671a78aedd5554942c8ae105d51a465fa82037949046da01`。
测试浏览器另需 `.venv/bin/python -m playwright install chromium`。

## 启动

```sh
.venv/bin/python scripts/run_web_core_controlled.py
# 或：make web-controlled PYTHON=.venv/bin/python
```

API、Web、scheduler默认随机选择三个空闲 loopback 端口；notification另取随机loopback端口。三者可用 `--api-port 18080 --web-port 15173
--scheduler-port 18081` 显式指定。`--directory` 只指定运行目录的父目录，不能指定已有库。
等标准输出出现 `ready`，打开其中 `web_url`。输出还会给出安全运行目录路径；
`status.json` 含 owner、supervisor/子进程 PID 与出生时间、容器 ID、当前健康及固定失败原因。
配置文件是受信进程私有材料，不要打开、粘贴或提交它。

每次新启动创建新的 PG、MinIO、bucket、tenant 与六个员工身份：演练老板甲、老板乙、经理、
销售、寻源、产品。UserId 与 EmployeeId 独立持久化。没有任何预置审批、认证通过、预热、
SENT、Message、Validated Need 或 Opportunity。评分/SLA配置是本地合成参数，不能解释为公司政策。

页面顶部始终显示“本机受控演练 · 开发身份”。选择角色会推进身份 generation、销毁旧页面
并重新请求；服务器仍读取持久员工权限，角色不会作为授权 header。此入口不提供多人登录。

## 人工配置与明确的演练输入

1. 选择老板甲，进入“系统设置”，新建 Company Playbook 提案。可明确选择这个合成例子：
   公司类型 `trading_company`、最低订单额 `1000.00 USD`、寻源区域 `controlled`、月预算3，
   备注“仅用于本次合成演练”。这些值只有在你实际提交后才进入提案，启动器不默认提交。
2. 等待 scheduler 产生审批后，切换老板乙，从精确审批深链核对并批准。老板甲自批被原域拒绝。
   再回设置页核对该版本已激活；“点击批准”与“已激活”是两个不同事实。
3. 国家政策也从设置页提案：国家键精确填写 `KE`（系统不把 `Kenya` 自动当同一键）。本次例子
   仅允许公开研究，联系人补全与冷邮件均关闭；其余字段明确填写，并对每个字段提供
   `employee_input` 与 `controlled-assessment:<字段名>` 来源，备注“合成演练政策，不代表真实法律判断”。
   切换老板乙独立审批后，等待 scheduler 激活。不要填写真实法律结论来满足演练。
4. 指挥中心可粘贴下面完整的中文演练输入。受控模型只识别这一明确场景，原 Guardrails 和
   提案 schema 仍校验响应；其它自由文本固定拒绝，不会访问真实模型。

> 受控演练：只研究肯尼亚家具五金需求，覆盖进口商、分销商、电商三线路，各查1次，最多读3页、记录3条信号和3条假设，最低证据档位low_mid，不触达不发送。

这会生成真实研究提案，供核对理解、范围和预算。原受控入口已装配typed研究端口：确认只表示
Run受理，不保证执行成功。缺活跃Playbook/国家政策时实际Run失败且外部调用0；上面的独立审批
生效后，由原scheduler/Gateway执行三路合成search/page/model，形成3条Signal和3条Hypothesis，
零联系人/Campaign/发送。修改一处输入不会隐式扩大已支持场景。合成exclusive技术配置不是
真实Tavily账户已就绪，真实Provider验证仍not_run。

Campaign 的正式路径仍是：先登记发件身份→原 DNS 认证工作流→人工启动预热→提交完整 Campaign
边界→另一老板独立审批→激活。**发件身份中心提供人工登记/认证/预热入口**，每次修改须明确确认；
不得通过直插身份/认证或虚构 sender ID 强行完成 Campaign。受控 DNS 域为
`tradeos-controlled.example.com`、DKIM selector 为 `controlled`；仅原受控 Resolver 响应，不查询公网 DNS。

## 当前能力与后续消费

| 能力 | 本入口真实状态 |
|---|---|
| API、scheduler、notification、Vite、PG、MinIO | 四应用及独立owned资源，当前健康检查 |
| Playbook、国家政策、审批、Outbox | 真实域与持久流程，需人工配置；批准与已应用分开 |
| TradeManager/研究 | 精确中文合成场景→真实提案→原typed研究工作流；未知自由文本拒绝 |
| Campaign / 联系人 / Gmail | typed单Provider经原Gateway enrich/verify→域持久verified→入组→受控邮箱发送；触达须单独获批，不沿research_only自动扩展 |
| DNS/发件身份 | 受控Resolver→原认证流程，未知域拒绝；Web人工登记/预热，不预置通过 |
| 入站/完整回复 | 原typed mailbox→Gateway→RawArtifact→ingest/Outbox→原分类/动作→Need/Opportunity/Handoff，重放幂等 |
| 通知 | 原notification worker消费持久job；站内enabled、email disabled |
| 寻源/报价 | 既有完整受控功能另有验收，Mac原入口完整quotation/自动寻源准入未配置；详见下节 |
| Agent、Browser worker | 生产任务来源未装配，disabled；通用Agent技能/上下文/权限交集只组件验证 |
| Catalog | 审批后终点为queued培养Case；下游消费者disabled，不是正式Product |
| 真实Provider/共享登录/桌面 | not_run或未实现；开发角色不是真实认证 |

受控外部账本在本owner私有场景文件，仅读具名响应，不读业务PG补造结果；API/scheduler重建
复用原幂等记录。每次实际send/enrich/verify及研究操作分别持久计数，重复send返回同引用仍算两次。
未知发送结果（Provider已动作、ack前故障）、原消息重放和HUP后无重复动作已由Task12真实回归证明。
原件仅走用途API读取，不能编辑SQLite模拟审批或结果；完整停止删除场景文件。

## 预算、未配置能力与独立Linux报价

研究只使用明确查询/页面/模型允许清单和原PG持久quota；预算上限来自已批准提案/政策，
未知操作拒绝，无自动付费回退。联系人秒/分钟沿原InMemoryHunterQuotaGuard，跨重启额度不持久；
持久ToolCall/调用账本不是额度持久化。research_only不代表同意联系人发现、发送或报价。

合成最低订单额、评分、SLA与预算不是生产建议。免费来源仍有模型/基础设施成本；当前可信模型usage、
费率、计费token和人工工时不足，单位合格贸易机会成本为unknown。reply model_calls=1只计回复模型，
不能代替研究/TradeManager等总调用；合成3 Signal/3 Hypothesis/机会数量不是业务成绩。

Mac入口未配置完整quotation/自动寻源准入，相关依赖缺失应显示未配置/503。Linux独立原验收
使用自身owned PG/对象/网络及不同Need/Opportunity，完整公开回复→来源确认/单位→Decimal成本→
独立审批→客户PDF已经通过。不能把临时QA装配当Mac用户入口，也不能把两环境当同一订单。

Linux完整报价浏览器验收使用既有入口（需要上述依赖、Chromium、Docker及原Linux构建依赖）：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_costing_quote_browser.py::test_real_costing_quote_browser -q --tb=short
```

该测试调用原[Linux生命周期](../../tests/e2e/costing_quote_lifecycle.py)与固定internal-network，
不提供持续运营launcher，不发送客户邮件；parser资源probe、审批和预算不能跳过。
生产来源/模型/供应商直报、共享部署和真实发送均未启用。

## 健康、重启与停止

Web每两秒读取监督器最近探测。只有 API 数据库就绪、scheduler当前持锁就绪、notification就绪和Vite HTTP
可达才显示整体就绪；超过4秒未更新显示未知。就绪不代表业务配置、审批、预算或真实服务验证。
API健康请求继续使用本次tenant/employee，未新增身份绕过。

前台 **Ctrl-C / SIGTERM** 停止全部本次资源。scheduler完成当前cycle后退出；超时仅对已核对
出生身份的owner进程KILL，并将结果记为非零。退出0且最终 `status=stopped`、
`cleanup_errors=[]` 才表示清理已确认。停止会删除本次数据库、对象和受控邮箱。
每个业务进程的安全状态还记录同组anchor的PID/出生时间；它在业务exec前握手建立，
TERM期间保持组归属，真实业务成员清空后才退出。它不保留API监听FD或业务管道。
leader即使在首次快照前退出，停止仍核对整组；未知归属或残留不会报告成功。

需要保持同一份演练数据时，对 `status.json` 中的 supervisor PID 发送 **SIGHUP**。
先核对 PID 出生时间仍一致；它重启 API/scheduler/notification/Vite 四应用，不迁移、不重置数据、不重置外部邮箱。
核验状态中的owner、完整PID与出生时间一致后，才在另一个终端执行 `kill -HUP <本次supervisor-pid>`。应用重启失败会终止整个本次环境并报告非零。

完整停止后重新运行启动命令将创建全新的 owner 与数据。端口冲突立即失败，不杀占用者、不自动
换到未声明端口。worker异常退出使本次监督器非零退出，并逐层清理；主错误保留，清理错误另列。
日志只保留固定安全状态，不保存 raw 异常或子进程stdout/stderr。

若 supervisor 被 SIGKILL 或机器重启，无法保证自动清理。仅按原 `status.json` 核对完整容器ID
及 `tradeos.controlled.owner` label，再人工处理本owner残留；不要 prune、pkill、复用旧库或清表。
身份核验不明的进程/容器不自动删除。保留安全诊断，并把清理状态视为未知。

## 数据备份恢复的当前界限

**当前没有用户业务备份/自动restore产品接口。** 完整停止会删除本次数据库/对象；重新启动只创建
新owner空业务环境，不是恢复旧数据。不要把运行库、真实dump或私有配置传给下面的测试。

本次最小演练只在静止owned数据上验证：新建source PG/MinIO（原迁移0059和合成员工）→
通过原RawArtifactStore写一份合成原件→内存pg_dump→另一全新owned空PG恢复→独立bucket复制原件→
原Store读回metadata与SHA256一致。同owner与已有public关系目标拒绝，原不可变约束保留；
所有客户端/连接先关闭，再按精确owner+容器ID清理。测试无API/scheduler等应用写入者。

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_web_core_backup_restore.py -q --tb=short
```

这是隔离验收命令，不接受source/target参数，也不恢复已有launcher。实际安全结果见
[backup-restore.json](../acceptance/web-core-delivery/backup-restore.json)。仅临时新owner配置由
ControlledConfig loader在进程内存解析，不输出DSN/密钥/dump/对象key；dump不留存。
尚未验证运行中PG/S3一致性快照、PITR、备份保留/加密轮换、受控邮箱/模型状态与游标恢复或生产灾备。
需要保留业务的共享部署必须先独立规格化并实际验收这些生命周期，不能套用演练删除语义。

PG容器单独stop/start可能改变随机公开HostPort；内部pg_isready成功不表示原端点恢复，原配置不会
自动更新。Task12前两轮因此ConnectionRefused，已保留失败与额外验收成本。A5改用同owner
pause/unpause仅证明固定端点短断恢复；四应用HUP不是数据库资源重建或端点自动修复。
故障时先读本次安全status/报告，只按已核归属资源处理；不要输出真实config或原始敏感日志。

## 验证入口

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_web_core_controlled.py -q --tb=short
```

缺 Docker、镜像、依赖或浏览器会失败，不以skip当通过。测试使用同一入口及自有资源；外部构造
探针和socket拒绝验证真实边界，不拿固定零计数冒充无公网证据。Python审计钩子是本进程的纵深
约束，不能当操作系统沙箱或对任意第三方原生扩展的安全保证。

完整受控主链使用上述`TRADEOS_REQUIRE_E2E=1`显式门禁；正式Task12命令和
按版本统计见[验收](../acceptance/2026-09-05-web-core-completion.md)。仅检查入口故障/停止可运行
`tests/integration/test_web_core_launcher.py`，不能把该局部检查冒称完整闭环。
安全历史报告、代表截图与所有Ruling理由/成本见[交付索引](../acceptance/web-core-delivery/README.md)。
