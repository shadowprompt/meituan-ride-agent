import json
import os
import unittest
from unittest.mock import patch
from ride_doctor import diagnose


class DoctorTests(unittest.TestCase):
    def test_no_secret_or_url_leak(self):
        with patch.dict(os.environ,{'RIDE_API_KEY':'SUPER-PRIVATE-KEY','RIDE_MODEL':'PRIVATE-MODEL',
                                    'RIDE_API_BASE':'https://secret-provider.example/v1'}):
            report=diagnose()
        self.assertTrue(report['vision_configured'])
        self.assertFalse(report['live_vision_verified'])
        raw=json.dumps(report)
        for value in ('SUPER-PRIVATE-KEY','PRIVATE-MODEL','secret-provider'):
            self.assertNotIn(value,raw)

    def test_missing_configuration_honest(self):
        with patch.dict(os.environ,{},clear=True):
            report=diagnose()
        self.assertFalse(report['vision_configured'])
        self.assertFalse(report['real_screenshot_acceptance'])

    def test_unsafe_provider_rejected(self):
        with patch.dict(os.environ,{'RIDE_API_KEY':'x','RIDE_MODEL':'x','RIDE_API_BASE':'http://remote.example/v1'}):
            report=diagnose()
        self.assertFalse(report['vision_configured'])


if __name__=='__main__':unittest.main()
