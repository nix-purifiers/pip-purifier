#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Exact target CPython points, independently selected from the installer SDK.
set -euo pipefail
source "$(dirname "$0")/network.sh"
source "$(dirname "$0")/source.sh"
repo=$(source_snapshot)
attribute=${1:?give a pinned nixpkgs Python attribute}
case "$attribute" in
  python311|python312|python313|python314|python315) ;;
  *) echo "unknown compatibility point: $attribute" >&2; exit 1 ;;
esac
installer=${2:-default}
system=$(nix eval --impure --raw --expr builtins.currentSystem)
if [ "$(uname -s)" = Darwin ]; then
  test "$system" = aarch64-darwin
fi
case "$installer" in
  default) cli=$(nix build --no-link --print-out-paths "$repo#cli")/bin/pip-purifier ;;
  current) cli=$(nix build --no-link --print-out-paths --impure --expr \
    "import $repo/tests/sdk.nix { repo = \"$repo\"; system = \"$system\"; }")/bin/pip-purifier ;;
  *) echo "unknown installer point: $installer" >&2; exit 1 ;;
esac
python=$(nix build --no-link --print-out-paths --impure --expr \
  "let f = builtins.getFlake \"$repo\"; pkgs = import f.inputs.nixpkgs { system = \"$system\"; }; in pkgs.$attribute")/bin/python
"$python" --version
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -r "$repo/tests/fixtures/project" "$work/project"
chmod -R u+w "$work/project"
cd "$work/project"
rm pip-purifier.lock.json
"$cli" lock --python "$python" --system "$system"
cp pip-purifier.lock.json "$work/first.json"
"$cli" lock --python "$python" --system "$system"
cmp pip-purifier.lock.json "$work/first.json"
# Verify a nonempty graph and the actual target interpreter recorded by capture.
"$python" - <<'PYTHON'
import json, sys
lock = json.load(open('pip-purifier.lock.json'))
assert lock['python'] == f'{sys.version_info.major}.{sys.version_info.minor}'
assert lock['artifacts'] and lock['pins'] and lock['outputs']['packages']
print('target', sys.version, 'installer pip', lock['pip'])
PYTHON
if [ -n "${PIP_PURIFIER_EVIDENCE_DIR:-}" ]; then
  cp pip-purifier.lock.json "$PIP_PURIFIER_EVIDENCE_DIR/initial.lock.json"
  printf '%s\n' "$cli" "$python" "$installer" > "$PIP_PURIFIER_EVIDENCE_DIR/toolchain-paths"
fi
cat > flake.nix <<NIX
{
  inputs.pip-purifier.url = "path:$repo";
  inputs.nixpkgs.follows = "pip-purifier/nixpkgs";
  outputs = { nixpkgs, pip-purifier, ... }:
    let
      pkgs = import nixpkgs { system = "$system"; overlays = [ pip-purifier.overlays.default ]; };
      source = builtins.path { path = ./.; name = "project-source"; filter = p: t: !(builtins.elem (baseNameOf p) [ ".venv" ".pip-purifier" ]); };
      project = pkgs.pip-purifier.buildPipProject {
        src = source;
        python = if "$installer" == "current" then
          import "\${pip-purifier}/tests/sdk.nix" {
            flake = pip-purifier; system = "$system";
            pythonAttribute = "$attribute"; output = "python";
          }
        else pkgs.$attribute;
        checkPhase = "greet";
      };
    in {
      packages.$system.default = project.packages.greet;
      checks.$system.tests = project.tests;
      checks.$system.development = import "\${pip-purifier}/tests/development.nix" {
        inherit pkgs project;
        cli = if "$installer" == "default" then pkgs.pip-purifier.cli else
          import "\${pip-purifier}/tests/sdk.nix" { flake = pip-purifier; system = "$system"; };
        src = source;
        wrongPython = if "$attribute" == "python314" then pkgs.python311 else pkgs.python314;
        checkPhase = "test \"\$(greet)\" = 'hello attrs=25.3.0 docopt=0.6.2 yaml=6.0.3'";
      };
    };
}
NIX
nix build --option sandbox true --no-link --json -L "path:.#packages.$system.default" "path:.#checks.$system.tests" "path:.#checks.$system.development" > "$work/builds.json"
if [ -n "${PIP_PURIFIER_EVIDENCE_DIR:-}" ]; then
  cp "$work/builds.json" flake.nix flake.lock "$PIP_PURIFIER_EVIDENCE_DIR/"
fi
# An existing environment from another target must be preserved and rejected.
wrong=python314
[ "$attribute" != python314 ] || wrong=python311
wrong_python=$(nix build --no-link --print-out-paths --impure --expr \
  "let f = builtins.getFlake \"$repo\"; pkgs = import f.inputs.nixpkgs { system = \"$system\"; }; in pkgs.$wrong")/bin/python
"$wrong_python" -m venv --without-pip .venv
if "$cli" prefetch > "$work/wrong-venv.log" 2>&1; then
  echo 'prefetch accepted an environment from another Python' >&2
  exit 1
fi
grep -q 'Recreate the virtual environment' "$work/wrong-venv.log"
# No automatic deletion of the user's environment.
test -x .venv/bin/python
rm -rf .venv
"$cli" prefetch
"$python" - .venv/bin/python <<'PYTHON'
import subprocess, sys
assert subprocess.check_output([sys.argv[1], '-c', 'import sys; print(sys.version)']) == subprocess.check_output([sys.executable, '-c', 'import sys; print(sys.version)'])
PYTHON
# Relocation and an ordinary activated interpreter under enforced network denial.
mv "$work/project" "$work/relocated"
cd "$work/relocated"
# Recreate the venv after relocation so absolute entry-point paths are current.
rm -rf .venv
"$cli" prefetch
offline_network bash -s "$cli" <<'OFFLINE'
set -euo pipefail
cli=$1
. .venv/bin/activate
python - <<'PYTHON'
import socket, attrs, docopt, yaml
assert attrs.__version__ == '25.3.0'
assert yaml.__version__ == '6.0.3'
try:
    socket.create_connection(('1.1.1.1', 443), timeout=5)
except OSError:
    pass
else:
    raise AssertionError('external network is reachable')
PYTHON
test "$(greet)" = 'hello attrs=25.3.0 docopt=0.6.2 yaml=6.0.3'
"$cli" pip --python .venv/bin/python check
OFFLINE
