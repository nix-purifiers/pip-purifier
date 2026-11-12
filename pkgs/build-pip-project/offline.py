# SPDX-License-Identifier: MIT
"""Installs a project's locked inputs into a virtual environment offline.

The Nix build (build-pip-project.nix) and `pip-purifier prefetch` both run
this. `inputs` is the directory build-pip-project.nix makes as `inputs`:

  wheelhouse/     every locked wheel and sdist for this system
  sources.json    each VCS or URL requirement, as written, to its local copy
                  (a store path, or a path relative to `inputs`)
  pins.txt        the versions the lock run resolved for this system
  versions.json   the version of each package built from a checkout (by its
                  requirement URL) or a local directory (by its path)
  args.json       the pip install arguments the lock run recorded
  repositories.json
                  each git repository that another package names as a
                  source, with its URL, the revision pip asks for, the
                  locked version and a clone in the store
  replay/         the files of URLs that another package names as a source,
                  in the layout `mitm-cache replay` serves them from
  shims.json      each Mercurial, Subversion or Bazaar repository that
                  another package names as a source, with its locked
                  checkout, for vcs_shim.py

pip runs with --no-index and the wheelhouse as its only find-links, both on
the command line and in the environment, so the isolated environments in
which it builds sdists read the same files.

A checkout is copied to a writable directory, because pip builds a local
directory in place. The copy, and in the Nix build every local directory
without VCS metadata, becomes a git repository with one commit, tagged with
the version the lock run saw. setuptools-scm and hatch-vcs then derive that
version, as they did from the real repository.
"""

import argparse
import gzip
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pip_inputs
import native_runtime
import python_identity


def environment(inputs):
    """The environment variables of an offline pip run."""
    env = dict(os.environ)
    for key in list(env):
        # PYTHONPATH from the caller would come ahead of the locked packages.
        if key.startswith("PIP_") or key == "PYTHONPATH":
            del env[key]
    env.update(
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=os.path.join(inputs, "wheelhouse"),
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_INPUT="1",
        PIP_PROGRESS_BAR="off",
    )
    bindings = os.path.join(inputs, "python-inputs.json")
    if os.path.exists(bindings):
        env["PIP_PURIFIER_BINDINGS"] = bindings
    return env


def git(directory, *args):
    subprocess.run(
        ["git", "-c", "user.name=pip-purifier", "-c", "user.email=pip-purifier@localhost", "-c", "init.defaultBranch=main", *args],
        cwd=directory,
        check=True,
        stdout=subprocess.DEVNULL,
        env=dict(os.environ, GIT_COMMITTER_DATE="1970-01-01T00:00:00Z", GIT_AUTHOR_DATE="1970-01-01T00:00:00Z"),
    )


def tag(directory, version):
    """Makes `directory` a git repository whose one commit is tagged
    `version`."""
    git(directory, "init", "-q")
    git(directory, "add", "-A", "-f")
    git(directory, "commit", "-q", "--allow-empty", "-m", version)
    git(directory, "tag", version)


def serve_repositories(inputs, tmp, env):
    """Points git at a local clone of each repository that another package
    names as a source, and of each of its submodules. Each clone is a
    writable copy of the locked checkout, whose shallow .git the purifier
    library rebuilt from the recorded commit object; the repository pip
    asked for carries the revision it asked by as a tag on the locked
    commit, or the version when the revision is a commit."""
    repositories = load(inputs, "repositories.json")
    if repositories:
        serve_full_clones(tmp, env)
    for n, repository in enumerate(repositories):
        copy = os.path.join(tmp, "repositories", str(n))
        shutil.copytree(repository["path"], copy, symlinks=True)
        subprocess.run(["chmod", "-R", "u+w", copy], check=True)
        revision = repository.get("revision")
        if revision:
            names = [revision] if not re.fullmatch(r"[0-9a-f]{40}", revision) else [repository["version"]]
            for name in names:
                git(copy, "tag", "-f", name, "HEAD")
        # The URL pip clones, with and without ".git": git applies the
        # longest matching insteadOf, so each form maps to the clone.
        base = repository["url"].removesuffix(".git")
        for target in (base, base + ".git"):
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
            env[f"GIT_CONFIG_KEY_{count}"] = f"url.file://{copy}.insteadOf"
            env[f"GIT_CONFIG_VALUE_{count}"] = target
            env["GIT_CONFIG_COUNT"] = str(count + 1)


