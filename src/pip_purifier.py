# SPDX-License-Identifier: MIT
"""pip-purifier: lock, prefetch and publish pip projects that Nix builds.

`lock` runs `pip install` once for real, through a recording index proxy on
localhost, and writes pip-purifier.lock.json: every file pip downloaded
(including what the isolated build environments of sdists downloaded), the
checkout of every VCS requirement, and the versions pip resolved, for each
Nix system. `prefetch` has Nix fetch the locked files, keeps them with a
garbage-collector root, and installs the project into .venv offline. `pip`
runs pip offline from the same files. `publish` uploads the release that the
Nix build made.
"""

import argparse
import base64
import hashlib
import html
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NIX_LIB = os.environ["PIP_PURIFIER_NIX_LIB"]
sys.path.insert(0, NIX_LIB)

import offline  # noqa: E402
import pip_inputs  # noqa: E402
import python_bindings  # noqa: E402
sys.path.insert(0, os.environ.get("PIP_PURIFIER_PROJECT_INPUTS_TOOLS", str(Path(NIX_LIB).parent / "purifier/tools")))

# The lock format version. nix/lib/lock-version.nix reads the same.
with open(os.path.join(NIX_LIB, "lock-version.nix")) as _f:
    FORMAT_VERSION = int(re.findall(r"^\d+$", _f.read(), re.M)[0])

LOCKFILE = "pip-purifier.lock.json"
PIP = [sys.executable, "-m", "pip"]
PYTHON = os.environ["PIP_PURIFIER_PYTHON"]
NIXPKGS = os.environ["PIP_PURIFIER_NIXPKGS"]

# The Nix systems a lockfile can cover, with the CPU name of their wheel
# tags: Linux's tags name the 64-bit Arm CPU aarch64, macOS's arm64.
SYSTEMS = {"x86_64-linux": "x86_64", "aarch64-linux": "aarch64", "aarch64-darwin": "arm64"}

# The systems a lockfile covers when `--system` names none, with the
# system the lock runs on.
DEFAULT_SYSTEMS = ["x86_64-linux", "aarch64-linux"]

# The manylinux glibc versions a lock run asks for. A newer glibc runs
# wheels for every older one; the Nix build keeps the wheels its own glibc
# runs (`wheels` of the purifier library).
GLIBC_MINORS = range(5, 45)

# The macOS platform tags a lock run asks for on aarch64-darwin, as
# packaging.tags.mac_platforms((14, 0), "arm64") lists them: the arm64 and
# universal2 wheels of macOS 11 to 14, and the universal2 wheels that name
# a macOS 10 release for their x86_64 half. 14.0 is nixpkgs'
# darwinMinVersion, which the Nix build gives `wheels`.
MACOS_PLATFORMS = [f"macosx_{n}_0_{f}" for n in range(14, 10, -1) for f in ("arm64", "universal2")] + [
    f"macosx_10_{n}_universal2" for n in range(16, 3, -1)
]


def log(message):
    print(f"pip-purifier: {message}", file=sys.stderr)


def die(message):
    log(message)
    sys.exit(1)


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def sri(digest):
    return "sha256-" + base64.b64encode(digest).decode()


def strip_credentials(url):
    parts = urllib.parse.urlsplit(url)
    if parts.username is None:
        return url
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


def open_url(url, accept=None):
    """Opens a URL. Credentials in the URL become a Basic Authorization
    header, and the proxy settings of the environment apply."""
    parts = urllib.parse.urlsplit(url)
    headers = {"User-Agent": "pip-purifier"}
    if accept:
        headers["Accept"] = accept
    if parts.username is not None:
        token = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(token.encode()).decode()
    return urllib.request.urlopen(urllib.request.Request(strip_credentials(url), headers=headers))


def download(url, out, accept=None, attempts=4):
    """Downloads a URL completely into the file object `out`, retrying a
    transfer that fails or ends early. Returns the final URL after
    redirects. An HTTP error status is raised at once."""
    for attempt in range(attempts):
        out.seek(0)
        out.truncate()
        try:
            with open_url(url, accept=accept) as response:
                shutil.copyfileobj(response, out)
                # A transfer can end early without an error; the length the
                # server announced catches it.
                expected = response.headers.get("Content-Length")
                encoded = response.headers.get("Content-Encoding", "identity") != "identity"
                if expected is not None and not encoded and out.tell() != int(expected):
                    raise http.client.IncompleteRead(b"", int(expected) - out.tell())
                return response.geturl()
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException) as error:
            if attempt == attempts - 1:
                raise
            log(f"retrying {strip_credentials(url)} after {error!r}")
            time.sleep(2**attempt)


def hash_url(url):
    """The SHA-256 digest of the file at a URL."""
    with tempfile.TemporaryFile() as data:
        download(url, data)
        data.seek(0)
        return hashlib.file_digest(data, "sha256").digest()


def file_name(url):
    return urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path))


# The recording proxy.


