"""Guidance regression tests: loopback HTTP only; CLI and external calls mocked."""
import json
from html.parser import HTMLParser
import subprocess
import unittest
from unittest.mock import patch

import test_ride_web as fixtures
from ride_web import GUIDANCE, PAGE


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.inputs = {}
        self.fieldsets = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'fieldset':
            self.depth += 1
        if 'id' in attrs:
            self.ids.append(attrs['id'])
            self.fieldsets.append((attrs['id'], self.depth))
        if tag == 'input':
            self.inputs[attrs.get('id')] = attrs

    def handle_endtag(self, tag):
        if tag == 'fieldset':
            self.depth -= 1


class GuidanceTests(unittest.TestCase):
    setUp = fixtures.WebTests.setUp
    tearDown = fixtures.WebTests.tearDown
    request = fixtures.WebTests.request
    state = fixtures.WebTests.state
    mock_cli = fixtures.WebTests.mock_cli

    def test_consent_unique_visible_before_create_and_still_used(self):
        page = self.request()[2].decode()
        parser = PageParser()
        parser.feed(page)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertLess(parser.ids.index('uploadConsent'), parser.ids.index('create'))
        self.assertEqual(dict(parser.fieldsets)['uploadConsent'], 1)
        self.assertEqual(parser.inputs['uploadConsent']['type'], 'checkbox')
        self.assertNotIn('checked', parser.inputs['uploadConsent'])
        self.assertIn('class="consent"', page)
        self.assertIn('每次继续前', page)
        self.assertEqual(page.count("consent_upload:$('uploadConsent').checked"), 2)
        self.assertIn("$('uploadConsent').checked=false", page)

    def test_roads_switch_disclosed_before_download_and_continue(self):
        notice = PAGE[PAGE.index('id="roadsNotice"'):PAGE.index('<label><input id="fetch"')]
        self.assertIn('自动切换为 roads', notice)
        self.assertIn('上传本地 OSM', notice)
        self.assertIn('独立检查', notice)
        self.assertLess(PAGE.index('id="roadsNotice"'), PAGE.index('id="resume"'))
        self.assertIn('d.recovery_hint', PAGE)
        self.assertIn('d.error_category', PAGE)
        self.assertNotIn('.innerHTML', PAGE)

    def test_status_fixed_categories_never_raw_exception(self):
        stages = [({}, 'extraction_required'),
                  ({'extracted': True}, 'centerline_required'),
                  ({'extracted': True, 'centerlined': True}, 'registration_required'),
                  ({'extracted': True, 'centerlined': True, 'registered': True}, 'registration_gate_failed'),
                  ({'extracted': True, 'centerlined': True, 'registered': True,
                    'registration_gate_passed': True}, 'road_processing_failed')]
        for flags, category in stages:
            with self.subTest(category=category):
                task = self.state(goal='roads', status='error',
                                  error='remote body SECRET /private/path <script>', **flags)
                code, _, body = self.request('/api/tasks/'+task)
                data = json.loads(body)
                self.assertEqual(code, 200)
                self.assertEqual(data['error_category'], category)
                self.assertEqual(data['recovery_hint'], GUIDANCE[category])
                for secret in (b'SECRET', b'/private/path', b'<script>'):
                    self.assertNotIn(secret, body)

    def test_cli_prerequisites_and_stdout_stderr_not_disclosed(self):
        cases = [({}, {'controls_json': '{}'}, 'extraction_required'),
                 ({'extracted': True, 'centerlined': True},
                  {'fetch_map': True, 'consent_map': True}, 'registration_required'),
                 ({'extracted': True, 'centerlined': True, 'registered': True},
                  {'fetch_map': True, 'consent_map': True}, 'registration_gate_failed')]
        result = subprocess.CompletedProcess([], 2, b'SECRET remote response', b'/private/path KEY')
        for flags, payload, category in cases:
            with self.subTest(category=category):
                task = self.state(**flags)
                with patch('ride_web.subprocess.run', return_value=result):
                    code, _, body = self.request('/api/tasks/'+task+'/resume', payload)
                data = json.loads(body)
                self.assertEqual(code, 422)
                self.assertEqual(data['error_category'], category)
                self.assertEqual(data['recovery_hint'], GUIDANCE[category])
                for secret in (b'SECRET', b'/private/path', b'KEY', b'remote response'):
                    self.assertNotIn(secret, body)

    def test_unknown_cli_error_is_generic_not_remote_text(self):
        task = self.state()
        result = subprocess.CompletedProcess([], 1, b'API secret', b'remote response')
        with patch('ride_web.subprocess.run', return_value=result):
            code, _, body = self.request('/api/tasks/'+task+'/resume', {'answer': 'yes'})
        self.assertEqual(code, 422)
        self.assertEqual(json.loads(body)['error_category'], 'cli_failed')
        self.assertNotIn(b'secret', body)
        self.assertNotIn(b'remote response', body)

    def test_timeout_and_local_exception_fixed_recovery(self):
        task = self.state()
        with patch('ride_web.subprocess.run', side_effect=subprocess.TimeoutExpired('SECRET', 1)):
            code, _, body = self.request('/api/tasks/'+task+'/resume', {'answer': 'yes'})
        self.assertEqual(code, 504)
        self.assertEqual(json.loads(body)['error_category'], 'timeout')
        self.assertNotIn(b'SECRET', body)
        with patch.object(self.server, 'status', side_effect=ValueError('SECRET remote response')):
            code, _, body = self.request('/api/tasks/'+task)
        self.assertEqual(code, 500)
        self.assertEqual(json.loads(body)['error_category'], 'local_state_failed')
        self.assertNotIn(b'SECRET', body)

    def test_consent_still_enforced_each_resume(self):
        task = self.state(mode='api')
        with patch('ride_web.subprocess.run', side_effect=self.mock_cli) as run:
            code, _, body = self.request('/api/tasks/'+task+'/resume', {'answer': 'yes'})
            self.assertEqual(code, 403)
            self.assertEqual(json.loads(body)['error_category'], 'request_rejected')
            run.assert_not_called()
            self.assertEqual(self.request('/api/tasks/'+task+'/resume',
                                         {'answer': 'yes', 'consent_upload': True})[0], 200)
            self.assertIn('--consent-upload', run.call_args.args[0])
            self.assertEqual(self.request('/api/tasks/'+task+'/resume', {'answer': 'yes'})[0], 403)
            self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
