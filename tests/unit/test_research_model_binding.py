"""研究模型只能绑定规范确认 Run，不能信任模型携带的身份。"""
import pytest

from apps.scheduler_worker.research_model_binding import validate_binding
from shared.errors import ValidationError


@pytest.mark.parametrize('run,employee',[('', 'emp'),('run',''),(' run','emp')])
def test_research_model_cannot_run_without_actor(run,employee):
    with pytest.raises(ValidationError):
        validate_binding(run,employee)
