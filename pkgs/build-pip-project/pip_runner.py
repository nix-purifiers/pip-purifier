# SPDX-License-Identifier: MIT
"""Run native pip with Nix repository bindings and optional wheel capture."""
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

def sha256_file(source):
    digest = hashlib.sha256()
    for block in iter(lambda: source.read(1024 * 1024), b''):
        digest.update(block)
    return digest


native_paths = sys.path.copy()
if os.environ.get('PIP_PURIFIER_PIP_PATH'):
    sys.path.insert(0, os.environ['PIP_PURIFIER_PIP_PATH'])
import pip
from pip._internal.cli import main_parser
from pip._internal.cli.main import main

# Keep the adapter when native pip delegates --python to a virtualenv.
# Build-isolation subprocesses retain pip's original entry point instead.
os.environ['PIP_PURIFIER_PIP_PATH'] = str(Path(pip.__file__).parent.parent)
main_parser.get_runnable_pip = lambda: str(Path(__file__).resolve())

def binding_protocol():
    """Check the exact hooks this runner patches, before any pip operation."""
    try:
        from pip._internal.models.link import Link
        from pip._internal.resolution.resolvelib.factory import Factory
        from pip._internal.metadata import select_backend
        from pip._internal.metadata.importlib._dists import Distribution
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.utils import canonicalize_name
        if select_backend().Distribution is not Distribution:
            return 'the active metadata backend is not the validated binding backend'
        inspect.signature(Factory._make_requirements_from_install_req).bind(None, None, set())
        inspect.signature(Distribution.iter_dependencies).bind(None, ())
        if not all(callable(item) for item in (Link, Requirement, canonicalize_name)):
            return 'producer binding constructors are unavailable'
    except (ImportError, AttributeError, TypeError, ValueError) as error:
        return 'producer binding protocol is unavailable: ' + str(error)
    return None


if sys.argv[1:] == ['--purifier-probe']:
    from pip._internal.commands import create_command
    from pip._internal.req import req_install
    from pip._internal.operations.install import wheel
    parser = create_command('install').parser
    capabilities = [name for name, option in [
        ('report', '--report'), ('build-constraint', '--build-constraint'),
        ('pylock', '--pylock')] if parser.has_option(option)]
    if main_parser.create_main_parser().has_option('--python'):
        capabilities.append('python')
    if callable(getattr(wheel, 'install_wheel', None)):
        capabilities.append('wheel-capture')
    binding_error = binding_protocol()
    if binding_error is None:
        capabilities.append('producer-bindings')
    print(json.dumps({'pip': pip.__version__, 'capabilities': capabilities,
                      'bindingError': binding_error}))
    sys.exit(0)

bindings = os.environ.get('PIP_PURIFIER_BINDINGS')
if bindings:
    binding_error = binding_protocol()
    if binding_error:
        raise SystemExit('pip-purifier: ' + binding_error)
    from pip._internal.models.link import Link
    from pip._internal.resolution.resolvelib.factory import Factory
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

    modules = json.loads(Path(bindings).read_text())['modules']
    original = Factory._make_requirements_from_install_req

    def requirements(self, requirement, requested_extras):
        module = modules.get(canonicalize_name(requirement.name)) if requirement.name else None
        if module and requirement.match_markers(requested_extras):
            wheel = Path(module['wheel'])
            extras = '[' + ','.join(sorted(requirement.extras)) + ']' if requirement.extras else ''
            requirement.req = Requirement(requirement.name + extras + ' @ ' + wheel.as_uri())
            requirement.link = requirement.original_link = Link(wheel.as_uri())
            requirement.local_file_path = str(wheel)
            with wheel.open('rb') as source:
                requirement.hash_options = {'sha256': [sha256_file(source).hexdigest()]}
        yield from original(self, requirement, requested_extras)

    Factory._make_requirements_from_install_req = requirements

    # Native conflict reporting and `pip check` must apply the same input
    # bindings as resolution, while installed project metadata stays native.
    from pip._internal.metadata.importlib._dists import Distribution
    from pip._vendor.packaging.specifiers import SpecifierSet
    original_dependencies = Distribution.iter_dependencies

    def dependencies(self, extras=()):
        for requirement in original_dependencies(self, extras):
            module = modules.get(canonicalize_name(requirement.name))
            if module:
                requirement = Requirement(str(requirement))
                requirement.specifier = SpecifierSet('==' + module['version'])
                requirement.url = None
            yield requirement

    Distribution.iter_dependencies = dependencies

capture = os.environ.get('PIP_PURIFIER_EXPORT_WHEELS')
if capture:
    from pip._internal.req import req_install
    try:
        from pip._internal.operations.install import wheel as wheel_install
        original_install = wheel_install.install_wheel
    except (ImportError, AttributeError) as error:
        raise RuntimeError(
            f"pip-purifier: pip {pip.__version__} has no supported wheel capture hook; use a tested pip SDK"
        ) from error

    def install(name, wheel_path, *args, **kwargs):
        destination = Path(capture)
        destination.mkdir(parents=True, exist_ok=True)
        wheel = Path(wheel_path)
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with wheel.open('rb') as source:
                shutil.copyfileobj(source, temporary)
        try:
            try:
                os.link(temporary_path, destination / wheel.name)
            except FileExistsError:
                with wheel.open('rb') as source, (destination / wheel.name).open('rb') as previous:
                    if sha256_file(source).digest() != sha256_file(previous).digest():
                        raise ValueError('conflicting native wheel bytes: ' + wheel.name)
        finally:
            temporary_path.unlink()
        return original_install(name, wheel_path, *args, **kwargs)

    # pip 26.3 imports this function lazily at installation time. Older
    # SDKs keep an eager alias in req_install, which needs the same hook.
    wheel_install.install_wheel = install
    if hasattr(req_install, 'install_wheel'):
        req_install.install_wheel = install

if __name__ == '__main__':
    # Keep SDK imports available through their package paths, while package
    # discovery sees only the interpreter this process is installing for.
    sys.path[:] = native_paths
    sys.exit(main())
