"""Work Uploads —— 员工上传聊单与文件。

POST /uploads                    上传批次（原件进 artifact_store）
GET  /uploads/{id}/extraction    提取结果（原始/提取/修改对照展示）
POST /uploads/{id}/confirm       员工确认（可修改后确认；确认才生效）
"""

from __future__ import annotations

from .module_status import build_status_router

router = build_status_router(
    module="work_uploads",
    mode="manual",
    reason_code="WORK_UPLOAD_PIPELINE_NOT_CONFIGURED",
)