class Proxy:
    """A package index on localhost in front of the real ones.

    /i/<n>/<path> serves the page <path> of upstream index n, with every
    link rewritten to /f/<url>/<file name>. /f/ serves the file and records
    its URL and SHA-256. A link's `.metadata` file (PEP 658) passes through
    without being recorded, because the offline install reads the files
    themselves.
    """

    def __init__(self):
        self.upstreams = []
        self.files = {}
        # The locked files to list, by normalized project name, on a relock.
        self.keep = {}
        self.lock = threading.Lock()
        self.errors = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    proxy.handle(self)
                except BrokenPipeError:
                    pass
                except Exception as error:  # reported once pip is done
                    proxy.errors.append(f"{self.path}: {error}")
                    try:
                        self.send_error(502, str(error))
                    except Exception:
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()

    def upstream(self, url):
        """The proxy URL that stands for an index or find-links URL."""
        if not url.endswith("/") and not os.path.splitext(urllib.parse.urlsplit(url).path)[1]:
            url += "/"
        if url not in self.upstreams:
            self.upstreams.append(url)
        return f"{self.base}/i/{self.upstreams.index(url)}/"

    def file_link(self, url):
        token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"{self.base}/f/{token}/{urllib.parse.quote(file_name(url))}"

    def handle(self, request):
        parts = request.path.split("/", 3)
        if len(parts) == 4 and parts[1] == "i":
            self.page(request, self.upstreams[int(parts[2])], parts[3])
        elif len(parts) == 4 and parts[1] == "f":
            token = parts[2] + "=" * (-len(parts[2]) % 4)
            url = base64.urlsafe_b64decode(token).decode()
            name = urllib.parse.unquote(parts[3])
            if name == file_name(url) + ".metadata":
                self.passthrough(request, url + ".metadata")
            else:
                self.file(request, url)
        else:
            request.send_error(404)

    def page(self, request, upstream, path):
        url = urllib.parse.urljoin(upstream, path)
        buffer = io.BytesIO()
        try:
            final = download(url, buffer, accept="text/html")
        except urllib.error.HTTPError as error:
            request.send_error(error.code)
            return
        body = buffer.getvalue().decode("utf-8", "replace")
        # Credentials of the upstream stay on links to the same host.
        up = urllib.parse.urlsplit(upstream)

        def link(match):
            href = urllib.parse.urljoin(final, html.unescape(match.group(2)))
            target, _, fragment = href.partition("#")
            t = urllib.parse.urlsplit(target)
            if up.username is not None and t.hostname == up.hostname and t.username is None:
                target = urllib.parse.urlunsplit(t._replace(netloc=up.netloc))
            new = self.file_link(target) + ("#" + fragment if fragment else "")
            return f"{match.group(1)}{html.escape(new)}{match.group(3)}"

        # On a relock, a project the existing lockfile covers lists only its
        # locked files.
        project = normalize(path.strip("/").split("/")[0]) if path.strip("/") else None
        if project in self.keep:
            kept = self.keep[project]

            def keep_anchor(match):
                href = html.unescape(match.group(1)).partition("#")[0]
                return match.group(0) if file_name(urllib.parse.urljoin(final, href)) in kept else ""

            body = re.sub(r'<a\b[^>]*href="([^"]*)"[^>]*>.*?</a>', keep_anchor, body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r'(href=")([^"]*)(")', link, body)
        data = body.encode()
        request.send_response(200)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    def passthrough(self, request, url):
        buffer = io.BytesIO()
        try:
            download(url, buffer)
        except urllib.error.HTTPError as error:
            request.send_error(error.code)
            return
        data = buffer.getvalue()
        request.send_response(200)
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    def file(self, request, url):
        # The whole file arrives first, so a failed transfer is retried
        # before pip sees any of it. The file is recorded before pip gets
        # it: pip, and then the lock run, can go on as soon as the last
        # byte arrives, while this server thread is still finishing.
        with tempfile.TemporaryFile() as data:
            try:
                download(url, data)
            except urllib.error.HTTPError as error:
                request.send_error(error.code)
                return
            size = data.tell()
            digest = hashlib.sha256()
            data.seek(0)
            while chunk := data.read(1 << 16):
                digest.update(chunk)
            self.record(url, digest.digest())
            request.send_response(200)
            request.send_header("Content-Length", str(size))
            request.end_headers()
            data.seek(0)
            while chunk := data.read(1 << 16):
                request.wfile.write(chunk)

    def record(self, url, digest):
        name = file_name(url)
        entry = {"path": name, "url": strip_credentials(url), "mirrors": [], "hash": sri(digest)}
        with self.lock:
            old = self.files.get(name)
            if old and old["hash"] != entry["hash"]:
                self.errors.append(f"two different files are named {name}: {old['url']} and {entry['url']}")
            self.files.setdefault(name, entry)


# Wheels and platforms.


def wheel_tags(name):
    """The (python, abi, platform) tag sets of a wheel file name."""
    parts = name[:-4].split("-")
    if len(parts) < 5:
        return None
    return parts[-3].split("."), parts[-2].split("."), parts[-1].split(".")


def dist_name_version(name):
    """The normalized project name and the version of a wheel or sdist file
    name."""
    if name.endswith(".whl"):
        parts = name.split("-")
        return normalize(parts[0]), parts[1]
    stem = re.sub(r"\.(tar\.gz|tar\.bz2|tar\.xz|zip|tgz|tar)$", "", name)
    m = re.match(r"^(.*)-([0-9][^-]*)$", stem)
    if not m:
        return None
    return normalize(m.group(1)), m.group(2)


def wheel_runs_on(name, arch, python):
    """Whether a wheel installs with CPython `python` ("3.14") on Linux for
    `arch`, with any glibc, or on macOS for "arm64", at one of
    MACOS_PLATFORMS. The Nix build applies its own glibc."""
    tags = wheel_tags(name)
    if not tags:
        return False
    major, minor = python.split(".")
    cp = f"cp{major}{minor}"
    pythons, abis, platforms = tags

    def python_ok(tag, abi):
        if abi == "none":
            m = re.match(rf"^(py|cp){major}(\d*)$", tag)
            return bool(m) and (m.group(2) == "" or int(m.group(2)) <= int(minor))
        if abi == "abi3":
            m = re.match(rf"^cp{major}(\d+)$", tag)
            return bool(m) and int(m.group(1)) <= int(minor)
        return abi in (cp, cp + "t") and tag == cp

    def platform_ok(tag):
        if arch == "arm64":
            return tag == "any" or tag in MACOS_PLATFORMS
        return tag == "any" or (tag.endswith("_" + arch) and tag.startswith(("manylinux", "linux")))

    return any(platform_ok(p) for p in platforms) and any(python_ok(t, a) for t in pythons for a in abis)


