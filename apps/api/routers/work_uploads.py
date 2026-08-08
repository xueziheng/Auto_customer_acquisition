"""Work Uploads —— 员工上传聊单与文件。

POST /uploads                    上传批次（原件进 artifact_store）
GET  /uploads/{id}/extraction    提取结果（原始/提取/修改对照展示）
POST /uploads/{id}/confirm       员工确认（可修改后确认；确认才生效）
"""

from __future__ import annotations
