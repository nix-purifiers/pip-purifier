# SPDX-License-Identifier: MIT
"""Repair pure-build backend prefixes before executing their native tools."""
import inspect
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
        owner, name = modern, 'install'
        probe = (None, (), None)
        keywords = {'kind': 'probe', 'for_req': None}
    else:
        owner, name = build_env.BuildEnvironment, '_install_requirements'
        probe = (None, None, (), None)
        keywords = {'kind': 'probe'}
    try:
        original = getattr(owner, name)
        descriptor = inspect.getattr_static(owner, name)
        signature = inspect.signature(original)
        signature.bind(*probe, **keywords)
        if 'prefix' not in signature.parameters:
            raise ValueError('no owned prefix argument')
        inspect.signature(build_env.get_runnable_pip).bind()
    except (AttributeError, TypeError, ValueError) as error:
        raise SystemExit('pip-purifier: unsupported isolated native installation protocol: ' + str(error)) from error

    def install(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs)
        prefix = arguments.arguments['prefix']
        result = original(*args, **kwargs)
        repair(prefix.path)
        return result

    setattr(owner, name, staticmethod(install) if isinstance(descriptor, staticmethod) else install)
    # Nested source build requirements must retain this adapter as well. The
    # legacy constraint adapter still validates and forwards this same entry.
    build_env.get_runnable_pip = lambda: str(Path(runner).resolve())
