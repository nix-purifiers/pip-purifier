#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# The conformance-relock job of CI. Runs the lock against the network on a
# copy of each fixture, and checks that it reproduces the committed
# lockfile byte for byte: a relock keeps the files of the existing
# lockfile, so an unchanged project locks the same.
set -euo pipefail

cli=$(nix build --no-link --print-out-paths '.#cli')/bin/pip-purifier
# Match the fixture/default builder SDK explicitly; the CLI adopts PATH.
fixture_python=$(nix build --no-link --print-out-paths --impure --expr \
  "let f = builtins.getFlake \"$(pwd)\"; in f.inputs.nixpkgs.legacyPackages.\${builtins.currentSystem}.python3")
export PATH="$fixture_python/bin:$PATH"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# The fixtures are locked for every system the flake's checks build on.
systems=(--system x86_64-linux --system aarch64-linux --system aarch64-darwin)

check() {
  local fixture=$1
  shift
  echo "== $fixture" >&2
  cp -r "tests/fixtures/$fixture" "$work/$fixture"
  (cd "$work/$fixture" && "$cli" lock "${systems[@]}" "$@")
  if ! cmp "tests/fixtures/$fixture/pip-purifier.lock.json" "$work/$fixture/pip-purifier.lock.json"; then
    diff "tests/fixtures/$fixture/pip-purifier.lock.json" "$work/$fixture/pip-purifier.lock.json" >&2 || true
    echo "the lock run did not reproduce the lockfile of $fixture" >&2
    exit 1
  fi
}

check project
check editable-extras
check sources
check pylock
check hashed
check script -- --requirements-from-script app.py

# A lockfile kept outside the project, which `lock --lockfile` relocks in
# place.
echo "== hashed, with the lockfile outside the project" >&2
mkdir "$work/external"
cp tests/fixtures/hashed/requirements.txt "$work/external/"
cp tests/fixtures/hashed/pip-purifier.lock.json "$work/external.lock.json"
(cd "$work/external" && "$cli" lock "${systems[@]}" --lockfile ../external.lock.json)
test ! -e "$work/external/pip-purifier.lock.json"
cmp tests/fixtures/hashed/pip-purifier.lock.json "$work/external.lock.json"
