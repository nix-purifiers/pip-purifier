#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# The development loop of the project fixture with no network, after
# `pip-purifier prefetch`. Online, it runs `prefetch` with the lockfile in
# the project and with one kept outside it (--lockfile), and
# `publish --dry-run`. With PIP_PURIFIER_TEST_GC=1, as in CI, it then
# collects garbage, so that only the roots under .pip-purifier/ keep the
# store paths. With the network cut, it runs a program of the .venv before
# and after an edit, `pip-purifier pip list`, and `pip-purifier pip install`
# of a package the lockfile lacks, which has to name `pip-purifier lock`.
#
# Network isolation is enforced by tests/network.sh; absence is a failure.
set -euo pipefail

source "$(dirname "$0")/network.sh"

cli=$(nix build --no-link --print-out-paths '.#cli')/bin/pip-purifier
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cp -r tests/fixtures/project "$work/project"
cd "$work/project"

echo "== prefetch, with the lockfile outside the project" >&2
mv pip-purifier.lock.json "$work/outside.lock.json"
"$cli" prefetch --lockfile "$work/outside.lock.json" --venv "$work/outside-venv"
[ "$("$work/outside-venv/bin/greet")" = "hello attrs=25.3.0 docopt=0.6.2 yaml=6.0.3" ]
"$cli" publish --lockfile "$work/outside.lock.json" --dry-run
mv "$work/outside.lock.json" pip-purifier.lock.json

echo "== prefetch" >&2
"$cli" prefetch
test -L .pip-purifier/inputs
test -L .pip-purifier/pip-purifier
"$cli" publish --dry-run

if [ "${PIP_PURIFIER_TEST_GC:-}" = 1 ]; then
  echo "== garbage collection" >&2
  nix-store --gc > /dev/null
fi


echo "== the development loop with no network" >&2
offline_network bash -s "$cli" <<'EOF'
set -euo pipefail
cli=$1
if .venv/bin/python -c 'import urllib.request; urllib.request.urlopen("https://pypi.org/simple/", timeout=10)' 2> /dev/null; then
  echo "pypi.org can be reached, so this is not an offline test" >&2
  exit 1
fi
[ "$(.venv/bin/greet)" = "hello attrs=25.3.0 docopt=0.6.2 yaml=6.0.3" ]
sed -i.orig 's/hello/howdy/' src/greet/__init__.py
rm src/greet/__init__.py.orig
[ "$(.venv/bin/greet)" = "howdy attrs=25.3.0 docopt=0.6.2 yaml=6.0.3" ]
"$cli" pip --python .venv/bin/python list | grep -q '^PyYAML *6.0.3'
if "$cli" pip --python .venv/bin/python install rich 2> missing.txt; then
  echo "pip installed a package the lockfile lacks" >&2
  exit 1
fi
grep -q 'the lockfile has no file for rich' missing.txt
grep -q 'run `pip-purifier lock`' missing.txt
EOF
echo "the development loop worked offline" >&2
