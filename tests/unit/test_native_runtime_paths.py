# SPDX-License-Identifier: MIT
"""Path-tag presence controls loader semantics even when its value is empty."""
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import native_runtime as nr


class NativePathTags(unittest.TestCase):
    def elf(self, directory, tags, *, machine=62, static=False):
        # Minimal ELF64 dynamic program header; no code is executed.
        header = bytearray(64)
        header[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<HH', header, 16, 3, machine)
        struct.pack_into('<Q', header, 32, 64)
        struct.pack_into('<HH', header, 54, 56, 1)
        entries = [(tag, 0) for tag in tags]
        if static:
            entries.append((0x6ffffffb, 0x08000000))
        entries.append((0, 0))
        dynamic = b''.join(struct.pack('<QQ', *item) for item in entries)
        program = bytearray(56)
        struct.pack_into('<I', program, 0, 2)
        struct.pack_into('<Q', program, 8, 120)
        struct.pack_into('<Q', program, 32, len(dynamic))
        path = Path(directory) / f'elf-{len(list(Path(directory).iterdir()))}'
        path.write_bytes(header + program + dynamic)
        return path

    def test_presence_and_runpath_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            for tags, expected in [((), None), ((15,), 'rpath'),
                                   ((29,), 'runpath'), ((15, 29), 'runpath')]:
                with self.subTest(tags=tags):
                    self.assertEqual(nr.elf_properties(self.elf(directory, tags))['pathKind'], expected)

    def test_proxy_preserves_only_matching_dynamic_legacy_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            linker = self.elf(directory, ())
            cases = [(self.elf(directory, (15,)), True),
                     (self.elf(directory, (29,)), False),
                     (self.elf(directory, (15, 29)), False),
                     (self.elf(directory, ()), False),
                     (self.elf(directory, (15,), machine=183), False),
                     (self.elf(directory, (15,), static=True), False),
                     (Path(directory) / 'missing', False)]
            text = Path(directory) / 'text'
            text.write_text('not ELF')
            cases.append((text, False))
            for path, force in cases:
                with self.subTest(path=path), patch.object(nr.os, 'execv') as execute:
                    args = ['--shrink-rpath', str(path)]
                    nr.exec_patchelf('/pinned/patchelf', str(linker), args)
                    execute.assert_called_once_with('/pinned/patchelf',
                        ['/pinned/patchelf', '--no-sort', *(['--force-rpath'] if force else []), *args])
            with patch.object(nr.os, 'execv') as execute:
                nr.exec_patchelf('/pinned/patchelf', str(linker), ['--print-rpath', str(cases[0][0])])
                self.assertNotIn('--force-rpath', execute.call_args.args[1])
