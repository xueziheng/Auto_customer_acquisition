"""Approval Center。

GET  /approvals/pending          我的待审批（按过期时间升序）
GET  /approvals/{id}             审批包全文（目标：不跳页面即可决定）
POST /approvals/{id}/decide      批准/否决（自批禁止在服务层强制）
"""

from __future__ import annotations
