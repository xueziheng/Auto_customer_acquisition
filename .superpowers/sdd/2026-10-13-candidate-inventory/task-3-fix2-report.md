# Task 3 第二轮定向修复

基线 `5723925cd8e146fbaa9666bd0c3c01ace680bde9`。同身份迟到响应的恢复记录竞态已修复，原清保护数据逻辑没有重写。

恢复模块的对外 `clear(expectedKey)` 仅在当前 pending.key 与响应对应的键相等时清除。所有确定响应都传捕获的 `request.key`；身份切换仍独立无条件清理。没有把所有 disposed 响应拒绝掉，因此组件卸载后，若仍是该原请求，其确定结果照常清理。

新增表驱动行为测试精确执行 A/k1 在途卸载、B 恢复并重试 k1 成功、B 创建未知 k2、迟到响应前重挂仍用 k2、A/k1 迟到确定结果、迟到后重挂仍用 k2；同时断言存储保持、后续 HTTP 原键和原体保持。覆盖迟到 202/400/403/409/429。另测原 k1 未被替代时的卸载后 202 会清理记录。

先新增测试运行观察失败；初次还发现存储不可用 fixture 的实例 setItem spy 恢复后污染后续 happy-dom 测试，改为 getter spy 并在清存储前 restore mocks。修正夹具后 RED 为 5 failed / 14 passed，五类迟到确定响应均把 k2 持久记录清为 null。只加入按键清理后 GREEN 为 19 passed。

验证（均实际运行）：

- `npm test -- tests/prospect-inventory.test.ts`：1 file，19 passed。
- `npm run typecheck`：通过。
- `npx eslint src/views/customer-discovery/InventoryPanel.vue src/views/customer-discovery/inventoryRecovery.ts tests/prospect-inventory.test.ts`：通过，零输出。
- `/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python scripts/check_boundaries.py`：通过。
- `git diff --check`：通过。

按本轮 brief 未泛跑全部前端、build 或后端。没有访问生产、凭证、原 /Volumes/T7 工作区，也没有派子代理。只提交本模块、组件、定向测试和本报告。
