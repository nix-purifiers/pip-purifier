# SPDX-License-Identifier: MIT
"""Reads and rewrites the inputs of a pip install.

A pip install is described by its arguments: requirement specifiers, local
project paths, requirements files (`-r`), constraints files (`-c`),
pylock.toml files (also `-r`), PEP 723 scripts (`--requirements-from-script`)
and index options. `Rewriter` walks all of them, follows `-r` and `-c` into
nested files, and writes rewritten copies into an output directory.

The lock run (src/pip_purifier.py) rewrites in "lock" mode: index URLs point
at the recording proxy, and everything else stays as written. The Nix build
and the interactive mode rewrite in "offline" mode: index options go away,
because pip then reads only the directory of locked files, and every VCS or
URL requirement points at the local copy the lockfile names.

Both modes record what they read: the input files (`inputs`), the VCS and URL
requirements (`specs`) and the local project directories (`dirs`).
"""

import os
import hashlib
from pathlib import Path
import re
import shlex
import tomllib

VCS_SCHEMES = ("git", "hg", "svn", "bzr")

# `name[extras] @ url ; marker`
NAMED_URL = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*(?:\s*\[[^\]]*\])?)\s*@\s*(\S+)(.*)$")

# The options of a requirements file that take a value.
FILE_OPTIONS_WITH_VALUE = {
    "-r": "requirement",
    "--requirement": "requirement",
    "-c": "constraint",
    "--constraint": "constraint",
    "-e": "editable",
    "--editable": "editable",
    "-i": "index",
    "--index-url": "index",
    "--extra-index-url": "extra-index",
    "-f": "find-links",
    "--find-links": "find-links",
    "--trusted-host": "trusted-host",
    "--only-binary": "keep",
    "--no-binary": "keep",
    "--hash": "keep",
    "--config-settings": "keep",
    "-C": "keep",
    "--global-option": "keep",
}

# The options of a pip install command line that take a value and name an
# input.
ARG_OPTIONS_WITH_VALUE = dict(
    FILE_OPTIONS_WITH_VALUE,
    **{
        "--requirements-from-script": "script",
        "--build-constraint": "constraint",
        "--python-version": "keep",
        "--platform": "keep",
        "--abi": "keep",
        "--implementation": "keep",
        "--extra": "keep",
        "--group": "group",
    },
)


def vcs_scheme(url):
    """The VCS of a pip VCS URL such as git+https://..., or None."""
    m = re.match(r"^(git|hg|svn|bzr)\+", url)
    return m.group(1) if m else None


def is_url(text):
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text))


def split_vcs_url(url):
    """Splits git+https://host/repo@rev#frag into (vcs, repo URL, rev,
    fragment parameters)."""
    vcs = vcs_scheme(url)
    rest = url[len(vcs) + 1 :]
    fragment = ""
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
    rev = None
    # The revision follows the last "@" of the path, not one in the
    # authority (user@host).
    scheme_end = rest.find("://") + 3
    path_start = rest.find("/", scheme_end)
    if path_start >= 0 and "@" in rest[path_start:]:
        at = rest.rindex("@")
        rest, rev = rest[:at], rest[at + 1 :]
    return vcs, rest, rev, fragment_params(fragment)


def fragment_params(fragment):
    """The `key=value` parameters of a URL fragment such as
    `egg=name&subdirectory=dir`."""
    params = {}
    for part in fragment.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            params[key] = value
    return params


def parse_spec(spec):
    """Classifies a requirement specifier: a named URL (`name @ url`), a bare
    URL, a local path, or an index requirement."""
    spec = spec.strip()
    m = NAMED_URL.match(spec)
    if m and (is_url(m.group(2)) or vcs_scheme(m.group(2))):
        name, url, rest = m.group(1), m.group(2), m.group(3)
        return {"kind": "url", "name": re.sub(r"\s+", "", name), "url": url, "rest": rest}
    first = spec.split(";", 1)[0].strip()
    token = first.split()[0] if first else ""
    if vcs_scheme(token) or is_url(token):
        params = fragment_params(token.split("#", 1)[1]) if "#" in token else {}
        return {"kind": "url", "name": params.get("egg"), "url": token, "rest": spec[spec.index(token) + len(token) :]}
    if token.startswith((".", "/", "~")) or os.sep in token.split("[")[0]:
        return {"kind": "path", "path": token, "rest": spec[spec.index(token) + len(token) :]}
    return {"kind": "index", "spec": spec}


