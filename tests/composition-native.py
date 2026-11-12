#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Native pip locking, editable development and ordinary downstream composition."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

root = Path(__file__).resolve().parent.parent
system = subprocess.check_output(['nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem'], text=True).strip()
snapshot = subprocess.check_output(
    ['bash', '-c', 'set -euo pipefail; source tests/source.sh; source_snapshot'],
    cwd=root, text=True).strip()
with tempfile.TemporaryDirectory(prefix='pip-native-composition-') as temporary:
    temporary = Path(temporary)
    subprocess.run(['nix-store', '--add-root', str(temporary / 'source'), '--realise', snapshot], check=True)
    cli = Path(subprocess.check_output(['nix', 'build', snapshot + '#cli', '--print-out-paths',
        '--out-link', str(temporary / 'cli')], text=True).strip()) / 'bin/pip-purifier'
    expression = '''let
      f = builtins.getFlake ("path:" + builtins.getEnv "PIP_COMPOSITION_FLAKE");
      p = f.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
    in p.runCommand "pip-composition-tools" {} ''
      mkdir -p "$out/bin"
      ln -s ${p.uv}/bin/uv "$out/bin/uv"
      ln -s ${p.python3.interpreter} "$out/bin/python"
    '' '''
    env = dict(os.environ, PIP_COMPOSITION_FLAKE=snapshot, UV_PYTHON_DOWNLOADS='never')
    tools = Path(subprocess.check_output(['nix', 'build', '--impure', '--print-out-paths',
        '--out-link', str(temporary / 'tools'), '--expr', expression], text=True, env=env).strip())
    # The producer and downstream fixture explicitly share this target SDK.
    env['PATH'] = str(tools / 'bin') + os.pathsep + env['PATH']
    project = temporary / 'project'
    source = project / 'src/pip_composed/__init__.py'
    source.parent.mkdir(parents=True)
    source.write_text('import greet\ndef main():\n    greet.main()\n')
    definition = project / 'pyproject.toml'
    definition.write_text('''[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.build_meta"
[project]
name = "pip-composed"
version = "1.0"
dependencies = ["greet>=99"]
[project.scripts]
pip-composed = "pip_composed:main"
''')
    requirements = project / 'requirements.txt'
    requirements.write_text('-e .\ngreet @ https://invalid.example/greet-99-py3-none-any.whl\n')
    original = definition.read_bytes(), requirements.read_bytes()
    def run(*args, **kwargs):
        return subprocess.run([str(value) for value in args], cwd=project, env=env, check=True, **kwargs)
    unlocked = subprocess.run([str(cli), 'pip', '--version'], cwd=project, env=env,
                             capture_output=True, text=True, check=True)
    assert 'warning:' in unlocked.stderr and 'impurely' in unlocked.stderr
    reference = 'path:' + snapshot + '#checks.' + system + '.composition-producer'
    run(cli, 'lock', '--system', system, '--input', reference)
    lockfile = project / 'pip-purifier.lock.json'
    lock = json.loads(lockfile.read_text())
    assert len(lock['projectInputs']) == 1
    assert not any(entry['path'].startswith(('greet-', 'attrs-', 'docopt-', 'pyyaml-'))
                   for entry in lock['artifacts']), 'provided wheels leaked into download lock'
    before = lockfile.read_bytes()
    run(cli, 'lock', '--system', system)
    assert before == lockfile.read_bytes(), 'relocking changed pinned dependencies'
    run(cli, 'prefetch')
    expected = 'hello attrs=25.3.0 docopt=0.6.2 yaml=6.0.3'
    output = subprocess.check_output([str(project / '.venv/bin/pip-composed')], text=True)
    assert output.strip() == expected, output
    run(cli, 'pip', '--python', '.venv', 'install', '--force-reinstall', '-r', 'requirements.txt')
    run(cli, 'pip', '--python', '.venv', 'check')
    assert before == lockfile.read_bytes()
    assert original == (definition.read_bytes(), requirements.read_bytes())
    expression = '''let
      f = builtins.getFlake ("path:" + builtins.getEnv "PIP_COMPOSITION_FLAKE");
      p = import f.inputs.nixpkgs { overlays = [ f.overlays.default ]; };
      source = builtins.path {
        path = /. + builtins.getEnv "PIP_COMPOSITION_PROJECT";
        name = "source";
        filter = path: type: !(p.lib.hasSuffix ".egg-info" (baseNameOf path)) && !(builtins.elem (baseNameOf path)
          [ ".git" ".venv" ".pip-purifier" "__pycache__" "dist" "build" ]);
      };
      project = p.pip-purifier.buildPipProject {
        src = source;
        checkPhase = "pip-composed";
      };
    in [ project.tests project.packages.pip-composed.projections.python ]'''
    env['PIP_COMPOSITION_PROJECT'] = str(project)
    built = json.loads(subprocess.check_output(['nix', 'build', '--impure', '--json',
        '--out-link', str(temporary / 'pure'), '--expr', expression], text=True, env=env))
    repository = Path(next(result['outputs']['out'] for result in built
                          if result['outputs']['out'].endswith('-python-projection')))
    closure = subprocess.check_output(['nix-store', '--query', '--requisites', str(repository)], text=True)
    assert '-pip-composed-environment' in closure
    downstream = temporary / 'downstream'
    run(tools / 'bin/uv', 'venv', '--python', tools / 'bin/python', downstream)
    run(tools / 'bin/uv', 'pip', 'install', '--python', downstream / 'bin/python',
        '--no-index', '--find-links', repository, 'pip-composed')
    output = subprocess.check_output([str(downstream / 'bin/pip-composed')], text=True)
    assert output.strip() == expected, output
    run(downstream / 'bin/python', '-c',
        'from importlib.metadata import version; assert "+nix." in version("pip-composed"); assert "+nix." in version("greet")')
    uploads, portfile = temporary / 'uploads', temporary / 'port'
    server = subprocess.Popen([str(tools / 'bin/python'), str(root / 'tests/upload_server.py'),
                               '0', str(uploads), str(portfile)])
    try:
        deadline = time.monotonic() + 30
        while not portfile.exists():
            assert server.poll() is None and time.monotonic() < deadline, 'upload endpoint did not start'
            time.sleep(0.05)
        run(cli, 'publish', '--package', 'pip-composed', '--', '--publish-url',
            'http://127.0.0.1:' + portfile.read_text() + '/', '--username', 'test', '--password', 'test')
        assert {path.name for path in uploads.iterdir()} == {path.name for path in repository.glob('*.whl')}
        for path in repository.glob('*.whl'):
            assert path.read_bytes() == (uploads / path.name).read_bytes()
    finally:
        server.terminate()
        server.wait(timeout=10)
    assert original == (definition.read_bytes(), requirements.read_bytes())
    # A frozen native pylock is rebound in a temporary copy too; its
    # canonical URLs and hashes never become redundant Nix download entries.
    modules = json.loads((project / '.pip-purifier/bindings/inputs.json').read_text())['modules']
    frozen = temporary / 'frozen'
    frozen.mkdir()
    native = frozen / 'pylock.toml'
    native.write_text('lock-version = "1.0"\ncreated-by = "pip"\n' + ''.join(
        '\n[[packages]]\nname = "' + name + '"\nversion = "99"\n'
        '[[packages.wheels]]\nname = "' + name.replace('-', '_') + '-99-py3-none-any.whl"\n'
        'url = "https://invalid.example/' + name + '-99-py3-none-any.whl"\n'
        '[packages.wheels.hashes]\nsha256 = "' + '0' * 64 + '"\n'
        for name in modules))
    native_bytes = native.read_bytes()
    subprocess.run([str(cli), 'lock', '--system', system, '--input', reference], cwd=frozen, env=env, check=True)
    assert not json.loads((frozen / 'pip-purifier.lock.json').read_text())['artifacts']
    subprocess.run([str(cli), 'prefetch'], cwd=frozen, env=env, check=True)
    assert subprocess.check_output([str(frozen / '.venv/bin/greet')], text=True).strip() == expected
    assert native.read_bytes() == native_bytes
    print('PIP NATIVE, PURE AND ORDINARY DOWNSTREAM COMPOSITION PASSED', flush=True)
