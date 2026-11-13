# SPDX-License-Identifier: MIT
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import isolated_native


class IsolatedNative(unittest.TestCase):
    def exercise(self, legacy):
        events = []

        class Modern:
            def install(self, requirements, prefix, *, kind, for_req):
                events.append(('install', requirements, prefix.path, kind, for_req))
                if requirements == ['fail']:
                    raise RuntimeError('native install failed')

        class Legacy:
            @staticmethod
            def _install_requirements(pip_runnable, finder, requirements, prefix, *, kind):
                events.append(('install', requirements, prefix.path, kind, None))
                if requirements == ['fail']:
                    raise RuntimeError('native install failed')

        module = SimpleNamespace(BuildEnvironment=Legacy, get_runnable_pip=lambda: '/original')
        if not legacy:
            module.SubprocessBuildEnvironmentInstaller = Modern
        with patch('pip._internal.build_env', module), patch.object(isolated_native, 'repair',
                                                                  side_effect=lambda p: events.append(('repair', p))):
            isolated_native.activate('/owned/runner.py')
            self.assertEqual(module.get_runnable_pip(), '/owned/runner.py')
            prefix = SimpleNamespace(path='/owned/overlay')
            def install(requirements):
                if legacy:
                    Legacy()._install_requirements('/owned/runner.py', None, requirements, prefix, kind='normal')
                else:
                    Modern().install(requirements, prefix, kind='normal', for_req=None)
            install(['backend'])
            self.assertEqual(events, [('install', ['backend'], prefix.path, 'normal', None), ('repair', prefix.path)])
            with self.assertRaisesRegex(RuntimeError, 'native install failed'):
                install(['fail'])
            self.assertEqual(len(events), 3, 'failed requirements must not be repaired')

    def test_modern_install_timing_and_failure(self):
        self.exercise(False)

    def test_legacy_static_install_timing_and_failure(self):
        self.exercise(True)

    def test_unknown_protocol_rejected_before_install(self):
        module = SimpleNamespace(SubprocessBuildEnvironmentInstaller=SimpleNamespace(install=lambda: None))
        with patch('pip._internal.build_env', module), self.assertRaisesRegex(SystemExit, 'unsupported isolated native'):
            isolated_native.activate('/owned/runner.py')

    def test_repair_uses_declared_management_command(self):
        with patch.dict(os.environ, {isolated_native.STATE: json.dumps(['/management/python', '-I', '/repair', '--repair', '/inputs'])}), \
                patch.object(isolated_native.subprocess, 'run') as run:
            isolated_native.repair('/owned/prefix')
            run.assert_called_once_with(['/management/python', '-I', '/repair', '--repair', '/inputs', '/owned/prefix'], check=True)
