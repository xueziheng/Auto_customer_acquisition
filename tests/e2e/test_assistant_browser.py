"""受控 UI 交互，真实 Chromium；后端持久性由独立 PG 验收覆盖。"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e.conftest import (
    _free_port,
    _minimal_process_env,
    _start_process,
    _wait_for_http,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.e2e
@pytest.mark.parametrize('width,height', [(1440,900),(390,844)])
async def test_assistant_create_clarify_refresh_and_source(width,height):
    origin=f'http://127.0.0.1:{_free_port()}'
    sessions=[]
    turns=[]
    posts=[]
    with TemporaryDirectory(prefix='tradeos-assistant-ui-') as temporary, (Path(temporary)/'vite.log').open('wb') as log:
        web=_start_process(['node','node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',origin.rsplit(':',1)[1],'--strictPort'],cwd=ROOT/'apps/web',env={**_minimal_process_env(),'VITE_API_BASE_URL':origin+'/api','VITE_TENANT_ID':'tenant_test','VITE_EMPLOYEE_ID':'emp_test'},stdout=log,stderr=log)
        try:
            await _wait_for_http(origin+'/commands',web)
            async with async_playwright() as p:
                browser=await p.chromium.launch(channel=os.environ.get("TRADEOS_TEST_BROWSER_CHANNEL"))
                page=await browser.new_page(viewport={'width':width,'height':height})
                errors=[]
                page.on('pageerror',lambda e:errors.append(type(e).__name__))
                async def api(route):
                    request=route.request
                    path=request.url.split('/api',1)[1]
                    data={}
                    if path=='/health/capabilities':
                        data=[{'name':'builtin_assistant','status':'enabled','reason':'composed'}]
                    elif path=='/agent/sessions':
                        if request.method=='POST':
                            sessions.append({'session_id':'session_test','created_at':'2026-09-22T00:00:00Z','version':1})
                            data=sessions[-1]
                        else:data=sessions
                    elif path=='/agent/sessions/session_test/turns':
                        if request.method=='POST':
                            posts.append(request.post_data_json)
                            turns.append({'turn_id':'turn_test','session_id':'session_test','run_id':'run_test','state':'awaiting_input','created_at':'2026-09-22T00:00:00Z','input_text':posts[-1]['text'],'result':{'kind':'clarify','questions':['请明确国家和搜索次数上限。'],'missing_fields':['target_countries']}})
                            data=turns[-1]
                        else:data=turns
                    elif path.startswith('/sourcing-admissions'):data={'policy':{'status':'policy_not_configured'},'items':[]}
                    elif 'notifications' in path:data={'unread_count':0,'items':[]}
                    else:
                        await route.fulfill(status=503,json={'code':'not_configured','message':'受控验收未装配此能力'});return
                    await route.fulfill(status=201 if path=='/agent/sessions' and request.method=='POST' else 202 if request.method=='POST' else 200,body=json.dumps(data),content_type='application/json')
                await page.route(origin+'/api/**',api)
                await page.goto(origin+'/commands')
                assert await page.title()=='TradeOS'
                await page.get_by_role('button',name='新建会话',exact=True).click(timeout=5000)
                await page.get_by_label('给助手的消息').fill('帮我研究市场')
                await page.get_by_role('button',name='发送消息',exact=True).click()
                await expect(page.get_by_text('请明确国家和搜索次数上限。',exact=True)).to_be_visible()
                await page.reload()
                await expect(page.get_by_text('帮我研究市场',exact=True)).to_be_visible()
                assert len(posts)==1
                turns[0]['result']={'kind':'explain','fragments':[{'text':'产品帮助：发现信号不等于已验证需求。','dependencies':[{'kind':'need','object_id':'need_test','version':'v1'}],'source_turn_ids':[]}]}
                turns[0]['state']='completed'
                await page.reload()
                link=page.get_by_role('link',name='查看来源 need_test')
                await expect(link).to_have_attribute('href','/demand/needs/need_test')
                assert await page.locator('vite-error-overlay').count()==0
                assert not errors
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                screenshots=Path(os.environ.get('TRADEOS_E2E_SCREENSHOTS',temporary));screenshots.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(screenshots/f'assistant-{width}.png'),full_page=True)
                await link.click()
                await expect(page).to_have_url(origin+'/demand/needs/need_test')
                await browser.close()
        finally:web.stop()
