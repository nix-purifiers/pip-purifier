# SPDX-License-Identifier: MIT
"""Consumer wheel tags, rather than management SDK tags, choose providers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import python_bindings
import python_repository


class ConsumerBindings(unittest.TestCase):
    def view(self, root, filenames):
        for name in filenames:
            (root / name).write_bytes(b'provider-fixture')
        catalog = root / 'catalog.json'
        catalog.write_text(json.dumps({'version': 1, 'modules': [
            {'name': 'native', 'version': '1', 'wheels': filenames}]}))
        return [{'repository': str(root), 'catalog': str(catalog)}]

    def test_consumer_order_selects_exact_abi_before_stable_abi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exact = 'native-1-cp312-cp312-manylinux_2_17_x86_64.whl'
            stable = 'native-1-cp310-abi3-manylinux_2_17_x86_64.whl'
            views = self.view(root, [stable, exact])
            with patch.object(python_repository, 'sys_tags', side_effect=AssertionError('helper tags used')):
                config = python_bindings.write(views, [], root / 'bindings', compatible_tags=[
                    'cp312-cp312-manylinux_2_17_x86_64', 'cp310-abi3-manylinux_2_17_x86_64'])
            self.assertEqual(json.loads(config.read_text())['modules']['native']['wheel'], str(root / exact))

    def test_stable_abi_remains_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            views = self.view(root, ['native-1-cp310-abi3-manylinux_2_17_x86_64.whl'])
            python_bindings.write(views, [], root / 'bindings', compatible_tags=[
                'cp312-cp312-manylinux_2_17_x86_64', 'cp310-abi3-manylinux_2_17_x86_64'])

    def test_incompatible_compiled_and_free_threaded_abis_are_refused(self):
        for wheel, tag in [
            ('cp312-cp312', 'cp314-cp314'), ('cp314-cp314', 'cp312-cp312'),
            ('cp314-cp314t', 'cp314-cp314'), ('cp314-cp314', 'cp314-cp314t')]:
            with self.subTest(wheel=wheel, target=tag), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                views = self.view(root, ['native-1-' + wheel + '-manylinux_2_17_x86_64.whl'])
                with self.assertRaisesRegex(ValueError, 'no supplied wheel installs'):
                    python_bindings.write(views, [], root / 'bindings', compatible_tags=[tag + '-manylinux_2_17_x86_64'])
                self.assertFalse((root / 'bindings').exists())

    def test_nonempty_provider_requires_context_but_empty_provider_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'require consumer target'):
                python_bindings.write([{}], [], root / 'missing')
            config = python_bindings.write([], [], root / 'empty')
            self.assertEqual(json.loads(config.read_text())['modules'], {})

    def test_empty_malformed_compressed_duplicate_and_conflicting_contexts_fail_closed(self):
        for tags in [[], 'cp312-cp312-any', [None], ['bad'], ['py2.py3-none-any'], ['py3-none-any', 'py3-none-any']]:
            with self.subTest(tags=tags), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises((ValueError, TypeError)):
                    python_bindings.write([{}], [], Path(tmp) / 'bad', compatible_tags=tags)
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            python_bindings.write([], [], Path('must-not-exist'), python='/unused', compatible_tags=['py3-none-any'])

    def test_explicit_interpreter_is_probed_instead_of_using_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            views = self.view(root, ['native-1-cp312-cp312-manylinux_2_17_x86_64.whl'])
            with patch.object(python_repository, 'interpreter_tags', return_value=['cp312-cp312-manylinux_2_17_x86_64']) as probe:
                python_bindings.write(views, [], root / 'bindings', python='/adopted/python')
                probe.assert_called_once_with('/adopted/python')
