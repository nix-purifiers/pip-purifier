# SPDX-License-Identifier: MIT
"""Stands in for hg, svn and bzr when pip clones a repository that another
package names as a source, and answers from the locked checkout.

offline.py puts a script named hg, svn and bzr on PATH that runs
`python vcs_shim.py <vcs> <arguments>`. PIP_PURIFIER_VCS_SHIMS names a JSON
list of { vcs, url, rev, version, path } entries, where `path` is the locked
checkout: the files of the repository at `rev`, without its history, as the
nixpkgs fetcher of that VCS makes them.

A clone or checkout of a URL in the list copies the checkout into the
destination and writes a marker file with the URL and revision. Inside a
marked directory, the shim answers the commands pip runs on a clone: the
update after a clone, the revision and URL queries, and `--version`. Any
other command fails with a message that names it.
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MARK = ".pip-purifier-vcs.json"
VERSIONS = {
    "hg": "Mercurial Distributed SCM (version 6.9)",
    "svn": "svn, version 1.14.5 (r1922182)",
    "bzr": "Breezy (brz) 3.3.0",
}
# The options of each command pip runs that take a value.
VALUE_OPTIONS = {"-r", "--rev", "--revision", "--username", "--password", "--config"}


def fail(vcs, args):
    print(
        f"pip-purifier: `{vcs} {' '.join(args)}` is not a command the locked checkout can answer.",
        file=sys.stderr,
    )
    sys.exit(1)


def positionals(args):
    out = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("-"):
            out.append(arg)
    return out


def normalize(url):
    return url.rstrip("/").removesuffix(".git")


def marked(directory):
    """The marker of the checkout that holds `directory`, and its root."""
    directory = os.path.abspath(directory)
    while True:
        path = os.path.join(directory, MARK)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f), directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None, None
        directory = parent


def checkout(vcs, url, dest):
    with open(os.environ["PIP_PURIFIER_VCS_SHIMS"]) as f:
        table = json.load(f)
    entry = next((e for e in table if e["vcs"] == vcs and normalize(e["url"]) == normalize(url)), None)
    if entry is None:
        print(f"pip-purifier: the lockfile has no checkout of {vcs} {url}. Run `pip-purifier lock`.", file=sys.stderr)
        sys.exit(1)
    shutil.copytree(entry["path"], dest, symlinks=True)
    subprocess.run(["chmod", "-R", "u+w", dest], check=True)
    if vcs == "svn":
        # pip asks `svn info --xml` for the revision only where .svn/entries
        # exists.
        os.makedirs(os.path.join(dest, ".svn"))
        open(os.path.join(dest, ".svn", "entries"), "w").close()
    if entry.get("version"):
        import offline

        offline.tag(dest, entry["version"])
    with open(os.path.join(dest, MARK), "w") as f:
        json.dump({"url": entry["url"], "rev": entry["rev"]}, f)


def main():
    vcs, args = sys.argv[1], sys.argv[2:]
    if args[:1] in (["--version"], ["version"]):
        print(VERSIONS[vcs])
        return
    command = next((a for a in args if not a.startswith("-")), None)
    rest = positionals(args[args.index(command) + 1 :]) if command else []

    if command in ("clone", "checkout") and len(rest) >= 2:
        checkout(vcs, rest[-2], rest[-1])
        return

    # Commands on a clone. svn names the directory; the others run in it.
    where = rest[-1] if vcs == "svn" and rest else os.getcwd()
    mark, root = marked(where)
    if mark is None:
        fail(vcs, args)
    if command in ("update", "up", "switch", "bind", "pull"):
        return
    if vcs == "hg" and command == "parents":
        # pip asks for `--template={rev}`, the local revision number, as the
        # commit it records, and for `{node}`. A copy has no local numbers,
        # so both get the locked node.
        print(mark["rev"], end="")
    elif vcs == "hg" and command == "root":
        print(root)
    elif vcs == "hg" and command == "showconfig" and "paths.default" in args:
        print(mark["url"])
    elif vcs == "svn" and command == "info" and "--xml" in args:
        print(
            '<?xml version="1.0" encoding="UTF-8"?>\n<info>\n'
            f'<entry kind="dir" path="." revision="{mark["rev"]}">\n'
            f"<url>{mark['url']}</url>\n"
            f'<commit revision="{mark["rev"]}"></commit>\n'
            "</entry>\n</info>"
        )
    elif vcs == "bzr" and command == "revno":
        print(mark["rev"])
    elif vcs == "bzr" and command == "info":
        print(f"Lightweight checkout (format: 2a)\nLocation:\n  light checkout root: .\n   checkout of branch: {mark['url']}")
    else:
        fail(vcs, args)


if __name__ == "__main__":
    main()
