import sys
import sysconfig
import unittest
import tempfile
import pathlib
import json
import os
import subprocess

import python_identity
import offline


class RuntimeIdentity(unittest.TestCase):
    def test_actual_runtime_and_same_version_wrong_abi(self):
        version = "%d.%d" % sys.version_info[:2]
        base = "cp%d%d" % sys.version_info[:2]
        free = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
        python_identity.validate(sys.executable, version, base + ("t" if free else ""))
        with self.assertRaises(SystemExit) as error:
            python_identity.validate(sys.executable, version, base + ("" if free else "t"))
        self.assertIn("version/ABI", str(error.exception))
        self.assertIn("pip-purifier lock --python", str(error.exception))

    def test_metadata_cannot_assert_another_runtime_version(self):
        with self.assertRaises(SystemExit):
            python_identity.validate(sys.executable, "99.0", "cp990")

    def test_false_sdk_identity_is_rejected_before_reading_or_installing_dependencies(self):
        version = "%d.%d" % sys.version_info[:2]
        base = "cp%d%d" % sys.version_info[:2]
        wrong = base + ("" if sysconfig.get_config_var("Py_GIL_DISABLED") else "t")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            (inputs / "python-identity.json").write_text(json.dumps({"python": version, "pythonAbi": wrong}))
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            os.symlink(sys.executable, venv / "bin/python")
            # No dependency manifests or installer exist. The identity gate
            # must fail first, rather than reaching either of them.
            with self.assertRaises(SystemExit):
                offline.install(["installer-must-not-run"], tmp, str(inputs), str(venv), editable=True)

    def test_command_gate_matches_validation_and_refuses_wrong_abi(self):
        version = "%d.%d" % sys.version_info[:2]
        base = "cp%d%d" % sys.version_info[:2]
        free = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
        actual = base + ("t" if free else "")
        wrong = base + ("" if free else "t")
        command = [sys.executable, "-I", python_identity.__file__, sys.executable, version]
        self.assertEqual(subprocess.run(command + [actual], capture_output=True).returncode, 0)
        refused = subprocess.run(command + [wrong], capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("version/ABI", refused.stderr)
