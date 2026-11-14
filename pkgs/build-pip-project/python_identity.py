# SPDX-License-Identifier: MIT
"""Validate the actual target runtime, independently of SDK metadata."""
import json
from pathlib import Path
import subprocess

PROBE = """
import json, sys, sysconfig
version = '%d.%d' % sys.version_info[:2]
abi = 'cp%d%d' % sys.version_info[:2]
if sysconfig.get_config_var('Py_GIL_DISABLED'):
    abi += 't'
print(json.dumps([version, abi, sys.implementation.name]))
"""


def validate(python, version, abi=None):
    expected = abi or "cp" + version.replace(".", "")
    actual_version, actual_abi, implementation = json.loads(
        subprocess.check_output([str(python), "-I", "-c", PROBE], text=True))
    if implementation != "cpython" or (actual_version, actual_abi) != (version, expected):
        raise SystemExit(
            f"pip-purifier: target Python version/ABI is {actual_version}/{actual_abi} ({implementation}), "
            f"but the lock requires CPython {version}/{expected}. Choose a matching runtime "
            "or run `pip-purifier lock --python` with this one.")


def validate_inputs(inputs, python):
    path = Path(inputs) / "python-identity.json"
    if path.exists():
        expected = json.loads(path.read_text())
        validate(python, expected["python"], expected.get("pythonAbi"))


if __name__ == "__main__":
    import sys
    validate(*sys.argv[1:])
