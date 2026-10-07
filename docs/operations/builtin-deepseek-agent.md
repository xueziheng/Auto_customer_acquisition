# 内置 DeepSeek Agent：本机独立运行

首批提供产品内登录、多轮会话、按当前权限读取业务资料、研究提案和人工确认后的公开需求研究。
API 与 scheduler 是独立 Python 进程，运行时不依赖 Codex。浏览器关闭后，后台仍可继续已接纳任务；
电脑、Docker 或 scheduler 停止时不会继续执行。本批只监听本机，不等于公司网址共享部署。
真实模型、真实搜索与共享部署的实际验收状态见[验收记录](2026-09-22-builtin-deepseek-acceptance.md)。

## 1. 准备依赖和本机存储

使用 Python 3.12+、Node 24、npm、Docker，以及本机已有镜像 `pgvector/pgvector:pg16`、
`bitnamilegacy/minio@sha256:50cec18ac4184af4671a78aedd5554942c8ae105d51a465fa82037949046da01`。
项目不自动拉取镜像。下列命令均在本分支仓库根目录执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm --prefix apps/web ci
PATH="$PWD/.venv/bin:$PATH" npm --prefix apps/web run gen:api
npm --prefix apps/web run build
```

按[持久内测说明的业务政策格式](web-internal-pilot.md#显式业务政策)，由负责人填写 POLICY_JSON。
这里的 PROFILE 是新的绝对目录路径；MODEL_SETTINGS 是另一私有目录里的 JSON 文件绝对路径。
所有大写占位符必须替换，不能照抄成默认业务值。

```sh
.venv/bin/python scripts/run_web_pilot.py init --profile PROFILE --policy-file POLICY_JSON
.venv/bin/python scripts/run_web_pilot.py migrate --profile PROFILE
.venv/bin/python scripts/run_web_pilot.py accounts --profile PROFILE create --username USERNAME --name NAME --role boss
```

`init` 完成后存储停止；`migrate` 显式升级到仓库当前唯一 head，并让数据库与对象存储保持运行。
账号命令会在终端隐蔽读取密码。没有默认账号或默认密码。`start` 必须显式传入
`--model-settings` 才启动模型入口；省略该参数仍启动无真实模型的受限 pilot。
已有 profile 必须先停止全部应用再迁移；应用启动不会代替迁移。

## 2. 管理员配置模型和明确限额

复制 `infra/standalone/model-settings.example.json` 的字段形状到 MODEL_SETTINGS，逐项填写。
示例中的 `null` 是不可运行的占位符。文件目录需为当前用户所有、权限 `0700`，文件 `0600`，
不能是符号链接；不要把配置、密钥或业务备份提交进仓库。

- `provider` 固定为 `deepseek`；`model` 填管理员选定、账号可访问的准确模型 ID。
- `secret_ref` 是 scheduler 环境变量名，例如 `DEEPSEEK_API_KEY`；文件只存引用，**不存密钥值**。
- `configuration_version` 是新的可追踪版本。改模型、密钥、限额或外发许可时使用新版本。
- `model_data_export_enabled` 明确设为 `true` 才允许业务上下文外发；关闭时业务调用拒绝。
- `limits` 中的窗口秒数、租户/员工调用数、并发数、输入字节数、输出 token 数和超时秒数，
  都由管理员明确填写正整数。它们是调用与资源上限，不是人民币钱包，不保证固定账单金额。

地址固定为官方 `https://api.deepseek.com`，不能在配置中替换 URL。本实现使用
[DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)，关闭 thinking、
SDK 自动重试与响应存储。接口协议与本地校验通过不代表真实账号或指定模型已可用，需显式 probe。

在托管启动命令所在终端设置密钥；若采用手动三进程方式，只在 scheduler 终端设置。
可用系统密钥管理器注入；zsh 交互终端也可隐蔽输入：

```zsh
unsetopt XTRACE
read -rs 'DEEPSEEK_API_KEY?DeepSeek API Key: '
export DEEPSEEK_API_KEY
```

密钥不得写入聊天、CLI 参数或 Web 设置。API 进程不需要模型密钥。
模型账户余额、搜索账户额度、机器持续运行、电费、数据库/对象容量与后续维护分别产生成本。

