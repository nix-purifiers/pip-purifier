# Design

## The lock run (`src/pip_purifier.py`)

`pip-purifier lock` starts a package index on localhost (`Proxy`) in front
of the real indexes. `/i/<n>/<path>` serves page `<path>` of upstream index
`n` with every link rewritten to `/f/<url>/<file name>`, and `/f/` serves
the file and records its URL and SHA-256. pip runs with `PIP_INDEX_URL`
pointing at the proxy, so the isolated environments in which it builds
sdists use the proxy too, and their build requirements are recorded.

The lock run then:

1. rewrites the pip arguments and every file they name (`pkgs/build-pip-project/pip_inputs.py`,
   "lock" mode), so index options point at the proxy;
2. runs `pip install --report` into a scratch venv, which resolves,
   downloads and builds everything for this system;
3. records each VCS requirement's commit with the hash nixpkgs' fetcher
   computes, each URL requirement's file, each URL and repository that a
   package names in its own metadata, and for a git repository of that
   kind its raw commit object and submodule commits (`commitObject`,
   `gitlinks`), from which the offline install rebuilds a shallow `.git`;
4. records the outputs: the programs of the venv, and the sdist and wheel
   of each local project, built with `pip wheel` and `python -m build`;
5. for each other system, resolves with `--platform` and
   `--only-binary=:all:`, and keeps this system's versions when that fails
   (an sdist-only package, or a pylock.toml);
6. for every package version any system uses, fetches a wheel for each
   system's CPU, or the sdist when no such wheel exists.

The systems are those `--system` names, or x86_64-linux, aarch64-linux and
the system the lock runs on, so a lock on a Mac also covers aarch64-darwin;
the system the lock runs on is always among them.
A relock keeps the files of the existing lockfile: the proxy lists only
those files for each package they cover. `update` drops them for the named
packages, or for every package. The lockfile is written with sorted keys,
so for the same systems it does not depend on the system the lock ran on,
except for the files pip picks for that system itself (on a Mac, for the
macOS version it runs) and for the environment markers, which every
system's resolve evaluates as the lock host does.

## The lockfile

`version`, `pip` and `python` identify the lock, and `hash` is the SHA-256
of the rest of the lockfile as compact JSON with sorted keys. Both the
command (`lock_hash`) and the Nix library (`lib-overlays/default/read-lock.nix`) compute
it, so an edit by hand fails. `args` are the pip install arguments.
`inputs` maps each file the arguments name (and the `pyproject.toml`,
`setup.py` and `setup.cfg` of each local project) to its SHA-256; the Nix
library and the command refuse a lock whose inputs changed. `pins` holds
`name==version` for each system, and its names are the systems the lock
covers. `versions` holds the version of each
package built from a checkout (by requirement URL) or a local directory (by
path). `outputs` names programs and packages. `sources` maps each VCS or
URL requirement to its download, `repositories` lists the git repositories
named in package metadata, and `artifacts` holds every download in the
entry shapes of the nix-purifiers/purifier library, except the files that a
`pylock.toml` among the inputs names: `readLock` reads those from the
`pylock.toml`.

The versions that a hashed requirement or a `pylock.toml` pins also go to
pip as `--build-constraint` (`Rewriter.build_constraints` in
`pkgs/build-pip-project/pip_inputs.py`), in the lock run and in the offline install, so
the build environments of sdists use the project's versions.

## Runtime identity in locks

Regular CPython captures retain format 4 without an ABI field. Free-threaded
captures use format 5 with required canonical `pythonAbi` (for example
`cp315t`), even when every dependency is pure Python or the graph is empty.
Readers validate the format-specific shape; older readers refuse format 5.
The actual target interpreter supplies its ABI and compatible tags, including
`abi3t` rather than `abi3` for free-threaded CPython. Wheel filtering and
cross-system downloads use those tags.

Sandboxed builds check the supplied SDK's primary ABI metadata, then probe the
actual interpreter before dependency installation. Local wrapper targets are
also probed before realizing inputs. Local capture/prefetch/wrapper commands
select an existing runtime; `.pip-purifier/runtime.json` records the capture
selection outside the portable lock. `locked-artifacts.nix` materializes local
downloads without a project SDK or Nix runtime repair configuration. Pure builds
use that same artifact implementation with a caller-supplied immutable SDK and
explicit native runtime configuration. A matching major/minor version alone does
not establish ABI compatibility.

## The Nix library

`readLock` (`lib-overlays/default/read-lock.nix`) reads the lockfile into the lock shape
of the purifier contract: the artifacts as `entries`, each wheel for one
CPU with its `systems`, the local projects as `outputs`, and the reasons the
lockfile cannot be used as `problems`. `buildPipProject` passes it to the
library's `checkLock`, then checks the system and the Python version, and
takes this system's downloads from `entriesFor`, of which it keeps the
wheels that the build's glibc (on macOS, nixpkgs' oldest macOS) and Python
run. The library's `fetchEntry` and `tree` fetch them with `fetchers`.

## The offline install (`pkgs/build-pip-project/offline.py`)

The Nix build and `pip-purifier prefetch` both rewrite the recorded
arguments in "offline" mode: index options go away, and each VCS or URL
requirement points at its local copy. pip runs with `--no-index`, the
wheelhouse as find-links, and the pins as constraints.

pip builds a local directory in place, so each checkout is copied to a
writable directory first. The copy, and in the Nix build each local project
without `.git`, becomes a one-commit git repository tagged with the
recorded version, so setuptools-scm and hatch-vcs derive the same version
as in the lock run. A git repository named in package metadata is served to
the git that pip runs through `url.<copy>.insteadOf`. The copy is the
checkout with the shallow `.git` that the purifier library rebuilt from the
recorded commit object, with the revision pip asks for as a tag. A git
wrapper first on `PATH` drops the `--filter` of pip's `git clone`: a
partial clone of a shallow repository fetches the shallow commit's parent
again and again. A Mercurial, Subversion or Bazaar
repository named in metadata is answered by `pkgs/build-pip-project/vcs_shim.py`, and a URL
named in metadata by `mitm-cache replay`.

## Local installer management tools

`installer_driver.py` selects a hash-bound pip payload independently of the
project runtime. Candidate wheel metadata supplies `Requires-Python`; the actual
existing interpreter probes the runner, supported options and native `--python`
delegation before a target environment is created or reused. Local replay selects
the lock's recorded pip version and checks any retained capture payload hash.
The payload is a separate tool, not a distribution installed into the project.
Capture records its actual resolver version; regular current-runtime locks retain
their existing bytes. Local state retains the selected tool identity beside the
runtime selection. Missing candidates or capabilities fail explicitly.

The initial compatible driver supports the tested older-runtime capture/replay
path. Older-pip build-constraint semantics and pylock handling remain separate
unproven gates; capture refuses unsupported generated build constraints before
creating its temporary target environment. No native adapter or pylock lowering
has been silently introduced by installer selection.
