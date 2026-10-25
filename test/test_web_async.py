"""真实 HTTP 异步协议；不使用旧 fixture 的等待/状态码兼容层，不访问外网。"""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from ride_web import PAGE, RideServer
import test_ride_web as fixtures


class AsyncHTTPTests(unittest.TestCase):
    setUp = fixtures.WebTests.setUp
    tearDown = fixtures.WebTests.tearDown
    state = fixtures.WebTests.state

    def raw(self, path, data=None):
        return fixtures.WebTests.request(self, path, data, wait=False)

    def wait_done(self, task_id):
        for _ in range(200):
            result = json.loads(self.raw('/api/tasks/'+task_id)[2])
            if result['execution_state'] not in ('queued', 'running'):
                return result
            time.sleep(.01)
        self.fail('后台任务未结束')

    def test_fast_create_status_resume_holes_and_safe_failure(self):
        entered, release = threading.Event(), threading.Event()
        def slow(args):
            entered.set()
            release.wait(4)
            raise RuntimeError('SECRET raw exception /private/path')
        with patch.object(self.server, 'run_cli', side_effect=slow):
            before = time.monotonic()
            code, _, body = self.raw('/api/tasks', {'image_base64':'eA==', 'mode':'demo'})
            self.assertEqual(code, 202)
            self.assertLess(time.monotonic()-before, .8)
            task_id = json.loads(body)['task_id']
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            status = json.loads(self.raw('/api/tasks/'+task_id)[2])
            self.assertEqual(status['execution_state'], 'running')
            self.assertLess(time.monotonic()-before, .8)
            for suffix, data in [('/resume', {'answer':'yes'}), ('/holes/preview', {}),
                                 ('/holes/apply', {}), ('/holes/decide', {})]:
                self.assertEqual(self.raw('/api/tasks/'+task_id+suffix, data)[0], 409)
            release.set()
            result = self.wait_done(task_id)
        self.assertEqual(result['execution_state'], 'failed')
        self.assertEqual(result['web_execution']['error_category'], 'local_state_failed')
        self.assertNotIn('SECRET', json.dumps(result))
        # 无 task.json 的失败仍保留编号和安全错误，重启不会重放。
        other = RideServer(0, output_root=self.root)
        try:
            self.assertEqual(other.status(task_id)['execution_state'], 'failed')
        finally:
            other.server_close()

    def test_resume_fast_failure_restart_and_no_automatic_upload(self):
        task_id = self.state(mode='api')
        entered, release = threading.Event(), threading.Event()
        def slow(args):
            entered.set(); release.wait(4)
        with patch.object(self.server, 'run_cli', side_effect=slow) as run:
            self.assertEqual(self.raw('/api/tasks/'+task_id+'/resume', {'answer':'ok'})[0], 403)
            self.assertEqual(self.raw('/api/tasks', {'mode':'api','image_base64':'eA=='})[0],403)
            run.assert_not_called()
            before = time.monotonic()
            self.assertEqual(self.raw('/api/tasks/'+task_id+'/resume', {'answer':'ok','consent_upload':True})[0],202)
            self.assertLess(time.monotonic()-before,.8)
            self.assertTrue(entered.wait(1))
            # 模拟另一个进程启动时读取上次未完成记录；不执行任何旧命令。
            other = RideServer(0, output_root=self.root)
            try:
                record = other.status(task_id)['web_execution']
                self.assertEqual(record['state'],'failed')
                self.assertEqual(record['error_category'],'interrupted')
                self.assertTrue(other.work_queue.empty())
                self.assertNotIn('consent', (self.root/task_id/'web-execution.json').read_text())
            finally:
                other.server_close()
            release.set()
            self.assertEqual(self.wait_done(task_id)['execution_state'],'succeeded')
            self.assertEqual(run.call_count,1)

    def test_bounded_queue_and_task_storage(self):
        release = threading.Event()
        with patch.object(self.server,'run_cli',side_effect=lambda args:release.wait(4)):
            ids=[]
            try:
                for _ in range(6):
                    code,_,body=self.raw('/api/tasks',{'mode':'demo','image_base64':'eA=='})
                    self.assertEqual(code,202);ids.append(json.loads(body)['task_id'])
                self.assertEqual(self.raw('/api/tasks',{'image_base64':'eA=='})[0],503)
                self.assertEqual(len(list(self.root.iterdir())),6)
                self.server.max_tasks=6
            finally:
                release.set()
                for task_id in ids:self.wait_done(task_id)
            self.assertEqual(self.raw('/api/tasks',{'image_base64':'eA=='})[0],503)

    def test_offline_success_create_resume_and_poll_artifacts(self):
        import io
        from PIL import Image, ImageDraw
        im=Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(im).line([(10,40),(50,40),(50,10)],fill='#00BFA5',width=5)
        out=io.BytesIO();im.save(out,format='PNG');image=out.getvalue()
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('禁止外部网络')):
            code,_,body=self.raw('/api/tasks',{'mode':'demo','goal':'pixels','image_base64':base64.b64encode(image).decode()})
            self.assertEqual(code,202)
            task_id=json.loads(body)['task_id']
            result=self.wait_done(task_id)
            self.assertEqual(result['execution_state'],'succeeded')
            code,_,_=self.raw('/api/tasks/'+task_id+'/resume',{'answer':'{"color":"#00BFA5","crop":[0,0,80,60]}'})
            self.assertEqual(code,202)
            result=self.wait_done(task_id)
            self.assertEqual(result['execution_state'],'succeeded')
            self.assertTrue(result['centerlined'])
            self.assertFalse(result['verified'])
            self.assertTrue(any(a['name']=='centerline/skeleton.png' for a in result['artifacts']))

    def test_background_timeout_is_polled_not_http_504(self):
        task_id=self.state()
        with patch('ride_web.subprocess.run',side_effect=subprocess.TimeoutExpired('secret',20)):
            code,_,body=self.raw('/api/tasks/'+task_id+'/resume',{'answer':'yes'})
            self.assertEqual(code,202)
            result=self.wait_done(task_id)
        self.assertEqual(result['execution_state'],'failed')
        self.assertEqual(result['web_execution']['error_category'],'timeout')
        self.assertEqual(result['web_execution']['http_error_code'],504)
        self.assertNotIn('secret',json.dumps(result))

    def test_default_budget_keeps_model_timeout(self):
        other=RideServer(0,output_root=self.root/'budget')
        try:self.assertEqual(other.cli_timeout,4500)
        finally:other.server_close()
        self.assertIn('timeout=300',Path('src/ride_agent.py').read_text())

    @unittest.skipUnless(shutil.which('node'),'需要本地 Node')
    def test_js_polling_terminal_error_and_bound(self):
        script=PAGE.split('<script nonce="__TOKEN__">')[1].split('</script>')[0]
        harness="""
const assert=require('assert');let timer,cleared=0,requests=0;const nodes={};
global.document={getElementById:id=>nodes[id]||(nodes[id]={}),querySelectorAll:()=>[]};
global.setTimeout=(fn,ms)=>{timer=fn;return 7;};global.clearTimeout=()=>{cleared++;};
"""+script+"""
(async()=>{
 $('task').value='one';schedulePoll({task_id:'one',execution_state:'running'});
 assert(executionBusy);assert(timer);assert.equal(pollTask,'one');
 request=async(path,data)=>{assert(!data);requests++;throw Error('offline');};
 await timer();assert.equal(requests,1);assert($('error').textContent.includes('手动刷新'));
 schedulePoll({task_id:'one',execution_state:'succeeded'});assert(!executionBusy);assert.equal(pollTask,'');
 schedulePoll({task_id:'one',execution_state:'running'});pollStarted=Date.now()-4700000;
 schedulePoll({task_id:'one',execution_state:'running'});assert($('error').textContent.includes('自动刷新已停止'));
 assert(cleared>0);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        subprocess.run(['node','-e',harness],check=True,capture_output=True)
