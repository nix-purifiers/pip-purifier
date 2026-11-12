# SPDX-License-Identifier: MIT
"""Give installed Linux wheel libraries and executables their Nix runtime paths.

Change installed copies only, preserving vendor $ORIGIN paths and refreshing
wheel RECORD hashes. Locked wheel archives and their integrity pins stay intact.
"""

import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess


def elf_properties(path):
    """Read the architecture and dynamic program headers without loading code."""
    with path.open("rb") as source:
        header = source.read(64)
        if header[:4] != b"\x7fELF" or len(header) < 52 or header[4] not in (1, 2):
            return None
        if header[5] not in (1, 2) or (header[4] == 2 and len(header) < 64):
            return None
        byteorder = "little" if header[5] == 1 else "big"

        def number(offset, length):
            return int.from_bytes(header[offset:offset + length], byteorder)

        if number(16, 2) not in (2, 3):
            return None
        if header[4] == 2:
            offset, size, count = number(32, 8), number(54, 2), number(56, 2)
        else:
            offset, size, count = number(28, 4), number(42, 2), number(44, 2)
        types = set()
        dynamic = None
        if size >= 4:
            for index in range(count):
                source.seek(offset + index * size)
                program = source.read(size)
                if len(program) != size:
                    return None
                kind = int.from_bytes(program[:4], byteorder)
                types.add(kind)
                if kind == 2:
                    if header[4] == 2:
                        dynamic = (int.from_bytes(program[8:16], byteorder),
                                   int.from_bytes(program[32:40], byteorder), 8)
                    else:
                        dynamic = (int.from_bytes(program[4:8], byteorder),
                                   int.from_bytes(program[16:20], byteorder), 4)
        flags = 0
        if dynamic:
            start, length, word = dynamic
            source.seek(start)
            for _ in range(length // (2 * word)):
                entry = source.read(2 * word)
                if len(entry) != 2 * word:
                    return None
                tag = int.from_bytes(entry[:word], byteorder)
                if tag == 0:
                    break
                if tag == 0x6FFFFFFB:  # DT_FLAGS_1
                    flags = int.from_bytes(entry[word:], byteorder)
        return {"machine": number(18, 2), "dynamic": 2 in types,
                "interpreter": 3 in types,
                "staticPie": bool(flags & 0x08000000) and 3 not in types}


def refresh_records(venv, changed=None):
    """Refresh owned installed-file hashes, also after Nix's final fixups."""
    root = Path(venv).resolve()
    for record in root.glob("lib/python*/site-packages/*.dist-info/RECORD"):
        with record.open(newline="") as source:
            rows = list(csv.reader(source))
        updated = False
        for row in rows:
            path = (record.parent.parent / row[0]).resolve()
            if (not path.is_relative_to(root) or path == record.resolve() or
                    not path.is_file() or (changed is not None and path not in changed)):
                continue
            if changed is None and not row[1]:
                continue
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").digest()
            encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            fields = ["sha256=" + encoded, str(path.stat().st_size)]
            if row[1:] != fields:
                row[1:] = fields
                updated = True
        if updated:
            with record.open("w", newline="") as destination:
                csv.writer(destination).writerows(rows)


def repair(inputs, venv):
    configuration = Path(inputs) / "native-runtime.json"
    if not configuration.exists():
        return
    settings = json.loads(configuration.read_text())
    library_path = settings["libraryPath"]
    linker = settings.get("dynamicLinker")
    if not library_path and not linker:
        return
    machine = elf_properties(Path(linker))["machine"] if linker else None
    changed = set()
    for directory, _, filenames in os.walk(venv):
        for filename in filenames:
            path = Path(directory) / filename
            if path.is_symlink():
                continue
            elf = elf_properties(path)
            if (not elf or not elf["dynamic"] or elf["staticPie"] or
                    (machine is not None and elf["machine"] != machine)):
                continue
            options = []
            if linker and elf["interpreter"]:
                old_linker = subprocess.check_output(
                    [settings["patchelf"], "--print-interpreter", str(path)], text=True).strip()
                if old_linker != linker:
                    options += ["--set-interpreter", linker]
            old = subprocess.check_output([settings["patchelf"], "--print-rpath", str(path)],
                                          text=True).strip()
            missing = [p for p in library_path.split(":") if p and p not in old.split(":")]
            if missing:
                options += ["--add-rpath", ":".join(missing)]
            if not options:
                continue
            subprocess.run([settings["patchelf"], *options, str(path)], check=True)
            changed.add(path.resolve())
    refresh_records(venv, changed)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Refresh installed RECORD hashes after Nix fixups")
    parser.add_argument("venv")
    refresh_records(parser.parse_args().venv)
