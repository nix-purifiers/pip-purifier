# SPDX-License-Identifier: MIT
"""Use native pip as the oracle for canonical source paths and extras."""
import contextlib
import os
from pathlib import Path
import tempfile
import unittest

from pip._internal.network.session import PipSession
from pip._internal.req.constructors import install_req_from_line, install_req_from_editable, install_req_from_parsed_requirement
from pip._internal.req.req_file import parse_requirements
import pip_inputs


class LocalExtras(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'project'
        for name in ['.', 'pkg', 'space pkg', 'pkg[]', 'pkg[part]', 'pkg[part]/nested']:
            path = self.project / name
            path.mkdir(parents=True, exist_ok=True)
            (path / 'pyproject.toml').write_text('[project]\nname="probe"\nversion="1"\n')
        self.counter = 0

    @contextlib.contextmanager
    def cwd(self):
        old = os.getcwd()
        os.chdir(self.project)
        try:
            yield
        finally:
            os.chdir(old)

    def rewriter(self, mode='offline', editable=False):
        self.counter += 1
        return pip_inputs.Rewriter(str(self.project), str(Path(self.tmp.name) / f'out-{self.counter}'), mode,
                                   editable=editable)

    def native(self, spec, editable):
        req = (install_req_from_editable if editable else install_req_from_line)(spec)
        return (req.link.file_path if req.link and req.link.is_file else None,
                sorted(req.extras), str(req.markers) if req.markers is not None else None)

    def test_native_local_paths_extras_and_markers_in_both_modes(self):
        specs = ['.[alpha,beta]', './pkg[alpha]', str(self.project / 'pkg') + '[alpha,beta]',
                 './space pkg[alpha,beta]', './pkg [alpha]', 'pkg[part]/nested[alpha]',
                 './pkg[]', './pkg[part][alpha]', './pkg[part]', 'pkg[alpha]', 'space pkg[alpha]',
                 './pkg[alpha] ; python_version >= "3.10"']
        with self.cwd():
            for spec in specs:
                for is_editable in [False, True]:
                    try:
                        expected = self.native(spec, is_editable)
                    except Exception:
                        continue  # Native rejection is tested separately below.
                    for mode, keep_editable in [('lock', True), ('offline', True), ('offline', False)]:
                        with self.subTest(spec=spec, editable=is_editable, mode=mode, keep=keep_editable):
                            rw = self.rewriter(mode, keep_editable)
                            rewritten, flag = rw.spec(spec, editable=is_editable)
                            self.assertEqual(self.native(rewritten, flag), expected)
                            if expected[0]:
                                rel = os.path.relpath(expected[0], self.project)
                                self.assertEqual(rw.dirs, [rel])
                                self.assertEqual(rw.inputs, [os.path.join(rel, 'pyproject.toml') if rel != '.' else 'pyproject.toml'])
                            else:
                                self.assertEqual(rw.dirs, [])

    def test_native_rejected_local_editable_markers_stay_rejected_before_tracking(self):
        spec = './pkg[alpha] ; python_version >= "3.10"'
        with self.cwd():
            with self.assertRaises(Exception):
                self.native(spec, True)
            for mode, keep in [('lock', True), ('offline', True), ('offline', False)]:
                rw = self.rewriter(mode, keep)
                with self.assertRaisesRegex(ValueError, 'local editable.*markers'):
                    rw.spec(spec, editable=True)
                self.assertEqual((rw.dirs, rw.inputs), ([], []))

    def test_original_extras_spacing_and_marker_suffix_is_forwarded(self):
        rw = self.rewriter()
        suffix = ' [Alpha,beta]  ; python_version >= "3.10"'
        rewritten, _ = rw.spec('./pkg' + suffix)
        self.assertEqual(rewritten, str(self.project / 'pkg') + suffix)
        self.assertEqual(rw.dirs, ['pkg'])

    def test_native_bracket_interpretation_does_not_prefer_literal_existing_directory(self):
        with self.cwd():
            rw = self.rewriter()
            rewritten, flag = rw.spec('./pkg[part]')
            self.assertEqual(self.native(rewritten, flag)[0], str(self.project / 'pkg'))
            self.assertEqual(rw.dirs, ['pkg'])
            rw = self.rewriter()
            rw.spec('./pkg[]')
            self.assertEqual(rw.dirs, ['pkg[]'])

    def test_named_vcs_url_markers_remain_outside_local_path_rules(self):
        spec = 'demo[alpha] @ git+https://example.org/repo.git@abcdef ; python_version >= "3.10"'
        with self.cwd():
            for editable in [False, True]:
                rw = self.rewriter('lock', True)
                rewritten, flag = rw.spec(spec, editable=editable)
                self.assertEqual(rewritten, spec)
                self.assertEqual(self.native(rewritten, flag), self.native(spec, editable))
                self.assertEqual((rw.dirs, rw.inputs), ([], []))

    def file_model(self, path):
        result = []
        for parsed in parse_requirements(str(path), session=PipSession()):
            req = install_req_from_parsed_requirement(parsed, isolated=False)
            result.append((req.link.file_path if req.link and req.link.is_file else None,
                           sorted(req.extras), str(req.markers) if req.markers is not None else None))
        return result

    def test_actual_native_requirements_files_spaces_extras_and_markers(self):
        lines = ['-e "./space pkg[alpha,beta]"', '-e./pkg[alpha]', '--editable=./pkg[alpha]',
                 './space pkg[alpha]', './pkg[alpha] ; python_version >= "3.10"', './pkg[]']
        with self.cwd():
            for i, line in enumerate(lines):
                path = self.project / f'requirements-{i}.txt'
                path.write_text(line + '\n')
                expected = self.file_model(path)
                for mode, keep in [('lock', True), ('offline', True), ('offline', False)]:
                    with self.subTest(line=line, mode=mode, keep=keep):
                        rw = self.rewriter(mode, keep)
                        self.assertEqual(self.file_model(rw.requirements_file(str(path))), expected)

    def test_quoted_noneditable_file_path_keeps_native_rejection(self):
        path = self.project / 'quoted.txt'
        path.write_text('"./space pkg[alpha]"\n')
        with self.cwd():
            with self.assertRaises(Exception):
                self.file_model(path)
            rewritten = self.rewriter().requirements_file(str(path))
            with self.assertRaises(Exception):
                self.file_model(rewritten)

    def parsed_file(self, path):
        return list(parse_requirements(str(path), session=PipSession()))

    def test_editable_file_ignored_marker_tail_is_preserved_locally_and_refused_purely(self):
        for option in ['-e ', '-e', '--editable=']:
            line = option + '"./pkg[alpha]"  ; python_version < "0"'
            value, tail = pip_inputs.editable_file_value(line)
            self.assertEqual(value, './pkg[alpha]')
            self.assertEqual(tail, '  ; python_version < "0"')
            path = self.project / 'ignored-tail.txt'
            path.write_text(line + '\n')
            with self.cwd():
                expected = self.file_model(path)
                self.assertIsNone(expected[0][2])  # Native pip ignores this marker.
                for mode in ['lock', 'offline']:
                    rw = self.rewriter(mode, True)
                    rewritten = rw.requirements_line(line, str(self.project))
                    self.assertTrue(rewritten.endswith(tail))
                    output = self.project / 'rewritten-tail.txt'
                    output.write_text(rewritten + '\n')
                    self.assertEqual(self.file_model(output), expected)
                rw = self.rewriter('offline', False)
                with self.assertRaisesRegex(ValueError, 'pure replay.*positional'):
                    rw.requirements_line(line, str(self.project))
                self.assertEqual((rw.dirs, rw.inputs), ([], []))

    def test_editable_file_ignored_hash_is_not_activated_by_pure_conversion(self):
        for suffix in [' --hash=sha256:' + '0' * 64, ' --hash sha256:' + '0' * 64]:
            line = '-e "./pkg[alpha]"' + suffix
            path = self.project / 'hash-tail.txt'
            path.write_text(line + '\n')
            with self.cwd():
                native = self.parsed_file(path)[0]
                self.assertNotIn('hashes', native.options)
                regular = self.project / 'regular-hash.txt'
                regular.write_text('./pkg[alpha]' + suffix + '\n')
                self.assertIn('hashes', self.parsed_file(regular)[0].options)
                for mode in ['lock', 'offline']:
                    rw = self.rewriter(mode, True)
                    result = rw.requirements_line(line, str(self.project))
                    self.assertTrue(result.endswith(suffix))
                rw = self.rewriter('offline', False)
                with self.assertRaisesRegex(ValueError, 'pure replay.*ignored.*hash'):
                    rw.requirements_line(line, str(self.project))
                self.assertEqual((rw.dirs, rw.inputs), ([], []))

    def test_normal_config_options_and_hash_named_values_retain_native_meaning(self):
        for tail in [' --config-settings="mode=compat"', ' -C "mode=compat"',
                     ' --config-settings "--hash"', ' --config-settings="key=--hash"',
                     ' -C--hash', ' --config-settings=mode=compat ; python_version < "0"']:
            line = '-e "./pkg[alpha]"' + tail
            path = self.project / 'config-tail.txt'
            path.write_text(line + '\n')
            with self.cwd():
                expected = self.parsed_file(path)[0].options
                for mode, keep in [('lock', True), ('offline', True), ('offline', False)]:
                    with self.subTest(tail=tail, mode=mode, keep=keep):
                        rw = self.rewriter(mode, keep)
                        result = rw.requirements_line(line, str(self.project))
                        self.assertTrue(result.endswith(tail))
                        output = self.project / 'config-rewritten.txt'
                        output.write_text(result + '\n')
                        self.assertEqual(self.parsed_file(output)[0].options, expected)

    def test_unknown_file_option_reaches_native_pip_unchanged(self):
        tail = ' --not-a-real-pip-option="unchanged value"'
        line = '-e "./pkg[alpha]"' + tail
        with self.cwd():
            for mode, keep in [('lock', True), ('offline', True), ('offline', False)]:
                rw = self.rewriter(mode, keep)
                result = rw.requirements_line(line, str(self.project))
                self.assertTrue(result.endswith(tail))
                path = self.project / 'unknown.txt'
                path.write_text(result + '\n')
                with self.assertRaises(Exception):
                    self.parsed_file(path)

    def test_quote_aware_editable_span_preserves_escaped_value_and_raw_tail(self):
        for line in ['-e "./space pkg[alpha]"  --config-settings=mode=compat',
                     '-e./space\\ pkg[alpha]  --config-settings=mode=compat',
                     '--editable="./space pkg[alpha]"  --config-settings=mode=compat']:
            value, tail = pip_inputs.editable_file_value(line)
            self.assertEqual(value, './space pkg[alpha]')
            self.assertEqual(tail, '  --config-settings=mode=compat')
