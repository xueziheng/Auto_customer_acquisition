# 内置 DeepSeek Agent：本机独立运行

首批提供产品内登录、多轮会话、按当前权限读取业务资料、研究提案和人工确认后的公开需求研究。
API 与 scheduler 是独立 Python 进程，运行时不依赖 Codex。浏览器关闭后，后台仍可继续已接纳任务；
电脑、Docker 或 scheduler 停止时不会继续执行。本批只监听本机，不等于公司网址共享部署。
真实模型、真实搜索与共享部署的实际验收状态见[验收记录](2026-09-22-builtin-deepseek-acceptance.md)。

## 1. 准备依赖和本机存储

使用 Python 3.12+、Node 24、npm、Docker，以及本机已有镜像 `pgvector/pgvector:pg16`、
`minio/minio:RELEASE.2025-04-22T22-12-26Z`。项目不自动拉取镜像。下列命令均在本分支仓库根目录执行：

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

`init` 完成后存储停止；`migrate` 显式升级到单一 head `0066`，并让数据库与对象存储保持运行。
账号命令会在终端隐蔽读取密码。没有默认账号或默认密码。此处只复用旧工具的存储与账号管理，
**不要运行旧工具的 `start`**：它启动的是无真实模型的受限 pilot。
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

只在 scheduler 所在终端设置密钥。可用系统密钥管理器注入；zsh 交互终端也可隐蔽输入：

```zsh
unsetopt XTRACE
read -rs 'DEEPSEEK_API_KEY?DeepSeek API Key: '
export DEEPSEEK_API_KEY
```

密钥不得写入聊天、CLI 参数或 Web 设置。API 进程不需要模型密钥。
模型账户余额、搜索账户额度、机器持续运行、电费、数据库/对象容量与后续维护分别产生成本。

## 3. 启动并验证

准备三个终端，均进入同一源码目录，使用同一 PROFILE 和 MODEL_SETTINGS。先保证上面的 `migrate`
已经完成；API/scheduler 每次启动都会核对数据库兼容性。

```sh
# 终端 1：同源 API 与生产 Web
.venv/bin/python -m apps.api.standalone --profile PROFILE --model-settings MODEL_SETTINGS
```

```sh
# 终端 2：已设置密钥的后台进程
.venv/bin/python -m apps.scheduler_worker.standalone --profile PROFILE --model-settings MODEL_SETTINGS
```

```sh
# 终端 3：既有站内通知；不会启用邮件发送
.venv/bin/python -m apps.notification_worker.pilot PROFILE
```

另一个终端执行 `scripts/run_web_pilot.py status --profile PROFILE` 查看 `web_url`，用浏览器打开
`http://127.0.0.1:对应端口`。该命令的旧应用 supervisor 状态不代表这三个手动启动进程；
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

三个手动启动终端分别按 Ctrl-C，等待进程退出，再停止本 profile 的存储：

```sh
.venv/bin/python scripts/run_web_pilot.py stop --profile PROFILE
```

不能仅依靠旧 `stop` 命令终止这三个手动进程。再次运行 `migrate` 启动并核对存储，再按第 3 节
启动三个进程，已接纳输入会由后台恢复。网页可主动取消；取消不代表撤销已发送到服务商的请求。

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
