# SPDX-License-Identifier: MIT
"""Build a real nested project and exercise native pip path semantics offline."""
from pathlib import Path
import argparse
import email.parser
import hashlib
import json
import os
import subprocess
import zipfile

p = argparse.ArgumentParser()
for name in ['pip', 'runner', 'wheelhouse', 'produced-wheel', 'out']:
    p.add_argument('--' + name, required=True)
a = p.parse_args()
root = Path.cwd()
source = root / 'greet/src/greet/__init__.py'
expected = source.read_bytes()
env = dict(os.environ, PIP_NO_INDEX='1', PIP_FIND_LINKS=a.wheelhouse,
           PIP_CONFIG_FILE='/dev/null', PIP_DISABLE_PIP_VERSION_CHECK='1',
           PIP_CACHE_DIR=str(root / 'pip-cache'))
for name in ['PYTHONPATH', 'PYTHONHOME', 'NIX_PYTHONPATH', 'PIP_INDEX_URL',
             'PIP_EXTRA_INDEX_URL', 'PIP_PURIFIER_BINDINGS',
             'PIP_PURIFIER_NATIVE_REPAIR', 'PIP_PURIFIER_EXPORT_WHEELS']:
    env.pop(name, None)
commands = []


def verify_wheel(path):
    with zipfile.ZipFile(path) as archive:
        assert archive.read('greet/__init__.py') == expected
        metadata = email.parser.BytesParser().parsebytes(archive.read('greet-0.1.0.dist-info/METADATA'))
        assert metadata['Name'] == 'greet' and metadata['Version'] == '0.1.0'
    return {'wheel': str(path), 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}


produced = verify_wheel(Path(a.produced_wheel))
for label, argument in [('old', 'greet'), ('relative', './greet'),
                        ('absolute', str(root / 'greet'))]:
    output = root / ('wheels-' + label)
    output.mkdir()
    argv = [a.pip, '-I', a.runner, 'wheel', '--no-deps', '--no-cache-dir', '--wheel-dir', str(output), argument]
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    commands.append({'argv': argv, 'exitCode': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
    if label == 'old':
        assert result.returncode != 0
        assert 'ERROR: No matching distribution found for greet' in result.stderr.splitlines()
        assert not list(output.iterdir())
    else:
        assert result.returncode == 0, commands[-1]
        wheel, = output.glob('*.whl')
        commands[-1]['artifact'] = verify_wheel(wheel)
Path(a.out).write_text(json.dumps({'publicApiProducedWheel': produced,
                                 'commands': commands,
                                 'scope': 'Relocated captured test fixture and native offline wheel commands; no project-cohort qualification'}, indent=2) + '\n')
