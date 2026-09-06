# Task 1 实施报告：技能注册与版本选择

## 状态

- 结果：DONE
- 提交：`143983f`（`feat: implement strict file skill router`）
- 工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`
- 分支：`codex/web-core-completion`
- 基线：`0b58bd9dc49faf1ff372fe1726a7ae9e8bc4e49e`

## 需求落实

- 新增 `agent_runtime/skill_router/service.py` 的 `FileSkillRouter`，保留既有 `SkillRouter` Protocol 和三个公开接口。
- `load_registry` 同时接受 `skills/` 根和 `skills/canonical/` 根，只检查 canonical 的两种受支持布局；忽略 `._*` 与明确排除的 schema/upstream 目录，拒绝越界链接、错误目录绑定、过深层级、空注册表和超过 262144 字节的 manifest。
- YAML 使用 `yaml.SafeLoader` 子类；重复映射键、未知标签、递归别名均失败关闭。解析、文件与路径异常统一转换为不带 context、cause、路径或原异常的固定 `ValidationError("技能注册表无效")`。
- schema 加载严格要求所有必填字段并拒绝未知字段；严格校验 domain/risk/cost、容器与元素类型、正整数 freshness、工具 ID、prompt 常规文件和非空 `evals/` 引用。eval 引用只做逻辑路径校验，未要求样例实际存在。
- `SkillManifest` 增加带兼容默认值的 `description`、`upstream_ref`、`evals`，加载时完整保留 schema 字段；未改权威 schema 或三个 canonical manifest/prompt。
- SemVer 2.0.0 实现数字与预发布优先级，无整数长度上限；同优先级 build metadata 用完整版本字符串稳定决胜，完整历史版本可精确查询。
- `select` 先按 skill ID 取最新版本，再精确匹配 trigger；按 trigger 数量、skill ID 排序；严格拒绝 bool/非整数/非正 `max_skills`；截断只记录固定中文日志。
- 首次成功加载前 `get/select` 固定拒绝；重载先建立完整本地快照，失败保留旧快照；`get/select` 深拷贝返回，外部不能修改内部 inputs/outputs。
- `pyproject.toml` 增加运行依赖 `PyYAML==6.0.3` 和开发依赖 `types-PyYAML`。按裁定使用已准备的私有 `.venv`，未访问网络、数据库、模型、密钥、`.env` 或 DSN。

## TDD 证据

首次 RED：

```text
.venv/bin/python -m pytest tests/unit/test_skill_router.py -q
74 failed in 0.52s
```

失败原因是 `agent_runtime.skill_router.service` 不存在以及 `SkillManifest` 缺少新契约字段，符合预期。实现后的自审又分别用失败测试确认了三个边界：同目录 prompt 符号链接未拒绝（1 failed）、显式 null upstream_ref 与超长合法 SemVer（2 failed）、canonical 根 manifest 层级（1 failed），然后做最小修复。

最终 GREEN：

```text
.venv/bin/python -m pytest tests/unit/test_skill_router.py -q
78 passed in 0.24s
```

覆盖精确选择、最新/历史版本、无匹配、现有三份 manifest、完整字段映射、SemVer 预发布/build/超长数字、排序/限额日志、not-ready/not-found、重复 ID/version、逐项缺字段、严格类型与枚举、未知字段、prompt/eval 路径、符号链接、层级、SafeLoader 拒绝项、大小上限、空 registry、目录绑定、原子重载和返回值修改隔离。

## 最终检查

```text
.venv/bin/python -m ruff check agent_runtime/skill_router/router.py agent_runtime/skill_router/service.py tests/unit/test_skill_router.py pyproject.toml
All checks passed!

.venv/bin/python -m mypy agent_runtime/skill_router/router.py agent_runtime/skill_router/service.py tests/unit/test_skill_router.py
Success: no issues found in 3 source files

.venv/bin/python scripts/check_boundaries.py
结构自检通过。

