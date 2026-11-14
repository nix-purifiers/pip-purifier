# SPDX-License-Identifier: MIT
import os
import sys
import unittest
from unittest.mock import patch

import legacy_build_constraints as constraints


class ChildProtocol(unittest.TestCase):
    def command(self):
        return [sys.executable, '/tools/pip/__pip-runner__.py', 'install',
                '--ignore-installed', '--prefix', '/scratch/overlay', '--target', '',
                '--', 'build-dependency>=1']

    def test_only_isolated_child_receives_both_constraint_scopes(self):
        command = self.command()
        original = command.copy()
        with patch.dict(os.environ, {'PIP_CONSTRAINT': '/user/runtime.txt'}):
            actual = constraints.forward_child(command, ['/build/static.txt', '/build/dynamic.txt'],
                                               '/tools/pip_runner.py', command[1])
            self.assertEqual(os.environ['PIP_CONSTRAINT'], '/user/runtime.txt')
        self.assertEqual(command, original)
        self.assertEqual(actual[:8], [sys.executable, '/tools/pip_runner.py', *original[2:8]])
        self.assertEqual(actual[8:], ['--constraint', '/build/static.txt', '--build-constraint', '/build/static.txt',
                                     '--constraint', '/build/dynamic.txt', '--build-constraint', '/build/dynamic.txt',
                                     '--', 'build-dependency>=1'])

    def test_unknown_or_truncated_protocol_refuses(self):
        original = self.command()
        for command in [original[:7], original[:8], ['unexpected'],
                        [*original[:2], 'wheel', *original[3:]],
                        [original[0], '/unknown-entry.py', *original[2:]]]:
            with self.subTest(command=command), self.assertRaisesRegex(SystemExit, 'unsupported isolated build child'):
                constraints.forward_child(command, ['/build/static.txt'], '/tools/pip_runner.py', original[1])
