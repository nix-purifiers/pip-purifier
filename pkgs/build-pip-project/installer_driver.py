# SPDX-License-Identifier: MIT
"""Select management tools independently of the existing project runtime."""
import email
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

from packaging.specifiers import SpecifierSet
from packaging.version import Version


def identity(path):
    path = Path(path)
    metadata = next(path.glob('pip-*.dist-info/METADATA'))
    info = email.message_from_bytes(metadata.read_bytes())
    digest = hashlib.sha256()
    for root in [path / 'pip', metadata.parent]:
        for item in sorted(root.rglob('*')):
            if item.is_file() and '__pycache__' not in item.parts and item.suffix != '.pyc':
                digest.update(str(item.relative_to(path)).encode() + b'\0')
                digest.update(hashlib.sha256(item.read_bytes()).digest())
    return {'path': str(path), 'version': info['Version'],
            'requiresPython': info['Requires-Python'] or '', 'sha256': digest.hexdigest()}


def candidates():
    current = identity(importlib.metadata.distribution('pip').locate_file(''))
    manifest = os.environ.get('PIP_PURIFIER_INSTALLER_DRIVERS')
    others = json.loads(Path(manifest).read_text()) if manifest else []
    return sorted([current, *(identity(path) for path in others)],
                  key=lambda item: Version(item['version']), reverse=True)


def select(target, expected=None, required=(), previous=None):
    version = subprocess.check_output([str(target), '-I', '-c',
        'import sys; print("%d.%d.%d" % sys.version_info[:3])'], text=True).strip()
    runner = str(Path(__file__).with_name('pip_runner.py'))
    failures = []
    for candidate in candidates():
        if expected and candidate['version'] != expected:
            continue
        if not SpecifierSet(candidate['requiresPython']).contains(version, prereleases=True):
            continue
        if previous and any(candidate[key] != previous[key] for key in ('version', 'sha256')):
            continue
        environment = {'PIP_PURIFIER_PIP_PATH': candidate['path'],
                       'PIP_PURIFIER_DRIVER_SHA256': candidate['sha256']}
        probe = subprocess.run([str(target), '-I', runner, '--purifier-probe'],
            env=dict(os.environ, **environment), capture_output=True, text=True)
        if probe.returncode:
            failures.append(candidate['version'] + ': ' + probe.stderr.strip())
            continue
        facts = json.loads(probe.stdout)
        missing = set(required) - set(facts['capabilities'])
        if facts['pip'] != candidate['version'] or missing:
            detail = (': ' + facts['bindingError']) if 'producer-bindings' in missing and facts.get('bindingError') else ''
            failures.append(candidate['version'] + ': missing ' + ', '.join(sorted(missing)) + detail)
            continue
        delegation = subprocess.run([str(target), '-I', runner, '--python', str(target), '--version'],
            env=dict(os.environ, **environment), capture_output=True, text=True)
        if delegation.returncode or not delegation.stdout.startswith('pip ' + candidate['version'] + ' '):
            failures.append(candidate['version'] + ': native target delegation failed: ' + delegation.stderr.strip())
            continue
        adapter = facts.get('buildConstraintAdapter')
        if previous and previous.get('buildConstraintAdapter') and previous['buildConstraintAdapter'] != adapter:
            failures.append(candidate['version'] + ': build constraint adapter provenance changed')
            continue
        if adapter:
            candidate = dict(candidate, buildConstraintAdapter=adapter)
        # The runner executes on the selected existing runtime, including
        # its native --python delegation. No helper distribution is installed.
        return {'command': [str(target), '-I', runner], 'environment': environment,
                'identity': candidate, 'capabilities': facts['capabilities']}
    detail = '; '.join(failures) or 'no hash-bound helper has compatible metadata and provenance'
    raise SystemExit(f'pip-purifier: no compatible installer driver for this runtime'
                     f'{" and locked pip " + expected if expected else ""}: {detail}')
