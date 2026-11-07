#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Retain exact-source identity, command, exit status, and complete runtime output.
set -euo pipefail
export NIX_CONFIG="${NIX_CONFIG:-}"$'\nsandbox = true'
mkdir -p evidence
export PIP_PURIFIER_EVIDENCE_DIR="$PWD/evidence"
python3 - "$@" <<'PYTHON'
import hashlib, json, os, platform, subprocess, sys
from pathlib import Path
run = lambda *args: subprocess.check_output(args, text=True).strip()
receipt = {
    'producerRevision': run('git', 'rev-parse', 'HEAD'),
    'producerTree': run('git', 'rev-parse', 'HEAD^{tree}'),
    'dirty': bool(run('git', 'status', '--porcelain', '--untracked-files=no')),
    'flakeLockSha256': hashlib.sha256(Path('flake.lock').read_bytes()).hexdigest(),
    'system': run('nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem'),
    'host': platform.platform(), 'nix': run('nix', '--version'),
    'nixSettings': json.loads(run('python3', 'tests/sandbox.py')),
    'command': sys.argv[1:], 'githubRunId': os.getenv('GITHUB_RUN_ID'),
    'githubRunAttempt': os.getenv('GITHUB_RUN_ATTEMPT'),
}
Path('evidence/run.json').write_text(json.dumps(receipt, indent=2)+'\n')
PYTHON
set +e
"$@" 2>&1 | tee evidence/runtime.log
status=${PIPESTATUS[0]}
set -e
if [ -f evidence/builds.json ]; then
  python3 - <<'PYTHON'
import json, shutil, subprocess
from pathlib import Path
builds = json.loads(Path('evidence/builds.json').read_text())
drvs = [b['drvPath'] for b in builds]
outputs = sorted({p for b in builds for p in b['outputs'].values()})
Path('evidence/output-info.json').write_bytes(subprocess.check_output(['nix', 'path-info', '--json', *outputs]))
Path('evidence/derivations.json').write_bytes(subprocess.check_output(['nix', 'derivation', 'show', *drvs]))
for out in outputs:
    runtime = Path(out) / 'runtime.json'
    if runtime.is_file():
        shutil.copy(runtime, 'evidence/development-runtime.json')
    if (Path(out) / 'sdk.json').is_file():
        for name in ('sdk.json', 'target.json', 'capture.sha256', 'conflict.log'):
            shutil.copy(Path(out) / name, Path('evidence') / ('installer-' + name))
PYTHON
elif [ "$status" = 0 ]; then
  echo 'successful evidence run produced no derivation receipt' >&2
  status=1
fi
printf '%s\n' "$status" > evidence/exit-status
exit "$status"
