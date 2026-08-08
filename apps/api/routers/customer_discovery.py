"""Customer Discovery —— 潜在企业、联系人、评分。

GET /prospects/accounts          企业列表（按 ABAC 范围过滤）
GET /prospects/accounts/{id}     详情：来源信号、假设、联系人、归属
GET /prospects/accounts/{id}/score   打分快照 + 门槛/因子解释
POST /prospects/accounts/{id}/assign 手动分配（走 employees 归属锁）
"""

from __future__ import annotations
