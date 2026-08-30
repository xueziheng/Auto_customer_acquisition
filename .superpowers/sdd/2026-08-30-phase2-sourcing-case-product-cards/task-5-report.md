# Task 5 实施报告：产品池与供应商域服务

## 结果

- 实现 `CandidateProductCreate` / `CandidateIndicativePriceRef` 严格、冻结、禁止额外字段的命令边界；JSON 金额只接受十进制字符串，Python 只接受 `Decimal`。
- 实现 `ProductServiceImpl`：所有入口 authorizer-first，候选卡固定 `candidate/source_only`，以 `(tenant, case, candidate)` 幂等并在 canonical winner 不同时抛冲突回滚。
- 以三个不同 DTO 强制 internal / sales / customer 字段边界；只有 internal view 可见供应商、完整内部成本五元组、来源聚合及原始 indicative price Evidence；`source_only` 的询价和样品入口关闭。
- 产品匹配仅使用规范化品类、关键字和稳定 Product ID；成本金额、币种、口径、单位、来源任一不完整即从自动交接合格项排除，并返回 `internal_cost_incomplete` 结构化 finding，不补零。
- 实现 `SupplierServiceImpl`：能力标签规范化交集、稳定排序及 50 条上限；价格只追加，quoted 要求 Artifact Evidence、明确有效期且记录时未过期，金额为有限正 `Decimal`；indicative 不投影为 quoted。
- 按 Ruling P11 为 Task 4 候选来源仓储增加 tenant-bound `get_by_product` 只读合同/实现；跨租户读取返回 `None`，错配绑定租户直接拒绝。该扩展只供 internal view，不进入 sales/customer DTO。

## TDD 记录

RED：

```text
pytest tests/unit/test_product_service.py tests/unit/test_supplier_service.py \
       tests/integration/test_product_candidate_idempotency.py -q
21 failed in 3.45s
```

失败原因符合预期：`CandidateProductCreate`、`ProductServiceImpl`、`SupplierServiceImpl` 尚不存在；失败直接命中计划要求的新增行为。

首轮 GREEN：

```text
21 passed in 3.62s
```

随后结构检查发现测试直接导入域内部模型；已改为经各域 `service.py` 公共重导出使用，生产依赖方向未放宽。

## 最终门禁

```text
pytest tests/unit/test_product_service.py tests/unit/test_supplier_service.py \
       tests/integration/test_product_candidate_idempotency.py \
       tests/integration/test_supply_pool_repositories.py \
       tests/integration/test_sourcing_migrations.py -q
48 passed in 23.23s

ruff check <Task 5 touched Python files>
All checks passed!

mypy domains/products domains/suppliers infra/db/repositories/products.py \
     tests/unit/test_product_service.py tests/unit/test_supplier_service.py \
     tests/integration/test_product_candidate_idempotency.py
Success: no issues found in 21 source files

python3 scripts/check_boundaries.py
结构自检通过。

git diff --check
通过（仅出现已知 AppleDouble pack index 警告）。
```

## 关注项

- 无未解决的 Task 5 阻断或测试失败。
- Task 7 工作流装配必须从可信内部身份派生 `ProductActor` / `SupplierActor`；不能把 HTTP 请求体里的 role 字符串直接构造成 actor。
- 已知 Git AppleDouble `non-monotonic index` 警告未处理，按总任务约束保留。

## Fix Round 1

处理 reviewer 的两项 Important：

- 产品候选参考价与供应商价格在 UoW 前执行 `NUMERIC(28,12)` 精确可表示性门禁：最多 12 位小数、最多 16 位整数；不量化、不舍入。Python `Decimal` 与 JSON 十进制字符串的超精度/整数溢出均拒绝，最大合法边界在真实 PostgreSQL 中按原始 Decimal tuple 精确回读。
- 供应商公共合同新增冻结的 `SupplierQuoteEvidence`、`SupplierQuoteSourceKind` 与 `SupplierQuoteEvidenceReader`。`SupplierServiceImpl` 注入可信 reader；quoted 路径在 UoW 前逐项核对 tenant、Artifact、supplier、product/spec description、quantity tier、金额、币种、观察时间、有效期及直接供应商报价来源。公开网页、indicative、字段错配和 reader 故障全部失败关闭；indicative 路径不读取该 reader。
- 仍保持 authorizer-first：未授权 quoted 请求不会做金额校验、Evidence IO 或打开 UoW。未增加 Evidence adapter；按计划留给 Task 13 装配。

TDD：

```text
RED: pytest tests/unit/test_product_service.py tests/unit/test_supplier_service.py -q
19 failed, 10 passed in 0.26s

GREEN: pytest tests/unit/test_product_service.py tests/unit/test_supplier_service.py -q
29 passed in 0.16s
```

Fix Round 1 最终门禁：

```text
pytest tests/unit/test_product_service.py tests/unit/test_supplier_service.py \
       tests/integration/test_product_candidate_idempotency.py \
       tests/integration/test_supply_pool_repositories.py \
       tests/integration/test_sourcing_migrations.py -q
59 passed in 17.15s

ruff check <Fix Round 1 touched Python files>
All checks passed!

mypy domains/products domains/suppliers infra/db/repositories/products.py \
     tests/unit/test_product_service.py tests/unit/test_supplier_service.py \
     tests/integration/test_product_candidate_idempotency.py
Success: no issues found in 21 source files

python3 scripts/check_boundaries.py
结构自检通过。

git diff --check
通过（仅出现已知 AppleDouble pack index 警告）。
```

真实双事务 barrier 竞态仍按账本 Minor 延后，本轮未扩大范围。