def platform_args(arch, python):
    major, minor = python.split(".")
    args = ["--only-binary=:all:", "--python-version", python, "--implementation", "cp", "--abi", f"cp{major}{minor}", "--abi", "abi3", "--abi", "none"]
    if arch == "arm64":
        platforms = MACOS_PLATFORMS
    else:
        platforms = [f"manylinux_2_{n}_{arch}" for n in GLIBC_MINORS] + [
            f"manylinux1_{arch}",
            f"manylinux2010_{arch}",
            f"manylinux2014_{arch}",
            f"linux_{arch}",
        ]
    for p in platforms:
        args += ["--platform", p]
    return args


def current_system():
    machine = os.uname().machine
    kernel = os.uname().sysname.lower()
    for system, arch in SYSTEMS.items():
        if arch == machine and system.endswith("-" + kernel):
            return system
    die(f"this machine ({os.uname().sysname} {machine}) is not one of the systems a lock covers: {', '.join(SYSTEMS)}")


# VCS checkouts.


def prefetch(vcs, url, rev, submodules):
    """The SRI NAR hash of a checkout without VCS metadata, as the nixpkgs
    fetcher of `vcs` computes it."""
    flag = "true" if submodules else "false"
    expr = {
        "git": f"pkgs.fetchgit {{ url = {json.dumps(url)}; rev = {json.dumps(rev)}; fetchSubmodules = {flag}; hash = pkgs.lib.fakeHash; }}",
        "hg": f"pkgs.fetchhg {{ url = {json.dumps(url)}; rev = {json.dumps(rev)}; fetchSubrepos = true; hash = pkgs.lib.fakeHash; }}",
        "svn": f"pkgs.fetchsvn {{ url = {json.dumps(url)}; rev = {json.dumps(rev)}; ignoreExternals = false; ignoreKeywords = false; hash = pkgs.lib.fakeHash; }}",
        "bzr": f'pkgs.fetchbzr {{ url = {json.dumps(url)}; rev = {json.dumps(rev)}; hash = pkgs.lib.fakeHash; }}',
    }[vcs]
    result = subprocess.run(
        ["nix-build", "--no-out-link", "-E", f"let pkgs = import {NIXPKGS} {{ }}; in {expr}"],
        capture_output=True,
        text=True,
    )
    m = re.search(r"got:\s+(sha256-\S+)", result.stderr)
    if not m:
        die(f"could not fetch {vcs}+{url}@{rev}:\n{result.stderr}")
    return m.group(1)


def git_repository(url, commit, tmp):
    """The download entry and the served repositories of a git repository
    at `commit`, with its submodules, recursively.

    The entry's hash covers only the plain checkout, which is reproducible.
    Its `commitObject` holds the raw commit object (`object`, base64) and the
    submodule commits its tree points at (`gitlinks`), from which the offline
    install rebuilds a shallow .git whose commit hashes to `commit`."""
    directory = tempfile.mkdtemp(dir=tmp)

    def git(*args):
        return subprocess.run(["git", *args], cwd=directory, capture_output=True, check=True).stdout

    git("init", "-q")
    git("fetch", "-q", "--depth", "1", url, commit)
    raw = git("cat-file", "commit", commit)
    gitlinks = []
    for line in git("ls-tree", "-r", commit).decode().splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if mode == "160000":
            gitlinks.append({"path": path, "rev": sha})
    entries, served = [], []
    if gitlinks:
        config = git("config", "--blob", f"{commit}:.gitmodules", "--get-regexp", r"^submodule\..*\.(path|url)$").decode()
        names = {}
        for line in config.splitlines():
            key, value = line.split(" ", 1)
            name, field = key[len("submodule.") :].rsplit(".", 1)
            names.setdefault(name, {})[field] = value
        urls = {v["path"]: urllib.parse.urljoin(url.rstrip("/") + "/", v["url"]) for v in names.values()}
        for link in gitlinks:
            link["url"] = urls[link["path"]]
            sub_entries, sub_served = git_repository(link["url"], link["rev"], tmp)
            entries += sub_entries
            served += sub_served
    log(f"prefetching the repository {url}@{commit}")
    entry = checkout_artifact("git", strip_credentials(url), commit, submodules=False)
    entry["path"] += "-repository"
    entry["git"].update(
        leaveDotGit=True,
        commitObject={
            "object": base64.b64encode(raw).decode(),
            "gitlinks": [{"path": link["path"], "rev": link["rev"]} for link in gitlinks],
        },
    )
    entries.append(entry)
    served.append({"url": url, "path": entry["path"]})
    return entries, served


def hg_node(url, revision):
    """The full node of a revision of a Mercurial repository (its tip when
    no revision is given)."""
    result = subprocess.run(
        ["hg", "identify", "--debug", "--id", "--rev", revision or "tip", url], capture_output=True, text=True
    )
    if result.returncode != 0:
        die(f"could not resolve {revision or 'tip'} of {url}:\n{result.stderr}")
    return result.stdout.split()[0].rstrip("+")


def checkout_artifact(vcs, url, rev, submodules):
    """A download entry of the nix-purifiers/purifier library for a
    checkout."""
    path = f"vcs/{re.sub(r'[^A-Za-z0-9._-]+', '-', url.rstrip('/').split('/')[-1])}-{rev[:12]}"
    fields = {"url": url, "rev": rev}
    # The checkout pip makes: git and Mercurial with their submodules and
    # subrepositories, Subversion with its externals and expanded keywords.
    if vcs == "git":
        fields.update(submodules=submodules, leaveDotGit=False, commitObject=None)
    elif vcs == "hg":
        fields.update(subrepos=True)
    elif vcs == "svn":
        fields.update(ignoreExternals=False, ignoreKeywords=False)
    return {"path": path, vcs: fields, "hash": prefetch(vcs, url, rev, submodules)}


# Inputs.


def default_args(project, pylock=None):
    if pylock:
        return ["-r", "pylock.toml"]
    for name in ("pylock.toml", "requirements.txt"):
        if os.path.exists(os.path.join(project, name)):
            return ["-r", name]
    if any(os.path.exists(os.path.join(project, n)) for n in ("pyproject.toml", "setup.py")):
        return ["."]
    die("found no pylock.toml, requirements.txt, pyproject.toml or setup.py; give the pip install arguments after `--`")


