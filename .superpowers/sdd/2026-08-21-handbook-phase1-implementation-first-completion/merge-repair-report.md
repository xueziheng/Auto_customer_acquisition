# 本地合并复验修复报告

日期：2026-08-27

修复基线：`d65b16911fa80fc637d076b2423bf2054b429dfd`

已在修复分支和 main 分别验证的代码提交：
`6a0e644312983514114336d61822ed53f78fec90`

## 结论

首次主目录复验的三项失败已修复，脚本子进程已显式绑定所选 checkout。独立代码审查通过，
修复分支与合并后的 main 均完成完整受控验收。用户选择的本地合并与临时工作树清理已完成；
未 push、部署、真实发信或调用真实 provider。后续文档提交不改变已验证代码。

## 根因与最小修复

1. Campaign 激活正式发布一条 `CampaignStateChanged`，但邮件反馈测试的 outbox 断言
   及触达演示脚本/测试仍采用旧事件白名单。更新精确事件集合和数量，保留租户隔离的真实
   数据库回读；没有删除生产事件，也没有改成忽略未知事件或仅检查子集。
2. Python 以文件脚本启动时，`cwd` 不会自动成为项目导入根；原演示验收的受限环境没有
   `PYTHONPATH`，导致子进程通过 editable 安装加载主目录，而父测试加载工作树。
   8 个启动器现在显式指定自身仓库根，并在额外环境合并后覆盖 `PYTHONPATH`，不继承
   父进程额外环境。没有重装依赖或修改全局 Python 环境。
3. 新回归调用实际演示启动器和真实 Python 子进程，使用带空格的临时 checkout、同名包、
   错误路径与父环境 sentinel，检查实际包文件来源、显式输入保留以及父 sentinel 未继承。
   它不依赖 mock 返回值或源码字符串断言。

修复涉及一个演示脚本、8 个演示测试文件和一个新增回归测试文件；没有业务域、公共事件、
数据库迁移、金额/置信度规则、审批或租户边界变更。

## RED → GREEN 与独立审查

- 新增导入隔离回归：修复前 **8 failed**（2.61s），失败均为加载了错误 checkout。
- 原三项集成测试：本轮修复前再次 **3 failed**（24.29s），与首次主目录失败一致。
- 修复后运行新回归及完整邮件反馈、触达演示测试文件：**17 passed**（42.32s）。
- 独立审查范围 `d65b169..6a0e644`，Critical / Important / Minor 均为 0；审查者
  独立执行新增回归 **8 passed**（1.49s），未修改工作树或运行重型数据库测试。

## 完整验收

Python 使用 `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3`。
后端命令为 `python3 -m pytest -q -m 'not e2e'`；浏览器命令为
`TRADEOS_REQUIRE_E2E=1 python3 -m pytest tests/e2e -q -W error`。

| 门禁 | 修复分支 | 合并后的 main |
|---|---|---|
| 完整非 E2E（含数据库迁移、集成测试） | 4007 passed / 6 deselected，442.62s | 4007 passed / 6 deselected，431.61s |
| 强制浏览器 E2E | 6 passed，48.14s | 6 passed，57.67s |
| Web tests | 19 files / 151 passed，20.21s | 19 files / 151 passed，13.13s |
| API 类型生成与生成文件零差异 | 通过 | 通过 |
| Web typecheck / build | 通过，96 modules | 通过，96 modules |
| Web lint | 0 errors / 133 既有 warnings | `--quiet` 错误门禁通过；未宣称警告消失 |
| Ruff / mypy | 通过 / 394 文件无错误 | 通过 / 394 文件无错误 |
| 架构边界 / 敏感扫描 / diff check | 七类通过 / 无发现 / 通过 | 七类通过 / 无发现 / 通过 |

两轮测试都验证同一代码提交。主目录复验之前已完成独立审查，且主目录无未提交修改；
使用 fast-forward 合并，没有冲突处理或隐式内容改写。

## 清理与可恢复性

- 删除已合并的 `codex/phase1-acceptance-completion` 分支及其工作树登记。
- 只移除工作树中的 `apps/web/node_modules` 符号链接，主目录依赖目录保留。
- Git 移除工作树时返回 `Directory not empty`，但已移除登记；剩余缓存、元数据与资料
  整体移动到下面归档目录的 `worktree-remnants/`，未使用强制删除。
- `ai-outbound-mvp` 工作树及其提交 `e70831c07abea739d0de71b4ab8a23bf0436cf11` 未变。
- 源码全部保留在 main；工作树中被忽略的简报/审查记录在删除前已归档、解压并逐文件
  比较核对。归档及可读副本位于：

`/Volumes/T7/Company/Auto_customer_acquisition/.superpowers/sdd/phase1-local-merge-archive-20260827.0ytrtt/`

其中 `worktree-notes.tar.gz` 的 SHA-256 为
`4eb0eb9a90f97d394b5b8b9d4fc4e0a07feedcea19ed1635c1baf58369fcae82`。

## 仍未完成的运营验收

真实 provider/model、Hunter/Gmail、真实邮件与客户需求、发件信誉和真实人工接管 SLA
均未运行或观察，仍为 `not_run`。本次恢复的是受控代码验收结论，不是 Phase 1 运营完成。
