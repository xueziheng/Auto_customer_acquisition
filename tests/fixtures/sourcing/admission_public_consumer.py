"""静态类型检查夹具：验证公开准入模块不依赖动态导出。"""

from domains.sourcing.admission import AdmissionState


def is_waiting(state: AdmissionState) -> bool:
    """模拟领域外只读取公开状态类型的消费者。"""

    return state is AdmissionState.WAITING
