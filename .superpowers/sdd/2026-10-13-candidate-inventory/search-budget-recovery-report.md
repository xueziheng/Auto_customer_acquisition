# 搜索子额度恢复与工具使用诊断

BASE：3f5fc1e。按父授权实施；无生产、凭证、真实模型/API 访问，无配额配置修改。

## 已证问题及范围

旧路径在两次搜索后，第 3 次搜索先预留一个总工具槽，再抛 budget_exhausted；Session 随即把 used 从 3 置为 7，抹掉剩余 4 个可读槽。此次明确改变该关闭语义：搜索子额度与总工具额度分别约束；超额搜索返回 search_limit_reached，占用当前槽、零外部搜索、不退款，随后仍可在原 7 槽内读取既有 page_id。重复超额搜索仍耗尽 7 槽。不是增加搜索或工具上限，也不是重试失败请求。

第 6 批生产 raw0/reject0 没有工具拒绝计数，不能证明该批实际走过第 3 次搜索路径。不能把本修复称为候选已恢复或生产空结果已完全解决。

## 实现与契约

- inventory_collector 仅将本地搜索次数达到 2 的错误改为 search_limit_reached。现有 guard 在它之前执行。未修改实际 query 组装、地区、国家、企业或 URL 选择策略。
- DiscoverySession 新增明确允许的固定错误，原预算/截止时间/未知异常关闭分支保持；固定 usage_snapshot() 显式提供 dict[str, int | bool]，不提供内部输入或输出。
- ProjectionSummary.tool_usage 为可空 ToolUsageSummary，默认 None。Session 快照由 collector 通过领域 model_validate 严格投影，经既有 CollectionResult 或 ProjectionDiagnosticError 进入 SourceDiagnostic/0074 JSONB。无迁移，无 Gateway 管线重构，无共享层或新适配器。
- 成功（包括合法空 candidates）记录会话关闭后的统计；已有 ProjectionDiagnosticError 在抛出时附快照。如果该时刻有未完成工具，明确保留 queued/inflight，不能记为成功/终态。其他异常不会被改写为虚构 projection shape。

固定统计：source_search/read_public_page/browse_public_page 为预留槽数，不是实际外部派发成功数；reserved 为其总和。queued、inflight（0 或 1）、terminal、cancelled 满足 reserved 守恒。terminal 再等于 succeeded、search_limit_reached、source_read_required、page_id_unknown、page_unavailable、budget_exhausted、failed_closed 之和。failed_closed 也包括已排队但会话先关闭而拒绝执行的槽，不暗示已外部派发。

未预留的总预算/截止时间拒绝分别为 unreserved_budget/unreserved_deadline，不计入 reserved。两者各封顶 100，再有拒绝时 unreserved_overflow=true，表示其计数已是下界。总额度和全部字段均严格有界整数，bool 不能充当整数；未知字段拒绝。库存实际预留总数仍最多 7；诊断模型的计数表示范围 0..100 不扩大运行上限。没有 query、URL、page_id、文本、异常消息或凭证字段。

旧 JSON 缺 tool_usage 或显式 null 读为 None；数据库 diagnostic IS NULL 也保持不变。版本 1 增加可空字段，旧严格解析程序不认识新字段；运维解析 helper 需随应用代码更新，不能用旧类解析新摘要。

## 验证

RED：新增摘要测试及第三次搜索回归共 6 failed，分别证明缺少类型/端口、错误码不支持，以及第三次搜索仍返回 budget_exhausted。GREEN：核心 11 passed。之后加入真实 collector 空输出/形状失败摘要、PG 往返与守恒/取消用例。

最终定向：157 passed，6 skipped（本机没有 Linux bwrap，父负责原生环境验收）。命令：

```bash
python -m pytest tests/unit/test_discovery_tool_usage.py tests/unit/test_inventory_search_guidance.py tests/unit/test_codex_discovery_session.py tests/unit/test_inventory_collector.py tests/unit/test_inventory_page_failures.py tests/unit/test_projection_diagnostic.py tests/integration/test_projection_diagnostic.py tests/unit/test_codex_agent_bridge.py tests/unit/test_codex_provider.py tests/unit/test_inventory_model.py tests/unit/test_inventory_acquisition_driver.py tests/unit/test_inventory_public_evidence.py tests/evals/test_inventory_evidence.py tests/integration/test_inventory_model.py -q --basetemp=/tmp/tos-search-recovery-regression
```

覆盖第三次搜索余量 4/后续读取成功、两次实际搜索封顶、重复超额耗尽 7 槽、无预留拒绝与溢出；排队/在途/取消/未知网络与权限关闭；固定计数无敏感输入回显；collector 合法空及已有形状失败；真实 PostgreSQL 新 JSON 往返、旧缺字段未知、SQL NULL、非法 nested model_copy 拒绝、租户/租约围栏；原页面失败 host 禁止重新派发、模型身份、证据门与业务 eval。

改动文件 Ruff、git diff --check 和项目虚拟环境 Python scripts/check_boundaries.py 全通过。未跑全库。

## 取舍及父验收

允许原先错误关闭后剩余槽继续读取，可能增加相对旧行为的实际页面调用；仍在已授权的 2 search / 7 tool、Gateway 和模型预算内。总预算、到期、权限/租约、未知请求继续关闭；已失败 host 的重复访问禁令不变；没有失败退款、自动重试、跨页拼证或证据门放宽。九条全局硬边界不改。

父应在 Linux 重跑上述定向组和既有 3 项真实 CLI synthetic 验收，核对新增固定错误与 meta 能经 broker 正常交付，最终 JSON 仍严格校验。无需数据库迁移。独立审查和部署后才考虑受用户额度授权约束的真实 UI；本补丁不修改 20 次模型上限，不宣称候选效果。
