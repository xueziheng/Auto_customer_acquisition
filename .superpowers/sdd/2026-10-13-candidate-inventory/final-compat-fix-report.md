# ADR0076 来源状态兼容补丁

基线 `4d4b95c`；只修 final-re-review 新增 P1。修改 `infra/db/inventory_sources.py` 与定向来源测试，不修改分页、额度计算、Gateway、预算、预留或API契约。

## 修复与证据

`prepare` 和 `list_sources` 仅将 `UNKNOWN 且 included_credits_free=False` 视为待核实状态。ADR0076 认可的 `UNKNOWN + included_credits_free=True + paygo_enabled=None` 按真实余额显示 available / quota_exhausted，且不做无谓 probe。快照 None、真正未核实 UNKNOWN 仍按原 guard/Gateway 恢复；已知付费仍 permission_denied。

新增参数化测试直接使用真实 Tavily `_decode_usage` 生成 `SearchUsage`，再构造真实 `SearchQuotaSnapshot`；验证 `verified_free_remaining`、来源状态、费用类别、来源选择计划、只读列表不probe及 prepare探测条件。覆盖套餐内免费余额5、耗尽0、明确免费、缺按量字段未核实、已知付费。

TDD RED：修复前 `tests/unit/test_inventory_sources.py` 得到 `2 failed, 9 passed`；两项失败均是套餐内免费状态错误显示unhealthy（应分别为available/quota_exhausted）。修复后来源及额度单测58通过。

## 精确命令与最终结果

在本工作树根执行：

```sh
/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python -m pytest tests/unit/test_inventory_sources.py tests/unit/test_free_search_quota.py -q
# 58 passed in 0.21s
/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python -m pytest tests/integration/test_search_quota.py -k 'null_paygo or unknown_usage_recovers or inventory_usage_observation' -q
# 5 passed, 42 deselected in 4.36s
/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/ruff check infra/db/inventory_sources.py tests/unit/test_inventory_sources.py
# All checks passed!
/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/mypy --follow-imports=silent infra/db/inventory_sources.py
# Success: no issues found in 1 source file
/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python scripts/check_boundaries.py
# 七项检查通过；结构自检通过。
git diff --check
# exit 0，无输出。
```

5项真实PG回归含 null按量未知拒绝、套餐内免费耗尽不派发、免费事实重启持久化、观察不提升上限，以及暂态UNKNOWN经新job恢复且不退款/不重派旧Run；均通过。

未访问生产/凭证，未调用真实Provider，未派子代理；未重复前端或此前94项全组。只验证本次兼容分支及相关既有额度契约，不声称已完成线上发布。