def serve_full_clones(tmp, env):
    """Puts a git first on PATH that clones without `--filter`. pip clones
    with `--filter=blob:none`, and a partial clone of a shallow repository
    such as a served copy does not record the copy's shallow commit: each
    lazy fetch of the commit's parent fetches again from the copy, nested,
    until the environment no longer fits in the argument space."""
    real = shutil.which("git", path=env.get("PATH"))
    if real is None:
        return
    bin_dir = os.path.join(tmp, "git-full-clones")
    os.makedirs(bin_dir)
    script = os.path.join(bin_dir, "git")
    with open(script, "w") as f:
        f.write(
            "#!/bin/sh\n"
            'if [ "$1" = clone ]; then\n'
            "  shift\n"
            "  for arg; do\n"
            "    shift\n"
            '    case "$arg" in --filter=*) ;; *) set -- "$@" "$arg" ;; esac\n'
            "  done\n"
            "  set -- clone \"$@\"\n"
            "fi\n"
            f'exec {real} "$@"\n'
        )
    os.chmod(script, 0o755)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")


def serve_replays(inputs, tmp, env):
    """Starts `mitm-cache replay` on the locked files of URLs that another
    package names as a source, when there are any, and points pip at it:
    pip downloads such a URL itself, over HTTPS through `https_proxy`, and
    trusts the proxy's certificate authority through PIP_CERT. Returns the
    proxy process, or None."""
    replay = os.path.join(inputs, "replay")
    if not os.path.isdir(replay) or not os.listdir(replay):
        return None
    certs = os.path.join(tmp, "mitm-cache")
    os.makedirs(certs)
    subprocess.run(["openssl", "genrsa", "-out", "ca.key", "2048"], cwd=certs, check=True, capture_output=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-new", "-nodes", "-key", "ca.key", "-sha256", "-days", "1", "-out", "ca.cer", "-subj", "/CN=pip-purifier"],
        cwd=certs,
        check=True,
        capture_output=True,
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{s.getsockname()[1]}"
    process = subprocess.Popen(
        ["mitm-cache", "-l", address, "replay", replay], cwd=certs, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    host, port = address.split(":")
    for _ in range(100):
        try:
            socket.create_connection((host, int(port)), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    ca = os.path.join(certs, "ca.cer")
    for key in ("no_proxy", "NO_PROXY"):
        env.pop(key, None)
    env.update(http_proxy=address, https_proxy=address, HTTP_PROXY=address, HTTPS_PROXY=address, PIP_CERT=ca)
    # On macOS pip checks certificates with the system's verifier, which
    # refuses the ones mitm-cache makes: they name no extended key usage.
    # The proxy serves only the locked files, which Nix fetched by hash, so
    # there pip takes the replayed hosts without the check.
    https = os.path.join(replay, "https")
    if sys.platform == "darwin" and os.path.isdir(https):
        env["PIP_TRUSTED_HOST"] = " ".join(sorted(os.listdir(https)))
    return process


def serve_shims(inputs, tmp, env):
    """Puts hg, svn and bzr shims (vcs_shim.py) first on PATH when another
    package names a Mercurial, Subversion or Bazaar repository as a source.
    pip clones such a repository itself, and the shims answer from the
    locked checkout."""
    path = os.path.join(inputs, "shims.json")
    if not os.path.exists(path) or not load(inputs, "shims.json"):
        return
    bin_dir = os.path.join(tmp, "vcs-shims")
    os.makedirs(bin_dir)
    shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vcs_shim.py")
    for vcs in ("hg", "svn", "bzr"):
        script = os.path.join(bin_dir, vcs)
        with open(script, "w") as f:
            f.write(f'#!/bin/sh\nexec {sys.executable} {shim} {vcs} "$@"\n')
        os.chmod(script, 0o755)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["PIP_PURIFIER_VCS_SHIMS"] = path


def load(inputs, name):
    with open(os.path.join(inputs, name)) as f:
        return json.load(f)


def install(pip, project, inputs, venv, editable, extra=(), pylock=None, repair_native=True, installer_env=None):
    """Installs the project's inputs into the virtual environment `venv`
    with `pip`, a command that runs pip. `editable` keeps editable installs
    editable; the Nix build turns them into plain installs."""
    python_identity.validate_inputs(inputs, os.path.join(venv, "bin", "python"))
    sources = load(inputs, "sources.json")
    versions = load(inputs, "versions.json")
    args = load(inputs, "args.json")
    wheelhouse = os.path.join(inputs, "wheelhouse")

    with tempfile.TemporaryDirectory() as tmp:
        copies = os.path.join(tmp, "checkouts")

        def local(kind, url, name):
            if kind == "file":
                return os.path.join(wheelhouse, os.path.basename(pip_inputs.url_without_fragment(url)))
            if url not in sources:
                raise SystemExit(f"pip-purifier: the lockfile has no copy of {url}. Run `pip-purifier lock`.")
            path = os.path.join(inputs, sources[url])
            if not os.path.isdir(path):
                return path
            copy = os.path.join(copies, str(len(os.listdir(copies)) if os.path.isdir(copies) else 0))
            shutil.copytree(path, copy, symlinks=True)
            subprocess.run(["chmod", "-R", "u+w", copy], check=True)
            if url in versions:
                tag(copy, versions[url])
            return copy

        configuration = os.path.join(inputs, "python-inputs.json")
        provided = {}
        if os.path.exists(configuration):
            with open(configuration) as source:
                provided = json.load(source)['modules']
        rewriter = pip_inputs.Rewriter(project, os.path.join(tmp, "inputs"), "offline",
                                      local=local, editable=editable, pylock=pylock, provided=provided)
        rewritten = rewriter.args(args)
        if not editable:
            for rel in rewriter.dirs:
                directory = os.path.join(project, rel)
                if rel in versions and not os.path.exists(os.path.join(directory, ".git")):
                    tag(directory, versions[rel])

        command = list(pip) + [
            "--python",
            os.path.join(venv, "bin", "python"),
            "install",
            "--no-index",
            "--find-links",
            wheelhouse,
            "--constraint",
            os.path.join(inputs, "pins.txt"),
            *extra,
            *rewritten,
        ]
        env = environment(inputs)
        env.update(installer_env or {})
        if os.environ.get("PIP_PURIFIER_EXPORT_WHEELS"):
            env["PIP_PURIFIER_EXPORT_WHEELS"] = os.environ["PIP_PURIFIER_EXPORT_WHEELS"]
        serve_repositories(inputs, tmp, env)
        serve_shims(inputs, tmp, env)
        replayer = serve_replays(inputs, tmp, env)
        print("pip-purifier: " + " ".join(command), file=sys.stderr)
        try:
            run_pip(command, project, env)
        finally:
            if replayer:
                replayer.terminate()
                replayer.wait()
        if repair_native:
            native_runtime.repair(inputs, venv)


def run_pip(command, cwd, env, stdout=False):
    """Runs pip, passing its output through, and turns a requirement it
    cannot find among the locked files into an error that names it and the
    command that fixes it. With `stdout`, pip's standard output goes to
    standard output untouched, and only its errors are read; otherwise
    both go to standard error."""
    if len(command) >= 3 and command[1:3] == ["-m", "pip"]:
        command = [command[0], os.path.join(os.path.dirname(__file__), "pip_runner.py"), *command[3:]]
    if stdout:
        process = subprocess.Popen(command, cwd=cwd, env=env, stderr=subprocess.PIPE, text=True)
        lines = process.stderr
    else:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = process.stdout
    missing = []
    # pip names a requirement it found no files for either directly, or,
    # when constraints are involved, in an indented list after this line.
    in_list = False
    for line in lines:
        sys.stderr.write(line)
        m = re.search(r"No matching distribution found for (\S+)", line)
        if m:
            missing.append(m.group(1))
        if "have no matching distributions available" in line:
            in_list = True
        elif in_list and re.match(r"^\s+\S+\s*$", line):
            missing.append(line.strip())
        else:
            in_list = False
    if process.wait() != 0:
        if missing:
            raise SystemExit(
                f"pip-purifier: the lockfile has no file for {', '.join(missing)}. The requirements need something the "
                "lock run did not see; run `pip-purifier lock`."
            )
        raise SystemExit(f"pip-purifier: pip failed with exit status {process.returncode}")


def normalize_sdist(path, mtime):
    """Rewrites the sdist at `path` so that it depends only on its files:
    setuptools stamps each member and the gzip header with the time of the
    build. Every member gets the time `mtime` and no owner, and the gzip
    header gets no time and no file name. The members keep their order."""
    with tarfile.open(path, "r:gz") as source:
        members = [(member, source.extractfile(member).read() if member.isfile() else None) for member in source.getmembers()]
    with open(path, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as target:
            for member, data in members:
                member.mtime = mtime
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                member.pax_headers = {}
                target.addfile(member, io.BytesIO(data) if data is not None else None)


def main():
    if sys.argv[1:2] == ["tag"]:
        # `offline.py tag DIRECTORY VERSION`, for builds that run pip
        # themselves.
        directory, version = sys.argv[2:4]
        if not os.path.exists(os.path.join(directory, ".git")):
            tag(directory, version)
        return
    if sys.argv[1:2] == ["normalize-sdist"]:
        # `offline.py normalize-sdist PATH`, after a build of an sdist.
        normalize_sdist(sys.argv[2], int(os.environ.get("SOURCE_DATE_EPOCH", "0")))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--venv", required=True)
    parser.add_argument("--no-editable", action="store_true")
    ns = parser.parse_args()
    install(
        [sys.executable, "-m", "pip"],
        os.path.abspath(ns.project),
        ns.inputs,
        os.path.abspath(ns.venv),
        editable=not ns.no_editable,
    )


if __name__ == "__main__":
    main()
