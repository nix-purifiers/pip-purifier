# SPDX-License-Identifier: MIT
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import installer_driver as driver


class Selection(unittest.TestCase):
    def candidates(self):
        return [dict(path='/current', version='26.1.2', requiresPython='>=3.10', sha256='current'),
                dict(path='/compatible', version='25.0.1', requiresPython='>=3.8', sha256='compatible')]

    def test_metadata_selects_separate_tool_and_checks_native_delegation(self):
        probe = SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(
            {'pip': '25.0.1', 'capabilities': ['report', 'python', 'wheel-capture']}))
        delegated = SimpleNamespace(returncode=0, stderr='', stdout='pip 25.0.1 from /compatible (python 3.8)\n')
        with patch.object(driver, 'candidates', self.candidates), patch.object(
                driver.subprocess, 'check_output', return_value='3.8.9\n'), patch.object(
                driver.subprocess, 'run', side_effect=[probe, delegated]) as run, patch.dict(
                os.environ, {'PIP_PURIFIER_NATIVE_REPAIR': 'must-not-reach-local-tools'}):
            selected = driver.select('/existing/python', required=['report'])
            self.assertEqual(selected['identity']['version'], '25.0.1')
            self.assertEqual(selected['command'][0], '/existing/python')
            self.assertEqual(selected['environment'], {'PIP_PURIFIER_PIP_PATH': '/compatible', 'PIP_PURIFIER_DRIVER_SHA256': 'compatible',
                                                       'PIP_PURIFIER_NATIVE_REPAIR': ''})
            self.assertTrue(all(call.kwargs['env']['PIP_PURIFIER_NATIVE_REPAIR'] == '' for call in run.call_args_list))
            self.assertIn('--python', run.call_args_list[1].args[0])

    def test_missing_capability_refuses_before_delegation(self):
        probe = SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(
            {'pip': '25.0.1', 'capabilities': ['report', 'python']}))
        with patch.object(driver, 'candidates', self.candidates), patch.object(
                driver.subprocess, 'check_output', return_value='3.8.9\n'), patch.object(
                driver.subprocess, 'run', return_value=probe) as run:
            with self.assertRaisesRegex(SystemExit, 'missing build-constraint'):
                driver.select('/existing/python', required=['build-constraint'])
            self.assertEqual(run.call_count, 1)

    def test_locked_version_and_hash_cannot_silently_change(self):
        with patch.object(driver, 'candidates', self.candidates), patch.object(
                driver.subprocess, 'check_output', return_value='3.8.9\n'), patch.object(
                driver.subprocess, 'run') as run:
            for expected, previous in [('26.1.2', None), ('25.0.1', dict(version='25.0.1', sha256='changed'))]:
                with self.assertRaisesRegex(SystemExit, 'no compatible installer'):
                    driver.select('/existing/python', expected=expected, previous=previous)
            run.assert_not_called()


class BindingPreflight(unittest.TestCase):
    def test_missing_binding_hook_refuses_before_native_delegation(self):
        candidate = dict(path='/compatible', version='25.0.1', requiresPython='>=3.8', sha256='compatible')
        probe = SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({
            'pip': '25.0.1', 'capabilities': ['report', 'python', 'wheel-capture'],
            'bindingError': 'producer binding protocol is unavailable: missing hook'}))
        with patch.object(driver, 'candidates', return_value=[candidate]), patch.object(
                driver.subprocess, 'check_output', return_value='3.8.9\n'), patch.object(
                driver.subprocess, 'run', return_value=probe) as run:
            with self.assertRaisesRegex(SystemExit, 'missing producer-bindings:.*missing hook'):
                driver.select('/existing/python', required=['producer-bindings'])
            self.assertEqual(run.call_count, 1)
