"""Product & Supply Center。

GET /products                    列表（登录角色决定返回哪个视图）
GET /products/{id}/internal      内部视图（boss/product/sourcing/finance）
GET /products/{id}/sales         销售视图
GET /products/{id}/customer      客户视图 —— **对外渠道唯一允许的方法**
GET /capabilities                供应能力池

纪律：三个视图三个端点三个响应模型，不做 ?view= 参数——
参数会传错，端点权限不会。
"""

from __future__ import annotations
