"""API与scheduler共用的显式自动跟进配置绑定；不读凭证或探测进程健康。"""
from infra.pilot.config import PilotConfig
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import EmployeeId


def reply_employee_binding(profile: PilotConfig, settings: StandaloneModelSettings) -> EmployeeId | None:
    """默认关闭；启用回复必须同时声明真实Gmail配置和精确员工绑定。"""
    if not settings.reply_enabled:
        return None
    if settings.qq_reply is not None:
        return EmployeeId(settings.qq_reply.classifier_employee_id)
    gmail = profile.gmail
    if gmail is None or not gmail.employee_id or not gmail.employee_id.strip():
        raise ValueError("回复模型缺少明确 Gmail 与员工绑定")
    return EmployeeId(gmail.employee_id)
