# SPDX-License-Identifier: MIT
"""Install a real header wheel, compile against it, and exercise venv bases."""
from pathlib import Path
import argparse
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile

p = argparse.ArgumentParser()
for name in ['python', 'pip', 'cc', 'script', 'original-script', 'bash', 'out']:
    p.add_argument('--' + name, required=True)
a = p.parse_args()
root = Path.cwd()
commands = []


def run(argv, expected=0):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    commands.append({'argv': argv, 'exitCode': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
    assert result.returncode == expected, commands[-1]
    return result


def expected_header_failure(code, stderr, destination):
    assert code != 0 and not any(c in destination for c in "'\"\r\n")
    errors = [
        "ERROR: Could not install packages due to an OSError: "
        + error + ": '" + destination + "'"
        for error in ['[Errno 13] Permission denied', '[Errno 30] Read-only file system']
    ]
    assert any(line in errors for line in stderr.splitlines())


version = run([a.python, '-I', '-c', 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")']).stdout.strip()
sdk = Path(a.python).resolve().parent.parent
header = sdk / 'include' / version / 'Python.h'
sdk_header_hash = hashlib.sha256(header.read_bytes()).hexdigest()
wheel = root / 'prefix_header_probe-1.0-py3-none-any.whl'
info = 'prefix_header_probe-1.0.dist-info'
files = {
    info + '/METADATA': b'Metadata-Version: 2.1\nName: prefix-header-probe\nVersion: 1.0\n',
    info + '/WHEEL': b'Wheel-Version: 1.0\nGenerator: prefix-header-regression\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    'prefix_header_probe-1.0.data/headers/prefix_header_probe.h': b'#define PREFIX_HEADER_VALUE 42\n',
}
record = io.StringIO()
writer = csv.writer(record)
for name, data in files.items():
    writer.writerow([name, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='), len(data)])
writer.writerow([info + '/RECORD', '', ''])
with zipfile.ZipFile(wheel, 'w') as archive:
    for name, data in files.items():
        archive.writestr(name, data)
    archive.writestr(info + '/RECORD', record.getvalue())

env = dict(os.environ, PIP_NO_INDEX='1', PIP_CONFIG_FILE='/dev/null', PIP_DISABLE_PIP_VERSION_CHECK='1')
for name in ['PYTHONPATH', 'PYTHONHOME', 'NIX_PYTHONPATH']:
    env.pop(name, None)
os.environ.clear()
os.environ.update(env)

old, fixed = root / 'old', root / 'fixed'
for script, prefix in [(a.original_script, old), (a.script, fixed)]:
    run([a.bash, script, str(sdk), version, str(prefix)])
    result = subprocess.run([a.pip, '-I', '-m', 'pip', '--python', str(prefix / 'bin/python'),
                             'install', '--no-index', '--no-deps', str(wheel)],
                            capture_output=True, text=True, timeout=60)
    commands.append({'prefix': str(prefix), 'exitCode': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
    if prefix == old:
        expected_header_failure(result.returncode, result.stderr,
                                str(old / 'include' / version / 'prefix-header-probe'))
        assert (old / 'include').is_symlink()
    else:
        assert result.returncode == 0, commands[-1]

assert not (fixed / 'include').is_symlink()
assert not (fixed / 'include' / version).is_symlink()
assert (fixed / 'include' / version / 'Python.h').resolve() == header
installed, = (fixed / 'include').rglob('prefix_header_probe.h')
assert installed.read_bytes() == files['prefix_header_probe-1.0.data/headers/prefix_header_probe.h']
assert not list((sdk / 'include').rglob('prefix_header_probe.h'))
assert hashlib.sha256(header.read_bytes()).hexdigest() == sdk_header_hash
config = json.loads(run([str(fixed / 'bin/python'), '-I', '-c',
    'import sys,sysconfig,json; assert sys.prefix==sys.base_prefix; print(json.dumps({"site":sysconfig.get_path("purelib"),"suffix":sysconfig.get_config_var("EXT_SUFFIX")}))']).stdout)
c = root / 'probe.c'
c.write_text('''#include <Python.h>
#include "prefix_header_probe.h"
static PyObject *answer(PyObject *self, PyObject *args) { return PyLong_FromLong(PREFIX_HEADER_VALUE); }
static PyMethodDef methods[] = {{"answer", answer, METH_NOARGS, NULL}, {NULL, NULL, 0, NULL}};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_prefix_header_probe", NULL, -1, methods};
PyMODINIT_FUNC PyInit__prefix_header_probe(void) { return PyModule_Create(&module); }
''')
extension = Path(config['site']) / ('_prefix_header_probe' + config['suffix'])
flags = ['-undefined', 'dynamic_lookup'] if sys.platform == 'darwin' else []
run([a.cc, '-shared', '-fPIC', *flags, '-I' + str(fixed / 'include' / version),
     '-I' + str(installed.parent), str(c), '-o', str(extension)])
probe = ('import _prefix_header_probe as p; from pathlib import Path; '
         'assert p.answer()==42; assert str(Path(p.__file__).resolve())=='
         + repr(str(extension.resolve())) + '; print(p.__file__)')
run([str(fixed / 'bin/python'), '-I', '-c', probe])
for name, system_site in [('with', True), ('without', False)]:
    target = root / name
    run([str(fixed / 'bin/python'), '-I', '-m', 'venv', '--without-pip',
         *(['--system-site-packages'] if system_site else []), str(target)])
    if system_site:
        run([str(target / 'bin/python'), '-I', '-c', probe])
    else:
        result = subprocess.run([str(target / 'bin/python'), '-I', '-c', probe], capture_output=True, text=True, timeout=60)
        assert result.returncode != 0 and 'ModuleNotFoundError' in result.stderr
        commands.append({'venvWithoutSystemSitePackages': 'correctly cannot import extension'})
Path(a.out).write_text(json.dumps({'commands': commands, 'sdkHeaderSha256': sdk_header_hash,
                                 'installedHeader': str(installed), 'extension': str(extension)}, indent=2) + '\n')