def input_path(project, rel, pylock=None):
    """The file that an input of the lockfile names: the project's file, or
    a pylock.toml kept outside the project."""
    if pylock and rel == "pylock.toml":
        return pylock
    return os.path.join(project, rel)


def hash_inputs(project, inputs, pylock=None):
    out = {}
    for rel in sorted(inputs):
        with open(input_path(project, rel, pylock), "rb") as f:
            out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


# The top-level fields of a lockfile. nix/lib/read-lock.nix requires the
# same.
LOCK_FIELDS = ["version", "hash", "pip", "python", "args", "inputs", "pins", "versions", "outputs", "sources", "artifacts"]


def lock_hash(lock):
    """The hash a lockfile holds of its own content: the SHA-256, in SRI
    form, of the lockfile without `hash` as compact JSON with sorted keys.
    Nix computes the same with builtins.toJSON (nix/lib/read-lock.nix)."""
    content = {k: v for k, v in lock.items() if k != "hash"}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sri(hashlib.sha256(text.encode()).digest())


def lockfile_path(ns, project):
    """The lockfile of a command: from --lockfile, else from
    PIP_PURIFIER_LOCKFILE, else pip-purifier.lock.json in the project."""
    given = ns.lockfile or os.environ.get("PIP_PURIFIER_LOCKFILE")
    return os.path.abspath(given) if given else os.path.join(project, LOCKFILE)


def tool_lockfile_path(ns):
    """A pylock.toml kept outside the project, from --tool-lockfile or
    PIP_PURIFIER_TOOL_LOCKFILE, or None for the project's own."""
    given = ns.tool_lockfile or os.environ.get("PIP_PURIFIER_TOOL_LOCKFILE")
    return os.path.abspath(given) if given else None


def read_lock(project, path, pylock=None):
    """The lockfile at `path`, for the project directory `project`, or an
    exit with what is wrong with it and the command that fixes it."""
    if not os.path.exists(path):
        die(f"{path} does not exist. Run `pip-purifier lock`.")
    try:
        with open(path) as f:
            lock = json.load(f)
    except ValueError as error:
        die(f"{path} is not valid JSON ({error}). Run `pip-purifier lock`.")
    missing = [field for field in LOCK_FIELDS if field not in lock] if isinstance(lock, dict) else LOCK_FIELDS
    if missing:
        die(f"{path} is not a pip-purifier lockfile: it lacks {', '.join(missing)}. Run `pip-purifier lock`.")
    if lock["version"] != FORMAT_VERSION:
        die(f"{path} has lock format version {lock['version']}; this pip-purifier reads version {FORMAT_VERSION}. Run `pip-purifier lock`.")
    if lock["hash"] != lock_hash(lock):
        die(f"{path} was changed after `pip-purifier lock` wrote it: its content does not match its hash. Run `pip-purifier lock`.")
    gone = [rel for rel in lock["inputs"] if not os.path.exists(input_path(project, rel, pylock))]
    if gone or hash_inputs(project, lock["inputs"], pylock) != lock["inputs"]:
        die(f"{path} does not match the project's requirements any more. Run `pip-purifier lock`.")
    return lock


def upstream_indexes(project):
    """The index URLs pip would use: from the environment, else from pip's
    configuration files, else PyPI."""
    config = {}
    result = subprocess.run(PIP + ["config", "list"], capture_output=True, text=True, cwd=project)
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        config[key.split(".", 1)[-1]] = value.strip().strip("'\"")
    index = os.environ.get("PIP_INDEX_URL") or config.get("index-url") or "https://pypi.org/simple/"
    extra = (os.environ.get("PIP_EXTRA_INDEX_URL") or config.get("extra-index-url") or "").split()
    links = (os.environ.get("PIP_FIND_LINKS") or config.get("find-links") or "").split()
    return index, extra, links


def lock_environment(tmp, proxy, index, extra, links):
    # PYTHONPATH from the caller would come ahead of what pip resolves.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_") and k != "PYTHONPATH"}
    no_proxy = ",".join(filter(None, [env.get("no_proxy", ""), "127.0.0.1", "localhost"]))
    env.update(
        HOME=os.path.join(tmp, "home"),
        PIP_CONFIG_FILE=os.devnull,
        PIP_NO_CACHE_DIR="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_INPUT="1",
        PIP_PROGRESS_BAR="off",
        PIP_INDEX_URL=proxy.upstream(index),
        no_proxy=no_proxy,
        NO_PROXY=no_proxy,
    )
    if extra:
        env["PIP_EXTRA_INDEX_URL"] = " ".join(proxy.upstream(u) for u in extra)
    if links:
        env["PIP_FIND_LINKS"] = " ".join(proxy.upstream(u) if pip_inputs.is_url(u) else u for u in links)
    os.makedirs(env["HOME"], exist_ok=True)
    return env


def run_pip(args, env, cwd, check=True):
    log(" ".join(args))
    command = [sys.executable, os.path.join(NIX_LIB, "pip_runner.py"), *args]
    if os.environ.get("PIP_PURIFIER_BINDINGS"):
        env["PIP_PURIFIER_BINDINGS"] = os.environ["PIP_PURIFIER_BINDINGS"]
    result = subprocess.run(command, env=env, cwd=cwd)
    if check and result.returncode != 0:
        die(f"pip failed with exit status {result.returncode}")
    return result.returncode == 0


def report_items(path):
    with open(path) as f:
        return json.load(f).get("install", [])


def pins_of(items, proxy):
    """`name==version` of every item pip installed from an index."""
    pins = []
    for item in items:
        url = item["download_info"]["url"]
        if url.startswith(proxy.base + "/f/") and "archive_info" in item["download_info"]:
            pins.append(f"{normalize(item['metadata']['name'])}=={item['metadata']['version']}")
    return sorted(set(pins))


