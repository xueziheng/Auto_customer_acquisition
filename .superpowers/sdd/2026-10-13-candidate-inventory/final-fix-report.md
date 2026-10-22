# 最终审查两项 P2 修复报告

基线 `cc80c8f`；只修改本工作树候选库存路径及定向测试。没有访问生产、读取凭证、调用真实 Provider、修改原 `/Volumes` 工作区或触碰父进程未跟踪验收/浏览器文件；未派子代理。

## 实现

1. `InventorySources.prepare` 对未建立快照和 UNKNOWN 允许新持租约任务沿原 guard → Gateway usage 路径重新观察。现有 worker 每 job 首次领取执行一次 prepare，重领仍终止，既有 Gateway 调用预算、幂等、未决处理保持。API 来源列表只读：已配置但未核实/UNKNOWN 为 unhealthy，已知非免费且无免费包含额度仍 permission_denied。正常快照不无谓 probe。未修改搜索额度账本或退款逻辑。
2. 分配历史保留 array 响应与 limit，新增可选 `cursor`（上一页末条 allocation_id，1–128 字符）。服务器每页重新授权当前员工，并在 tenant/owner 过滤内读取游标锚点；无效、不再可见或其他企业锚点统一400，游标不授予权限。按 allocated_at DESC、allocation_id ASC 做 keyset 分页，唯一ID打破同时间并列。OpenAPI TypeScript 已重新生成。
3. UI 每页100条，整页时显示“加载更多历史候选”（末页恰好100条时多一次空页请求是保留 array 契约的代价）。追加按ID去重，刷新首页替换列表并使旧分页响应失效。失败清空保护资料；身份变更和卸载的旧响应不能写回。永久公司去重未改，历史读取不消耗新分配。

## TDD 与验证

先增加 UNKNOWN/cold-start 状态恢复及210条分页回归：定向初跑 `3 failed, 2 passed`，失败分别是 None被标成not_configured、UNKNOWN被标成permission_denied、仓储不接受cursor；修复后同组 `5 passed`。前端5项新增分页测试初跑 `5 failed, 19 passed`（缺加载更多入口），实现后24通过；随后追加刷新与迟到分页竞态覆盖，最终25通过。

真实PG新增用例由合成transport经实际Gateway观察：先预留并 dispatch 1次、写UNKNOWN；一次usage暂态失败后，另一新job观察恢复为FREE。Provider新上限20没有提高原5上限，remaining=4、reservations=1，search调用=0，旧Run新key仍request_uncertain。210条同时间历史37条/页完整遍历无漏无重，并覆盖其他owner/tenant、空owner、无效cursor及新增更晚记录不扰乱续页。

执行目录为本工作树，除标注外：

```sh
/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python -m pytest tests/unit/test_inventory_sources.py tests/unit/test_inventory_access.py tests/unit/test_prospect_inventory_api.py tests/unit/test_inventory_acquisition_driver.py tests/unit/test_inventory_web_gateway.py tests/integration/test_prospect_inventory.py tests/integration/test_search_quota.py -q
# 94 passed, 1 warning in 14.44s
PATH=/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin:$PATH npm --prefix apps/web run gen:api
# api.d.ts 与 platform-api.d.ts 生成成功；仅api.d.ts新增cursor字段产生diff
# 以下在 apps/web 执行
npm test -- tests/prospect-inventory.test.ts
# 25 passed，1 test file passed
npm run typecheck
# exit 0
npx eslint src/views/customer-discovery/InventoryPanel.vue tests/prospect-inventory.test.ts
# exit 0
# 返回工作树根
/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/ruff check apps/api/routers/prospect_inventory.py domains/prospecting/inventory.py domains/prospecting/inventory_access.py infra/db/inventory_sources.py infra/db/prospect_inventory.py tests/unit/test_inventory_sources.py tests/unit/test_inventory_access.py tests/unit/test_prospect_inventory_api.py tests/integration/test_prospect_inventory.py tests/integration/test_search_quota.py
# All checks passed!
/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python scripts/check_boundaries.py
# 七项检查通过；结构自检通过
git diff --check
# exit 0，无输出
```

中间一次PG测试因测试构造 `TavilyTransientError('temporary')` 参数不合法失败（93 passed/1 failed），改为实际无参构造后完整定向94通过。一次从根目录运行npm exec eslint因路径解析失败，改为apps/web目录运行后通过。最终仅余既有Alembic path_separator弃用warning，没有隐藏测试失败。

## 范围与局限

未重跑全库测试或外部CLI32项；未执行真实市场/部署验收。按父任务安排，发布构建及一次 scoped re-review 由主代理执行。游标分页不是跨请求数据库快照：刷新首页可看到新记录；锚点不可见时安全失败并要求刷新。没有提高免费额度保守上限，因此Provider额度实际增长仍不会自动放大本地可用量。
