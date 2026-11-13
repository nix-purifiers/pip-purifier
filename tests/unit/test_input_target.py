# SPDX-License-Identifier: MIT
"""Local binding inputs carry consumer tags without capturing its executable."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import pip_purifier as pp
import python_repository


class LocalInputTarget(unittest.TestCase):
    def test_local_input_tags_follow_explicit_consumer_without_sdk_dependency(self):
        tags = ['cp312-cp312-manylinux_2_17_x86_64', 'cp310-abi3-manylinux_2_17_x86_64']
        with tempfile.TemporaryDirectory() as tmp:
            lock = str(Path(tmp) / 'lock.json')
            with patch.object(python_repository, 'interpreter_tags', return_value=tags) as probe, \
                 patch.object(pp, 'read_lock'), \
                 patch.object(pp.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='/store/inputs\n')) as build:
                self.assertEqual(pp.inputs_of(tmp, lock, target_python='/caller/python312'), '/store/inputs')
                probe.assert_called_once_with('/caller/python312')
            command = build.call_args.args[0]
            self.assertEqual(command[command.index('pythonTags') - 1], '--argstr')
            self.assertEqual(json.loads(command[command.index('pythonTags') + 1]), tags)
            self.assertNotIn('/caller/python312', command)
            self.assertIn('localInputs', command)
            self.assertFalse(Path(lock).exists())

    def test_no_implicit_consumer_fallback(self):
        with self.assertRaises(TypeError):
            pp.inputs_of('/unused', '/unused/lock')
