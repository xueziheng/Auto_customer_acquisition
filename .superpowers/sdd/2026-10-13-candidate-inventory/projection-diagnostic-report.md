# 候选零新增安全诊断实施报告

基线6211940；本轮唯一实施代理inventory_live_fix。实现来自已批准projection-diagnostic-brief.md。未访问生产、凭证、实际模型原文或私密上下文；未派子代理。未改父代理docs/.playwright-cli或E2E实现。

## 实现

1. 新增domain专属严格SourceDiagnostic/ProjectionSummary/RejectionCounts/SaveSummary契约。只允许固定枚举及严格有界整数，extra=forbid、frozen；布尔数值与布尔version拒绝，计数守恒且层间数量一致。诊断只用于本域，不把业务规则放进shared。
2. 真实模型和collector复用原有顶层接受谓词：恰好candidates键、list≤100。shape明确区分not_object、invalid_top_keys、candidates_not_list、candidates_over_limit、invalid_json、valid；合法空列表记录raw_count=0。超长仅保留100上界及overflow=true，仍拒绝；其他无法计数的输入为null。原来collector.get默认空的替代端口缺口已补，真实端口接受条件不变。
3. domain证据拒绝改为typed ProjectionRejected固定reason，保留所有原有证据谓词。首个拒绝按姓名/国家代码/国家地址/产品/邮箱原文/域名身份/邮箱域名/公开证据契约分别统计；不解析异常字符串。未预期投影异常标记internal_failure，关闭来源，不把它伪装为普通证据不足。
4. collector返回显式CollectionResult(candidates, summary)，不使用全局last_summary。统计raw/projected及每条的第一拒绝；区分未知页、未成功读取页、discovery_only。不增加工具请求、不拼跨页证据，候选仍须同一官方snapshot支持全部事实。保留已有缺失public_emails为空与tuple转换兼容接受范围。记录本次search结果ID数、成功快照ID数、官方成功快照ID数（重复读取同ID不重复计数）。
5. 持久保存端口返回显式SaveSummary。原save_public仍返回原子新增新鲜域名整数；实际SQL分支统计inserted_fresh、inserted_not_fresh、existing_refreshed、existing_unchanged。另在每条记录同一时刻统计query_match/query_mismatch、fresh/future_observed/expired。这些是本批投影输入的正交统计，不是全公共库计数或实际最终交付量；不改变原刷新/新鲜度/过滤谓词。
6. 0074为acquisition_attempts新增可空diagnostic JSONB列。原outcome、valid_new_count语义与30字符字段不变；旧记录SQL NULL。成功及shape/内部投影失败摘要沿显式结果/异常附件到driver，再由record_source_attempt在既有tenant/job/lease fence事务内重新严格验证后写入；成功save统计与valid_new_count一致。SQL不保存候选原文、字段名、URL、邮箱、异常文本或模型原始输出。

九条硬边界、权限、租约、来源范围、模型/工具预算、现有无重派规则不变；未新增Adapter、Gateway重构、跨页拼证、自动重试、quota调整或队列终态修正。

## RED与验证

先验证缺少typed shape/rejection契约时13项RED；未知/不可哈希page_id混合列表复现collector异常；新保存诊断端口不存在/返回int的真实PG测试RED；bool version误接受也先RED再修复。

最终定向回归 **93 passed**，1条原有Alembic path_separator DeprecationWarning，17.95秒：

```bash
python -m pytest tests/unit/test_projection_diagnostic.py tests/unit/test_inventory_public_evidence.py tests/evals/test_inventory_evidence.py tests/unit/test_inventory_collector.py tests/unit/test_inventory_page_failures.py tests/unit/test_inventory_acquisition_driver.py tests/integration/test_projection_diagnostic.py tests/integration/test_inventory_model.py tests/integration/test_prospect_inventory.py -q --basetemp=/tmp/tos-proj-final
```

涵盖合法候选/空列表、混合拒绝、恶意原文不回显、模型shape错误附件、真实Gateway计量仍不等于schema成功、旧NULL、重新验证损坏摘要、不同tenant/错误lease拒绝、实际新增/刷新/旧观察值不更新、过期/未来时间/查询不匹配、真实driver失败来源摘要落库及原证据evals。

迁移验收在独立真实PG数据库执行：head→0073确认列移除→0074确认nullable JSONB恢复；另head→0072→head恢复全部库存表并与ORM逐列nullable一致，RLS仍强制。没有修改生产schema。

改动文件Ruff、git diff --check、项目Python3.12结构自检通过。新增两个契约文件mypy --follow-imports=silent通过。未跑全库pytest或全库类型检查。

## 交付与剩余验证

父代理需先独立review、Linux定向验收、备份与迁移0074后再启动新代码。建议Linux运行上述93项及以下原生CLI测试：

```bash
python -m pytest tests/unit/test_inventory_codex_cli.py -q --basetemp=/tmp/tos-projection-native
python scripts/check_boundaries.py
```

TRADEOS_TEST_CODEX_BINARY必须已指向已验证的Linux binary，否则3个CLI案例会skip，不能算通过。本轮无新浏览器实现，真实browser回归可沿父代理已有定向验收套件；尚未执行Linux验收或新真实UI任务。

生产只读脚本应对非NULL诊断执行SourceDiagnostic.model_validate(row.diagnostic).model_dump()后输出，异常仅输出固定类型。第一看shape/raw，其次看projected与第一拒绝计数，再看save实际分支和query/time统计；NULL代表旧记录或未到此诊断阶段，不等于0。

本补丁只能让下一次真实0候选具有可定位证据；第四job原输出已正常清理，无法事后恢复。未宣称真实0根因已修复，也不根据token数推测候选数。
