"""Demand Radar —— 需求信号、假设、需求簇。

GET /demand/signals              信号列表（按类型/状态过滤）
GET /demand/hypotheses           假设列表（置信度档位现算，含解释）
GET /demand/hypotheses/{id}      假设详情 + 证据链（可点击到原始来源）
GET /demand/needs                已验证需求（含完整度与缺失字段）
GET /demand/needs/{id}           详情：每个字段带来源与客户原话摘录
GET /demand/clusters             需求簇

界面要求：假设与已验证需求必须视觉区分（is_inference 标记）——
推断长得和事实一样，老板就会把推断当事实。
"""

from __future__ import annotations