def previous_files(path):
    """The files of the existing lockfile at `path`, by normalized project
    name, which a relock keeps (Proxy.keep). Empty when there is no usable
    lockfile. Every format version so far has the same `artifacts`, so a
    relock in a new format keeps the files of the old one."""
    try:
        with open(path) as f:
            previous = json.load(f)
        artifacts = previous["artifacts"]
    except (OSError, ValueError, KeyError):
        return {}
    keep = {}
    for artifact in artifacts:
        found = dist_name_version(artifact["path"]) if "url" in artifact else None
        if found:
            keep.setdefault(found[0], set()).add(artifact["path"])
    return keep


def native_lock(ns):
    project = os.getcwd()
    lockfile = lockfile_path(ns, project)
    # A pylock.toml kept outside the project starts as a copy of the
    # project's own.
    pylock = tool_lockfile_path(ns)
    if pylock and not os.path.exists(pylock):
        tree_copy = os.path.join(project, "pylock.toml")
        if not os.path.exists(tree_copy):
            die(f"neither {pylock} nor {tree_copy} exists. Run `pip lock` to write a pylock.toml.")
        os.makedirs(os.path.dirname(pylock), exist_ok=True)
        shutil.copyfile(tree_copy, pylock)
    args = ns.pip_args or default_args(project, pylock)
    python = ns.python or PYTHON
    pyver = subprocess.run(
        [python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], capture_output=True, text=True, check=True
    ).stdout.strip()
    pip_version = subprocess.run(PIP + ["--version"], capture_output=True, text=True, check=True).stdout.split()[1]
    here = current_system()
    systems = list(dict.fromkeys([*(ns.system or DEFAULT_SYSTEMS), here]))
    index, extra, links = upstream_indexes(project)

    with tempfile.TemporaryDirectory() as tmp, Proxy() as proxy:
        env = lock_environment(tmp, proxy, index, extra, links)
        configuration = os.environ.get("PIP_PURIFIER_BINDINGS")
        provided = json.loads(Path(configuration).read_text())["modules"] if configuration else {}
        rewriter = pip_inputs.Rewriter(project, os.path.join(tmp, "inputs"), "lock",
                                      index=proxy.upstream, pylock=pylock, provided=provided)
        lock_args = rewriter.args(args)
        # A pylock.toml pins every version, and does not record the
        # requirements `pip lock` resolved it from.
        pylocks = [rel for rel in rewriter.inputs if re.fullmatch(r"pylock(\.[^/]+)?\.toml", os.path.basename(rel))]
        if ns.update is not None and pylocks:
            die(f"{', '.join(pylocks)} pins the versions. Run `pip lock` with the project's requirements to update it, then `pip-purifier lock`.")

        # The real install, on this system: pip resolves, downloads and
        # builds every sdist, so the files of the build environments pass
        # through the proxy too.
        venv = os.path.join(tmp, "venv")
        subprocess.run([python, "-m", "venv", "--without-pip", venv], check=True)
        report = os.path.join(tmp, "report.json")
        install = ["--python", os.path.join(venv, "bin", "python"), "install", "--report", report, *lock_args]

        # A relock keeps the files of the existing lockfile, for the
        # requirements and for every build environment: the proxy lists
        # only those files for a project they cover. `update` keeps none,
        # or none of the packages it names.
        proxy.keep = previous_files(lockfile)
        if ns.update is not None:
            names = {normalize(name) for name in ns.update}
            proxy.keep = {name: files for name, files in proxy.keep.items() if names and name not in names}
        if proxy.keep and not run_pip(install, env, project, check=False):
            log("the requirements need versions the existing lockfile does not have; resolving afresh")
            proxy.keep = {}
            proxy.files.clear()
            shutil.rmtree(venv)
            subprocess.run([python, "-m", "venv", "--without-pip", venv], check=True)
        if not proxy.keep:
            run_pip(install, env, project)
        items = report_items(report)
        pins = {here: pins_of(items, proxy)}

        # The versions of packages built from a local directory, by the
        # directory's path, and from a checkout, by its requirement URL. The
        # offline install tags a git repository with the version for
        # setuptools-scm and hatch-vcs, because the copies it builds from
        # have no VCS metadata of their own.
        versions = {}
        for item in items:
            info = item["download_info"]
            if "dir_info" in info:
                path = urllib.parse.unquote(urllib.parse.urlsplit(info["url"]).path)
                versions[os.path.relpath(path, project)] = item["metadata"]["version"]

        # The outputs, which Nix names at evaluation time from the lockfile:
        # the programs the environment installs, and the wheel and sdist of
        # every local project inside this one. A wheel with a platform tag
        # is named for each system by its CPU.
        # The interpreter entries of a venv (python, python3.14 and, from
        # Python 3.14, 𝜋thon) are symlinks; the scripts pip installs are
        # files.
        bin_dir = os.path.join(venv, "bin")
        programs = sorted(
            name
            for name in os.listdir(bin_dir)
            if not os.path.islink(os.path.join(bin_dir, name))
            and not re.match(r"^(activate.*|Activate\.ps1)$", name)
        )
        packages = {}
        for rel in rewriter.dirs:
            directory = os.path.join(project, rel)
            if rel.startswith("..") or not any(os.path.exists(os.path.join(directory, n)) for n in ("pyproject.toml", "setup.py")):
                continue
            dist = os.path.join(tmp, "dist", rel.replace(os.sep, "_"))
            run_pip(["--python", os.path.join(venv, "bin", "python"), "wheel", "--no-deps", "--wheel-dir", dist, directory], env, project)
            subprocess.run([sys.executable, "-m", "build", "--sdist", "--outdir", dist, directory], env=env, cwd=project, check=True)
            names = os.listdir(dist)
            wheel = next(n for n in names if n.endswith(".whl"))
            sdist = next(n for n in names if not n.endswith(".whl"))
            name, version = dist_name_version(wheel)
            wheels = {}
            for system in dict.fromkeys([*DEFAULT_SYSTEMS, *systems, here]):
                arch = SYSTEMS[system]
                tags = wheel_tags(wheel)
                platforms = [re.sub(r"(x86_64|aarch64)$", arch, p) for p in tags[2]]
                wheels[system] = "-".join(wheel[:-4].split("-")[:-1] + [".".join(platforms)]) + ".whl"
            packages[name] = {"path": rel, "version": version, "sdist": sdist, "wheels": wheels}

        # VCS and URL requirements.
        artifacts = {}
        sources = {}
        by_name = {normalize(i["metadata"]["name"]): i for i in items}
        # The files of a pylock.toml, which readLock (nix/lib/read-lock.nix)
        # reads from the pylock.toml itself; `artifacts` holds the others.
        in_pylock = set()
        for spec in rewriter.specs:
            url = spec["url"]
            if spec["kind"] == "file":
                if not spec.get("hashes", {}).get("sha256"):
                    die(f"{url} in a pylock.toml has no sha256 hash")
                in_pylock.add(os.path.basename(pip_inputs.url_without_fragment(url)))
            elif spec["kind"] == "url":
                digest = hash_url(pip_inputs.url_without_fragment(url))
                name = file_name(pip_inputs.url_without_fragment(url))
                artifacts[name] = {"path": name, "url": strip_credentials(pip_inputs.url_without_fragment(url)), "mirrors": [], "hash": sri(digest)}
                sources[url] = name
            else:
                vcs, repo, rev, params = pip_inputs.split_vcs_url(url)
                item = by_name.get(normalize(spec["name"] or params.get("egg", "")))
                if item is None:
                    for candidate in items:
                        if candidate["download_info"].get("url", "").rstrip("/") == repo.rstrip("/"):
                            item = candidate
                if item is None or "vcs_info" not in item["download_info"]:
                    die(f"pip did not report what it installed from {url}")
                commit = item["download_info"]["vcs_info"]["commit_id"]
                if vcs == "hg":
                    # pip records Mercurial's local revision number. Lock the
                    # node.
                    commit = hg_node(repo, rev)
                log(f"prefetching {vcs}+{repo}@{commit}")
                entry = checkout_artifact(vcs, strip_credentials(repo), commit, submodules=True)
                artifacts[entry["path"]] = entry
                sources[url] = entry["path"]
                versions[url] = item["metadata"]["version"]

        # A git repository that another package names as a source in its own
        # metadata. pip clones it itself, so the offline install points git
        # at a local clone of the repository (url.<clone>.insteadOf), which
        # holds the locked commit under the revision pip asks for.
        wanted = {normalize(s["name"]) for s in rewriter.specs if s.get("name")}
        repositories = []
        replays = []
        shims = []
        for item in items:
            info = item["download_info"]
            name = normalize(item["metadata"]["name"])
            if name in wanted:
                continue
            if "vcs_info" in info:
                vcs_info = info["vcs_info"]
                commit = vcs_info["commit_id"]
                if vcs_info["vcs"] == "hg":
                    # pip records Mercurial's local revision number, which
                    # only means something in one clone. Lock the node.
                    commit = hg_node(info["url"], vcs_info.get("requested_revision"))
                if vcs_info["vcs"] != "git":
                    # pip clones it with hg, svn or bzr, which have no
                    # setting that serves a repository from a local copy, so
                    # the offline install answers with shims (vcs_shim.py)
                    # from the locked checkout.
                    log(f"prefetching the {vcs_info['vcs']} repository {info['url']}@{commit}")
                    entry = checkout_artifact(vcs_info["vcs"], strip_credentials(info["url"]), commit, submodules=False)
                    artifacts[entry["path"]] = entry
                    shims.append(
                        {"vcs": vcs_info["vcs"], "url": info["url"], "rev": commit, "version": item["metadata"]["version"], "path": entry["path"]}
                    )
                    continue
                entries, served = git_repository(info["url"], commit, tmp)
                for entry in entries:
                    artifacts[entry["path"]] = entry
                # The repository pip asked for carries the revision it asked
                # by and the version; its submodules are served at their
                # commits.
                served[-1].update(revision=vcs_info.get("requested_revision") or commit, version=item["metadata"]["version"])
                repositories += served
            elif "archive_info" in info and not info["url"].startswith((proxy.base, "file:")) and info["url"] not in sources:
                # A URL that another package names as a source. pip
                # downloads it itself, so the offline install answers the
                # download from a replaying HTTPS proxy (mitm-cache).
                url = info["url"]
                digest = hash_url(url)
                name = file_name(url)
                artifacts[name] = {"path": name, "url": strip_credentials(url), "mirrors": [], "hash": sri(digest)}
                replays.append({"url": strip_credentials(url), "path": name})

        # The other systems. pip resolves for each system's wheels; a project
        # that needs an sdist pip may not build for another platform keeps
        # this system's versions.
        main_files = set(proxy.files)
        for system in systems:
            if system == here:
                continue
            arch = SYSTEMS[system]
            report = os.path.join(tmp, f"report-{system}.json")
            ok = run_pip(
                ["install", "--dry-run", "--ignore-installed", "--target", os.path.join(tmp, "t-" + system), "--report", report, *platform_args(arch, pyver), *lock_args],
                env,
                project,
                check=False,
            )
            pins[system] = pins_of(report_items(report), proxy) if ok else pins[here]
            if not ok:
                log(f"keeping the versions of {here} for {system}")

        # Every package version the builds use, for every system: the
        # resolved versions, and the build requirements this system's build
        # environments installed. A version with no wheel for a system's CPU
        # gets one if the index has it, and its sdist otherwise.
        built = {dist_name_version(n) for n in main_files} - {None}
        for system in systems:
            arch = SYSTEMS[system]
            needed = {tuple(p.split("==")) for p in pins[system]} | built
            for name, version in sorted(needed):
                files = [n for n in proxy.files if dist_name_version(n) == (name, version)]
                if any(not f.endswith(".whl") or wheel_runs_on(f, arch, pyver) for f in files):
                    continue
                dl = os.path.join(tmp, "dl")
                spec = f"{name}=={version}"
                if not run_pip(["download", "--no-deps", "--dest", dl, *platform_args(arch, pyver), spec], env, project, check=False):
                    run_pip(["download", "--no-deps", "--no-binary=:all:", "--dest", dl, spec], env, project)

        if proxy.errors:
            die("the index proxy failed:\n" + "\n".join(proxy.errors))

        for entry in proxy.files.values():
            artifacts.setdefault(entry["path"], entry)
        for name in in_pylock:
            artifacts.pop(name, None)

        lock = {
            "version": FORMAT_VERSION,
            "pip": pip_version,
            "python": pyver,
            "args": args,
            "inputs": hash_inputs(project, rewriter.inputs, pylock),
            "pins": pins,
            "versions": dict(sorted(versions.items())),
            "outputs": {"programs": programs, "packages": packages},
            "sources": dict(sorted(sources.items())),
            "repositories": sorted(repositories, key=lambda r: r["url"]),
            "replays": sorted(replays, key=lambda r: r["url"]),
            "shims": sorted(shims, key=lambda r: r["url"]),
            "artifacts": sorted(artifacts.values(), key=lambda a: a["path"]),
        }
        lock["hash"] = lock_hash(lock)
    # Sorted keys, so that the lockfile does not depend on which system the
    # lock ran on.
    with open(lockfile, "w") as f:
        json.dump(lock, f, indent=2, sort_keys=True)
        f.write("\n")
    log(f"wrote {lockfile} with {len(lock['artifacts'])} downloads")


