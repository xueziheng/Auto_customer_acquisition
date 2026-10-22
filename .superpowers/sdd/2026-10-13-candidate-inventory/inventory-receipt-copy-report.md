# 库存领取回执文案修复

BASE：4c61e58。纯文案，不涉及采集状态、轮询、请求、配额、幂等恢复或接口。

`InventoryPanel.vue` 的成功提示现在明确区分：
- 有后台任务：“提交时从库存交付 N 家候选，剩余部分已安排采集。最新结果见下方。”N 仍只取本次 POST 回执的 allocations.length，不推算后来交付数。
- 无后台任务：“本次从库存即时交付 N 家候选。”
- 列表标题改为“未完成的采集任务”，对应后端 list_jobs 不返回 complete 的既有契约。

没有匹配旧提示的既有文字断言需要更新；按 brief 不新增镜像测试。

实际验证（文案修改后）：
- `cd apps/web && npm test -- --run tests/prospect-inventory.test.ts tests/web-core-state-recovery.test.ts`：2 files、50 tests passed，3.73 秒。
- `npm run build`：vue-tsc 与 Vite 成功，218 modules；保留既有 PlatformConsole 静态/动态同时导入的非阻塞 chunk 警告。
- 项目 Python `scripts/check_boundaries.py` 通过；`git diff --check` 通过。

前端 dist 已按最终文案重建，dist 为构建产物不入提交。未访问生产、SSH、UI 或真实 provider，不操作额度；父 acceptance、维护工具及私有 Playwright 文件未改动。