## 3. 启动并验证

推荐复用持久 profile 的 supervisor。先完成显式迁移和模型配置，再运行：

```sh
.venv/bin/python scripts/run_web_pilot.py start --profile PROFILE --model-settings MODEL_SETTINGS
.venv/bin/python scripts/run_web_pilot.py status --profile PROFILE
```

PROFILE 是目录，MODEL_SETTINGS 是私有 JSON 文件。启动器验证配置后启动存储、API、唯一的
scheduler 和通知进程；三个健康检查都通过才返回成功。模型与可选搜索凭证只按配置引用传给
scheduler，不传给 API 或通知进程；无关环境变量不继承。缺少凭证不会在启动阶段发起探测。
重复启动同一 profile 会被拒绝。托管进程被记录在 profile 中，原 `stop` 与冷备份机制可识别它们。
Linux 停启与凭证隔离的受控验证见 `tests/integration/test_standalone_supervisor.py`；这不代表真实
模型、搜索或云服务器资源容量已验收。

如需手动排查，可准备三个终端，使用下面的入口。不要与 supervisor 同时运行。
这些入口的 `--profile` 参数接收 **PROFILE/config.json 文件**，不是目录：

```sh
# 终端 1：同源 API 与生产 Web
.venv/bin/python -m apps.api.standalone --profile PROFILE/config.json --model-settings MODEL_SETTINGS
```

```sh
# 终端 2：已设置密钥的后台进程
.venv/bin/python -m apps.scheduler_worker.standalone --profile PROFILE/config.json --model-settings MODEL_SETTINGS
```

```sh
# 终端 3：既有站内通知；不会启用邮件发送
.venv/bin/python -m apps.notification_worker.pilot PROFILE/config.json
```

另一个终端执行 `scripts/run_web_pilot.py status --profile PROFILE` 查看 `web_url`，用浏览器打开
`http://127.0.0.1:对应端口`。仅采用手动入口时，supervisor 状态不代表这三个手动启动进程；
模型进程可用性以登录后的“设置 → 模型设置”当前心跳状态为准。

boss 登录后主动点击模型连接探测。探测也消耗一次调用额度，页面轮询不会自行触发探测。
两个进程使用相同配置版本、心跳活跃且本版本 probe 成功，才显示已验证并允许业务调用。

Web 可修改非秘密模型配置和业务资料外发许可，保存后显示待重启。管理员必须同步更新 MODEL_SETTINGS 为页面保存的
同一模型、版本、限额和许可，重启 API/scheduler，再显式探测。界面不会替你改本机文件或安装密钥。
保存新版本也不会清零已消耗的调用次数。

## 4. 聊天与研究

命令中心支持新建、切换自己的会话，刷新后从服务器读回历史；同租户其他员工（包括老板）也不能
查看这份私人会话。老板/经理/销售的业务读取仍按原业务权限裁剪，Run 审计保持老板专属；
其他角色只开放本批允许的产品帮助。员工停用、业务归属或来源版本变化后，旧摘要也会重新裁剪。

首版对研究范围采用明确字段，不把含糊指令猜成付费默认值。可以先提要求，再根据澄清补齐：
目标、目标国家、目标品类、排除国家、排除品类、搜索次数、页面数、信号数、假设数、最低证据等级、
策略组和每条查询结果数。字段格式与解析约束见
[研究提案实现](../../agent_runtime/assistant/proposal.py)。
多轮预算修正以有来源的最新明确值为准；不允许用模型自报的概率替代证据等级。

生成提案本身不会运行搜索。老板打开提案核对完整范围与限额，再确认，才能生成 canonical Run。
研究结果是 Demand Signal 与 Need Hypothesis，**不是客户已明确表达的 Validated Need**。
本批不能自动发客户邮件、作价格或交期承诺、报价或标记成交。

要启用真实公开研究，还须在 MODEL_SETTINGS 加 `research` 对象，明确所有字段：

```jsonc
{
  "secret_ref": "<搜索密钥环境变量名>",
  "exclusive_account_confirmed": true,
  "playbook_reader_user_id": "<本租户当前活跃老板的用户 ID>",
  "maximum_artifact_bytes": "<正整数>",
  "search_timeout_seconds": "<1–30 的整数>",
  "page_timeout_seconds": "<1–30 的整数>"
}
```

