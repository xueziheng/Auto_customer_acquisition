# Task 3 审查修复报告

两项 P2 已 addressed。只修改候选库存 UI、最小恢复模块及定向 Vue 测试；没有修改后端、访问生产、凭证或原 /Volumes/T7 工作区。

## 变更

- GET 任一 403 到达后立即清空额度、分配、邮箱/证据、任务、来源和交付提示，不等待其他 GET；所有 GET 非 200 或网络失败同样清空。POST403 清空保护数据并使在途 GET 失效。
- 未确认 POST 在提交前保留原 key/body，绑定 identitySnapshot 的 mode/tenant/employee。sessionStorage 仅保存身份绑定和请求元数据，不保存候选、邮箱、认证或凭证；同身份导航重新挂载和页面运行时重建可恢复。未决请求优先沿原键/原体重试，控件禁止改参数；只有确定响应清除。身份订阅在组件未挂载时也清理旧记录；身份 epoch 与 snapshot generation 阻止迟到响应影响新身份。
- sessionStorage 不能写入时保留内存会话，显示“刷新页面无法恢复原请求”，不再作绝对幂等保证。页面恢复测试为 happy-dom + 模块重建 + 保留 sessionStorage 的受控模拟，没有冒充真实浏览器 reload。
- available 改为“可尝试采集”。两个客户发现旧测试的“无需手动操作”断言同步为库存领取与公开邮箱仍需验证的真实契约；没有恢复误导文案。客户发现测试补库存 status 合成响应，避免 Invalid Date。

## TDD 与验证

首次新增测试 7 项失败，分别复现 GET 403/503/network 保留资料、POST403 保留资料、available 文案、导航丢键和浏览器恢复丢键；修复后 10 passed。再补立即403/存储降级/迟到响应，观察立即403和存储不可用两项失败后处理，13 passed。

最终定向命令 `npm test -- tests/prospect-inventory.test.ts tests/customer-discovery.test.ts tests/research.test.ts`：3 files，37 passed。

`npm run typecheck`：通过。

`npx eslint src/views/customer-discovery/InventoryPanel.vue src/views/customer-discovery/inventoryRecovery.ts tests/prospect-inventory.test.ts tests/customer-discovery.test.ts tests/research.test.ts`：通过，零警告。

`npm run lint`：0 errors，1060 warnings，均为改动文件之外的历史 Vue 格式等警告；日志 task-3-fix-lint.log。

`npm run build`：通过，218 modules；已有 PlatformConsole 动静态同时 import 的警告仍存在。

`/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python scripts/check_boundaries.py`：通过。`git diff --check`：通过。

曾执行一次全部前端 `npm test`：50 files，548 tests，545 passed / 3 failed：

1. tests/customer-discovery.test.ts 的 `shows found-customer evidence and verification states without manual discovery controls`：旧“无需手动操作”断言，本报告上述定向运行已修复。
2. tests/research.test.ts 的 `自动找客户页仅展示已找到客户及其研究证据`：相同旧断言，已修复并通过定向运行。
3. tests/team-center.test.ts 的 `renders active employees and deterministic territory rules without fuzzy scores`：预期仅 /team/employees 和 /team/territory，实际额外请求 /team/account-creation。TeamCenter 不在本次范围，未修改。

全量运行还出现一次 socket hang up / ECONNRESET 输出，未成为额外测试失败。按父任务要求不继续扩大或重跑全部前端；最终全量通过未宣称。没有重跑全后端、生产 UI 或真实采集。
