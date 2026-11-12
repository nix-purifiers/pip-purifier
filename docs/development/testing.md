# Testing

- `nix flake check -L` builds every fixture in `tests/fixtures/` offline in
  the sandbox (`tests/default.nix`), runs its programs, checks the package
  files and the release, and runs the unit tests (`tests/unit`) and the
  formatting check. It holds the conformance checks of the purifier
  contract that the sandbox can run: `conformance-stale`,
  `conformance-tampered`, `conformance-missing` and `conformance-malformed`
  (`tests/conformance.nix`), `conformance-offline` (`pip-purifier prefetch`
  against prebuilt inputs, then the development loop), `conformance-external`
  (a fixture built from a lockfile outside its source), and
  `conformance-rebuild` (the sdist and wheel that CI builds twice). The
  `pythonpath` check adds a nixpkgs Python package as a native build input
  and checks that PYTHONPATH is clear once the builds have run pip.
- `nix flake check --all-systems --no-build` evaluates the checks for
  x86_64-linux, aarch64-linux and aarch64-darwin.
- `tests/lock-run.sh`, the CI job `conformance-relock`, runs the lock
  against the network on a copy of each fixture, and on a lockfile kept
  outside a fixture, and compares each result with the committed lockfile
  byte for byte.
- `tests/offline.sh`, the CI job `conformance-offline`, runs `prefetch`,
  with the lockfile inside and outside the project, and
  `publish --dry-run`. In CI it then collects garbage. With the network cut,
  it runs the development loop and `pip-purifier pip`, which has to name
  `pip-purifier lock` for a package the lockfile lacks.
- `tests/update.sh`, the CI job `conformance-update`, updates a pinned
  package and checks that `prefetch` accepts the new lockfile.
- `tests/real-world.sh`, the CI job `real-world`, works through
  pallets/click 8.1.8 at a pinned commit.
- `tests/relock-fixtures.sh` rewrites the fixtures' lockfiles after a
  change to a fixture or to the lock format.

CI runs every job on x86_64-linux and aarch64-linux.

Dependency-group rewriter tests cover group-only source tracking, included and
normalized names, nested pyprojects, URL rewriting without fetching unused
groups, invalid dependency options, cycles, and native pylock group/extra
selection. A changed group manifest must invalidate prefetch even when the
project itself is not installed.

The Linux `native-runtime` check compiles a C++/zlib shared library without Nix
runtime paths and proves that loading fails before repair and succeeds after
repair with `LD_LIBRARY_PATH` unset. It also checks preservation of vendor
`$ORIGIN` paths, installed wheel `RECORD` hashes and repeated repair.
It also executes a bundled C++ program with a deliberately missing loader,
checks that static and foreign-architecture binaries remain unchanged, and
verifies RECORD refresh after a subsequent file modification. Environment
builds refresh hashes again after Nix's final strip/shebang fixups.

The opt-in [HTTPX Python3.13 diagnostic](../../tests/real-world/httpx-python313/README.md)
retains a pinned real-project failure and a matched raw-pip baseline. Both
commands are expected to fail on the same upstream trio warning; the receipt
records the exact source, SDK, dependency lock, selection and observed counts.
These failed observations remain separate from the passing default checks.
