# 本机受控 Web：启动、配置、停止

这个入口启动本机合成演练环境。它不连接既有业务库，不解析真实 Provider 密钥，
不部署、不发送真实邮件。它尚不是 A1–A10 全链完成证明。

## 首次准备

需要 Python 3.12+、Node 24、运行中的本机 Docker（`/var/run/docker.sock`）。在当前仓库
建立独立环境；不需要旧 Catalog 工作树，也不要复制 `.env`。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm --prefix apps/web ci
```

依赖准备允许访问公开包仓库。启动器自身不会拉镜像；请预先准备
`pgvector/pgvector:pg16` 和 `minio/minio:RELEASE.2025-04-22T22-12-26Z`。
测试浏览器另需 `.venv/bin/python -m playwright install chromium`。

## 启动

```sh
.venv/bin/python scripts/run_web_core_controlled.py
# 或：make web-controlled PYTHON=.venv/bin/python
```

默认随机选择三个空闲 loopback 端口，可用 `--api-port 18080 --web-port 15173
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

这会生成真实研究提案，供核对理解、范围和预算。本批未装配研究外部场景，确认执行仍会准确
拒绝，不能把提案当研究成果。修改一处输入不会隐式扩大已支持场景。

Campaign 的正式路径仍是：先登记发件身份→原 DNS 认证工作流→人工启动预热→提交完整 Campaign
边界→另一老板独立审批→激活。**当前发件身份 Web 登记/预热入口待 Task8**，冷启动在此等待，
不得通过直插身份/认证或虚构 sender ID 强行完成 Campaign。受控 DNS 域为
`tradeos-controlled.test`、DKIM selector 为 `controlled`；只在后续入口可用时使用。

## 当前能力与后续消费

| 能力 | 本入口状态 / 后续任务 |
|---|---|
| API、scheduler、Vite、PG、MinIO | 独立进程/资源，当前健康检查 |
| Playbook、国家政策、审批、Outbox | 真实域与持久流程，需人工配置 |
| TradeManager 提案 | 仅上述明确中文合成场景 |
| Campaign / Gmail transport | 原发送 core 已接；先等待发件身份配置；邮件只进本 owner 受控邮箱 |
| DNS | 受控 Resolver → 原 Connector/Gateway/认证流程，未知域拒绝 |
| 入站正文、完整回复 | Task5/6接 typed 端口；可消费当前持久受控邮箱，不等Task12 |
| 发件身份登记/预热 Web | Task8 |
| 研究、联系人、寻源、报价外部场景 | 本入口 disabled；Task12按原typed ports补合成场景验收 |
| 通知投递 worker | 当前未启动；Task6接管场景须增加原worker与受控外部端口 |
| Agent、Browser worker | disabled |

受控 Gmail 状态保存在本 owner 的独立 SQLite 场景文件；它只模拟外部邮箱，不读业务PG补造结果。
重建 API/scheduler仍共享同一幂等邮件记录。`list_calls()` 按tenant返回独立持久的每次send/search调用；
重复send即使返回同一消息引用仍记录两次调用，不能拿唯一邮件数证明没有重复发送尝试。
未知发送结果注入由Task6/12扩展，当前不声称已验证该恢复场景。Task5扩展正文读取时必须保留该owner、消息ID和游标
边界；完整停止后场景删除。不要通过编辑场景文件模拟审批或业务数据库状态。

## 健康、重启与停止

Web每两秒读取监督器最近探测。只有 API 数据库就绪、scheduler当前持锁就绪、Vite HTTP
可达才显示整体就绪；超过4秒未更新显示未知。就绪不代表业务配置、审批、预算或真实服务验证。
API健康请求继续使用本次tenant/employee，未新增身份绕过。

前台 **Ctrl-C / SIGTERM** 停止全部本次资源。scheduler完成当前cycle后退出；超时仅对已核对
出生身份的owner进程组KILL，并将结果记为非零。退出0且最终 `status=stopped`、
`cleanup_errors=[]` 才表示清理已确认。停止会删除本次数据库、对象和受控邮箱。

需要保持同一份演练数据时，对 `status.json` 中的 supervisor PID 发送 **SIGHUP**。
先核对 PID 出生时间仍一致；它只重启 API/scheduler/Vite，不迁移、不重置数据、不重置外部邮箱。
可用另一个终端执行 `kill -HUP <本次supervisor-pid>`。应用重启失败会终止整个本次环境并报告非零。

完整停止后重新运行启动命令将创建全新的 owner 与数据。端口冲突立即失败，不杀占用者、不自动
换到未声明端口。worker异常退出使本次监督器非零退出，并逐层清理；主错误保留，清理错误另列。
日志只保留固定安全状态，不保存 raw 异常或子进程stdout/stderr。

若 supervisor 被 SIGKILL 或机器重启，无法保证自动清理。仅按原 `status.json` 核对完整容器ID
及 `tradeos.controlled.owner` label，再人工处理本owner残留；不要 prune、pkill、复用旧库或清表。
身份核验不明的进程/容器不自动删除。保留安全诊断，并把清理状态视为未知。

## 验证入口

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_web_core_launcher.py -q --tb=short
```

缺 Docker、镜像、依赖或浏览器会失败，不以skip当通过。测试使用同一入口及自有资源；外部构造
探针和socket拒绝验证真实边界，不拿固定零计数冒充无公网证据。Python审计钩子是本进程的纵深
约束，不能当操作系统沙箱或对任意第三方原生扩展的安全保证。
