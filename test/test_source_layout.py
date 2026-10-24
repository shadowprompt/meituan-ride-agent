"""源码目录迁移与默认超时回归；只做本地检查，不调用远端。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ride_web
from ride_doctor import diagnose

ROOT = Path(__file__).resolve().parent.parent


class SourceLayoutTests(unittest.TestCase):
    def test_sources_separated_and_doctor_ready(self):
        self.assertEqual(Path(ride_web.__file__).resolve().parent, ROOT/'src')
        self.assertFalse((ROOT/'ride_agent.py').exists())
        self.assertFalse(list(ROOT.glob('test_*.py')))
        self.assertEqual(Path(__file__).resolve().parent, ROOT/'test')
        self.assertTrue(diagnose()['local_ready'])

    def test_default_task_location_and_five_minute_timeout(self):
        # 不在真实任务目录写数据；检查默认路径并拦截子进程。
        with patch('ride_web.safe_path'), patch.object(Path, 'mkdir'):
            server = ride_web.RideServer(0)
        try:
            self.assertEqual(server.root, ROOT/'outputs'/'web')
            self.assertEqual(server.cli_timeout, 300)
            with patch('ride_web.subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
                server.run_cli(['status', '--task-dir', str(ROOT/'outputs'/'example')])
            self.assertEqual(Path(run.call_args.args[0][1]), ROOT/'src'/'ride_agent.py')
            self.assertEqual(Path(run.call_args.kwargs['cwd']), ROOT)
            self.assertEqual(run.call_args.kwargs['timeout'], 300)
        finally:
            server.server_close()

    def test_cli_entrypoints_from_other_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('ride_agent', 'ride_web'):
                result = subprocess.run([sys.executable, str(ROOT/'src'/f'{name}.py'), '--help'],
                                        cwd=tmp, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, str(ROOT/'src'/'ride_doctor.py')],
                                    cwd=tmp, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['local_ready'])


if __name__ == '__main__':
    unittest.main()
