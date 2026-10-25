"""界面简化回归：仅本地模拟 DOM/CLI，不调用模型、不下载地图。"""
import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

import test_ride_web as fixtures
from ride_web import PAGE


class Layout(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = []
        self.parents = {}
        self.attrs = {}
        self.options = {}
        self.select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        identity = attrs.get('id')
        if identity:
            self.parents[identity] = tuple(self.details)
            self.attrs[identity] = attrs
        if tag == 'details':
            self.details.append(identity)
        if tag == 'select':
            self.select = identity
        if tag == 'option':
            self.options.setdefault(self.select, []).append(attrs['value'])

    def handle_endtag(self, tag):
        if tag == 'details':
            self.details.pop()
        if tag == 'select':
            self.select = None


class SimplePageTests(unittest.TestCase):
    def test_default_flow_and_advanced_controls(self):
        page = Layout()
        page.feed(PAGE)
        self.assertEqual(page.details, [])
        for identity in ('image', 'uploadConsent', 'create', 'resultSummary'):
            self.assertEqual(page.parents[identity], ())
        for identity in ('goal', 'mode', 'controls', 'osm', 'roadNames',
                         'roadHypothesis', 'color', 'crop', 'fetch', 'mapConsent', 'status'):
            self.assertTrue(page.parents[identity], identity)
        for identity in ('startAdvanced', 'toolsAdvanced', 'taskRecovery', 'candidatePanel'):
            self.assertNotIn('open', page.attrs[identity])
        self.assertEqual(page.options['goal'][0], 'corridor')
        self.assertEqual(page.options['mode'][0], 'api')
        for identity in ('uploadConsent', 'reviewConsent', 'mapConsent'):
            self.assertNotIn('checked', page.attrs[identity])
        self.assertEqual(page.parents['queryMapConsent'], ())
        self.assertIn('固定 Overpass', PAGE)
        self.assertIn('具体对象', PAGE)

    @unittest.skipUnless(shutil.which('node'), '需要本地 Node')
    def test_summary_create_and_restore_do_not_inject_evidence(self):
        script = PAGE.split('<script nonce="__TOKEN__">')[1].split('</script>')[0]
        harness = r"""
const assert=require('assert'),nodes={},calls=[];
global.document={getElementById:id=>nodes[id]||(nodes[id]={checked:false,value:'',files:[]}),querySelectorAll:()=>[],querySelector:()=>null};
global.FileReader=class{readAsDataURL(){this.result='data:image/png;base64,c2NyZWVuc2hvdA==';this.onload()}};
""" + script + r"""
assert.equal(calls.length,0); // 加载页面不恢复、更不重跑旧任务。
assert(resultSummary({goal:'corridor',map_available:false}).includes('地图证据'));
assert(!resultSummary({goal:'corridor',map_available:false}).includes('JSON'));
assert(resultSummary({goal:'corridor',map_available:true}).includes('已有本地地图'));
assert(resultSummary({corridor_complete:true}).includes('不可导航'));
assert(resultSummary({execution_state:'failed',recovery_hint:'配置未就绪'}).includes('配置未就绪'));
assert(resultSummary({execution_state:'failed',goal:'corridor',map_available:false}).includes('地图证据'));
action=async fn=>fn();render=d=>{};request=async(path,data)=>{calls.push({path,data});return {}};
$('image').files=[{size:12}];$('mode').value='api';$('goal').value='corridor';
$('uploadConsent').checked=true;$('roadNames').value='stale road';$('fetch').checked=true;
(async()=>{
 await nodes.create.onclick();
 assert.deepEqual(calls[0],{path:'/api/tasks',data:{image_base64:'c2NyZWVuc2hvdA==',mode:'api',goal:'corridor',consent_upload:true}});
 $('task').value='11111111-1111-4111-8111-111111111111';
 await nodes.refresh.onclick();assert.equal(calls.length,2);
 assert.equal(calls[1].path,'/api/tasks/'+nodes.task.value);assert.equal(calls[1].data,undefined);
 $('queryMapConsent').checked=true;await nodes.continueMap.onclick();
 assert.deepEqual(calls[2].data,{consent_upload:true,consent_map:true});
 assert(!('road_names' in calls[2].data));assert(!('fetch_map' in calls[2].data));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        subprocess.run(['node', '-e', harness], check=True, capture_output=True)


class MapAvailabilityTests(unittest.TestCase):
    setUp = fixtures.WebTests.setUp
    tearDown = fixtures.WebTests.tearDown
    request = fixtures.WebTests.request
    state = fixtures.WebTests.state

    def test_current_local_map_fact_is_read_only(self):
        task = self.state(goal='corridor')
        self.assertFalse(json.loads(self.request('/api/tasks/'+task)[2])['map_available'])
        # 合成空文件只用于测试“本地存在”事实，不是假装真实道路数据。
        (self.root/task/'task'/'map.osm').write_text('<osm/>')
        result = json.loads(self.request('/api/tasks/'+task)[2])
        self.assertTrue(result['map_available'])
        self.assertNotIn(str(self.root), json.dumps(result))