def lock(ns):
    project = os.getcwd()
    lockfile = lockfile_path(ns, project)
    try:
        previous = json.loads(Path(lockfile).read_text()).get("projectInputs", [])
    except (OSError, ValueError):
        previous = []
    references = getattr(ns, "input", None)
    if references is None:
        references = [entry["reference"] for entry in previous]
    if not references:
        return native_lock(ns)
    import project_inputs
    records = project_inputs.pin(references, previous, refresh=ns.update is not None)
    views = project_inputs.python_views(records, os.path.join(roots(lockfile), "project-inputs"))
    configuration = python_bindings.write(views, [], Path(roots(lockfile)) / "bindings", python=ns.python or PYTHON) if views else None
    previous_environment = os.environ.get("PIP_PURIFIER_BINDINGS")
    try:
        if configuration:
            os.environ["PIP_PURIFIER_BINDINGS"] = str(configuration)
        native_lock(ns)
    finally:
        if previous_environment is None:
            os.environ.pop("PIP_PURIFIER_BINDINGS", None)
        else:
            os.environ["PIP_PURIFIER_BINDINGS"] = previous_environment
    if records:
        data = json.loads(Path(lockfile).read_text())
        data["projectInputs"] = records
        data["projectInputNames"] = sorted(json.loads(Path(configuration).read_text())["modules"])
        data["hash"] = lock_hash(data)
        with open(lockfile, "w") as destination:
            json.dump(data, destination, indent=2, sort_keys=True)
            destination.write("\n")


