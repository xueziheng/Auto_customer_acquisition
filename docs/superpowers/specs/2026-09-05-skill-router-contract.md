# 技能注册与版本选择契约

日期：2026-09-05。归属：Web 核心收口 Task 1。本文记录实施前细化要求及审查中收敛的解析边界；注册技能本身不启动 Agent、联网或写业务数据。

## 接口与资产

`FileSkillRouter` 实现既有 `SkillRouter` 的 `load_registry`、`select`、`get`，保留调用形状。加载入口接受 skills 根或 canonical 根，只认以下 manifest 布局：

```text
canonical/{skill_id}/manifest.yaml
canonical/{skill_id}/{version}/manifest.yaml
```

技能与版本目录必须和 manifest 相符，同一完整 ID/version 不得重复。SemVer 形状的版本目录缺少 manifest、manifest 出现在其他层级，都使整次加载失败。普通 prompt 资产可以嵌套。

只读取 manifest，注册时不读取 prompt 正文。排除 AppleDouble `._*` 和 schema/upstream 子树。目录遍历使用显式栈，最多 64 层和 10000 个目录项；目录符号链接拒绝。单 manifest 最多 262144 字节，超限固定拒绝。这些是配置解析的资源界限。

`prompt_ref` 必须是 manifest 目录内的相对常规文件，允许 `prompts/system.md`；绝对路径、`..`、最终文件符号链接、缺失文件和解析后越界路径拒绝。`eval_refs` 为非空的 `evals/` 逻辑引用，禁止绝对路径与越界；不要求当前样例引用逐一对应实际文件，也不据此宣称已执行评估。

## 解析与字段

运行依赖为 PyYAML 6.0.3，使用 SafeLoader。重复映射键、未知标签、递归别名和不符合 schema 的字段拒绝；别名检查同时维护活动与已完成节点集合，避免共享非递归结构造成指数遍历。

加载要求全部必填字段且拒绝未知字段；严格校验 domain/risk/cost 枚举、容器/元素类型、工具标识符及正整数 freshness。布尔值不能代替整数。工具标识符只做词法校验，具体注册和授权由后续运行装配与 Gateway 独立检查。

`SkillManifest` 保留原字段，新增带兼容默认值的 `description`、`upstream_ref`、`evals`；文件加载仍执行必填校验，不丢弃护栏或评估元数据。未修改原始 prompt 和三份 canonical manifest。

解析与路径异常统一为固定中文 `ValidationError`，不携带原 YAML、路径或异常链。首次成功加载前查询拒绝；失败重载保留此前完整快照，禁止发布半份注册表。查询结果深拷贝，修改返回的 inputs/outputs 不得改变注册表。

## 版本和选择

遵循 SemVer 2.0.0 的 ASCII 数字、预发布标识符与优先级规则；合法的数字开头混合标识符（如 `1alpha`）允许，纯数字标识符禁止多余前导零。数字比较不依赖受限长度的整数转换。

同优先级 build metadata 以完整版本字符串稳定决胜，指定完整历史版本仍能精确查询。`select` 先取各技能最新版本，再精确匹配 trigger；声明 trigger 更少者优先，其后按 skill_id 稳定排序。无匹配返回空列表，不兜底加载万能技能。`max_skills` 为严格正整数；超限只记固定中文警告。

## 验证与完成口径

行为测试位于 `tests/unit/test_skill_router.py`，覆盖现有资产、版本优先级、路径与布局、YAML 拒绝矩阵、大小界限、别名共享结构、原子重载及返回值修改隔离。测试结果以实施计划及最终同版本验收报告为准，不累加各轮数量。

本组件不读取用户权限、执行 prompt、触发工具或申请业务审批。后续 Context Builder 需要把技能允许工具与员工权限及运行策略取交集，再扣除明确禁用项；Gateway 仍独立判权。

解析依据：[PyYAML 文档](https://pyyaml.org/wiki/PyYAMLDocumentation)、[SemVer 2.0.0](https://semver.org/spec/v2.0.0.html)。
