# pip-purifier

pip-purifier keeps pip's development workflow on locked,
Nix-resolved dependencies. Use the normal build and test commands in your
working tree; building the project itself in the Nix sandbox is an
optional next step.

Where a project has a `pylock.toml` or a requirements file with `--hash`
pins, pip-purifier locks those versions. They also constrain the
environments in which pip builds sdists, through pip's
`--build-constraint`. The Nix build reads the files that a `pylock.toml`
names, with their URLs and hashes, from the `pylock.toml` itself.

## Quickstart

From the project root:

```zsh
nix shell 'github:nix-purifiers/pip-purifier'
pip-purifier lock
pip-purifier prefetch
. .venv/bin/activate
python -m pytest
```

`prefetch` creates and installs the project’s `.venv` from the locked
dependencies. The activation command makes it available to your shell.

Commit the lockfiles that the lock command writes, including the build
tool's own lockfile when it has one. Keep using the project's usual build,
test and editor commands with this setup. Nix provides the locked
dependencies; the development commands above run outside the Nix sandbox.
No project Nix files are needed for this workflow.

Sandboxed builds of the whole project are an optional
[next step](#building-the-project-with-nix).

For persistent use, add these packages through your usual Nix configuration.


## Development loop

pip has no plugin mechanism, so `pip-purifier` is a command of its own
that runs pip. The development loop is the project's `.venv`, and
`pip-purifier pip` runs pip against the locked files in place of a pip
inside `.venv`.

`pip-purifier prefetch` installs the project into `.venv` offline, with
editable installs left editable. Then work in `.venv` as usual: an edit to
the project's source shows in the next run without a reinstall.

```zsh
pip-purifier prefetch
.venv/bin/pytest
```

`pip-purifier pip` runs any pip command offline against the locked files:

```zsh
pip-purifier pip --python .venv/bin/python list
```

After a change to the requirements, `pip-purifier lock` relocks. A relock
keeps the files of the existing lockfile: for each package that the
lockfile has, including the build requirements of sdists, the lock run
offers pip only those files. An unchanged project relocks to the same file
byte for byte, and adding a requirement leaves the other versions alone.
When a changed requirement needs a version that the lockfile lacks, the
lock run says so and resolves afresh.

`pip-purifier update` resolves every package afresh, and
`pip-purifier update <package>...` resolves only the named ones, with the
`pip install` arguments that the lockfile records. A `pylock.toml` pins
every version itself, so to update such a project, run `pip lock`, then
`pip-purifier lock`.

## Dependencies from another purified project

```zsh
nix shell 'github:nix-purifiers/pip-purifier'
pip-purifier lock \
  --input 'github:owner/library/<rev>#library'
pip-purifier prefetch
source .venv/bin/activate
```

The producer exposes `projections.python`; uv and PDM producers use the same
format. Repeat `--input` for more producers. Pip resolves those packages
from their Nix-built wheels, without changing project requirements. The
lock records the Nix source pin and omits supplied wheels from downloads.
Relocking keeps that pin; `update` refreshes it.

Use `pip-purifier pip` for pip operations, including `check`, so they apply
the same bindings. Without a lockfile, it warns and runs ordinary pip.

Each local package and the complete build expose `projections.python`.
Wheelhouses include the native runtime dependency graph, with Nix-derived
versions and rewritten dependency metadata for ordinary installers.
Composed publishing uploads these wheels. Use an index accepting local
versions, or consume the repository with `--find-links`.

## Building the project with Nix

Once the native development workflow is working, you can also build and
test the whole project in the Nix sandbox. For this step, keep a flake or
another Nix build definition in the project's repository, or maintain an
external packaging repository that points at its sources and lockfiles.
The builder consumes the same lockfiles as the development workflow.

A flake is optional. It can live in the project or in a separate
packaging repository and call the builder directly:

```nix
{
  inputs.pip-purifier.url =
    "github:nix-purifiers/pip-purifier";
  inputs.nixpkgs.follows = "pip-purifier/nixpkgs";

  outputs =
    { nixpkgs, pip-purifier, ... }:
    let
      pkgs = import nixpkgs {
        system = "x86_64-linux";
        overlays = [ pip-purifier.overlays.default ];
      };
      project = pkgs.pip-purifier.buildPipProject {
        src = ./.;
      };
    in
    {
      packages.x86_64-linux = {
        default = project.environment;
        greet = project.programs.greet;
        release = project.release;
      };
      checks.x86_64-linux.tests = project.tests;
    };
}
```

`buildPipProject` gives:

- `packages.<name>`, the sdist and wheel of each local project, with their
  file names as `.sdist` and `.wheel` and their paths under `.files`;
- `tests`, the project's tests in the sandbox: `checkPhase` when you give
  it, else `pytest` when the environment has it, run in a copy of the
  source with the environment's programs on `PATH`;
- `release`, every sdist and wheel of `packages`, which
  `pip-purifier publish` uploads;
- `fetches`, the download entries of the lock, for SBOM, license and
  vulnerability tools;
- `environment`, a virtual environment with everything the recorded
  `pip install` installs, with editable installs made plain;
- `programs.<name>`, each program of that environment, for `nix run`;
- `inputs`, the locked downloads for this system.

The lockfile names every output, so Nix knows them at evaluation time.

`buildPipProject` takes `src` and these optional arguments:

- `fetchers`, nixpkgs fetchers to use in place of the defaults, such as a
  `fetchurl` that sends credentials;
- `lockFile`, for a lockfile outside `src`;
- `pylock`, for a `pylock.toml` outside `src`, which the build uses in place
  of the one in `src`;
- `python`, whose version has to be the one the lock was made with;
- `nativeBuildInputs` and `buildInputs`, for native dependencies of sdists;
- `pathSources`, for local projects outside `src`;
- `checkPhase`.

`buildPipProject.override` replaces the packages it takes from the package
set, and `pip-purifier.overrideScope` replaces what everything in the scope
shares. `lib.pip-purifier.readLock { src; }` reads both lockfiles into the
lock shape that every purifier gives.

We recommend committing `pip-purifier.lock.json` and `pylock.toml` to the
repository of the project. The builder and the commands look for them
there.

### One-off builds without a project Nix file

For an ad hoc build, the builder can also be evaluated directly from the
project root. Replace the example package name with one from your project:

```zsh
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/pip-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.pip-purifier.buildPipProject { src = ./.; }).environment'
```

This uses the purifier's pinned nixpkgs, matching the lock command above.
To build with ambient `<nixpkgs>` from your Nix search path instead,
replace `import purifier.inputs.nixpkgs` with `import <nixpkgs>`.
Both forms need no Nix files in your project. The build tool and language
versions must match the lockfile; with ambient nixpkgs, lock with the
purifier from that package set or pass a compatible toolchain to the builder.

## Working offline

`prefetch` keeps everything the development loop needs alive with
garbage-collector roots in `.pip-purifier/`, beside the lockfile: the
locked downloads for this system, and the `pip-purifier` command with its
Python, pip, nixpkgs and purifier library. After a prefetch, `.venv`, the
editor and `pip-purifier pip` work with the network cut off. Keep the
command itself installed, for example in a dev shell, so that running it
needs no download either.

A project can instead keep its build outside its tree, such as in a
packaging flake for a project whose author does not use pip-purifier. Give
the path of `pip-purifier.lock.json` to each command with `--lockfile`, or
set `PIP_PURIFIER_LOCKFILE`, and pass the same path to `buildPipProject` as
`lockFile`:

```zsh
PIP_PURIFIER_LOCKFILE=../packaging/pip-purifier.lock.json \
  pip-purifier lock
```

The same goes for `pylock.toml`, through `--tool-lockfile` or
`PIP_PURIFIER_TOOL_LOCKFILE`, and the `pylock` argument of
`buildPipProject`. With either one, `lock` and `update` read the
`pylock.toml` there, and when it does not exist yet, `lock` starts from a
copy of the `pylock.toml` in the tree.

## Editor

Point the editor at `.venv`, as for any virtual environment. For pyright
and basedpyright, `prefetch` makes `.venv` in the project root, where they
look for it by default. The packages in `.venv` hold their sources, so
go-to-definition and type information work offline. The language server
itself comes from your editor setup, such as nixpkgs' `pyright`.

## Troubleshooting

- **"does not match … any more. Run `pip-purifier lock`."** A requirements
  file, `pylock.toml`, script or `pyproject.toml` changed after the lock.
  Relock.
- **"was changed after `pip-purifier lock` wrote it"**. The lockfile holds
  a hash of its own content, and an edit by hand changed the content.
  Relock, and review the lockfile's diff before you commit it.
- **"is not a pip-purifier lockfile"**, **"is not valid JSON"** or **"has
  lock format version …"**. The lockfile is damaged, or comes from another
  version of pip-purifier. Relock.
- **"the lockfile has no file for <name> … run `pip-purifier lock`."** pip
  needs a package that the lock run did not see, for example after you
  added a requirement and ran `pip-purifier pip install` before relocking.
- **"hash mismatch in fixed-output derivation"** from Nix. The server now
  serves different bytes under a locked URL. Relock, and review the
  lockfile's diff before you commit it.
- **"was made for Python …"**. Pass `buildPipProject` the `python` of the
  version that the lock names, or relock with `--python`.
- **"has no lock for <system>"**. The lock covers x86_64-linux,
  aarch64-linux and the system it ran on. Relock with `--system` for every
  system it should cover, such as `--system x86_64-linux --system
  aarch64-linux --system aarch64-darwin`.

## Supported language repositories

pip-purifier locks what pip installs from:

- package indexes, from `PIP_INDEX_URL` and `PIP_EXTRA_INDEX_URL`, from
  pip's configuration files, or from the index options of the
  requirements, and find-links directories;
- requirements files, with nested `-r` and `-c` files, `--hash` pins (as
  pip-compile writes them) and environment markers;
- `pylock.toml` files ([PEP 751](https://peps.python.org/pep-0751/)), as
  `pip lock` writes them;
- the dependencies of a script ([PEP 723](https://peps.python.org/pep-0723/)),
  with `--requirements-from-script`;
- local projects, editable or not, inside the project directory or beside
  it;
- git, Mercurial, Subversion and Bazaar repositories, and URLs of wheels
  and sdists;
- a repository or URL that another package names as a source in its own
  metadata. pip clones or downloads those itself, and the offline install
  answers it: git through `url.<local clone>.insteadOf`, Mercurial,
  Subversion and Bazaar through stand-in commands that copy the locked
  checkout, and URLs through `mitm-cache replay`, an HTTPS proxy that
  serves the locked files;
- sdists, which pip builds with the build requirements that the lock run
  recorded.

A lock covers x86_64-linux, aarch64-linux and the system it runs on (so
aarch64-darwin on a Mac), or the systems `--system` names and the one it
runs on, with the wheels of the Python version that the lock run used; on
aarch64-darwin, the wheels that macOS 14, the oldest macOS of nixpkgs,
runs. The lockfile records URLs without their credentials, so a private
index needs a `fetchers.fetchurl` that supplies them, such as nixpkgs'
`fetchurl` with `netrcPhase`.

## Examples

For day-to-day work on these projects, use the native-tool setup and
commands in [Quickstart](#quickstart) and [Development loop](#development-loop).
The Nix expressions below show optional sandboxed packaging of those
projects.

[pallets/click](https://github.com/pallets/click) installs its test
dependencies with `pip install -r requirements/tests.txt -e .`. In a clone
of it, lock those arguments, and work offline from `.venv`:

```zsh
nix shell 'github:nix-purifiers/pip-purifier'
pip-purifier lock -- -r \
  requirements/tests.txt -e .
pip-purifier prefetch
.venv/bin/pytest tests
```

Then add a `flake.nix` that builds its sdist and wheel and runs its tests in
the sandbox:

```nix
{
  inputs.pip-purifier.url =
    "github:nix-purifiers/pip-purifier";
  inputs.nixpkgs.follows = "pip-purifier/nixpkgs";

  outputs =
    { nixpkgs, pip-purifier, ... }:
    let
      pkgs = import nixpkgs {
        system = "x86_64-linux";
        overlays = [ pip-purifier.overlays.default ];
      };
      project = pkgs.pip-purifier.buildPipProject {
        src = ./.;
        checkPhase = "pytest tests";
      };
    in
    {
      packages.x86_64-linux.default =
        project.packages.click;
      checks.x86_64-linux.tests = project.tests;
    };
}
```

`nix build` makes the sdist and wheel, and `nix flake check` runs the
tests. Commit `pip-purifier.lock.json` and `flake.nix` with the project.

## Credits

The purifier idea was inspired by nixcloud's
[cargo (libnix)](https://github.com/nixcloud/cargo).

## License

MIT
