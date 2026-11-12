# SPDX-License-Identifier: MIT
"""Give installed Linux wheel libraries their declared Nix runtime paths.

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


def repair(inputs, venv):
    configuration = Path(inputs) / "native-runtime.json"
    if not configuration.exists():
        return
    settings = json.loads(configuration.read_text())
    library_path = settings["libraryPath"]
    if not library_path:
        return
    changed = set()
    for directory, _, filenames in os.walk(venv):
        for filename in filenames:
            path = Path(directory) / filename
            if path.is_symlink() or ".so" not in filename:
                continue
            with path.open("rb") as source:
                header = source.read(18)
            if header[:4] != b"\x7fELF" or len(header) < 18:
                continue
            byteorder = "little" if header[5] == 1 else "big"
            if int.from_bytes(header[16:18], byteorder) != 3:
                continue
            old = subprocess.check_output([settings["patchelf"], "--print-rpath", str(path)],
                                          text=True).strip()
            missing = [p for p in library_path.split(":") if p not in old.split(":")]
            if not missing:
                continue
            subprocess.run([settings["patchelf"], "--add-rpath", ":".join(missing), str(path)],
                           check=True)
            changed.add(path.resolve())
    if not changed:
        return
    for record in Path(venv).glob("lib/python*/site-packages/*.dist-info/RECORD"):
        with record.open(newline="") as source:
            rows = list(csv.reader(source))
        updated = False
        for row in rows:
            path = (record.parent.parent / row[0]).resolve()
            if path in changed:
                content = path.read_bytes()
                digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                row[1:] = ["sha256=" + digest, str(len(content))]
                updated = True
        if updated:
            with record.open("w", newline="") as destination:
                csv.writer(destination).writerows(rows)
