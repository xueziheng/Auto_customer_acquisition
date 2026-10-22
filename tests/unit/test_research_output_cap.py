"""研究独立上限不改变全局模型配置。"""
import pytest

from apps.scheduler_worker.research_model_binding import BoundResearchCapability
from infra.standalone.settings import StandaloneModelSettings
from shared.errors import ValidationError
from tests.unit.test_standalone_model_settings import settings


@pytest.mark.parametrize('value,expected', [(128,128),(4096,4096),(8192,8192),(32768,8192)])
def test_research_only_cap(value,expected):
    data=settings()
    data['limits']['max_output_tokens']=value
    data['research']={'secret_ref':'TAVILY_API_KEY','exclusive_account_confirmed':True,
        'playbook_reader_user_id':'user_test','maximum_artifact_bytes':4194304,
        'search_timeout_seconds':30,'page_timeout_seconds':30}
    config=StandaloneModelSettings.model_validate(data)
    assert config.limits.max_output_tokens==value
    capability=BoundResearchCapability(None,config.model,config.limits.max_output_tokens)
    assert capability._limit==expected

@pytest.mark.parametrize('value', [127,0,True,'8192'])
def test_invalid_research_limit(value):
    with pytest.raises(ValidationError):
        BoundResearchCapability(None,'synthetic',value)

def test_reply_preserves_immutable_global_limits():
    data=settings()
    data['limits']['max_output_tokens']=32768
    data['reply_enabled']=True
    assert StandaloneModelSettings.model_validate(data).limits.max_output_tokens == 32768