def url_without_fragment(url):
    return url.split("#", 1)[0]


def logical_lines(text):
    """The logical lines of a requirements file: continuation lines joined
    and comments removed."""
    joined = re.sub(r"\\\r?\n", "", text)
    for line in joined.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if line:
            yield line


def script_metadata(text):
    """The PEP 723 `script` block of a Python script, as (start, end, table),
    where start and end are line indexes of the block's first and last
    line."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.match(r"^# /// script\s*$", line):
            for j in range(i + 1, len(lines)):
                if re.match(r"^# ///\s*$", lines[j]):
                    body = "".join(
                        (l[2:] if l.startswith("# ") else l[1:]) for l in lines[i + 1 : j]
                    )
                    return i, j, tomllib.loads(body)
            raise ValueError("the script block has no closing `# ///` line")
    return None


def toml_value(value):
    """A TOML value, written inline."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"cannot write {value!r} as TOML")


def toml_key(key):
    return key if re.match(r"^[A-Za-z0-9_-]+$", key) else toml_value(key)


def write_pylock(data):
    """A pylock.toml document: the top-level keys, then one [[packages]]
    table per package, with every nested value written inline."""
    out = []
    for key, value in data.items():
        if key != "packages":
            out.append(f"{toml_key(key)} = {toml_value(value)}")
    for package in data.get("packages", []):
        out.append("")
        out.append("[[packages]]")
        for key, value in package.items():
            out.append(f"{toml_key(key)} = {toml_value(value)}")
    return "\n".join(out) + "\n"


