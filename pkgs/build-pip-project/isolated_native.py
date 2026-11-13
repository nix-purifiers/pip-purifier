# SPDX-License-Identifier: MIT
"""Repair pure-build backend prefixes before executing their native tools."""
import inspect
from functools import wraps
import json
import os
from pathlib import Path
import subprocess

STATE = 'PIP_PURIFIER_NATIVE_REPAIR'


def repair(prefix):
    command = json.loads(os.environ[STATE])
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise SystemExit('pip-purifier: invalid isolated native repair command')
    subprocess.run([*command, str(Path(prefix).resolve())], check=True)


def activate(runner):
    """Wrap the actual pip installation protocol, without changing its inputs."""
    from pip._internal import build_env

    modern = getattr(build_env, 'SubprocessBuildEnvironmentInstaller', None)
    if modern is not None:
        owners = [modern]
        inprocess = getattr(build_env, 'InprocessBuildEnvironmentInstaller', None)
        if inprocess is not None:
            owners.append(inprocess)
        name = 'install'
        probe = (None, (), None)
        keywords = {'kind': 'probe', 'for_req': None}
    else:
        owners, name = [build_env.BuildEnvironment], '_install_requirements'
        probe = (None, None, (), None)
        keywords = {'kind': 'probe'}
    try:
        plans = []
        for owner in owners:
            original = getattr(owner, name)
            descriptor = inspect.getattr_static(owner, name)
            signature = inspect.signature(original)
            signature.bind(*probe, **keywords)
            if 'prefix' not in signature.parameters:
                raise ValueError('no owned prefix argument')
            plans.append((owner, original, descriptor, signature))
        # The subprocess call site may live in a submodule rather than the
        # public build_env namespace. Patch only the helper its function uses.
        namespace = plans[0][1].__globals__
        inspect.signature(namespace['get_runnable_pip']).bind()
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise SystemExit('pip-purifier: unsupported isolated native installation protocol: ' + str(error)) from error

    def hook(original, signature):
        @wraps(original)
        def install(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs)
            prefix = arguments.arguments['prefix']
            result = original(*args, **kwargs)
            repair(prefix.path)
            return result
        return install

    for owner, original, descriptor, signature in plans:
        install = hook(original, signature)
        setattr(owner, name, staticmethod(install) if isinstance(descriptor, staticmethod) else install)
    # Nested source build requirements must retain this adapter as well. The
    # legacy constraint adapter still validates and forwards this same entry.
    namespace['get_runnable_pip'] = lambda: str(Path(runner).resolve())
