# SPDX-License-Identifier: MIT
"""Local backends may leave sidecars; only their actual artifacts are outputs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pip_purifier as pp


class LocalDistributions(unittest.TestCase):
    def test_nested_and_flattened_paths_keep_their_own_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = {str(root / path): name for path, name in
                        [('project', 'rootpkg'), ('project/vectors', 'vectors'),
                         ('project/a/b', 'nested'), ('project/a_b', 'flat')]}
            def wheel(args, env, cwd):
                destination = Path(args[args.index('--wheel-dir') + 1])
                name = projects[args[-1]]
                (destination / f'{name}-1.0-py3-none-any.whl').touch()
                (destination / 'cache.whl').mkdir()
            def sdist(directory, destination, *args):
                destination = Path(destination)
                (destination / 'sidecar-directory').mkdir()
                (destination / 'notes.txt').write_text('backend notes')
                # The sdist hook's unrelated wheel must not replace pip's wheel.
                (destination / 'unrelated-0.0-py3-none-any.whl').touch()
                artifact = destination / f'{projects[directory]}-1.0.tar.gz'
                artifact.touch()
                return str(artifact)
            with patch.object(pp, 'run_pip', side_effect=wheel), \
                 patch.object(pp, 'source_sdist', side_effect=sdist):
                for directory, name in projects.items():
                    self.assertEqual(pp.source_distributions(
                        directory, temporary, '/caller/python', {}, str(root), [], [], {}, True),
                        (f'{name}-1.0-py3-none-any.whl', f'{name}-1.0.tar.gz'))

    def test_returned_sdist_must_be_an_owned_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'dist'
            destination.mkdir()
            outside = root / 'outside.tar.gz'
            outside.touch()
            escaped = destination / 'escape.tar.gz'
            escaped.symlink_to(outside)
            directory = destination / 'directory.tar.gz'
            directory.mkdir()
            for artifact in (outside, escaped, directory, destination / 'missing.tar.gz'):
                with self.subTest(artifact=artifact), self.assertRaises(SystemExit):
                    pp.distribution_filename(destination, artifact, 'sdist')
            actual = destination / 'actual.tar.gz'
            actual.touch()
            self.assertEqual(pp.distribution_filename(destination, actual, 'sdist'), 'actual.tar.gz')

    def test_sdist_hook_cannot_replace_wheel_with_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / 'outside.whl'
            outside.touch()
            def wheel(args, env, cwd):
                destination = Path(args[args.index('--wheel-dir') + 1])
                (destination / 'owned-1.0-py3-none-any.whl').touch()
            def sdist(directory, destination, *args):
                destination = Path(destination)
                path = destination / 'owned-1.0-py3-none-any.whl'
                path.unlink()
                path.symlink_to(outside)
                result = destination / 'owned-1.0.tar.gz'
                result.touch()
                return result
            with patch.object(pp, 'run_pip', side_effect=wheel), \
                 patch.object(pp, 'source_sdist', side_effect=sdist), self.assertRaises(SystemExit):
                pp.source_distributions('.', temporary, '/caller/python', {}, '.', [], [], {}, True)

    def test_missing_or_ambiguous_wheel_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            for count in (0, 2):
                def wheel(args, env, cwd):
                    destination = Path(args[args.index('--wheel-dir') + 1])
                    for i in range(count):
                        (destination / f'package{i}-1.0-py3-none-any.whl').touch()
                with self.subTest(count=count), patch.object(pp, 'run_pip', side_effect=wheel), \
                     self.assertRaises(SystemExit):
                    pp.source_distributions('.', temporary, '/caller/python', {}, '.', [], [], {}, True)
