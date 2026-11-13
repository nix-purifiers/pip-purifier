# SPDX-License-Identifier: MIT
"""Unit tests of pkgs/build-pip-project/offline.py and pkgs/build-pip-project/vcs_shim.py: tagging a copy
with its locked version, the git that clones without a filter, and the
answers of the hg, svn and bzr shims."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

import offline

SHIM = os.path.join(os.path.dirname(offline.__file__), "vcs_shim.py")


class EditableSourceLifetime(unittest.TestCase):
    def exercise(self, editable):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "module.py").write_text("original\n")
            inputs = root / "inputs"
            inputs.mkdir()
            url = "git+https://example.invalid/owned.git@main#egg=probe"
            for name, value in {
                "sources.json": {url: str(source)}, "versions.json": {},
                "args.json": ["-e", url], "repositories.json": [],
            }.items():
                (inputs / name).write_text(json.dumps(value))
            destinations = []
            def consume(command, *_args):
                self.assertNotIn("PIP_PURIFIER_NATIVE_REPAIR", _args[-1])
                target = command[-1].split(" @ ", 1)[-1]
                path = Path(urllib.parse.unquote(urllib.parse.urlsplit(target).path))
                self.assertTrue((path / "module.py").exists())
                destinations.append(path)
            def install():
                with patch.object(offline, "run_pip", side_effect=consume), \
                        patch.dict(os.environ, {"PIP_PURIFIER_NATIVE_REPAIR": "must-not-reach-local-pip"}):
                    offline.install(["pip"], str(root), str(inputs), str(root / "venv"), editable, repair_native=False)
            install()
            destination = destinations[-1]
            if editable:
                self.assertTrue(destination.exists(), "editable source disappeared after pip returned")
                (destination / "module.py").write_text("local edit\n")
                install()
                self.assertEqual(destinations[-1], destination)
                self.assertEqual((destination / "module.py").read_text(), "local edit\n")
                self.assertEqual((source / "module.py").read_text(), "original\n")
                replacement = root / "next-locked-source"
                replacement.mkdir()
                (replacement / "module.py").write_text("new locked source\n")
                (inputs / "sources.json").write_text(json.dumps({url: str(replacement)}))
                install()
                self.assertNotEqual(destinations[-1], destination)
                self.assertEqual((destinations[-1] / "module.py").read_text(), "new locked source\n")
                self.assertEqual((destination / "module.py").read_text(), "local edit\n")
            else:
                self.assertFalse(destination.exists())

    def test_editable_source_survives_and_local_edits_are_preserved(self):
        self.exercise(True)

    def test_noneditable_build_source_is_temporary(self):
        self.exercise(False)


class Tag(unittest.TestCase):
    def test_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("x = 1\n")
            offline.tag(tmp, "1.2.3")
            described = subprocess.run(["git", "describe", "--tags", "--exact-match"], cwd=tmp, capture_output=True, text=True, check=True)
            self.assertEqual(described.stdout.strip(), "1.2.3")
            status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp, capture_output=True, text=True, check=True)
            self.assertEqual(status.stdout, "")


class FullClones(unittest.TestCase):
    def test_clone_drops_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(real)
            with open(os.path.join(real, "git"), "w") as f:
                f.write('#!/bin/sh\nprintf "%s|" "$@"\n')
            os.chmod(os.path.join(real, "git"), 0o755)
            env = {"PATH": real}
            offline.serve_full_clones(tmp, env)

            def run(*args):
                return subprocess.run(["git", *args], env=env, capture_output=True, text=True, check=True).stdout

            self.assertEqual(run("clone", "--filter=blob:none", "--quiet", "u r l", "dest"), "clone|--quiet|u r l|dest|")
            self.assertEqual(run("fetch", "--filter=blob:none", "origin"), "fetch|--filter=blob:none|origin|")


class Shims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.checkout = os.path.join(self.tmp.name, "checkout")
        os.makedirs(self.checkout)
        with open(os.path.join(self.checkout, "setup.py"), "w") as f:
            f.write("")
        self.table = os.path.join(self.tmp.name, "shims.json")
        with open(self.table, "w") as f:
            json.dump(
                [
                    {"vcs": "hg", "url": "https://hg.example/r", "rev": "a" * 40, "version": None, "path": self.checkout},
                    {"vcs": "svn", "url": "https://svn.example/r", "rev": "7", "version": None, "path": self.checkout},
                    {"vcs": "bzr", "url": "https://bzr.example/r", "rev": "3", "version": None, "path": self.checkout},
                ],
                f,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def shim(self, vcs, *args, cwd=None, check=True):
        result = subprocess.run(
            [sys.executable, SHIM, vcs, *args],
            cwd=cwd or self.tmp.name,
            env=dict(os.environ, PIP_PURIFIER_VCS_SHIMS=self.table),
            capture_output=True,
            text=True,
            check=False,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_hg(self):
        dest = os.path.join(self.tmp.name, "hg")
        self.shim("hg", "clone", "--noupdate", "-q", "https://hg.example/r", dest)
        self.shim("hg", "update", "-q", "--rev=" + "a" * 40, cwd=dest)
        self.assertEqual(self.shim("hg", "parents", "--template={node}", cwd=dest).stdout, "a" * 40)
        self.assertEqual(self.shim("hg", "parents", "--template={rev}", cwd=dest).stdout, "a" * 40)
        self.assertEqual(self.shim("hg", "showconfig", "paths.default", cwd=dest).stdout.strip(), "https://hg.example/r")
        self.assertTrue(os.path.exists(os.path.join(dest, "setup.py")))

    def test_svn(self):
        dest = os.path.join(self.tmp.name, "svn")
        self.assertTrue(self.shim("svn", "--version").stdout.startswith("svn, version "))
        self.shim("svn", "checkout", "-q", "--non-interactive", "-r", "7", "https://svn.example/r", dest)
        xml = self.shim("svn", "info", "--xml", dest).stdout
        self.assertIn('revision="7"', xml)
        self.assertIn("<url>https://svn.example/r</url>", xml)

    def test_bzr(self):
        dest = os.path.join(self.tmp.name, "bzr")
        self.shim("bzr", "checkout", "--lightweight", "-q", "-r", "3", "https://bzr.example/r", dest)
        self.assertEqual(self.shim("bzr", "revno", cwd=dest).stdout.strip(), "3")

    def test_unknown_url_and_command(self):
        result = self.shim("hg", "clone", "https://other.example/r", os.path.join(self.tmp.name, "x"), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Run `pip-purifier lock`", result.stderr)
        dest = os.path.join(self.tmp.name, "hg")
        self.shim("hg", "clone", "https://hg.example/r", dest)
        result = self.shim("hg", "log", cwd=dest, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not a command the locked checkout can answer", result.stderr)


if __name__ == "__main__":
    unittest.main()