class Rewriter:
    """Rewrites the inputs of a pip install.

    project: the directory pip runs in; relative paths resolve against it.
    out: the directory that receives the rewritten files.
    mode: "lock" or "offline".
    index: in lock mode, a function from an index or find-links URL to the
      URL pip should use instead.
    local: in offline mode, a function from (kind, url, name) to the local
      path that replaces a VCS or URL requirement, where kind is "vcs",
      "url" or "file" (a wheel or sdist URL inside a pylock.toml).
    editable: in offline mode, whether editable installs stay editable.
    pylock: a pylock.toml kept outside the project, which is read in place
      of the project's pylock.toml.
    """

    def __init__(self, project, out, mode, index=None, local=None, editable=True, pylock=None, provided=None):
        self.project = os.path.abspath(project)
        self.pylock_path = pylock
        self.provided = provided or {}
        self.out = os.path.abspath(out)
        self.mode = mode
        self.index = index
        self.local = local
        self.editable = editable
        self.inputs = []
        self.specs = []
        self.dirs = []
        self.written = {}
        self.group_files = 0
        # The versions that a hashed requirement or a pylock.toml pins, by
        # normalized name, which constrain the build environments too.
        self.pinned = {}

    # Paths.

    def relative(self, path):
        return os.path.relpath(os.path.abspath(path), self.project)

    def inside(self, path):
        rel = self.relative(path)
        return not (rel == ".." or rel.startswith(".." + os.sep))

    def output_path(self, path):
        if self.inside(path):
            return os.path.join(self.out, self.relative(path))
        return os.path.join(self.out, "_outside", str(len(self.written)) + "-" + os.path.basename(path))

    def record_input(self, path):
        rel = self.relative(path)
        if rel not in self.inputs:
            self.inputs.append(rel)

    def local_path(self, path, base=None):
        """A local path made absolute against the project (or `base`)."""
        path = os.path.expanduser(path)
        if path.startswith("file:"):
            path = re.sub(r"^file:(//)?", "", path)
        return os.path.normpath(os.path.join(base or self.project, path))

    def record_dir(self, path):
        if os.path.isdir(path):
            rel = self.relative(path)
            if rel not in self.dirs:
                self.dirs.append(rel)
            for name in ("pyproject.toml", "setup.py", "setup.cfg"):
                if os.path.exists(os.path.join(path, name)):
                    self.record_input(os.path.join(path, name))

    # Requirement specifiers.

    def spec(self, spec, editable=False):
        """A rewritten requirement specifier (without `-e`), and whether it
        stays editable."""
        parsed = parse_spec(spec)
        name = parsed.get("name")
        module = self.provided.get(re.sub(r"[-_.]+", "-", name.split("[")[0].lower())) if name else None
        if module:
            return name + " @ " + Path(module['wheel']).as_uri() + parsed.get("rest", ""), False
        if parsed["kind"] == "index":
            return spec, editable
        if parsed["kind"] == "path":
            path = self.local_path(parsed["path"])
            self.record_dir(path)
            return path + parsed["rest"], editable and (self.mode == "lock" or self.editable)
        url = parsed["url"]
        vcs = vcs_scheme(url)
        if url.startswith("file:"):
            path = self.local_path(url_without_fragment(url))
            self.record_dir(path)
            target = path
        else:
            kind = "vcs" if vcs else "url"
            self.specs.append({"kind": kind, "url": url, "name": parsed["name"]})
            if self.mode == "lock":
                return spec, editable
            target = self.local(kind, url, parsed["name"])
            if vcs:
                _, _, _, params = split_vcs_url(url)
                if "subdirectory" in params:
                    target = os.path.join(target, params["subdirectory"])
        keep_editable = editable and self.mode == "offline" and self.editable and not (
            target.startswith("/nix/store/")
        )
        if parsed["name"] and not url.startswith("file:"):
            return f"{parsed['name']} @ file://{target}{parsed['rest']}", keep_editable
        return target + parsed["rest"], keep_editable

    # Index options.

    def index_option(self, option, value, base):
        """The rewritten form of an index option, as a list of tokens."""
        kind = ARG_OPTIONS_WITH_VALUE[option]
        if kind == "find-links" and not is_url(value):
            return [option, self.local_path(value, base)]
        if self.mode == "lock":
            if kind in ("index", "extra-index", "find-links"):
                return [option, self.index(value)]
            return []  # trusted-host: the proxy is plain HTTP on localhost
        return []

    # Files.

    def requirements_file(self, path, constraint=False):
        """Rewrites a requirements or constraints file, or a pylock.toml, and
        returns the path of the rewritten copy."""
        path = os.path.abspath(path)
        if path in self.written:
            return self.written[path]
        if os.path.basename(path) == "pylock.toml" or re.match(r"^pylock\.[^.]+\.toml$", os.path.basename(path)):
            return self.pylock(path)
        self.record_input(path)
        target = self.output_path(path)
        self.written[path] = target
        base = os.path.dirname(path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        out = []
        for line in logical_lines(text):
            out.append(self.requirements_line(line, base))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n".join(l for l in out if l) + "\n")
        return target

    def requirements_line(self, line, base):
        tokens = shlex.split(line, posix=True)
        option = tokens[0]
        if "=" in option and option.startswith("--"):
            name, value = option.split("=", 1)
            tokens = [name, value] + tokens[1:]
            option = name
        elif re.match(r"^-[rcefi]\S", option):
            tokens = [option[:2], option[2:]] + tokens[1:]
            option = option[:2]
        if option.startswith("-"):
            kind = FILE_OPTIONS_WITH_VALUE.get(option)
            if kind in ("requirement", "constraint"):
                nested = os.path.join(base, tokens[1]) if not is_url(tokens[1]) else tokens[1]
                if is_url(nested):
                    raise SystemExit(f"pip-purifier: requirements files from a URL are not supported, because the build reads them offline: {nested}")
                return f"{option} {shlex.quote(self.requirements_file(nested))}"
            if kind == "editable":
                spec, editable = self.spec(" ".join(tokens[1:]), editable=True)
                return ("-e " if editable else "") + spec
            if kind in ("index", "extra-index", "find-links", "trusted-host"):
                return " ".join(shlex.quote(t) for t in self.index_option(option, tokens[1], base))
            if option == "--no-index":
                return "--no-index" if self.mode == "lock" else ""
            return line
        # A requirement, perhaps followed by per-requirement options such as
        # --hash. The specifier ends where the first option starts.
        m = re.match(r"^(.*?)(\s+--?[A-Za-z].*)?$", line)
        spec, options = m.group(1), m.group(2) or ""
        if "--hash" in options:
            pinned = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;,]+)", spec)
            if pinned:
                self.pin(pinned.group(1), pinned.group(2))
        rewritten, _ = self.spec(spec)
        return rewritten + options

    def pin(self, name, version):
        """Records a pinned version. A name pinned to two versions, such as
        under two environment markers, constrains nothing."""
        key = re.sub(r"[-_.]+", "-", name).lower()
        self.pinned[key] = version if self.pinned.get(key, version) == version else None

    def build_constraints(self):
        """Writes the pinned versions as a constraints file for pip's
        `--build-constraint`, and returns its path, or None when nothing is
        pinned. A build environment then installs the version that the
        project pins."""
        pins = sorted(f"{name}=={version}" for name, version in self.pinned.items() if version is not None)
        if not pins:
            return None
        path = os.path.join(self.out, "_build-constraints.txt")
        os.makedirs(self.out, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(pins) + "\n")
        return path

    def pylock(self, path):
        """Rewrites a pylock.toml. In offline mode every wheel, sdist and
        archive comes from its local copy, and a VCS package becomes the
        directory of its checkout."""
        self.record_input(path)
        target = self.output_path(path)
        self.written[path] = target
        source = self.pylock_path if self.pylock_path and path == os.path.join(self.project, "pylock.toml") else path
        with open(source, "rb") as f:
            data = tomllib.load(f)
        base = os.path.dirname(path)
        for package in data.get("packages", []):
            name = package.get("name")
            module = self.provided.get(re.sub(r"[-_.]+", "-", name.lower())) if name else None
            if module:
                wheel = module['wheel']
                with open(wheel, 'rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                package['version'] = module['version']
                for key in ('wheels', 'sdist', 'archive', 'vcs', 'directory'):
                    package.pop(key, None)
                package['wheels'] = [{'name': os.path.basename(wheel), 'path': wheel,
                                      'hashes': {'sha256': digest}}]
                continue
            if name and "version" in package:
                self.pin(name, package["version"])
            for wheel in package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else []):
                if "url" in wheel:
                    self.specs.append({"kind": "file", "url": wheel["url"], "name": name, "hashes": wheel.get("hashes", {})})
                    if self.mode == "offline":
                        wheel["path"] = self.local("file", wheel.pop("url"), name)
                elif "path" in wheel:
                    wheel["path"] = self.local_path(wheel["path"], base)
            if "archive" in package:
                archive = package["archive"]
                if "url" in archive:
                    self.specs.append({"kind": "file", "url": archive["url"], "name": name, "hashes": archive.get("hashes", {})})
                    if self.mode == "offline":
                        archive["path"] = self.local("file", archive.pop("url"), name)
                elif "path" in archive:
                    archive["path"] = self.local_path(archive["path"], base)
            if "vcs" in package:
                vcs = package["vcs"]
                if "url" in vcs:
                    url = f"{vcs['type']}+{vcs['url']}@{vcs['commit-id']}"
                    self.specs.append({"kind": "vcs", "url": url, "name": name})
                    if self.mode == "offline":
                        checkout = self.local("vcs", url, name)
                        if vcs.get("subdirectory"):
                            checkout = os.path.join(checkout, vcs["subdirectory"])
                        del package["vcs"]
                        package["directory"] = {"path": checkout, "editable": False}
                elif "path" in vcs:
                    vcs["path"] = self.local_path(vcs["path"], base)
            if "directory" in package and not package["directory"]["path"].startswith("/nix/store/"):
                directory = self.local_path(package["directory"]["path"], base)
                self.record_dir(directory)
                package["directory"]["path"] = directory
                if self.mode == "offline" and not self.editable:
                    package["directory"]["editable"] = False
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(write_pylock(data))
        return target

    def script(self, path):
        """Rewrites the dependencies in a script's PEP 723 metadata."""
        path = os.path.abspath(path)
        self.record_input(path)
        target = self.output_path(path)
        self.written[path] = target
        with open(path, encoding="utf-8") as f:
            text = f.read()
        found = script_metadata(text)
        lines = text.splitlines(keepends=True)
        if found:
            start, end, table = found
            table["dependencies"] = [self.spec(d)[0] for d in table.get("dependencies", [])]
            block = ["# /// script\n"]
            for key, value in table.items():
                block.append(f"# {toml_key(key)} = {toml_value(value)}\n")
            block.append("# ///\n")
            lines[start : end + 1] = block
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("".join(lines))
        return target

    # The command line.

    def dependency_group(self, value):
        """Materialize a PEP 735 group as requirements, tracking its source.

        pip resolves includes relative to the selected pyproject, and a group
        does not install that project. Validate requirement strings before
        writing them so requirement-file options cannot acquire new semantics.
        """
        from packaging.requirements import Requirement

        filename, separator, group = value.rpartition(":")
        if not separator:
            filename, group = "pyproject.toml", value
        lock_filename, _, extras = filename.partition("[")
        if re.fullmatch(r"pylock(?:\.[^.]+)?\.toml", os.path.basename(lock_filename)):
            target = self.pylock(self.local_path(lock_filename))
            suffix = "[" + extras if extras else ""
            return ["--group", f"{target}{suffix}:{group}"]
        path = self.local_path(filename)
        self.record_input(path)
        with open(path, "rb") as source:
            data = tomllib.load(source).get("dependency-groups", {})
        normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
        groups = {}
        for name, dependencies in data.items():
            normalized = normalize(name)
            if normalized in groups:
                raise ValueError(f"duplicate dependency group {name!r} in {path}")
            groups[normalized] = dependencies

        def resolve(name, stack=()):
            key = normalize(name)
            if key in stack:
                raise ValueError(f"cyclic dependency group {name!r} in {path}")
            if key not in groups or not isinstance(groups[key], list):
                raise ValueError(f"missing or malformed dependency group {name!r} in {path}")
            requirements = []
            for dependency in groups[key]:
                if isinstance(dependency, str):
                    Requirement(dependency)
                    requirements.append(self.spec(dependency)[0])
                elif (isinstance(dependency, dict) and set(dependency) == {"include-group"}
                      and isinstance(dependency["include-group"], str)):
                    requirements.extend(resolve(dependency["include-group"], (*stack, key)))
                else:
                    raise ValueError(f"invalid dependency in group {name!r} in {path}")
            return requirements

        requirements = resolve(group)
        target = os.path.join(self.out, "_groups", f"{self.group_files}.txt")
        self.group_files += 1
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as destination:
            destination.write("".join(requirement + "\n" for requirement in requirements))
        return ["-r", target]

    def args(self, args):
        """Rewrites the arguments of `pip install`."""
        out = []
        i = 0
        while i < len(args):
            arg = args[i]
            value = None
            option = arg
            if arg.startswith("--") and "=" in arg:
                option, value = arg.split("=", 1)
            elif re.match(r"^-[rcefi]\S", arg):
                option, value = arg[:2], arg[2:]
            kind = ARG_OPTIONS_WITH_VALUE.get(option)
            if kind:
                if value is None:
                    i += 1
                    value = args[i]
                if kind in ("requirement", "constraint"):
                    out += [option, self.requirements_file(os.path.join(self.project, value))]
                elif kind == "script":
                    out += [option, self.script(os.path.join(self.project, value))]
                elif kind == "group":
                    out += self.dependency_group(value)
                elif kind == "editable":
                    spec, editable = self.spec(value, editable=True)
                    out += (["-e"] if editable else []) + [spec]
                elif kind in ("index", "extra-index", "find-links", "trusted-host"):
                    out += self.index_option(option, value, self.project)
                else:
                    out += [option, value]
            elif arg == "--no-index":
                if self.mode == "lock":
                    out.append(arg)
            elif arg.startswith("-"):
                out.append(arg)
            else:
                out.append(self.spec(arg)[0])
            i += 1
        constraints = self.build_constraints()
        if constraints:
            out += ["--build-constraint", constraints]
        return out
