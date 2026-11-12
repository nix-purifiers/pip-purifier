# SPDX-License-Identifier: MIT
"""Unit tests of pkgs/build-pip-project/pip_inputs.py: requirement parsing and the
rewriting of pip's inputs in lock and offline modes."""

import os
import shlex
import tempfile
import tomllib
import unittest

import pip_inputs


class ParseSpec(unittest.TestCase):
    def test_index_requirement(self):
        self.assertEqual(pip_inputs.parse_spec("attrs==25.3.0 ; python_version >= '3.8'")["kind"], "index")

    def test_named_vcs_url(self):
        spec = pip_inputs.parse_spec("attrs[tests] @ git+https://github.com/python-attrs/attrs@25.3.0 ; os_name == 'posix'")
        self.assertEqual(spec["kind"], "url")
        self.assertEqual(spec["name"], "attrs[tests]")
        self.assertEqual(spec["url"], "git+https://github.com/python-attrs/attrs@25.3.0")
        self.assertEqual(spec["rest"].strip(), "; os_name == 'posix'")

    def test_bare_url_with_egg(self):
        spec = pip_inputs.parse_spec("git+https://example.org/repo.git@v1#egg=thing&subdirectory=py")
        self.assertEqual((spec["kind"], spec["name"]), ("url", "thing"))

    def test_named_archive_url(self):
        spec = pip_inputs.parse_spec("six @ https://example.org/six-1.17.0.tar.gz")
        self.assertEqual((spec["kind"], spec["name"]), ("url", "six"))

    def test_local_path(self):
        self.assertEqual(pip_inputs.parse_spec("./vendor/pkg[extra]")["kind"], "path")
        self.assertEqual(pip_inputs.parse_spec(".")["kind"], "path")


class SplitVcsUrl(unittest.TestCase):
    def test_revision_and_fragment(self):
        vcs, repo, rev, params = pip_inputs.split_vcs_url("git+https://user@host.org/a/b.git@v2.1#egg=b&subdirectory=src")
        self.assertEqual((vcs, repo, rev), ("git", "https://user@host.org/a/b.git", "v2.1"))
        self.assertEqual(params, {"egg": "b", "subdirectory": "src"})

    def test_no_revision_keeps_userinfo(self):
        vcs, repo, rev, _ = pip_inputs.split_vcs_url("hg+https://user@host.org/repo")
        self.assertEqual((vcs, repo, rev), ("hg", "https://user@host.org/repo", None))


class LogicalLines(unittest.TestCase):
    def test_continuations_and_comments(self):
        text = "idna==3.10 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n# comment\nsix # trailing\n\n"
        self.assertEqual(list(pip_inputs.logical_lines(text)), ["idna==3.10     --hash=sha256:aa     --hash=sha256:bb", "six"])


class ScriptMetadata(unittest.TestCase):
    def test_block(self):
        start, end, table = pip_inputs.script_metadata('# /// script\n# dependencies = ["six"]\n# ///\nprint(1)\n')
        self.assertEqual((start, end, table), (0, 2, {"dependencies": ["six"]}))

    def test_unclosed_block(self):
        with self.assertRaises(ValueError):
            pip_inputs.script_metadata("# /// script\n# dependencies = []\n")


class WritePylock(unittest.TestCase):
    def test_round_trip(self):
        data = {
            "lock-version": "1.0",
            "created-by": "test",
            "packages": [
                {"name": "a", "version": "1", "wheels": [{"name": "a-1-py3-none-any.whl", "path": "/x/a.whl", "hashes": {"sha256": "00"}}]},
                {"name": "b", "directory": {"path": "/y", "editable": False}},
            ],
        }
        self.assertEqual(tomllib.loads(pip_inputs.write_pylock(data)), data)


class RewriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.tmp.name, "project")
        os.makedirs(os.path.join(self.project, "requirements"))
        os.makedirs(os.path.join(self.project, "vendor", "pkg"))
        with open(os.path.join(self.project, "vendor", "pkg", "pyproject.toml"), "w") as f:
            f.write("[project]\nname = 'pkg'\n")
        with open(os.path.join(self.project, "pyproject.toml"), "w") as f:
            f.write("[project]\nname = 'top'\n")
        self.write(
            "requirements.txt",
            "--index-url https://example.org/simple\n"
            "--extra-index-url=https://extra.org/simple\n"
            "-r requirements/base.txt\n"
            "-c constraints.txt\n"
            "-e .\n"
            "./vendor/pkg\n"
            "attrs @ git+https://github.com/python-attrs/attrs@25.3.0\n"
            "six @ https://example.org/six-1.17.0.tar.gz\n",
        )
        self.write("requirements/base.txt", "idna==3.10 --hash=sha256:aa\n")
        self.write("constraints.txt", "idna<4\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        with open(os.path.join(self.project, rel), "w") as f:
            f.write(text)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_lock_mode(self):
        out = os.path.join(self.tmp.name, "lock")
        rewriter = pip_inputs.Rewriter(self.project, out, "lock", index=lambda url: "http://proxy/" + url.split("//")[1])
        args = rewriter.args(["-r", "requirements.txt"])
        text = self.read(args[1])
        self.assertIn("--index-url http://proxy/example.org/simple", text)
        self.assertIn("--extra-index-url http://proxy/extra.org/simple", text)
        self.assertIn(f"-e {self.project}", text)
        self.assertIn("attrs @ git+https://github.com/python-attrs/attrs@25.3.0", text)
        self.assertEqual(
            sorted(rewriter.inputs),
            ["constraints.txt", "pyproject.toml", "requirements.txt", "requirements/base.txt", "vendor/pkg/pyproject.toml"],
        )
        self.assertEqual(sorted(rewriter.dirs), [".", "vendor/pkg"])
        self.assertEqual([s["kind"] for s in rewriter.specs], ["vcs", "url"])
        nested = self.read(os.path.join(out, "requirements", "base.txt"))
        self.assertEqual(nested, "idna==3.10 --hash=sha256:aa\n")

    def test_dependency_groups_track_and_rewrite_only_selected_dependencies(self):
        self.write("vendor/pkg/pyproject.toml", '''
[dependency-groups]
common = ["six @ https://example.org/six.whl"]
test_group = [{include-group = "common"}, "attrs; python_version >= '3.11'"]
other = ["unused @ https://example.org/unused.whl"]
''')
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "offline"),
                                      "offline", local=lambda *args: "/locked/six.whl")
        args = rewriter.args(["--group", "vendor/pkg/pyproject.toml:Test.Group"])
        self.assertEqual(rewriter.inputs, ["vendor/pkg/pyproject.toml"])
        self.assertEqual(rewriter.dirs, [])
        self.assertEqual(args[0], "-r")
        requirements = self.read(args[1])
        self.assertIn("six @ file:///locked/six.whl", requirements)
        self.assertIn("attrs; python_version >= '3.11'", requirements)
        self.assertNotIn("unused", requirements)
        self.assertEqual(len(rewriter.specs), 1)

    def test_dependency_group_only_project_records_pyproject(self):
        self.write("pyproject.toml", '[dependency-groups]\ntest = ["six"]\n')
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "lock"), "lock")
        args = rewriter.args(["--group=test", "--group", "test"])
        self.assertEqual(rewriter.inputs, ["pyproject.toml"])
        self.assertEqual(args[0], "-r")
        self.assertEqual(args[2], "-r")
        self.assertEqual(self.read(args[1]), "six\n")

    def test_dependency_group_rejects_cycles_and_non_requirement_options(self):
        for groups in ('a = [{include-group = "a"}]',
                       'a = ["--index-url https://example.org"]'):
            with self.subTest(groups=groups):
                self.write("pyproject.toml", '[dependency-groups]\n' + groups + '\n')
                rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "lock"), "lock")
                with self.assertRaises(ValueError):
                    rewriter.args(["--group", "a"])

    def test_pylock_group_keeps_native_group_and_extra_selection(self):
        self.write("pylock.tests.toml", '''lock-version = "1.0"
dependency-groups = ["dev", "docs"]
[[packages]]
name = "six"
version = "1.17.0"
wheels = [{url = "https://example.org/six.whl", hashes = {sha256 = "aa"}}]
''')
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "offline"),
                                      "offline", local=lambda *args: "/locked/six.whl")
        args = rewriter.args(["--group", "pylock.tests.toml[feature]:dev",
                              "--group=pylock.tests.toml:docs"])
        self.assertEqual(args[0], "--group")
        self.assertTrue(args[1].endswith("pylock.tests.toml[feature]:dev"))
        self.assertTrue(args[3].endswith("pylock.tests.toml:docs"))
        target = args[1].split("[")[0]
        data = tomllib.loads(self.read(target))
        self.assertEqual(data["packages"][0]["wheels"][0]["path"], "/locked/six.whl")
        self.assertEqual(rewriter.inputs, ["pylock.tests.toml"])

    def test_offline_mode(self):
        out = os.path.join(self.tmp.name, "offline")
        local = {"vcs": "/nix/store/attrs-checkout", "url": "/inputs/wheelhouse/six-1.17.0.tar.gz"}
        rewriter = pip_inputs.Rewriter(self.project, out, "offline", local=lambda kind, url, name: local[kind], editable=False)
        args = rewriter.args(["-r", "requirements.txt"])
        text = self.read(args[1])
        self.assertNotIn("index-url", text)
        self.assertNotIn("-e ", text)
        self.assertIn("attrs @ file:///nix/store/attrs-checkout", text)
        self.assertIn("six @ file:///inputs/wheelhouse/six-1.17.0.tar.gz", text)

    def test_offline_mode_keeps_editable(self):
        out = os.path.join(self.tmp.name, "offline")
        rewriter = pip_inputs.Rewriter(self.project, out, "offline", local=lambda kind, url, name: "/x", editable=True)
        text = self.read(rewriter.args(["-r", "requirements.txt"])[1])
        self.assertIn(f"-e {self.project}", text)

    def test_editable_vcs_requirement_keeps_named_url_as_one_argument(self):
        self.write("editable.txt", "-e git+https://example.invalid/probe.git@main#egg=probe\n")
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "offline"), "offline",
                                      local=lambda *_args: "/retained/probe", editable=True)
        line = self.read(rewriter.args(["-r", "editable.txt"])[1]).strip()
        self.assertEqual(shlex.split(line), ["-e", "probe @ file:///retained/probe"])

    def test_command_line_options(self):
        out = os.path.join(self.tmp.name, "lock")
        rewriter = pip_inputs.Rewriter(self.project, out, "lock", index=lambda url: "http://proxy/")
        args = rewriter.args(["--pre", "-i", "https://example.org/simple", "--no-binary=:all:", "-rrequirements/base.txt", "."])
        self.assertEqual(args[:4], ["--pre", "-i", "http://proxy/", "--no-binary"])
        self.assertEqual(args[4], ":all:")
        self.assertEqual(args[5], "-r")
        self.assertEqual(args[-3], self.project)
        self.assertEqual(args[-2], "--build-constraint")
        self.assertEqual(self.read(args[-1]), "idna==3.10\n")

    def test_build_constraints(self):
        # Hashed pins constrain the build environments; an unhashed pin and
        # a name pinned to two versions do not.
        self.write(
            "requirements.txt",
            "Setuptools==80.9.0 --hash=sha256:aa\n"
            'wheel==0.45.1 ; python_version >= "3" --hash=sha256:bb\n'
            'wheel==0.44.0 ; python_version < "3" --hash=sha256:cc\n'
            "six==1.17.0\n",
        )
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "b"), "lock", index=lambda url: url)
        args = rewriter.args(["-r", "requirements.txt"])
        self.assertEqual(args[-2], "--build-constraint")
        self.assertEqual(self.read(args[-1]), "setuptools==80.9.0\n")

    def test_no_build_constraints_without_pins(self):
        self.write("requirements.txt", "six\n")
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "n"), "lock", index=lambda url: url)
        self.assertNotIn("--build-constraint", rewriter.args(["-r", "requirements.txt"]))

    def test_requirements_from_url_is_refused(self):
        self.write("requirements.txt", "-r https://example.org/requirements.txt\n")
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "x"), "lock", index=lambda url: url)
        with self.assertRaises(SystemExit):
            rewriter.args(["-r", "requirements.txt"])

    def test_pylock_offline(self):
        self.write(
            "pylock.toml",
            'lock-version = "1.0"\ncreated-by = "pip"\n\n[[packages]]\nname = "six"\nversion = "1.17.0"\n\n'
            '[[packages.wheels]]\nname = "six-1.17.0-py2.py3-none-any.whl"\nurl = "https://files.example/six-1.17.0-py2.py3-none-any.whl"\n'
            '[packages.wheels.hashes]\nsha256 = "00"\n\n'
            '[[packages]]\nname = "thing"\n[packages.vcs]\ntype = "git"\nurl = "https://example.org/thing"\ncommit-id = "abc"\nsubdirectory = "py"\n',
        )
        local = {"file": "/wh/six-1.17.0-py2.py3-none-any.whl", "vcs": "/nix/store/thing"}
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "o"), "offline", local=lambda kind, url, name: local[kind])
        data = tomllib.loads(self.read(rewriter.args(["-r", "pylock.toml"])[1]))
        self.assertEqual(data["packages"][0]["wheels"][0]["path"], "/wh/six-1.17.0-py2.py3-none-any.whl")
        self.assertNotIn("url", data["packages"][0]["wheels"][0])
        self.assertEqual(data["packages"][1]["directory"], {"path": "/nix/store/thing/py", "editable": False})
        self.assertEqual([s["kind"] for s in rewriter.specs], ["file", "vcs"])
        self.assertEqual(self.read(rewriter.build_constraints()), "six==1.17.0\n")

    def test_script_offline(self):
        self.write("app.py", '# /// script\n# dependencies = ["six @ https://example.org/six.tar.gz"]\n# ///\nprint(1)\n')
        rewriter = pip_inputs.Rewriter(self.project, os.path.join(self.tmp.name, "o"), "offline", local=lambda kind, url, name: "/wh/six.tar.gz")
        args = rewriter.args(["--requirements-from-script", "app.py"])
        _, _, table = pip_inputs.script_metadata(self.read(args[1]))
        self.assertEqual(table["dependencies"], ["six @ file:///wh/six.tar.gz"])


if __name__ == "__main__":
    unittest.main()