def update(ns):
    """`lock` with the `pip install` arguments that the existing lockfile
    records, keeping none of the files of the named packages, or none at
    all."""
    try:
        with open(lockfile_path(ns, os.getcwd())) as f:
            ns.pip_args = json.load(f)["args"]
    except (OSError, ValueError, KeyError, TypeError):
        ns.pip_args = []
    lock(ns)


# Nix.


def nix_build(project, lockfile, attr, out_link=None, pylock=None):
    """Builds the output `attr` of buildPipProject (nix/lib/cli.nix) for the
    project, after checking its lockfile."""
    read_lock(project, lockfile, pylock)
    command = [
        "nix-build",
        os.path.join(NIX_LIB, "cli.nix"),
        "--arg",
        "nixpkgs",
        NIXPKGS,
        "--argstr",
        "src",
        project,
        "--argstr",
        "lockFile",
        lockfile,
        "--argstr",
        "pylock",
        pylock or "",
        "--argstr",
        "attr",
        attr,
    ]
    command += ["--out-link", out_link] if out_link else ["--no-out-link"]
    result = subprocess.run(command, stdout=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        die("nix-build failed")
    return result.stdout.strip().splitlines()[-1]


def roots(lockfile):
    """The directory of the garbage-collector roots, .pip-purifier/ beside
    the lockfile."""
    root = os.path.join(os.path.dirname(lockfile), ".pip-purifier")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, ".gitignore"), "w") as f:
        f.write("*\n")
    return root


def inputs_of(project, lockfile, pylock=None):
    """The project's offline inputs, kept alive by a garbage-collector root
    under .pip-purifier/."""
    return nix_build(project, lockfile, "inputs", out_link=os.path.join(roots(lockfile), "inputs"), pylock=pylock)


def prefetch_command(ns):
    """Realizes what the development loop and the editor need, and installs
    the project into a virtual environment offline: the locked downloads
    for this system, and this command with its Python, pip, nixpkgs and
    purifier library, each kept alive by a root under .pip-purifier/."""
    project = os.getcwd()
    lockfile = lockfile_path(ns, project)
    pylock = tool_lockfile_path(ns)
    if ns.inputs:
        inputs = ns.inputs
    else:
        inputs = inputs_of(project, lockfile, pylock)
        # This command's package holds the Python of the virtual
        # environment, the pip that installs offline, and the nixpkgs and
        # purifier library that `pip` builds the inputs with.
        package = os.path.dirname(os.path.dirname(os.path.dirname(NIX_LIB)))
        subprocess.run(
            ["nix-store", "--add-root", os.path.join(roots(lockfile), "pip-purifier"), "--realise", package],
            stdout=subprocess.DEVNULL,
            check=True,
        )
    venv = os.path.abspath(ns.venv)
    if not os.path.exists(os.path.join(venv, "bin", "python")):
        subprocess.run([PYTHON, "-m", "venv", "--without-pip", venv], check=True)
    offline.install(PIP, project, inputs, venv, editable=True, extra=ns.pip_args, pylock=pylock)


