#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Works through pip-purifier the way a developer would, on a real project
# pinned to a commit: pallets/click 8.1.8, whose tests install with
# `pip install -r requirements/tests.txt -e .`.
#
#   1. the README quickstart: lock, then build with the README's flake, with
#      click's test suite as the checkPhase;
#   2. a relock reproduces the lockfile byte for byte, and how long a lock
#      takes;
#   3. the development loop offline: prefetch into .venv, run the tests, check
#      that pyright resolves every import from that .venv;
#   4. adding a requirement: prefetch refuses the stale lock with a message
#      that says to relock, and works after the relock.
set -euo pipefail

source "$(dirname "$0")/network.sh"
source "$(dirname "$0")/source.sh"

repo=$(source_snapshot)
cli=$(nix build --no-link --print-out-paths "$repo#cli")/bin/pip-purifier
# Match the fixture/default builder SDK explicitly; the CLI adopts PATH.
fixture_python=$(nix build --no-link --print-out-paths --impure --expr \
  "let f = builtins.getFlake \"$repo\"; in f.inputs.nixpkgs.legacyPackages.\${builtins.currentSystem}.python3")
export PATH="$fixture_python/bin:$PATH"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

commit=934813e4d421071a1b3db3973c02fe2721359a6e
git clone --quiet https://github.com/pallets/click "$work/click"
git -C "$work/click" -c advice.detachedHead=false checkout --quiet "$commit"
cd "$work/click"

lock() {
  "$cli" lock -- -r requirements/tests.txt -e .
}

echo "== lock" >&2
start=$(date +%s)
lock
echo "the lock run took $(($(date +%s) - start)) s" >&2

echo "== relock reproduces the lockfile" >&2
cp pip-purifier.lock.json "$work/first.json"
start=$(date +%s)
lock
echo "the relock took $(($(date +%s) - start)) s" >&2
cmp "$work/first.json" pip-purifier.lock.json

echo "== the README's flake, with the test suite as checkPhase" >&2
cat > flake.nix <<EOF
{
  inputs.pip-purifier.url = "path:$repo";
  inputs.nixpkgs.follows = "pip-purifier/nixpkgs";
  outputs = { nixpkgs, pip-purifier, ... }:
    let
      system = builtins.currentSystem or "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ pip-purifier.overlays.default ];
      };
      project = pkgs.pip-purifier.buildPipProject {
        src = ./.;
        checkPhase = "pytest tests";
      };
    in {
      packages.\${system}.default = project.packages.click;
      checks.\${system} = {
        tests = project.tests;
        development = import "\${pip-purifier}/tests/development.nix" {
          inherit pkgs project;
          cli = pkgs.pip-purifier.cli;
          src = ./.;
          checkPhase = "pytest -q tests";
        };
      };
    };
}
EOF
system=$(nix eval --impure --raw --expr builtins.currentSystem)
sed -i.orig "s/builtins.currentSystem or \"x86_64-linux\"/\"$system\"/" flake.nix
rm flake.nix.orig
nix build --option sandbox true --no-link --json -L "path:.#checks.$system.tests" "path:.#checks.$system.development" "path:.#packages.$system.default" > "$work/builds.json"

if [ -n "${PIP_PURIFIER_EVIDENCE_DIR:-}" ]; then
  cp "$work/builds.json" pip-purifier.lock.json flake.nix flake.lock "$PIP_PURIFIER_EVIDENCE_DIR/"
  git rev-parse HEAD > "$PIP_PURIFIER_EVIDENCE_DIR/sample-revision"
  printf '%s\n' "$cli" > "$PIP_PURIFIER_EVIDENCE_DIR/cli-path"
fi

echo "== the development loop offline" >&2
"$cli" prefetch
offline_network bash -s <<'OFFLINE'
set -euo pipefail
.venv/bin/python - <<'PYTHON'
import socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5)
except OSError:
    pass
else:
    raise AssertionError("direct external network is reachable")
PYTHON
.venv/bin/pytest -q tests
OFFLINE
nix run 'nixpkgs#pyright' -- --pythonpath .venv/bin/python --outputjson src/click > "$work/pyright.json" || true
# reportMissingModuleSource means pyright has stubs but the package is not
# installed, which is right for click's Windows-only colorama and its
# type-checking-only typing_extensions (requirements/typing.txt). An import
# the .venv should provide and does not is reportMissingImports.
nix run 'nixpkgs#jq' -- -r '.generalDiagnostics[] | select(.rule == "reportMissingImports" or .rule == "reportMissingModuleSource") | "\(.rule) \(.file):\(.range.start.line + 1): \(.message)"' "$work/pyright.json" >&2
missing=$(nix run 'nixpkgs#jq' -- '[.generalDiagnostics[] | select(.rule == "reportMissingImports")] | length' "$work/pyright.json")
echo "pyright found $missing imports with neither a package nor stubs" >&2
[ "$missing" = 0 ]

echo "== adding a requirement" >&2
echo "rich==13.9.4" >> requirements/tests.txt
if "$cli" prefetch 2> "$work/stale.txt"; then
  echo "prefetch accepted a stale lock" >&2
  exit 1
fi
grep -q 'Run `pip-purifier lock`' "$work/stale.txt"
lock
"$cli" prefetch
.venv/bin/python -c 'import importlib.metadata, rich; print("rich", importlib.metadata.version("rich"))'
