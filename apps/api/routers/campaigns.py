"""Campaign Center。

GET  /campaigns                  列表 + 今日用量/上限
POST /campaigns                  创建（边界校验，发件身份只能选
                                 list_available_for_campaign 的结果）
POST /campaigns/{id}/submit      提交审批
POST /campaigns/{id}/pause       暂停（只停新发送，回复处理不停）
POST /campaigns/{id}/revise      修改边界 = 新版本重新审批
GET  /campaigns/{id}/enrollments 序列进度
GET  /sending-identities         发件身份状态（认证/预热/信誉/熔断）
"""

from __future__ import annotations