def pip(ns):
    project = os.getcwd()
    lockfile = lockfile_path(ns, project)
    if not os.path.exists(lockfile):
        log("warning: no lockfile; pip is resolving dependencies impurely")
        raise SystemExit(subprocess.run(PIP + ns.pip_args).returncode)
    inputs = inputs_of(project, lockfile, tool_lockfile_path(ns))
    env = offline.environment(inputs)
    env["PIP_CONSTRAINT"] = os.path.join(inputs, "pins.txt")
    offline.run_pip(PIP + ns.pip_args, project, env, stdout=True)


def publish(ns):
    project = os.getcwd()
    lockfile = lockfile_path(ns, project)
    pylock = tool_lockfile_path(ns)
    packages = list(read_lock(project, lockfile, pylock)["outputs"]["packages"])
    if ns.package and ns.package not in packages:
        die(f"the project has no package {ns.package}; its packages are: {', '.join(packages) or 'none'}")
    out = nix_build(project, lockfile, f"packages.{ns.package}" if ns.package else "release", pylock=pylock)
    files = sorted(os.path.join(out, n) for n in os.listdir(out)
                   if n.endswith((".whl", ".tar.gz", ".zip")) and os.path.isfile(os.path.join(out, n)))
    if not files:
        die(f"{out} holds no distributions")
    # uv publish, because twine refuses the metadata 2.5 that current
    # build backends write.
    os.execvp("uv", ["uv", "publish", *(["--dry-run"] if ns.dry_run else []), *ns.uv_args, *files])


def main():
    parser = argparse.ArgumentParser(prog="pip-purifier", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    lockfile_help = "the lockfile to write or read (default: pip-purifier.lock.json in the current directory)"
    tool_lockfile_help = "a pylock.toml kept outside the project, read in place of pylock.toml in the project (default: PIP_PURIFIER_TOOL_LOCKFILE)"
    system_help = f"a Nix system to lock for (default: {', '.join(DEFAULT_SYSTEMS)} and the system of this machine)"

    p = commands.add_parser("lock", help="write pip-purifier.lock.json")
    p.add_argument("--lockfile", help=lockfile_help)
    p.add_argument("--tool-lockfile", help=tool_lockfile_help)
    p.add_argument("--input", action="append", help="a Nix producer exposing projections.python")
    p.add_argument("--python", help="the Python interpreter pip installs for (default: the Python of this package set)")
    p.add_argument("--system", action="append", choices=list(SYSTEMS), help=system_help)
    p.add_argument("pip_args", nargs="*", metavar="-- PIP-ARG", help="the arguments of `pip install` (default: -r pylock.toml, -r requirements.txt, or .)")
    p.set_defaults(run=lock, update=None)

    p = commands.add_parser(
        "update",
        help="resolve the named packages afresh, or every package, and write pip-purifier.lock.json",
    )
    p.add_argument("--lockfile", help=lockfile_help)
    p.add_argument("--tool-lockfile", help=tool_lockfile_help)
    p.add_argument("--input", action="append", help="a Nix producer exposing projections.python")
    p.add_argument("--python", help="the Python interpreter pip installs for (default: the Python of this package set)")
    p.add_argument("--system", action="append", choices=list(SYSTEMS), help=system_help)
    p.add_argument("update", nargs="*", metavar="PACKAGE", help="a package to update (default: every package)")
    p.set_defaults(run=update)

    p = commands.add_parser(
        "prefetch",
        help="realize the locked downloads and this command with garbage-collector roots, and install into a virtual environment offline",
    )
    p.add_argument("--lockfile", help=lockfile_help)
    p.add_argument("--tool-lockfile", help=tool_lockfile_help)
    p.add_argument("--venv", default=".venv")
    p.add_argument("--inputs", help="the inputs Nix built (default: build them with nix-build)")
    p.add_argument("pip_args", nargs="*", metavar="-- PIP-ARG", help="more arguments for `pip install`")
    p.set_defaults(run=prefetch_command)

    p = commands.add_parser(
        "pip",
        help="run pip offline on the locked files; pass --python .venv/bin/python to act on a virtual environment",
    )
    p.add_argument("--lockfile", help=lockfile_help)
    p.add_argument("--tool-lockfile", help=tool_lockfile_help)
    p.add_argument("pip_args", nargs=argparse.REMAINDER)
    p.set_defaults(run=pip)

    p = commands.add_parser("publish", help="upload the release Nix built, with `uv publish`")
    p.add_argument("--lockfile", help=lockfile_help)
    p.add_argument("--tool-lockfile", help=tool_lockfile_help)
    p.add_argument("--package", help="publish only this local project (default: every one)")
    p.add_argument("--dry-run", action="store_true", help="check the upload without making it")
    p.add_argument("uv_args", nargs="*", metavar="-- UV-PUBLISH-ARG")
    p.set_defaults(run=publish)

    # Everything after `pip` and its own --lockfile goes to pip unparsed,
    # options included.
    if sys.argv[1:2] == ["pip"] and sys.argv[2:3] not in (["-h"], ["--help"]):
        rest = sys.argv[2:]
        own = {"--lockfile": None, "--tool-lockfile": None}
        while rest:
            option, equals, value = rest[0].partition("=")
            if option not in own:
                break
            if equals:
                own[option], rest = value, rest[1:]
            elif len(rest) > 1:
                own[option], rest = rest[1], rest[2:]
            else:
                break
        pip(argparse.Namespace(lockfile=own["--lockfile"], tool_lockfile=own["--tool-lockfile"], pip_args=rest))
        return
    ns = parser.parse_args()
    ns.run(ns)


if __name__ == "__main__":
    main()
