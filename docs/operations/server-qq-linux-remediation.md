# 服务器Linux定向修复

从309cf97开始，代码与定向测试均在服务器独立stage的src内由tradeos-app执行。没有连接运行数据库、修改配置、启停服务或push。邮件仅MailServer模拟；浏览器仅本次私有profile和空白页，不测试真实邮件或搜索来源。

## 根因和改动

Python3.12.3的默认SMTP78列将64位摘要头折行，解析后保留前导FWS。旧断言失败之外，真实Sent对账也会因严格字符串比较拒绝合法记录。生成采用RFC行长上限998使摘要保持一行；接收对账仅对摘要头规范化首尾ASCII空格/Tab，仍精确比较摘要，内部空格、重复、畸形及线程/收发人错误均拒绝。新增外部折行摘要的模拟reconcile，原严格MIME断言保持。

生命周期监督器的read_output原在事件循环同步导入整个Docker/Linux验收stack。冷诊断第一次await耗1.018秒；将原样输出过滤规则抽至轻量纯模块并在原入口重导出，新增Docker不可导入时仍读安全回执的回归。真实总预算仍在准备前计时。

测试子入口原直接执行整个pytest测试文件，改为独立stdlib私有fixture。Linux Chromium另有Playwright高层导入3.24秒，原4秒工作窗未启动便到期。经控制者确认，此进程所有权测试直接启动已安装的真实headless Chromium，固定参数、独立session、空白页、私有profile，以真实ps树核验子孙PID/组；缺失二进制明确失败。业务Playwright接口另有控制者浏览器验收，未修改业务浏览器实现。非Linux保留原Playwright路径，本轮未跨平台实测。

固定sleep(.3)原只用于等监督器发现子PID，负载下仍偶发。改为监督器确实观察根与子PID后写ack，fixture才合作收尾。原2秒/5秒预算、正常/强停、端口关闭、PID消失、无关sentinel存活断言均保留，不将迟到退出强判成功。readiness的httpx导入移至stack准备阶段，client不继承系统代理；总监督时钟仍涵盖准备。

## 证据与限制

服务器路径：/srv/tradeos/acceptance/qq-20261019/agent-upgrade-20261022/src。

- 三项新旧回归先RED：MIME、折行Sent去重、无Docker输出读取；随后GREEN。
- HOME指向TASK/test-home，venv执行pytest两个文件：test_qq_business_reply_connector.py和test_costing_quote_lifecycle.py，50通过/20.16秒。全部为定向结果，不是全套验收。
- ruff和结构检查通过；新纯helper、独立fixture及修改connector的mypy通过。
- 扩展到lifecycle/stack的五文件mypy命令另报13处错误：Docker/boto3缺stubs及既有e2e/conftest的UoW、可空Task契约等；不宣称整个e2e类型检查通过，不为本任务扩修或添加ignore。
- 初次baseline因慢fixture未出现使visual测试等待，已仅终止本次精确pytest进程；中间一次参数误加至另一私有fixture，已修正。后续49通过1normal负载时序失败促成确定性握手，而非加预算。
- 全suite原asyncio255警告最终定向未再出现；没有增加waitpid，也未独立证明其所有成因。重负载并行时序的最终独占复测由控制者负责。

日志位于stage .superpowers/linux-fix/。主/QQ运行环境未切换，33来源验收与全套结果由控制者单独记录。