上述字符串占位符须替换为正确类型。研究使用已有 Tavily basic、公开网页读取与对象原件存储；
须确认搜索账号由本系统独占、在 scheduler 注入对应密钥，并在产品中配置当前公司 Playbook 与
目标国家的公开研究政策。未知或禁止国家会拒绝。带 research 时输出 token 上限需在 128–8192。
没有 `research` 配置时聊天仍可形成提案，但不能把研究未装配说成已经完成。

## 5. 停止、恢复与不确定请求

托管启动时，以下命令会停止当前 profile 的全部应用和存储，保留持久卷：

```sh
.venv/bin/python scripts/run_web_pilot.py stop --profile PROFILE --timeout-seconds 420
```

托管方式再次运行带 `--model-settings` 的 `start` 即可恢复；它只检查 schema，不代替迁移。
模型模式同时通知全部应用停止，给当前批次共享 300 秒排空窗口，不启动下一轮 scheduler cycle。
一个 cycle 可串行运行多个请求，300 秒不是单次模型超时，也不保证任意长度的批次都能完成。
超过窗口只清理已核验的 owned 进程并保留失败/unknown，不自动重发或报告正常停机。
CLI 等待须覆盖排空及存储收尾；模型服务建议 `TimeoutStopSec=480`、`TimeoutStartSec=600`。
省略 `--timeout-seconds` 保留旧 pilot 的 40 秒等待；CLI 超时不清空仍活跃的进程记录，
须确认原 supervisor 已退出才能重新启动，不得另起 scheduler 绕过 profile 锁。
手动方式须先在三个终端分别按 Ctrl-C 并等待退出，再运行 `stop`；不能仅依靠该命令终止未登记的
手动进程。手动重启前运行 `migrate` 启动并核对存储，再按第 3 节启动。
已接纳输入会由后台恢复。网页可主动取消；取消不代表撤销已发送到服务商的请求。

尚未发出的请求不扣调用额度；一旦发出，超时/崩溃导致结果未知时不会自动重发、退款或释放并发槽。
页面显示 unknown 时先核对服务商记录。明确“重新生成”是新请求，可能再次计费；不能把 unknown
当作服务商没收到。未知槽释放仅提供受审计的后台管理方法，本批没有 HTTP 或界面入口。

本批未验收公司共享 HTTPS、自动服务托管、生产备份恢复和灾难恢复；旧 pilot 的备份证据不能代替
新 Agent 数据的恢复验收。需要长期给同事使用，仍需完成下一批服务器部署与运维验收。

## 6. 分层验收

默认只运行受控测试，不读取真实密钥、不发起真实模型或搜索调用：

```sh
.venv/bin/python -m scripts.accept_builtin_agent --report CONTROLLED_REPORT_JSON
```

真实验收需要已运行并登录可用的产品、私有配置和人工预批准文件，以下操作会消耗额度：

```sh
.venv/bin/python -m scripts.accept_builtin_agent --live --settings-file MODEL_SETTINGS --approved-research-input APPROVED_INPUT_JSON --report LIVE_REPORT_JSON
```

预批准文件需 0600、父目录 0700，字段为 `base_url`、`username`、`messages`（逐轮文本数组）、
`expected_parsed_fields`（完整预批准提案字段的字符串映射）。工具交互读取产品密码，先做 probe，
再按输入对话；只有 `research_only`、当前可确认且全部提案字段与预批准映射完全相等时才确认。
不相等会停止，不会替管理员接受模型扩大的范围。报告分别标记受控、live 模型、live 来源和共享部署，
未运行明确写 `not_run`。这份脚本不部署共享服务。

## 6. 本人 Gmail 邮箱

全账号历史与后续邮件同步使用独立只读授权，见 [本人邮箱同步](private-gmail-mailbox.md)。
此入口不依赖模型、不会把邮件自动送入 Agent。完成 Google 授权并启动同步进程后，在“我的邮箱”查看。

## 7. 已关联业务回复的模型识别