git diff --check
exit 0
```

提交前 staged 范围仅包含：

```text
agent_runtime/skill_router/router.py
agent_runtime/skill_router/service.py
pyproject.toml
tests/unit/test_skill_router.py
```

## 自审与关注点

- 未发现未满足的 Task 1 要求或需要控制器裁定的 schema/布局冲突。
- 按裁定没有验证 `eval_refs` 对应文件是否存在；这是有意兼容现有三份示例引用，不代表相关业务 eval 已运行。
- 本任务只验证纯文件注册和选择，没有运行全仓数据库或真实外部服务验收；Task 1 不需要这些检查。
- Git 对共享对象库仍会产生已知 AppleDouble `non-monotonic index` stderr；所有 Git 子进程 stderr 均重定向，未修复 `.git` 或旧 Catalog 环境，提交成功。

## Fix round 1（task-1-review 四项 Important）

- 提交：`ca7827d`（`fix: harden skill router parsing`）。
- SemVer 改为纯 ASCII 数字规则，接受 `1.0.0-1alpha`、`1.0.0-01a` 等包含字母/连字符的合法非数字 prerelease identifier，拒绝 core/prerelease 中的非 ASCII 数字。测试逐步加载并核对从 digit-leading prerelease、alpha/beta/rc 到 stable/build 决胜的完整上升序列。
- alias DAG 遍历保留 active 集合做环检测，并增加 completed 集合；每个 mapping/list 最多完整遍历一次。回归用 28 层小型 DAG 和独占子进程 2 秒 timeout 验证公开 `load_registry` 快速固定拒绝；RED 超时时子进程已 kill + communicate 清理，没有运行巨型压力测试。
- `prompt_ref` 允许 manifest 目录内的嵌套常规文件，仍拒绝绝对路径、`..`、缺失文件、最终符号链接和解析后越界；manifest 扫描只把受支持层级的 `manifest.yaml` 当技能版本，并允许普通 prompt 子目录。
- 将空 registry 拒绝与 AppleDouble 忽略拆成两个测试；AppleDouble 测试在合法 manifest 旁放置恶意 `._manifest.yaml`、`._` 目录和嵌套坏 manifest，证明只加载合法项。

针对性 RED：

```text
.venv/bin/python -m pytest tests/unit/test_skill_router.py -q -k 'semver_precedence or invalid_schema_values and version or nested_prompt or alias_dag or appledouble'
5 failed, 5 passed, 72 deselected in 2.15s
```

失败分别命中合法 prerelease 拒绝、两种非 ASCII 数字错误接受、嵌套 prompt 错误拒绝和 alias DAG 超时。AppleDouble 行为证明测试在 RED 阶段已通过，说明实现已有忽略行为，原测试只是未能证明它。

针对性 GREEN：

```text
10 passed, 72 deselected in 0.19s
```

最终验证：

```text
.venv/bin/python -m pytest tests/unit/test_skill_router.py -q
82 passed in 0.27s

.venv/bin/python -m ruff check agent_runtime/skill_router/router.py agent_runtime/skill_router/service.py tests/unit/test_skill_router.py pyproject.toml
All checks passed!

.venv/bin/python -m mypy agent_runtime/skill_router/router.py agent_runtime/skill_router/service.py tests/unit/test_skill_router.py
Success: no issues found in 3 source files

.venv/bin/python scripts/check_boundaries.py
结构自检通过。

git diff --check
exit 0
```

本轮提交只包含 `agent_runtime/skill_router/service.py` 与 `tests/unit/test_skill_router.py`。未发现剩余的 review Important 或需要控制器裁定的新冲突。

## Fix round 2（深层错位 manifest 与空版本目录）

- 提交：`5ab99fe`（`fix: bound skill manifest discovery`）。
- manifest 发现改为显式迭代目录遍历，最多 64 层、10000 个目录项，超过上限固定拒绝；不递归 `._*` 和 schema/upstream 排除子树。
- 任意深度出现 `manifest.yaml` 时，仅允许 `canonical/{skill_id}/manifest.yaml` 与 `canonical/{skill_id}/{version}/manifest.yaml` 两种位置；更深的错位 manifest 固定拒绝。
- skill 直属的 SemVer 形状目录必须包含该层 `manifest.yaml`；有效版本旁存在空 `2.0.0/` 也会使整次加载失败，保留原子快照语义。
- 扫描不递归目录符号链接；非 AppleDouble 目录符号链接一律固定拒绝。嵌套常规 prompt 资产继续通过完整公开 `load_registry` 加载，扫描不读取 prompt 正文。

针对性 RED：

```text
.venv/bin/python -m pytest tests/unit/test_skill_router.py -q -k 'deep_manifest_beside or empty_semver_directory or directory_symlink_inside or nested_prompt_inside'
3 failed, 1 passed, 81 deselected in 0.11s
```

三个失败分别证明旧发现器会忽略目录符号链接、有效版本旁的更深错位 manifest、有效版本旁的空 SemVer 目录；嵌套 prompt 的公开加载回归保持通过。

针对性 GREEN：

```text
4 passed, 81 deselected in 0.10s
```

最终验证：

```text
.venv/bin/python -m pytest tests/unit/test_skill_router.py -q
85 passed in 0.30s

.venv/bin/python -m ruff check agent_runtime/skill_router/router.py agent_runtime/skill_router/service.py tests/unit/test_skill_router.py pyproject.toml
All checks passed!

.venv/bin/python -m mypy agent_runtime/skill_router/router.py agent_runtime/skill_router/service.py tests/unit/test_skill_router.py
Success: no issues found in 3 source files

.venv/bin/python scripts/check_boundaries.py
结构自检通过。

git diff --check
exit 0
```

提交范围仅为扫描实现与对应测试。未修改 schema、manifest、prompt、依赖或其他任务文件。
