#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Report only the sandbox setting; never print other potentially private config."""
import json
import subprocess

configuration = json.loads(subprocess.check_output(['nix', 'config', 'show', '--json'], text=True))
sandbox = configuration['sandbox']['value']
assert sandbox is True or sandbox == 'true', f'sandbox must be true, got {sandbox!r}'
print(json.dumps({'sandbox': True}))