`reply_enabled` 默认是 `false`。只有在同一 profile 已明确配置 Gmail、委托员工，且模型设置中的
输出 token 上限处于 128–8192 时，才能显式设为 `true`。这仅装配原入站、回复和 Campaign 服务；
不会批准 Campaign、修改发件身份或代替联系人可达性、抑制、发送审批等检查。已有 Gmail 的 profile
不能通过保持回复关闭来启动残缺的 standalone 组合，也不能删除 Gmail 绑定来绕过启动拒绝。

回复仍使用 `pilot-gmail` 邮箱别名、`pilot` 路由和 `pilot-gmail-v1` 入站版本，避免将入口切换误当成
全新邮箱而重置游标。资料来源仅限已关联 Outreach Attempt 与 Enrollment 的业务回复；“我的邮箱”
里的普通邮件不会因此自动成为需求。

启用回复、变更委托员工、模型或限额时，操作者必须提供新 `configuration_version`，让 API 与
scheduler 使用相同私有配置，再由网页当前老板显式执行模型探测。构造和启动本身不发起模型探测。
当前员工资格、资料外发许可、有效心跳、已验证版本和额度任一不满足，都拒绝模型调用。

切换前显式停止原手动 API/scheduler 进程并等待退出；旧 supervisor 的 status/stop 不代表这些手动
进程已停止。沿用第 3 节入口启动，不能增加第二个 scheduler。原单副本锁仍保护入站扫描、工作流
推进和模型生命周期。按 profile 管理日常服务，受控验收使用独立资源，不能复制演示业务数据到日常租户。

每条回复固定使用 canonical Run 和 `sequence=0`。发生 unknown、派发后超时、崩溃或业务结果丢失时，
不得自动重发付费请求，也不能靠更换配置版本、模型或员工重新调用。保留原账本进行人工核对；未知用量
仍为空，不补零。本次没有增加未分类回复的一键恢复接口。

真实模型是否通过 160 条回复语料及完整需求建档/人工接管验收，应以本次验收报告为准。
受控 Provider 测试只证明接线和确定性流程，不代表真实模型质量已通过。

### 回复链路验收与切换顺序

具体证据与未完成项见[回复模型网关验收记录](../acceptance/2026-10-05-reply-model-gateway.md)。
先构建当前 Web，再执行受控验收；两项入口均使用独立 owned 数据，不使用日常客户：

```sh
npm --prefix apps/web run build
.venv/bin/python -m pytest tests/evals/test_reply_gateway_evals.py tests/evals/test_reply_live_settings.py -q
.venv/bin/python -m pytest tests/e2e/test_standalone_reply_browser.py -q --tb=short
```

真实验收需由操作者提供模型配置绝对路径、可信环境秘密解析与调用预算；下面的 MODEL_SETTINGS
和 CALL_BUDGET 都是待替换占位符。最少需要一次 probe、160 条冻结语料、正向回复和退订各一次，
共163次调用。调用上限不等于费用上限，缺失用量保留未知。不要为跑通验收自动提高配置限额。

```sh
TRADEOS_REPLY_LIVE=1 \
TRADEOS_REPLY_MODEL_SETTINGS_PATH=MODEL_SETTINGS \
TRADEOS_REPLY_LIVE_MAX_CALLS=CALL_BUDGET \
.venv/bin/python -m pytest tests/integration/test_live_reply_acceptance.py -q --tb=short
```

不设置 `TRADEOS_REPLY_LIVE=1` 时入口明确跳过；显式启用但缺配置、密钥、外发许可或预算时失败，
不会回退为受控模型。报告位于 `output/acceptance/reply-model/<本次ID>.json`，必须核对其
`real_model`、逐题偏差、两个业务场景及实际调用账本。失败后不要自动重跑整批；先核对未知请求和剩余额度。

只有受控网页、真实模型和完整业务场景全部达标，才按本节前述顺序切换日常 API/scheduler，
保留原通知进程并确认仅一个 scheduler 持锁。按 profile 区分日常数据与验收数据，不停止另行保留的演示。
切换后在远端 Windows 的交互式 Edge 打开日常网页；没有合法业务关联的新回复时，真实邮件建档效果
仍标为待验证，不把个人测试回信或验收记录复制成日常需求。
